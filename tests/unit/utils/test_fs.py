"""Semantics of the writable-directory resolution primitive.

``resolve_writable_dir`` is the single resolution point for every durability
surface that creates its directory at construction. These tests pin the origin
split (an operator-chosen directory fails loud, a hardcoded default falls
back), the order of the fallback chain, the deterministic leaf name, the trust
check on the segments the primitive creates, and the resolution registry.

Unwritable directories are simulated by monkeypatching ``Path.mkdir`` /
``Path.touch`` rather than by chmod: the POSIX permission tricks do not work on
Windows, and the same contract has to be pinned on both platforms.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
import threading
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from baldur.core.exceptions import ConfigurationError
from baldur.utils import fs
from baldur.utils.fs import (
    ResolvedDir,
    get_writable_dir_resolutions,
    reset_writable_dir_resolutions,
    resolve_writable_dir,
)
from tests.factories.writable_dir import log_events, spoof_lstat

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only chain step / uid semantics",
)
windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-only chain shape",
)

PROBE_ERRORS = [
    PermissionError(errno.EACCES, "Permission denied"),
    OSError(errno.EROFS, "Read-only file system"),
    OSError(errno.ELOOP, "Too many levels of symbolic links"),
    NotADirectoryError(errno.ENOTDIR, "Not a directory"),
]
PROBE_ERROR_IDS = ["permission", "read_only_fs", "symlink_loop", "not_a_directory"]


class TestResolveWritableDirBehavior:
    """Origin split and fallback chain order."""

    def test_writable_preferred_dir_is_used_without_falling_back(
        self, writable_dir_chain, tmp_path
    ):
        """A writable preferred directory is returned unchanged."""
        preferred = tmp_path / "configured"

        with capture_logs() as logs:
            resolved = resolve_writable_dir(
                preferred, purpose="checkpoint", operator_set=False
            )

        assert resolved.path == preferred
        assert resolved.fell_back is False
        assert resolved.reason is None
        assert log_events(logs, "storage.writable_dir_probe_failed") == []

    def test_unwritable_default_dir_falls_back_to_the_state_dir(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """The persistent per-user state directory is the first chain step."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)

        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        assert resolved.fell_back is True
        assert resolved.path.is_relative_to(writable_dir_chain.state)
        assert resolved.path.is_dir()

    def test_unwritable_default_dir_emits_one_warning_naming_both_paths(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """The fallback warning names the preferred path, the fallback and the override."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)

        with capture_logs() as logs:
            resolved = resolve_writable_dir(
                preferred,
                purpose="checkpoint",
                operator_set=False,
                env_override_name="BALDUR_AUDIT_PATH",
            )

        warnings = log_events(logs, "storage.writable_dir_probe_failed")
        assert len(warnings) == 1
        assert warnings[0]["preferred"] == str(preferred)
        assert warnings[0]["fallback"] == str(resolved.path)
        assert warnings[0]["override_env"] == "BALDUR_AUDIT_PATH"
        assert warnings[0]["log_level"] == "warning"

    @posix_only
    def test_unwritable_default_dir_without_a_state_dir_falls_back_to_var_tmp(
        self, writable_dir_chain, no_state_dir, deny_dir, tmp_path
    ):
        """With no HOME/XDG the reboot-preserved system temp is chosen."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)

        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        assert resolved.path.is_relative_to(writable_dir_chain.var_tmp)

    @posix_only
    def test_unwritable_default_dir_without_state_dir_or_var_tmp_falls_back_to_tempdir(
        self, writable_dir_chain, no_state_dir, deny_dir, tmp_path
    ):
        """The volatile temp directory is the last resort, not an earlier choice."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)
        deny_dir(writable_dir_chain.var_tmp)

        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        assert resolved.path.is_relative_to(writable_dir_chain.temp)

    @posix_only
    def test_tempdir_is_not_chosen_while_the_state_dir_is_writable(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """Negative: a later chain step never wins over an earlier writable one."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)

        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        assert not resolved.path.is_relative_to(writable_dir_chain.temp)
        assert not resolved.path.is_relative_to(writable_dir_chain.var_tmp)
        assert list(writable_dir_chain.temp.iterdir()) == []

    @posix_only
    def test_posix_fallback_segment_is_scoped_to_the_effective_uid(
        self, writable_dir_chain, no_state_dir, deny_dir, tmp_path
    ):
        """World-writable bases get a per-user segment, so users cannot collide."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)

        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        user_segment = resolved.path.relative_to(writable_dir_chain.var_tmp).parts[0]
        assert user_segment == f"{fs._PROJECT_DIR_NAME}-{os.geteuid()}"

    @windows_only
    def test_localappdata_is_the_first_chain_step_on_windows(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """``%LOCALAPPDATA%`` gives Windows a persistent step 1 too."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)

        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        assert resolved.path.is_relative_to(writable_dir_chain.state)

    @windows_only
    def test_windows_chain_falls_back_to_tempdir_without_localappdata(
        self, writable_dir_chain, no_state_dir, deny_dir, tmp_path
    ):
        """Negative: Windows has no ``/var/tmp`` analogue, so that step is skipped."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)

        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        assert resolved.path.is_relative_to(writable_dir_chain.temp)
        assert list(writable_dir_chain.var_tmp.iterdir()) == []

    @pytest.mark.parametrize("error", PROBE_ERRORS, ids=PROBE_ERROR_IDS)
    def test_any_os_error_from_the_probe_is_treated_as_unwritable(
        self, writable_dir_chain, deny_dir, tmp_path, error
    ):
        """Probe failure is any ``OSError``, not only ``PermissionError``."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred, error)

        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        assert resolved.fell_back is True
        assert type(error).__name__ in resolved.reason

    def test_preferred_dir_failing_only_the_write_test_falls_back(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """A creatable but unwritable directory is still a probe failure."""
        preferred = tmp_path / "creatable-but-read-only"
        deny_dir(preferred, op="touch")

        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        assert resolved.fell_back is True
        assert resolved.path.is_relative_to(writable_dir_chain.state)

    @posix_only
    def test_chain_step_failing_only_the_write_test_demotes_to_the_next_step(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """A creatable but unwritable chain step is skipped, not returned."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)
        deny_dir(writable_dir_chain.state, op="touch")

        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        assert resolved.path.is_relative_to(writable_dir_chain.var_tmp)

    def test_operator_set_unwritable_dir_raises_naming_path_and_override_env(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """An operator-chosen directory fails loud instead of relocating data."""
        preferred = tmp_path / "compliance-path"
        deny_dir(preferred)

        with pytest.raises(ConfigurationError) as exc_info:
            resolve_writable_dir(
                preferred,
                purpose="checkpoint",
                operator_set=True,
                env_override_name="BALDUR_AUDIT_PATH",
            )

        message = str(exc_info.value)
        assert str(preferred) in message
        assert "BALDUR_AUDIT_PATH" in message

    def test_operator_set_unwritable_dir_does_not_fall_back(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """Negative: no chain step is created for an operator-chosen directory."""
        preferred = tmp_path / "compliance-path"
        deny_dir(preferred)

        with pytest.raises(ConfigurationError):
            resolve_writable_dir(preferred, purpose="checkpoint", operator_set=True)

        assert list(writable_dir_chain.state.iterdir()) == []
        assert list(writable_dir_chain.var_tmp.iterdir()) == []
        assert list(writable_dir_chain.temp.iterdir()) == []

    def test_unwritable_chain_raises_naming_every_tried_path(
        self, writable_dir_chain, no_state_dir, deny_dir, tmp_path
    ):
        """Exhausting the chain is a named failure, not a silent no-op."""
        preferred = tmp_path / "unwritable"
        for root in (
            preferred,
            writable_dir_chain.state,
            writable_dir_chain.var_tmp,
            writable_dir_chain.temp,
        ):
            deny_dir(root)

        with pytest.raises(ConfigurationError) as exc_info:
            resolve_writable_dir(preferred, purpose="checkpoint", operator_set=False)

        message = str(exc_info.value)
        assert str(preferred) in message
        assert str(writable_dir_chain.temp) in message

    def test_no_override_env_yields_a_generic_override_hint(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """A surface with no environment override promises no variable name."""
        preferred = tmp_path / "compliance-path"
        deny_dir(preferred)

        with pytest.raises(ConfigurationError) as exc_info:
            resolve_writable_dir(
                preferred,
                purpose="event_bus_wal",
                operator_set=True,
                env_override_name=None,
            )

        assert "writable path" in str(exc_info.value)


class TestResolveWritableDirContract:
    """Registry statuses and the resolution outcome shape."""

    def test_writable_dir_records_ok_status(self, writable_dir_chain, tmp_path):
        """Design contract: a probe that passes records ``"ok"``."""
        preferred = tmp_path / "configured"

        resolve_writable_dir(preferred, purpose="checkpoint", operator_set=False)

        entry = next(iter(get_writable_dir_resolutions().values()))
        assert entry["status"] == "ok"
        assert entry["path"] == str(preferred)
        assert entry["preferred"] == str(preferred)

    def test_fallback_records_fallback_status(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """Design contract: a resolved fallback records ``"fallback"``."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)

        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        entry = next(iter(get_writable_dir_resolutions().values()))
        assert entry["status"] == "fallback"
        assert entry["path"] == str(resolved.path)
        assert entry["preferred"] == str(preferred)

    def test_operator_set_failure_records_unwritable_before_raising(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """Design contract: the failure is registered even though it raises."""
        preferred = tmp_path / "compliance-path"
        deny_dir(preferred)

        with pytest.raises(ConfigurationError):
            resolve_writable_dir(preferred, purpose="checkpoint", operator_set=True)

        entry = next(iter(get_writable_dir_resolutions().values()))
        assert entry["status"] == "unwritable"
        assert entry["path"] == ""
        assert entry["preferred"] == str(preferred)

    def test_exhausted_chain_records_unwritable_before_raising(
        self, writable_dir_chain, no_state_dir, deny_dir, tmp_path
    ):
        """Design contract: a pathological host is still visible in the report."""
        preferred = tmp_path / "unwritable"
        for root in (
            preferred,
            writable_dir_chain.state,
            writable_dir_chain.var_tmp,
            writable_dir_chain.temp,
        ):
            deny_dir(root)

        with pytest.raises(ConfigurationError):
            resolve_writable_dir(preferred, purpose="checkpoint", operator_set=False)

        entry = next(iter(get_writable_dir_resolutions().values()))
        assert entry["status"] == "unwritable"

    def test_registry_entry_carries_exactly_the_reported_fields(
        self, writable_dir_chain, tmp_path
    ):
        """Design contract: the startup report copies this shape verbatim."""
        resolve_writable_dir(
            tmp_path / "configured",
            purpose="checkpoint",
            operator_set=False,
            env_override_name="BALDUR_AUDIT_PATH",
        )

        entry = next(iter(get_writable_dir_resolutions().values()))
        assert set(entry) == {"status", "path", "preferred", "override_env"}
        assert entry["override_env"] == "BALDUR_AUDIT_PATH"

    def test_registry_records_an_empty_override_when_none_is_offered(
        self, writable_dir_chain, tmp_path
    ):
        """Design contract: a surface with no override promises no variable.

        Reporting keys off this field, so "offers none" has to be
        distinguishable from a variable name rather than absent.
        """
        resolve_writable_dir(
            tmp_path / "configured", purpose="event_bus_wal", operator_set=False
        )

        entry = next(iter(get_writable_dir_resolutions().values()))
        assert entry["override_env"] == ""

    def test_resolved_dir_is_frozen(self, writable_dir_chain, tmp_path):
        """Design contract: an adopting surface cannot rewrite its resolution."""
        resolved = resolve_writable_dir(
            tmp_path / "configured", purpose="checkpoint", operator_set=False
        )

        assert isinstance(resolved, ResolvedDir)
        with pytest.raises(AttributeError):
            resolved.path = Path("/elsewhere")


class TestWritableDirLeafContract:
    """The fallback leaf is a pure function of ``(purpose, preferred)``."""

    def test_leaf_is_purpose_joined_to_a_preferred_path_digest(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """Design contract: leaf == ``<purpose>-<sha1(preferred)[:8]>``."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)
        digest = hashlib.sha1(
            str(preferred).encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:8]

        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        assert resolved.path.name == f"checkpoint-{digest}"

    def test_fallback_leaf_is_never_the_bare_purpose(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """Negative: no resolution order can produce an unsuffixed directory."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)

        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        assert resolved.path.name != "checkpoint"

    def test_re_resolving_the_same_pair_returns_the_cached_outcome(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """Design contract: re-resolution is idempotent, not a second fallback."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)

        with capture_logs() as logs:
            first = resolve_writable_dir(
                preferred, purpose="checkpoint", operator_set=False
            )
            second = resolve_writable_dir(
                preferred, purpose="checkpoint", operator_set=False
            )

        assert first is second
        assert len(get_writable_dir_resolutions()) == 1
        assert len(log_events(logs, "storage.writable_dir_probe_failed")) == 1

    def test_operator_set_resolve_raises_even_when_a_fallback_is_cached(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """The cache must not launder a fallback into an operator-set resolve.

        Regression: the cache is keyed on ``(purpose, preferred)`` only, so a
        default resolve that already fell back would satisfy a later
        operator-set resolve of the same directory — silently writing an
        explicitly configured compliance path somewhere else, the one outcome
        the origin split exists to prevent.
        """
        preferred = tmp_path / "compliance-path"
        deny_dir(preferred)
        fallback = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )
        assert fallback.fell_back is True

        with pytest.raises(ConfigurationError) as excinfo:
            resolve_writable_dir(
                preferred,
                purpose="checkpoint",
                operator_set=True,
                env_override_name="BALDUR_AUDIT_PATH",
            )

        assert str(preferred) in str(excinfo.value)
        assert "BALDUR_AUDIT_PATH" in str(excinfo.value)

    def test_cached_non_fallback_still_satisfies_an_operator_set_resolve(
        self, writable_dir_chain, tmp_path
    ):
        """Negative: a writable directory stays idempotent across both origins."""
        preferred = tmp_path / "configured"

        first = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )
        second = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=True
        )

        assert first is second
        assert second.fell_back is False

    def test_leaf_is_the_same_whichever_surface_resolves_first(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """Order independence: the regression test for cross-restart directory swaps.

        Two surfaces sharing a purpose must keep their own directory no matter
        which one boots first, or one recovers the other's files.
        """
        # Given — two surfaces sharing a purpose but wanting different dirs
        first_pref = tmp_path / "unwritable-a"
        second_pref = tmp_path / "unwritable-b"
        deny_dir(first_pref)
        deny_dir(second_pref)

        # When — resolved forward, then again in the reverse order
        forward = [
            resolve_writable_dir(p, purpose="wal_audit_wal", operator_set=False).path
            for p in (first_pref, second_pref)
        ]
        reset_writable_dir_resolutions()
        reverse = [
            resolve_writable_dir(p, purpose="wal_audit_wal", operator_set=False).path
            for p in (second_pref, first_pref)
        ]

        # Then — each preferred path keeps its own leaf either way
        assert forward == list(reversed(reverse))
        assert forward[0] != forward[1]


class TestWritableDirTrustCheckBehavior:
    """Pre-existing chain segments this primitive claims to own."""

    @posix_only
    def test_symlinked_chain_segment_demotes_to_the_next_step(
        self, writable_dir_chain, deny_dir, monkeypatch, tmp_path
    ):
        """A squatted symlink must not receive audit-trail writes."""
        # Given — the state step's project segment already exists as a symlink
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)
        squatted = writable_dir_chain.state / fs._PROJECT_DIR_NAME
        squatted.mkdir()
        spoof_lstat(monkeypatch, squatted, mode=stat.S_IFLNK | 0o777)

        # When
        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        # Then
        assert resolved.path.is_relative_to(writable_dir_chain.var_tmp)

    @posix_only
    def test_foreign_owned_chain_segment_demotes_to_the_next_step(
        self, writable_dir_chain, deny_dir, monkeypatch, tmp_path
    ):
        """A segment owned by another uid is not trusted with durability data."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)
        squatted = writable_dir_chain.state / fs._PROJECT_DIR_NAME
        squatted.mkdir()
        spoof_lstat(monkeypatch, squatted, uid=os.geteuid() + 1)

        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        assert resolved.path.is_relative_to(writable_dir_chain.var_tmp)

    @posix_only
    def test_untrusted_segment_emits_a_warning_naming_the_path_and_reason(
        self, writable_dir_chain, deny_dir, monkeypatch, tmp_path
    ):
        """The demotion is announced, not silent."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)
        squatted = writable_dir_chain.state / fs._PROJECT_DIR_NAME
        squatted.mkdir()
        spoof_lstat(monkeypatch, squatted, uid=os.geteuid() + 1)

        with capture_logs() as logs:
            resolve_writable_dir(preferred, purpose="checkpoint", operator_set=False)

        records = log_events(logs, "storage.writable_dir_untrusted_base")
        assert len(records) == 1
        assert str(squatted) in records[0]["reason"]
        assert records[0]["log_level"] == "warning"

    @posix_only
    def test_untrusted_segment_does_not_raise_while_a_later_step_is_writable(
        self, writable_dir_chain, deny_dir, monkeypatch, tmp_path
    ):
        """Negative: squatting one base must not become a boot-blocking DoS."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)
        squatted = writable_dir_chain.state / fs._PROJECT_DIR_NAME
        squatted.mkdir()
        spoof_lstat(monkeypatch, squatted, uid=os.geteuid() + 1)

        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        assert resolved.fell_back is True

    @posix_only
    def test_foreign_owned_preferred_dir_is_exempt_from_the_trust_check(
        self, writable_dir_chain, monkeypatch, tmp_path
    ):
        """``/var/log/audit`` as ``root:adm 0775`` is a legitimate configured path."""
        # Given — a writable preferred directory owned by another uid
        preferred = tmp_path / "provisioned"
        preferred.mkdir()
        spoof_lstat(monkeypatch, preferred, uid=os.geteuid() + 1)

        # When
        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        # Then — the check applies to created segments only, so no fallback
        assert resolved.fell_back is False
        assert resolved.path == preferred


class TestWritableDirSegmentModeBehavior:
    """Modes of the directory segments the primitive creates."""

    @posix_only
    def test_every_created_fallback_segment_is_private_to_the_owner(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """``mkdir(parents=True, mode=…)`` skips parents, so segments are made one by one."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)

        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        created = [resolved.path, *resolved.path.parents]
        for segment in created[: created.index(writable_dir_chain.state)]:
            assert stat.S_IMODE(segment.stat().st_mode) == fs._FALLBACK_DIR_MODE

    @posix_only
    def test_no_created_fallback_segment_is_group_or_world_accessible(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """Negative: fallback data under a 1777 base must not inherit the umask default."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)

        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        created = [resolved.path, *resolved.path.parents]
        for segment in created[: created.index(writable_dir_chain.state)]:
            assert stat.S_IMODE(segment.stat().st_mode) & 0o077 == 0


class TestWritableDirProbeFileBehavior:
    """The write-test probe file."""

    def test_probe_file_name_carries_the_process_id(
        self, writable_dir_chain, monkeypatch, tmp_path
    ):
        """A fixed name lets concurrently booting workers unlink each other's probe."""
        touched: list[Path] = []
        real_touch = Path.touch

        def spy(self, *args, **kwargs):
            touched.append(self)
            return real_touch(self, *args, **kwargs)

        monkeypatch.setattr(Path, "touch", spy)

        resolve_writable_dir(
            tmp_path / "configured", purpose="checkpoint", operator_set=False
        )

        assert touched
        assert all(p.name.startswith(fs._PROBE_FILE_STEM) for p in touched)
        assert all(f".{os.getpid()}." in p.name for p in touched)

    def test_probe_file_name_differs_between_probes(
        self, writable_dir_chain, monkeypatch, tmp_path
    ):
        """The uuid suffix keeps two probes of the same directory distinct."""
        touched: list[Path] = []
        real_touch = Path.touch

        def spy(self, *args, **kwargs):
            touched.append(self)
            return real_touch(self, *args, **kwargs)

        monkeypatch.setattr(Path, "touch", spy)

        resolve_writable_dir(tmp_path / "a", purpose="checkpoint", operator_set=False)
        resolve_writable_dir(tmp_path / "b", purpose="disk_buffer", operator_set=False)

        assert len({p.name for p in touched}) == len(touched)

    def test_probe_file_is_removed_after_a_successful_probe(
        self, writable_dir_chain, tmp_path
    ):
        """The resolved directory is left clean."""
        preferred = tmp_path / "configured"

        resolved = resolve_writable_dir(
            preferred, purpose="checkpoint", operator_set=False
        )

        assert list(resolved.path.iterdir()) == []


class TestWritableDirRegistryBehavior:
    """Registry lifecycle and its lock-guarded read-then-write sections."""

    def test_concurrent_resolution_of_one_key_yields_a_single_entry(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """Concurrent boots must not double-register or double-warn."""
        # Given — one unwritable default that eight workers resolve at once
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)
        results: list[ResolvedDir] = []
        errors: list[BaseException] = []
        start = threading.Event()

        def worker() -> None:
            start.wait(timeout=5.0)
            try:
                results.append(
                    resolve_writable_dir(
                        preferred, purpose="checkpoint", operator_set=False
                    )
                )
            except BaseException as e:  # noqa: BLE001 - reported as a failure
                errors.append(e)

        # When
        threads = [threading.Thread(target=worker) for _ in range(8)]
        with capture_logs() as logs:
            for thread in threads:
                thread.start()
            start.set()
            for thread in threads:
                thread.join(timeout=10.0)

        # Then
        assert errors == []
        assert len(results) == 8
        assert len({r.path for r in results}) == 1
        assert len(get_writable_dir_resolutions()) == 1
        assert len(log_events(logs, "storage.writable_dir_probe_failed")) == 1

    def test_get_resolutions_returns_a_copy_that_cannot_corrupt_the_registry(
        self, writable_dir_chain, tmp_path
    ):
        """Immutability: the startup report mutating its copy must not leak back."""
        resolve_writable_dir(
            tmp_path / "configured", purpose="checkpoint", operator_set=False
        )

        snapshot = get_writable_dir_resolutions()
        key = next(iter(snapshot))
        snapshot[key]["status"] = "tampered"
        snapshot["injected"] = {"status": "ok", "path": "", "preferred": ""}

        assert get_writable_dir_resolutions()[key]["status"] == "ok"
        assert "injected" not in get_writable_dir_resolutions()

    def test_reset_clears_recorded_resolutions(self, writable_dir_chain, tmp_path):
        """The reset half of the singleton pair empties the registry."""
        resolve_writable_dir(
            tmp_path / "configured", purpose="checkpoint", operator_set=False
        )
        assert get_writable_dir_resolutions()

        reset_writable_dir_resolutions()

        assert get_writable_dir_resolutions() == {}

    def test_reset_re_arms_the_one_time_fallback_warning(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """Warning dedup is registry state, so a reset must clear it too."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)

        with capture_logs() as logs:
            resolve_writable_dir(preferred, purpose="checkpoint", operator_set=False)
            reset_writable_dir_resolutions()
            resolve_writable_dir(preferred, purpose="checkpoint", operator_set=False)

        assert len(log_events(logs, "storage.writable_dir_probe_failed")) == 2


class TestWritableDirPurposeCollisionBehavior:
    """The wiring-bug diagnostic for a purpose reused across directories."""

    def test_one_purpose_with_two_preferred_dirs_reports_both(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """A shared purpose is a wiring bug worth naming, even though it is contained."""
        first = tmp_path / "unwritable-a"
        second = tmp_path / "unwritable-b"
        deny_dir(first)
        deny_dir(second)

        with capture_logs() as logs:
            resolve_writable_dir(first, purpose="wal_audit_wal", operator_set=False)
            resolve_writable_dir(second, purpose="wal_audit_wal", operator_set=False)

        records = log_events(logs, "storage.writable_dir_purpose_error")
        assert len(records) == 1
        assert records[0]["preferred"] == str(second)
        assert records[0]["previous_preferred"] == str(first)
        assert records[0]["log_level"] == "error"

    def test_one_purpose_with_two_preferred_dirs_still_resolves_separately(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """The deterministic leaf separates the directories by construction."""
        first = tmp_path / "unwritable-a"
        second = tmp_path / "unwritable-b"
        deny_dir(first)
        deny_dir(second)

        first_resolved = resolve_writable_dir(
            first, purpose="wal_audit_wal", operator_set=False
        )
        second_resolved = resolve_writable_dir(
            second, purpose="wal_audit_wal", operator_set=False
        )

        assert first_resolved.path != second_resolved.path
        assert len(get_writable_dir_resolutions()) == 2

    def test_one_purpose_with_one_preferred_dir_reports_nothing(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """Negative: the known-good layout must not fire the diagnostic."""
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)

        with capture_logs() as logs:
            resolve_writable_dir(preferred, purpose="wal_audit_wal", operator_set=False)
            resolve_writable_dir(preferred, purpose="wal_audit_wal", operator_set=False)

        assert log_events(logs, "storage.writable_dir_purpose_error") == []
