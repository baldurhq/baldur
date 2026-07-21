"""Writable-directory resolution for durability storage surfaces.

Canonical primitive for every surface that creates its directory at
construction (audit checkpoint storage, write-ahead logs, the DLQ disk
buffer). Resolution is origin-split:

- A directory the operator **chose** (env var, explicit argument, or an
  explicitly-set settings field) that fails the write probe raises
  :class:`~baldur.core.exceptions.ConfigurationError` naming the path and
  its override variable. Honoring an explicit compliance path by writing
  somewhere else is worse than failing loud.
- A directory the operator **never chose** (a hardcoded class or dataclass
  default) that fails the probe walks a fallback chain — persistent user
  state directory, then the reboot-preserved system temp, then the volatile
  temp directory — and returns the first writable step with a one-time
  warning naming both paths and the override variable.

The resolved directory is recorded in a process-level registry so boot-time
reporting can show which surfaces fell back.

Usage:
    from baldur.utils.fs import resolve_writable_dir

    resolved = resolve_writable_dir(
        settings.wal_dir,
        purpose="wal_audit",
        operator_set="wal_dir" in settings.model_fields_set,
        env_override_name="BALDUR_AUDIT_WAL_DIR",
    )
    wal_dir = resolved.path
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

import structlog

from baldur.core.exceptions import ConfigurationError

__all__ = [
    "ResolvedDir",
    "get_writable_dir_resolutions",
    "reset_writable_dir_resolutions",
    "resolve_writable_dir",
]

logger = structlog.get_logger()

# Mode applied to every directory segment created below a fallback chain
# base. Both POSIX system-temp bases are world-writable (1777), so fallback
# data must not inherit the umask default.
_FALLBACK_DIR_MODE = 0o700

# Reboot-preserved system temp base (FHS: temporary files preserved between
# reboots). Module-level so a test can redirect the chain step instead of
# depending on the host's real /var/tmp permissions.
_VAR_TMP_BASE = "/var/tmp"

# Directory name owned by this project under a per-user chain base.
_PROJECT_DIR_NAME = "baldur"

# Write-probe filename stem. The pid/uuid suffix keeps concurrently booting
# workers from unlinking each other's probe file - a fixed name makes one
# worker's unlink raise inside another's probe and falls back a perfectly
# writable directory.
_PROBE_FILE_STEM = ".baldur_write_test"

# Length of the preferred-path digest appended to every fallback leaf.
_LEAF_HASH_LENGTH = 8

STATUS_OK = "ok"
STATUS_FALLBACK = "fallback"
STATUS_UNWRITABLE = "unwritable"


@dataclass(frozen=True)
class ResolvedDir:
    """Outcome of a writable-directory resolution.

    Attributes:
        path: The directory that was resolved and is writable.
        preferred: The directory that was asked for.
        fell_back: ``True`` when ``path`` is not ``preferred``.
        reason: The probe failure that caused the fallback (``None`` when
            ``preferred`` was used).
    """

    path: Path
    preferred: Path
    fell_back: bool
    reason: str | None = None


# Resolution registry. Guarded by ``_registry_lock`` - the cached-entry
# lookup, the purpose-collision check and the one-time-warning dedup are all
# read-then-write, so concurrent resolves would otherwise double-emit.
_registry_lock = threading.Lock()
_resolutions: dict[str, dict[str, str]] = {}
_resolved_dirs: dict[str, ResolvedDir] = {}
_purpose_preferred: dict[str, str] = {}
_warned_keys: set[str] = set()


def get_writable_dir_resolutions() -> dict[str, dict[str, str]]:
    """Return a copy of the resolution registry.

    Keys are ``<purpose>-<digest>``; each value carries ``status``
    (``"ok"`` / ``"fallback"`` / ``"unwritable"``), the resolved ``path``,
    the ``preferred`` path that was asked for, and ``override_env`` — the
    variable the resolving surface reads to choose the directory (``""``
    when it offers none). Reporting keys off the recorded variable rather
    than off a purpose string, so a remedy it names is one the surface
    actually honors.
    """
    with _registry_lock:
        return {key: dict(entry) for key, entry in _resolutions.items()}


def reset_writable_dir_resolutions() -> None:
    """Clear the resolution registry (test isolation)."""
    with _registry_lock:
        _resolutions.clear()
        _resolved_dirs.clear()
        _purpose_preferred.clear()
        _warned_keys.clear()


def resolve_writable_dir(
    preferred: str | Path,
    *,
    purpose: str,
    operator_set: bool,
    env_override_name: str | None = None,
) -> ResolvedDir:
    """Resolve a writable directory for ``preferred``.

    Args:
        preferred: The directory the surface wants to use.
        purpose: Stable identifier for the surface (``"checkpoint"``,
            ``"wal_audit_wal"``, ...). Together with ``preferred`` it
            determines the fallback directory name and the registry key.
        operator_set: ``True`` when ``preferred`` came from operator input
            (env var, explicit argument, explicitly-set settings field).
            Operator-chosen directories never fall back.
        env_override_name: Environment variable an operator can set to
            choose the directory, named in warnings and errors.

    Returns:
        The resolution outcome. Re-resolving the same ``(purpose,
        preferred)`` pair returns the cached outcome unchanged, unless the
        cached outcome is a fallback and this call is ``operator_set``.

    Raises:
        ConfigurationError: When ``operator_set`` is true and ``preferred``
            is not writable — including when an earlier non-operator resolve
            of the same pair already fell back — or when no fallback step is
            writable.
    """
    preferred_path = Path(preferred)
    key = _registry_key(purpose, preferred_path)

    with _registry_lock:
        cached = _resolved_dirs.get(key)
        if cached is not None:
            if operator_set and cached.fell_back:
                # The cache is keyed on (purpose, preferred) alone, so an
                # earlier resolve that treated this directory as a hardcoded
                # default already recorded a fallback. Returning it would let
                # an operator-chosen compliance path be honored by writing
                # somewhere else - the one outcome the origin split exists to
                # prevent.
                raise ConfigurationError(
                    f"Configured directory for {purpose!r} is not writable: "
                    f"{preferred_path} ({cached.reason}). "
                    f"{_override_hint(env_override_name)}"
                )
            return cached

        _check_purpose_collision(purpose, preferred_path)

        probe_error = _probe_preferred(preferred_path)
        if probe_error is None:
            return _record(
                key,
                STATUS_OK,
                ResolvedDir(
                    path=preferred_path,
                    preferred=preferred_path,
                    fell_back=False,
                ),
                env_override_name,
            )

        if operator_set:
            _record_unwritable(key, preferred_path, env_override_name)
            raise ConfigurationError(
                f"Configured directory for {purpose!r} is not writable: "
                f"{preferred_path} ({probe_error}). "
                f"{_override_hint(env_override_name)}"
            )

        return _resolve_fallback(
            key=key,
            purpose=purpose,
            preferred_path=preferred_path,
            probe_error=probe_error,
            env_override_name=env_override_name,
        )


# =============================================================================
# Resolution internals (all called with ``_registry_lock`` held)
# =============================================================================


def _registry_key(purpose: str, preferred: Path) -> str:
    """Build the deterministic registry key / fallback leaf name.

    The leaf is a pure function of ``(purpose, preferred)`` so a surface
    keeps the same fallback directory across restarts regardless of which
    surface resolves first. Order-dependent naming would let two surfaces
    swap directories between boots and recover each other's files.
    """
    digest = hashlib.sha1(
        str(preferred).encode("utf-8", errors="replace"),
        usedforsecurity=False,
    ).hexdigest()[:_LEAF_HASH_LENGTH]
    return f"{purpose}-{digest}"


def _check_purpose_collision(purpose: str, preferred: Path) -> None:
    """Report a purpose reused for a different preferred directory.

    The deterministic leaf already separates the two directories, so this
    is a wiring-bug signal rather than a repair.
    """
    previous = _purpose_preferred.get(purpose)
    if previous is not None and previous != str(preferred):
        logger.error(
            "storage.writable_dir_purpose_error",
            purpose=purpose,
            preferred=str(preferred),
            previous_preferred=previous,
        )
    _purpose_preferred[purpose] = str(preferred)


def _resolve_fallback(
    *,
    key: str,
    purpose: str,
    preferred_path: Path,
    probe_error: str,
    env_override_name: str | None,
) -> ResolvedDir:
    """Walk the fallback chain and return the first writable step."""
    tried: list[str] = [f"{preferred_path} ({probe_error})"]

    for base, tail in _fallback_chain(key):
        candidate = base.joinpath(*tail)

        untrusted = _untrusted_reason(base, tail)
        if untrusted is not None:
            logger.warning(
                "storage.writable_dir_untrusted_base",
                purpose=purpose,
                path=str(candidate),
                reason=untrusted,
            )
            tried.append(f"{candidate} ({untrusted})")
            continue

        step_error = _probe_fallback_step(base, tail)
        if step_error is not None:
            tried.append(f"{candidate} ({step_error})")
            continue

        resolved = _record(
            key,
            STATUS_FALLBACK,
            ResolvedDir(
                path=candidate,
                preferred=preferred_path,
                fell_back=True,
                reason=probe_error,
            ),
            env_override_name,
        )
        if key not in _warned_keys:
            _warned_keys.add(key)
            logger.warning(
                "storage.writable_dir_probe_failed",
                purpose=purpose,
                preferred=str(preferred_path),
                fallback=str(candidate),
                override_env=env_override_name,
                error=probe_error,
            )
        return resolved

    _record_unwritable(key, preferred_path, env_override_name)
    raise ConfigurationError(
        f"No writable directory found for {purpose!r}. "
        f"Tried: {'; '.join(tried)}. {_override_hint(env_override_name)}"
    )


def _record(
    key: str,
    status: str,
    resolved: ResolvedDir,
    env_override_name: str | None,
) -> ResolvedDir:
    """Store a successful resolution in the registry."""
    _resolutions[key] = {
        "status": status,
        "path": str(resolved.path),
        "preferred": str(resolved.preferred),
        "override_env": env_override_name or "",
    }
    _resolved_dirs[key] = resolved
    return resolved


def _record_unwritable(
    key: str,
    preferred: Path,
    env_override_name: str | None,
) -> None:
    """Record a failed resolution before raising."""
    _resolutions[key] = {
        "status": STATUS_UNWRITABLE,
        "path": "",
        "preferred": str(preferred),
        "override_env": env_override_name or "",
    }


def _override_hint(env_override_name: str | None) -> str:
    """Build the operator-facing override instruction."""
    if env_override_name:
        return f"Set {env_override_name} to a writable path."
    return "Point the surface at a writable path."


# =============================================================================
# Filesystem probing
# =============================================================================


def _probe_preferred(path: Path) -> str | None:
    """Probe ``path`` as requested, without imposing a mode.

    A configured directory is routinely provisioned root-owned and
    group-writable, so neither the trust check nor the private mode applies
    here - only creation and a write test.

    Returns:
        ``None`` when writable, else the failure description.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        _write_test(path)
    except OSError as e:
        return f"{type(e).__name__}: {e}"
    return None


