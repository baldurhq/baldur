"""
Django Session Invalidation Adapter.

SessionInvalidationProvider backed by Django Session model.
"""

from __future__ import annotations

from baldur.interfaces.session_provider import SessionInvalidationProvider

__all__ = ["DjangoSessionAdapter"]


class DjangoSessionAdapter(SessionInvalidationProvider):
    """SessionInvalidationProvider backed by Django Session model."""

    def invalidate_user_sessions(self, user_id: int | str) -> list[str]:
        from django.contrib.sessions.models import Session

        from baldur.utils.time import utc_now

        deleted_count = 0
        for session in Session.objects.filter(expire_date__gte=utc_now()):
            data = session.get_decoded()
            if str(data.get("_auth_user_id")) == str(user_id):
                session.delete()
                deleted_count += 1
        return [f"django_db_sessions({deleted_count})"]

    def get_active_session_count(self, user_id: int | str) -> int:
        from django.contrib.sessions.models import Session

        from baldur.utils.time import utc_now

        count = 0
        for session in Session.objects.filter(expire_date__gte=utc_now()):
            data = session.get_decoded()
            if str(data.get("_auth_user_id")) == str(user_id):
                count += 1
        return count
