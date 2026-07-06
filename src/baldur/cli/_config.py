"""
CLI bootstrap config resolution (429 Part 7, D10).

Without a Django ``settings.py`` to implicitly bootstrap the process, CLI
invocations must resolve configuration from a deterministic chain before
:func:`baldur.init` runs.

Resolution order (higher overrides lower):
    1. CLI flag ``--config /path/to/baldur.toml``
    2. Env var ``BALDUR_CONFIG`` (absolute path)
    3. Working directory auto-detect: ``./baldur.toml`` -> ``./.baldur.toml``
    4. XDG: ``$XDG_CONFIG_HOME/baldur/config.toml`` (POSIX) /
       ``%APPDATA%/baldur/config.toml`` (Windows)
    5. Individual ``BALDUR_*`` env vars (Pydantic default behavior)

``.env`` loading is explicit-only (``--env-file <path>`` or
``BALDUR_DOTENV=1``) - never silent. Missing files at any step are not
errors; the chain falls through.

TOML sections are flattened into the ``BALDUR_<SECTION>_*`` env vars that
each Pydantic settings class actually consumes. Settings classes are
configured via ``make_settings_config("BALDUR_<SECTION>_")`` (see
``settings/base.py:20``) - a single underscore between the section prefix
and the field name. ``env_nested_delimiter="__"`` is used only for
sub-BaseModel fields inside a settings class, never between section and
class-prefix.

Resulting mapping:

``[baldur.admin] bind = "..."``           -> ``BALDUR_ADMIN_BIND``
``[baldur.admin.retry] max = 5``          -> ``BALDUR_ADMIN_RETRY__MAX``
``[baldur.dlq.retention] days = 7``       -> ``BALDUR_DLQ_RETENTION__DAYS``

Top-level ``[baldur]`` scalars are rejected: no settings class uses a
bare ``BALDUR_`` prefix with those field names, so projecting them would
create dead env vars that silently look like they took effect but never
reach any class. Those keys are skipped with a WARNING log so users
surface the misconfiguration early.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

__all__ = [
    "resolve_config",
    "apply_config_to_env",
    "load_dotenv_if_requested",
    "ConfigResolution",
]


_BALDUR_TOML_ENV = "BALDUR_CONFIG"
_BALDUR_DOTENV_ENV = "BALDUR_DOTENV"
_CWD_CANDIDATES = ("baldur.toml", ".baldur.toml")
_XDG_RELATIVE = ("baldur", "config.toml")


class ConfigResolution:
    """Resolution result - records which source won for observability."""

    __slots__ = ("path", "source")

    def __init__(self, path: Path | None, source: str) -> None:
        self.path = path
        self.source = source

    def __repr__(self) -> str:
        return f"ConfigResolution(path={self.path!r}, source={self.source!r})"


def resolve_config(explicit_path: str | None = None) -> ConfigResolution:
    """Walk the config resolution chain. Returns the first existing file.

    Args:
        explicit_path: Value of ``--config`` (highest precedence). May be
            ``None`` when the user did not pass the flag.

    Returns:
        ConfigResolution - ``path`` is ``None`` when no file is found at
        any step; env-var-only bootstrap is still valid.
    """
    if explicit_path:
        candidate = Path(explicit_path).expanduser()
        if candidate.is_file():
            return ConfigResolution(candidate, "cli_flag")
        # An explicit flag that does not resolve is a user error, not a
        # silent fallthrough - surface it immediately.
        raise FileNotFoundError(f"--config path does not exist: {candidate}")

    env_value = os.environ.get(_BALDUR_TOML_ENV, "").strip()
    if env_value:
        candidate = Path(env_value).expanduser()
        if candidate.is_file():
            return ConfigResolution(candidate, "env_var")
        logger.warning(
            "cli.config_env_var_path_missing",
            env=_BALDUR_TOML_ENV,
            path=str(candidate),
        )

    cwd = Path.cwd()
    for name in _CWD_CANDIDATES:
        candidate = cwd / name
        if candidate.is_file():
            return ConfigResolution(candidate, "cwd")

    xdg_candidate = _xdg_config_path()
    if xdg_candidate is not None and xdg_candidate.is_file():
        return ConfigResolution(xdg_candidate, "xdg")

    return ConfigResolution(None, "env_only")


def _xdg_config_path() -> Path | None:
    """Compute the per-platform XDG-style config path."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if not base:
            return None
        return Path(base).joinpath(*_XDG_RELATIVE)

    xdg_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_home:
        return Path(xdg_home).joinpath(*_XDG_RELATIVE)

    home = os.environ.get("HOME")
    if not home:
        return None
    return Path(home, ".config", *_XDG_RELATIVE)