def _probe_fallback_step(base: Path, tail: tuple[str, ...]) -> str | None:
    """Create and probe a fallback chain step.

    Every segment below ``base`` is created individually:
    ``mkdir(parents=True, mode=...)`` applies the mode to the leaf only and
    would leave intermediate segments at the umask default.

    Returns:
        ``None`` when writable, else the failure description.
    """
    try:
        base.mkdir(parents=True, exist_ok=True)
        current = base
        for segment in tail:
            current = current / segment
            current.mkdir(parents=False, mode=_FALLBACK_DIR_MODE, exist_ok=True)
        _write_test(current)
    except OSError as e:
        return f"{type(e).__name__}: {e}"
    return None


def _write_test(path: Path) -> None:
    """Touch and remove a uniquely-named probe file under ``path``."""
    probe = path / f"{_PROBE_FILE_STEM}.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    probe.touch()
    try:
        probe.unlink(missing_ok=True)
    except OSError:
        # The directory is writable; a leftover probe file is harmless.
        pass


def _untrusted_reason(base: Path, tail: tuple[str, ...]) -> str | None:
    """Reject a pre-existing chain segment this primitive claims to own.

    Both POSIX system-temp bases are world-writable, so any local user can
    pre-create the segments below them. ``mkdir(exist_ok=True)`` does not
    chmod an existing directory and the write test passes, which would hand
    an attacker-owned directory to a durability writer.

    Only the segments below ``base`` are checked. ``base`` itself is
    root-owned by definition for the system-temp steps, and the preferred
    directory is exempt entirely.

    Returns:
        ``None`` when trusted, else the reason to demote to the next step.
    """
    euid = _effective_uid()
    if euid is None:
        return None

    current = base
    for segment in tail:
        current = current / segment
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            # Nothing below this point exists yet - nothing to distrust.
            return None
        except OSError as e:
            return f"{type(e).__name__}: {e}"

        if stat.S_ISLNK(info.st_mode):
            return f"{current} is a symlink"
        if info.st_uid != euid:
            return f"{current} is owned by uid {info.st_uid}, expected {euid}"

    return None


