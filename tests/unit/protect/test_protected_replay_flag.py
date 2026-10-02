"""``@protected(..., replay=True)``: a job Baldur may re-run from its stored arguments.

Target: ``baldur.protect_facade`` (``protected`` / ``aprotected``'s ``replay=``)
and ``baldur.services.replay_service.function_replay.arm_function_replay``.

What makes a job replayable is decided when it is decorated, so an
unreplayable job fails when its module is imported, not when it fails in
production: every refusal below raises ``ValueError`` at decoration. An
accepted job registers one ``FunctionReplayHandler`` under its stored domain;
a module reload replaces it, any other claim on the name is refused.

This module uses ``from __future__ import annotations``: every annotation the
decorator checks is a postponed string, resolved one parameter at a time.

UNIT_TEST_GUIDELINES.md: decision tables via ``parametrize`` (§6.7); the
registry is process-global, so each test restores it (§6.5).
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import textwrap
import uuid
from collections.abc import Iterator
from typing import Optional, Union
from unittest.mock import patch

import pytest

from baldur.core.exceptions import LLMUnavailableError
from baldur.interfaces.repositories import FailedOperationData
from baldur.interfaces.resilience_policy import PolicyContext
from baldur.protect_facade import aprotected, protected
from baldur.services.replay_service.function_replay import FunctionReplayHandler
from baldur.services.replay_service.handlers import (
    ReplayHandler,
    _replay_handlers,
    get_replay_handler,
    has_replay_handler,
    register_replay_handler,
)
from baldur.services.replay_service.models import ReplayResult
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.sinks import retry_exhausted_failure_type
from baldur.settings.protect import reset_protect_settings
from baldur.utils.domain_validation import resolve_stored_domain

_STORE = "baldur.services.retry_handler.sinks.store_to_dlq"


@pytest.fixture(autouse=True)
def replay_registry() -> Iterator[None]:
    """The handler registry as it was, before and after each test."""
    before = dict(_replay_handlers)
    reset_protect_settings()
    yield
    _replay_handlers.clear()
    _replay_handlers.update(before)
    reset_protect_settings()


def _job_name() -> str:
    return f"job.summarize_{uuid.uuid4().hex[:10]}"


class _HandWrittenHandler(ReplayHandler):
    """A handler a team wrote by hand for the same domain."""

    def __init__(self, domain: str) -> None:
        self._domain = domain

    @property
    def domain(self) -> str:
        return self._domain

    def can_replay(self, failed_op: FailedOperationData) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op: FailedOperationData) -> ReplayResult:
        return ReplayResult.succeeded(failed_op.id, "done")


# Decorations replay=True must refuse, and the words the refusal must contain.
def _dlq_off(name):
    @protected(name, dlq=False, replay=True)
    def job(doc_id: str) -> str:
        return doc_id


def _context_from(name):
    @protected(name, replay=True, context_from=lambda *a, **k: PolicyContext())
    def job(doc_id: str) -> str:
        return doc_id


def _context_from_off(name):
    @protected(name, replay=True, context_from=False)
    def job(doc_id: str) -> str:
        return doc_id


def _masked_max_tokens(name):
    @protected(name, replay=True)
    def job(doc_id: str, max_tokens: int = 256) -> str:
        return doc_id


def _masked_author_id(name):
    @protected(name, replay=True)
    def job(author_id: str) -> str:
        return author_id


def _list_annotation(name):
    @protected(name, replay=True)
    def job(doc_ids: list[str]) -> str:
        return ",".join(doc_ids)


def _dict_annotation(name):
    @protected(name, replay=True)
    def job(options: dict) -> str:
        return str(options)


def _union_of_two(name):
    @protected(name, replay=True)
    def job(doc_id: Union[str, int]) -> str:  # noqa: UP007 — the spelling under test
        return str(doc_id)


def _optional_list(name):
    @protected(name, replay=True)
    def job(doc_ids: Optional[list]) -> str:  # noqa: UP045 — the spelling under test
        return str(doc_ids)


def _var_positional(name):
    @protected(name, replay=True)
    def job(*doc_ids: str) -> str:
        return ",".join(doc_ids)


def _var_keyword(name):
    @protected(name, replay=True)
    def job(**options: str) -> str:
        return str(options)


def _positional_only(name):
    @protected(name, replay=True)
    def job(doc_id: str, /) -> str:
        return doc_id


def _unaddressable_name(_name):
    @protected("-", replay=True)
    def job(doc_id: str) -> str:
        return doc_id


def _retry_domain_elsewhere(name):
    @protected(name, replay=True, retry=RetryPolicyConfig(domain="llm_jobs"))
    def job(doc_id: str) -> str:
        return doc_id


def _async_dlq_off(name):
    @aprotected(name, dlq=False, replay=True)
    async def job(doc_id: str) -> str:
        return doc_id


_REFUSALS = [
    (_dlq_off, "dlq=False"),
    (_context_from, "context_from"),
    (_context_from_off, "context_from"),
    (_masked_max_tokens, "'max_tokens' is redacted"),
    (_masked_author_id, "'author_id' is redacted"),
    (_list_annotation, "'doc_ids' is annotated"),
    (_dict_annotation, "'options' is annotated"),
    (_union_of_two, "'doc_id' is annotated"),
    (_optional_list, "'doc_ids' is annotated"),
    (_var_positional, "variadic positional"),
    (_var_keyword, "variadic keyword"),
    (_positional_only, "positional-only"),
    (_unaddressable_name, "cannot be stored as a DLQ domain"),
    (_retry_domain_elsewhere, "parks its failures under 'llm_jobs'"),
    (_async_dlq_off, "dlq=False"),
]


class TestProtectedReplayFlagBehavior:
    """Refused at decoration, or registered as the name's one replay handler."""

    @pytest.mark.parametrize(
        ("decorate", "words"),
        _REFUSALS,
        ids=[decorate.__name__.lstrip("_") for decorate, _ in _REFUSALS],
    )
    def test_unreplayable_job_is_refused_at_decoration(self, decorate, words):
        """The refusal names the problem, and nothing is registered."""
        name = _job_name()

        with pytest.raises(ValueError, match=words):
            decorate(name)

        assert has_replay_handler(resolve_stored_domain(name)) is False

    def test_retry_config_naming_the_job_itself_is_accepted(self):
        """A retry config whose domain is the job's own name parks where replay looks."""
        name = _job_name()

        @protected(name, replay=True, retry=RetryPolicyConfig(domain=name))
        def job(doc_id: str) -> str:
            return doc_id

        assert has_replay_handler(resolve_stored_domain(name)) is True

    def test_exact_types_and_optionals_are_accepted(self):
        """``str``/``int``/``float``/``bool``/``None``, ``Optional`` of one, unannotated."""
        name = _job_name()

        @protected(name, replay=True)
        def job(
            doc_id: str,
            pages: int,
            temperature: float,
            stream: bool,
            note: None,
            lang: Optional[str] = None,  # noqa: UP045 — the spelling under test
            region: str | None = None,
            tag="default",
        ) -> NotDefinedAnywhere:  # noqa: F821 — an unresolvable return type is ignored
            return doc_id

        handler = get_replay_handler(resolve_stored_domain(name))
        assert isinstance(handler, FunctionReplayHandler)

    def test_registers_under_the_stored_domain_with_the_function_identity(self):
        """The handler is found where the store files the job's entries."""
        suffix = uuid.uuid4().hex[:8]
        name = f"Summarize-Job-{suffix}"

        @protected(name, replay=True)
        def summarize(doc_id: str) -> str:
            return doc_id

        handler = get_replay_handler(f"summarize_job_{suffix}")
        assert isinstance(handler, FunctionReplayHandler)
        assert handler.domain == f"summarize_job_{suffix}"
        assert handler.function_identity == (__name__, summarize.__qualname__)

    def test_async_job_registers_through_aprotected(self):
        """``@aprotected(..., replay=True)`` arms the same handler for a coroutine."""
        name = _job_name()

        @aprotected(name, replay=True)
        async def summarize(doc_id: str) -> str:
            return doc_id

        handler = get_replay_handler(resolve_stored_domain(name))
        assert isinstance(handler, FunctionReplayHandler)
        assert asyncio.run(summarize("doc-1")) == "doc-1"

    def test_replay_implies_dlq_so_a_failed_job_is_parked(self):
        """``replay=True`` without ``dlq=`` still parks the failure, with its arguments."""
        name = _job_name()

        @protected(name, replay=True, circuit_breaker=False, timeout=None)
        def summarize(doc_id: str) -> str:
            raise LLMUnavailableError("no endpoint")

        with patch(_STORE, autospec=True) as store, pytest.raises(LLMUnavailableError):
            summarize("doc-7")

        store.assert_called_once()
        assert store.call_args.kwargs["domain"] == name
        assert store.call_args.kwargs["failure_type"] == (
            retry_exhausted_failure_type(LLMUnavailableError.__name__)
        )
        assert store.call_args.kwargs["request_data"] == {"doc_id": "doc-7"}

    def test_a_different_function_under_the_same_name_is_refused(self):
        """A name replays one function; a second one would silently take its entries."""
        name = _job_name()

        @protected(name, replay=True)
        def first(doc_id: str) -> str:
            return doc_id

        with pytest.raises(ValueError, match="already replayed by"):

            @protected(name, replay=True)
            def second(doc_id: str) -> str:
                return doc_id

        handler = get_replay_handler(resolve_stored_domain(name))
        assert handler.function_identity == (__name__, first.__qualname__)

    def test_a_hand_written_handler_for_the_name_is_refused(self):
        """A team's own handler is never overwritten by the decorator."""
        name = _job_name()
        hand_written = _HandWrittenHandler(resolve_stored_domain(name))
        register_replay_handler(hand_written)

        with pytest.raises(ValueError, match="_HandWrittenHandler"):

            @protected(name, replay=True)
            def summarize(doc_id: str) -> str:
                return doc_id

        assert get_replay_handler(resolve_stored_domain(name)) is hand_written

    def test_reload_of_the_same_module_replaces_the_handler(
        self, tmp_path, monkeypatch
    ):
        """A reload builds a new function object; the same module and name replace the handler."""
        # Given — a job module on disk, imported once
        module_name = f"replay_job_module_{uuid.uuid4().hex[:10]}"
        name = _job_name()
        (tmp_path / f"{module_name}.py").write_text(
            textwrap.dedent(
                f"""
                from baldur.protect_facade import protected

                @protected({name!r}, replay=True)
                def summarize(doc_id: str) -> str:
                    return doc_id
                """
            ),
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        domain = resolve_stored_domain(name)
        try:
            module = importlib.import_module(module_name)
            before = get_replay_handler(domain)

            # When
            importlib.reload(module)

            # Then
            after = get_replay_handler(domain)
            assert after is not before
            assert (
                after.function_identity
                == before.function_identity
                == (
                    module_name,
                    "summarize",
                )
            )
        finally:
            sys.modules.pop(module_name, None)