def apply_config_to_env(resolution: ConfigResolution) -> dict[str, str]:
    """Load a resolved TOML file and project it onto ``BALDUR_*`` env vars.

    Does NOT overwrite env vars that are already set - process-level env
    vars always win over TOML so users can override a committed
    ``baldur.toml`` via shell export. Returns the mapping that was
    applied (keys that were actually new).
    """
    if resolution.path is None:
        return {}

    try:
        with resolution.path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML in {resolution.path}: {exc}") from exc

    flat = _flatten_baldur_section(data)
    applied: dict[str, str] = {}
    for key, value in flat.items():
        if key in os.environ:
            continue
        str_value = _stringify(value)
        os.environ[key] = str_value
        applied[key] = str_value

    logger.debug(
        "cli.config_applied",
        source=resolution.source,
        path=str(resolution.path),
        applied_count=len(applied),
    )
    return applied


def _flatten_baldur_section(data: dict[str, Any]) -> dict[str, str]:
    """Project ``[baldur.<section>]`` tables onto ``BALDUR_<SECTION>_*`` env vars.

    Each direct sub-table of ``[baldur]`` is treated as one Pydantic
    settings class with ``env_prefix="BALDUR_<SECTION>_"``. Scalars at
    the top level have no owning class and are rejected (logged and
    skipped). Non-``[baldur]`` sections in the TOML file are ignored so
    the file can host unrelated tool configuration.
    """
    section = data.get("baldur")
    if not isinstance(section, dict):
        return {}

    flat: dict[str, str] = {}
    for key, value in section.items():
        if not isinstance(value, dict):
            logger.warning(
                "cli.config_top_level_baldur_key_ignored",
                key=key,
                reason="top-level [baldur] scalars have no owning settings class",
            )
            continue
        _walk(f"BALDUR_{key.upper()}", value, flat, depth=1)
    return flat


def _walk(
    prefix: str,
    node: dict[str, Any],
    out: dict[str, str],
    depth: int,
) -> None:
    """Project a sub-tree with a depth-aware delimiter.

    ``depth == 1`` - direct children of ``[baldur.<section>]`` are class
    fields; join with ``_`` so the result matches the class's single-
    underscore ``env_prefix`` + field name.

    ``depth >= 2`` - nested sub-BaseModel fields; join with Pydantic's
    ``env_nested_delimiter="__"``.
    """
    sep = "_" if depth == 1 else "__"
    for key, value in node.items():
        env_key = f"{prefix}{sep}{key.upper()}"
        if isinstance(value, dict):
            _walk(env_key, value, out, depth + 1)
        else:
            out[env_key] = _stringify(value)


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (list, tuple)):
        return ",".join(_stringify(v) for v in value)
    return str(value)


def load_dotenv_if_requested(explicit_path: str | None) -> Path | None:
    """Load a ``.env`` file when the caller explicitly opts in.

    Activation signals (both accepted):
        - ``--env-file <path>`` (explicit path; wins)
        - ``BALDUR_DOTENV=1`` env var (looks for ``./.env``)

    No silent ``.env`` loading ever - matches settings/base.py:13
    (``env_file=None``) and 12-factor compliance. Returns the path
    that was loaded, or ``None`` when no dotenv was requested.
    """
    target: Path | None = None
    if explicit_path:
        target = Path(explicit_path).expanduser()
    elif _is_truthy(os.environ.get(_BALDUR_DOTENV_ENV)):
        candidate = Path.cwd() / ".env"
        if candidate.is_file():
            target = candidate

    if target is None:
        return None
    if not target.is_file():
        raise FileNotFoundError(f"--env-file path does not exist: {target}")

    _apply_dotenv(target)
    logger.debug("cli.dotenv_loaded", path=str(target))
    return target


def _apply_dotenv(path: Path) -> None:
    """Minimal .env parser - ``KEY=value`` lines, ``#`` comments, no quoting tricks.

    Dependency-free so the CLI ships without a ``python-dotenv`` dependency.
    Lines that don't parse are logged at DEBUG and skipped. Existing env
    vars are preserved (process env wins).
    """
    with path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                logger.debug("cli.dotenv_line_skipped", line=raw_line.rstrip())
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            # Strip a single matched pair of surrounding quotes. Unmatched
            # or mixed quotes are kept verbatim - the parser is deliberately
            # minimal and does not attempt escape-sequence handling.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


def _is_truthy(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}