# =============================================================================
# Fallback chain
# =============================================================================


def _fallback_chain(leaf: str) -> list[tuple[Path, tuple[str, ...]]]:
    """Build the ordered fallback chain as ``(base, segments)`` pairs."""
    chain: list[tuple[Path, tuple[str, ...]]] = []

    state_step = _state_dir_step(leaf)
    if state_step is not None:
        chain.append(state_step)

    if sys.platform == "win32":
        chain.append((Path(tempfile.gettempdir()), (_PROJECT_DIR_NAME, leaf)))
        return chain

    # POSIX: a per-user directory under each world-writable base, so
    # concurrent users neither collide nor read each other's data.
    uid = _effective_uid()
    user_dir = _PROJECT_DIR_NAME if uid is None else f"{_PROJECT_DIR_NAME}-{uid}"
    chain.append((Path(_VAR_TMP_BASE), (user_dir, leaf)))
    chain.append((Path(tempfile.gettempdir()), (user_dir, leaf)))
    return chain


def _state_dir_step(leaf: str) -> tuple[Path, tuple[str, ...]] | None:
    """Build the persistent per-user state directory step.

    Survives reboot and temp-file aging, so on long-lived hosts - exactly
    where temp-directory volatility bites - fallback data is persistent.
    Returns ``None`` when the platform's state directory is unresolvable
    (scratch containers with no HOME).
    """
    if sys.platform == "win32":
        # LOCALAPPDATA, not APPDATA: roaming profiles must not sync WAL files.
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            return None
        return (Path(local_app_data), (_PROJECT_DIR_NAME, leaf))

    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return (Path(xdg_state_home), (_PROJECT_DIR_NAME, leaf))

    home = os.environ.get("HOME")
    if not home:
        return None
    return (Path(home), (".local", "state", _PROJECT_DIR_NAME, leaf))


def _effective_uid() -> int | None:
    """Return the effective uid, or ``None`` where the platform has none."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return None
    return int(geteuid())
