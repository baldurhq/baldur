"""
Unit tests for Event Journal service init module.

검증 항목:
- init_event_journal() 싱글톤 동작
- get_event_journal() / reset_event_journal() 라이프사이클
- ProviderRegistry 통합
- enabled=False 시 초기화 건너뛰기

테스트 대상: baldur.services.event_journal.__init__
"""

from unittest.mock import MagicMock, patch

from baldur.interfaces.event_journal import EventJournalRepository
from baldur.services.event_journal import (
    get_event_journal,
    init_event_journal,
    reset_event_journal,
)


class TestEventJournalSingletonBehavior:
    """EventJournal 싱글톤 캐싱/리셋 동작 검증."""

    def setup_method(self):
        reset_event_journal()

    def test_get_event_journal_returns_none_before_init(self):
        """초기화 전 get_event_journal()은 None을 반환한다."""
        assert get_event_journal() is None

    @patch("baldur.services.event_bus.bus.get_event_bus")
    @patch("baldur.factory.ProviderRegistry.get_event_journal_repo")
    def test_init_event_journal_returns_subscriber(self, mock_get_repo, mock_get_bus):
        """init_event_journal()은 JournalSubscriber를 반환한다."""
        mock_repo = MagicMock(spec=EventJournalRepository)
        mock_get_repo.return_value = mock_repo
        mock_bus = MagicMock()
        mock_get_bus.return_value = mock_bus

        subscriber = init_event_journal()

        assert subscriber is not None
        mock_bus.subscribe.assert_called()

    @patch("baldur.services.event_bus.bus.get_event_bus")
    @patch("baldur.factory.ProviderRegistry.get_event_journal_repo")
    def test_init_event_journal_returns_same_instance_on_second_call(
        self, mock_get_repo, mock_get_bus
    ):
        """init_event_journal()을 2회 호출하면 동일 인스턴스를 반환한다."""
        mock_repo = MagicMock(spec=EventJournalRepository)
        mock_get_repo.return_value = mock_repo
        mock_bus = MagicMock()
        mock_get_bus.return_value = mock_bus

        first = init_event_journal()
        second = init_event_journal()

        assert first is second
        mock_get_repo.assert_called_once()

    @patch("baldur.services.event_bus.bus.get_event_bus")
    @patch("baldur.factory.ProviderRegistry.get_event_journal_repo")
    def test_init_uses_provided_bus(self, mock_get_repo, mock_get_bus):
        """bus 파라미터를 전달하면 get_event_bus()를 호출하지 않는다."""
        mock_repo = MagicMock(spec=EventJournalRepository)
        mock_get_repo.return_value = mock_repo
        custom_bus = MagicMock()

        init_event_journal(bus=custom_bus)

        mock_get_bus.assert_not_called()
        custom_bus.subscribe.assert_called()

    @patch("baldur.services.event_bus.bus.get_event_bus")
    @patch("baldur.factory.ProviderRegistry.get_event_journal_repo")
    def test_reset_clears_singleton(self, mock_get_repo, mock_get_bus):
        """reset 후 get_event_journal()은 None을 반환한다."""
        mock_repo = MagicMock(spec=EventJournalRepository)
        mock_get_repo.return_value = mock_repo
        mock_bus = MagicMock()
        mock_get_bus.return_value = mock_bus

        init_event_journal()
        assert get_event_journal() is not None

        reset_event_journal()
        assert get_event_journal() is None


class TestEventJournalEnabledSettingBehavior:
    """enabled 설정에 따른 init_event_journal() 동작 검증."""

    def setup_method(self):
        reset_event_journal()

    @patch(
        "baldur.settings.event_journal.get_event_journal_settings",
    )
    def test_init_returns_none_when_disabled(self, mock_get_settings):
        """enabled=False이면 None을 반환하고 구독을 등록하지 않는다."""
        mock_settings = MagicMock()
        mock_settings.enabled = False
        mock_get_settings.return_value = mock_settings

        result = init_event_journal()

        assert result is None
        assert get_event_journal() is None
