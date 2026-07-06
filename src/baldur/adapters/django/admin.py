"""
Django Admin for Baldur Models.

Provides Django Admin base classes for Baldur models.
Host apps can inherit from these classes to reuse the Admin configuration.

Available Base Admin Classes:
    - BasePostmortemRecordAdmin: Postmortem record Admin
    - BaseDLQEntryAdmin: Dead Letter Queue (failed operation) Admin
    - BaseCircuitBreakerStateAdmin: Circuit Breaker state Admin

Example:
    # myapp/admin.py
    from django.contrib import admin
    from baldur.adapters.django.admin import (
        BasePostmortemRecordAdmin,
        BaseDLQEntryAdmin,
        BaseCircuitBreakerStateAdmin,
    )
    from myapp.models import PostmortemRecord, FailedOperation, CircuitBreakerState

    @admin.register(PostmortemRecord)
    class PostmortemRecordAdmin(BasePostmortemRecordAdmin):
        pass  # Inherit all settings

    @admin.register(FailedOperation)
    class FailedOperationAdmin(BaseDLQEntryAdmin):
        pass

    @admin.register(CircuitBreakerState)
    class CircuitBreakerStateAdmin(BaseCircuitBreakerStateAdmin):
        pass
"""

# Django admin convention assigns short_description / admin_order_field
# attributes onto method functions (read at runtime by ModelAdmin). django-stubs
# types them as plain Callable, so every assignment shows up as attr-defined.
# Stub limitation — fix at the file level rather than per-line.
# mypy: disable-error-code="attr-defined"

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

try:
    from django.contrib import admin
    from django.urls import reverse
    from django.utils.html import format_html
    from django.utils.safestring import mark_safe

    DJANGO_AVAILABLE = True
except ImportError:
    DJANGO_AVAILABLE = False


if TYPE_CHECKING:
    from django.http import HttpRequest


logger = structlog.get_logger()


# =============================================================================
# Base Postmortem Record Admin
# =============================================================================


class BasePostmortemRecordAdmin(admin.ModelAdmin if DJANGO_AVAILABLE else object):  # type: ignore[misc]
    """
    Base Admin configuration for Postmortem Records.

    Base Admin class for browsing and managing postmortem records.
    Host apps can inherit this class to use the Admin without extra configuration.

    Features:
        - Incident list: start time, duration, affected services, etc.
        - Filtering: by date, source (auto/manual)
        - Search: incident ID, service name
        - Read-only: created by the system only, no modification

    Example:
        from django.contrib import admin
        from baldur.adapters.django.admin import BasePostmortemRecordAdmin
        from myapp.models import PostmortemRecord

        @admin.register(PostmortemRecord)
        class PostmortemRecordAdmin(BasePostmortemRecordAdmin):
            pass
    """

    if not DJANGO_AVAILABLE:
        raise ImportError(
            "Django is required to use BasePostmortemRecordAdmin. "
            "Install it with: pip install django"
        )

    # =========================================================================
    # List View Configuration
    # =========================================================================

    list_display = [
        "incident_id",
        "started_at",
        "duration_display",
        "affected_services_display",
        "source_display",
        "created_at",
    ]

    list_filter = [
        "source",
        "started_at",
        "created_at",
    ]

    search_fields = [
        "incident_id",
        "affected_services",
    ]

    ordering = ["-started_at"]

    date_hierarchy = "started_at"

    # =========================================================================
    # Detail View Configuration
    # =========================================================================

    readonly_fields = [
        "id",
        "incident_id",
        "started_at",
        "resolved_at",
        "duration_seconds",
        "affected_services",
        "timeline",
        "auto_actions",
        "recommendations",
        "system_snapshot",
        "created_at",
        "source",
    ]

    fieldsets = (
        (
            "Incident Overview",
            {
                "fields": (
                    "id",
                    "incident_id",
                    "source",
                ),
            },
        ),
        (
            "Timeline",
            {
                "fields": (
                    "started_at",
                    "resolved_at",
                    "duration_seconds",
                ),
            },
        ),
        (
            "Impact Analysis",
            {
                "fields": ("affected_services",),
            },
        ),
        (
            "Detailed Data",
            {
                "fields": (
                    "timeline",
                    "auto_actions",
                    "recommendations",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "System Snapshot",
            {
                "fields": ("system_snapshot",),
                "classes": ("collapse",),
            },
        ),
        (
            "Metadata",
            {
                "fields": ("created_at",),
                "classes": ("collapse",),
            },
        ),
    )

    # =========================================================================
    # Permission Methods
    # =========================================================================

    def has_add_permission(self, request: HttpRequest) -> bool:
        """
        Disallow manual addition of Postmortem records.

        Postmortem records are created automatically by the system,
        so manual addition is not permitted through the Admin.
        """
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: Any = None,
    ) -> bool:
        """
        Disallow modification of Postmortem records.

        Modification is disallowed to preserve incident-record integrity.
        """
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: Any = None,
    ) -> bool:
        """
        Only administrators can delete.

        Regular staff users cannot delete;
        only superusers have delete permission.
        """
        return request.user.is_superuser

    # =========================================================================
    # Display Methods
    # =========================================================================

    def duration_display(self, obj: Any) -> str:
        """
        Display duration in a human-readable format.

        Args:
            obj: PostmortemRecord instance

        Returns:
            Formatted duration string
            - Under 60 seconds: "45 sec"
            - 60-3600 seconds: "5.0 min"
            - 3600 seconds or more: "2.00 hr"
            - None: "-"
        """
        if obj.duration_seconds is None:
            return "-"

        seconds = obj.duration_seconds
        if seconds < 60:
            return f"{seconds:.0f} sec"
        if seconds < 3600:
            minutes = seconds / 60
            return f"{minutes:.1f} min"
        hours = seconds / 3600
        return f"{hours:.2f} hr"

    duration_display.short_description = "Duration"
    duration_display.admin_order_field = "duration_seconds"

    def affected_services_display(self, obj: Any) -> str:
        """
        Display the affected services list in a compact form.

        Shows all services when there are 3 or fewer; otherwise shows only
        the first 3 and abbreviates the rest as "(+N more)".

        Args:
            obj: PostmortemRecord instance

        Returns:
            Formatted services list string
        """
        services = obj.affected_services or []
        if not services:
            return "-"

        if len(services) <= 3:
            return ", ".join(services)
        return format_html(
            "{} <span style='color: #888;'>(+{} more)</span>",
            ", ".join(services[:3]),
            len(services) - 3,
        )

    affected_services_display.short_description = "Affected Services"

    def source_display(self, obj: Any) -> str:
        """
        Display the source distinguished by icon and color.

        Args:
            obj: PostmortemRecord instance

        Returns:
            HTML-formatted source string
            - auto: Auto (blue)
            - manual: Manual (green)
        """
        if obj.source == "auto":
            return mark_safe(
                '<span style="color: blue; font-weight: bold;">🤖 Auto</span>'
            )
        return mark_safe(
            '<span style="color: green; font-weight: bold;">👤 Manual</span>'
        )

    source_display.short_description = "Source"
    source_display.admin_order_field = "source"


# =============================================================================
# Base DLQ Entry Admin (Dead Letter Queue / Failed Operation)
# =============================================================================


class BaseDLQEntryAdmin(admin.ModelAdmin if DJANGO_AVAILABLE else object):  # type: ignore[misc]
    """
    Base Admin configuration for the Dead Letter Queue.

    Base Admin class for browsing, replaying, and resolving failed operations (DLQ Entries).
    Host apps can inherit this class to use the Admin without extra configuration.

    Features:
        - Failed operation list: domain, failure type, status, retry count, etc.
        - Filtering: domain, status, failure type, creation date
        - Search: error message, entity information
        - Admin Actions: replay, mark as resolved/rejected/requires_review

    Example:
        from django.contrib import admin
        from baldur.adapters.django.admin import BaseDLQEntryAdmin
        from myapp.models import FailedOperation

        @admin.register(FailedOperation)
        class FailedOperationAdmin(BaseDLQEntryAdmin):
            pass
    """

    if not DJANGO_AVAILABLE:
        raise ImportError(
            "Django is required to use BaseDLQEntryAdmin. "
            "Install it with: pip install django"
        )

    # =========================================================================
    # List View Configuration
    # =========================================================================

    list_display = [
        "id",
        "domain",
        "failure_type",
        "status_display",
        "entity_display",
        "user_link",
        "retry_count",
        "created_at",
        "resolved_at",
    ]

    list_filter = [
        "domain",
        "status",
        "failure_type",
        "created_at",
        "resolved_at",
    ]

    search_fields = [
        "failure_type",
        "error_code",
        "error_message",
        "entity_type",
        "entity_id",
        "user__username",
        "user__email",
    ]

    ordering = ["-created_at"]

    # =========================================================================
    # Detail View Configuration
    # =========================================================================

    readonly_fields = [
        "domain",
        "failure_type",
        "entity_type",
        "entity_id",
        "user",
        "snapshot_data",
        "error_code",
        "error_message",
        "retry_count",
        "max_retries",
        "last_retry_at",
        "request_data",
        "response_data",
        "metadata",
        "next_action_hint",
        "recommended_action",
        "created_at",
        "updated_at",
        "expires_at",
    ]

    actions = [
        "replay_selected",
        "mark_as_resolved",
        "mark_as_rejected",
        "mark_as_requires_review",
    ]

    fieldsets = (
        (
            "Classification",
            {
                "fields": ("domain", "failure_type", "status"),
            },
        ),
        (
            "References",
            {
                "fields": ("entity_type", "entity_id", "user"),
            },
        ),
        (
            "Error Details",
            {
                "fields": (
                    "error_code",
                    "error_message",
                    "next_action_hint",
                    "recommended_action",
                ),
            },
        ),
        (
            "Retry Information",
            {
                "fields": ("retry_count", "max_retries", "last_retry_at"),
                "classes": ("collapse",),
            },
        ),
        (
            "Resolution",
            {
                "fields": (
                    "resolved_at",
                    "resolved_by",
                    "resolution_type",
                    "resolution_note",
                ),
            },
        ),
        (
            "Forensic Data",
            {
                "fields": (
                    "snapshot_data",
                    "request_data",
                    "response_data",
                    "metadata",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Timestamps",
            {
                "fields": ("created_at", "updated_at", "expires_at"),
                "classes": ("collapse",),
            },
        ),
    )

    # =========================================================================
    # Display Methods
    # =========================================================================

    def status_display(self, obj: Any) -> str:
        """
        Display status with a color indicator.

        Args:
            obj: FailedOperation instance

        Returns:
            HTML-formatted status string
        """
        colors = {
            "pending": "orange",
            "reviewing": "blue",
            "replayed": "purple",
            "requires_review": "red",
            "resolved": "green",
            "rejected": "gray",
            "archived": "lightgray",
            "expired": "lightgray",
        }
        color = colors.get(obj.status, "black")
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color,
            obj.get_status_display(),
        )

    status_display.short_description = "Status"
    status_display.admin_order_field = "status"

    def entity_display(self, obj: Any) -> str:
        """
        Display the entity type and ID.

        Args:
            obj: FailedOperation instance

        Returns:
            Formatted entity information or "-"
        """
        if obj.entity_type and obj.entity_id:
            return format_html(
                '<span style="font-weight: bold;">{}</span> #{}',
                obj.entity_type.title(),
                obj.entity_id,
            )
        return "-"

    entity_display.short_description = "Entity"

    def user_link(self, obj: Any) -> str:
        """
        Display a link to the user.

        Args:
            obj: FailedOperation instance

        Returns:
            User Admin link or "-"

        Note:
            Host apps can override the User model's admin URL.
        """
        if obj.user:
            try:
                # Host app User model — overridable via get_user_admin_url()
                url = self.get_user_admin_url(obj.user)
                return format_html('<a href="{}">{}</a>', url, obj.user.username)
            except Exception:
                return obj.user.username
        return "-"

    user_link.short_description = "User"

    def get_user_admin_url(self, user: Any) -> str:
        """
        Return the user Admin URL.

        Host apps can override this to point at a different User model.

        Args:
            user: User instance

        Returns:
            Admin change-page URL
        """
        app_label = user._meta.app_label
        model_name = user._meta.model_name
        return reverse(f"admin:{app_label}_{model_name}_change", args=[user.id])

    # =========================================================================
    # Admin Actions
    # =========================================================================

    @admin.action(description="Replay selected DLQ entries")
    def replay_selected(self, request: HttpRequest, queryset) -> None:
        """
        Replay selected DLQ entries.

        Only entries in pending or requires_review status are replayed.
        Entries that exceed the maximum retry count are skipped.
        """
        from baldur.services import get_replay_service

        service = get_replay_service()
        success_count = 0
        fail_count = 0

        for entry in queryset.filter(status__in=["pending", "requires_review"]):
            if entry.retry_count >= entry.max_retries:
                fail_count += 1
                continue

            result = service.replay_single(entry.id)
            if result.success:
                success_count += 1
            else:
                fail_count += 1

        self.message_user(
            request,
            f"Replay complete: {success_count} succeeded, {fail_count} failed.",
        )

    @admin.action(description="Mark as RESOLVED")
    def mark_as_resolved(self, request: HttpRequest, queryset) -> None:
        """Mark selected entries as resolved."""
        count = 0
        for entry in queryset:
            entry.mark_as_resolved(
                resolved_by=request.user,
                note=f"Manually resolved by {request.user.username}",
                resolution_type="manual_fix",
            )
            count += 1

        self.message_user(request, f"Marked {count} entries as resolved.")

    @admin.action(description="Mark as REJECTED (unrecoverable)")
    def mark_as_rejected(self, request: HttpRequest, queryset) -> None:
        """Mark selected entries as rejected (unrecoverable)."""
        count = 0
        for entry in queryset:
            entry.mark_as_rejected(
                resolved_by=request.user,
                note=f"Rejected by {request.user.username}",
            )
            count += 1

        self.message_user(request, f"Marked {count} entries as rejected.")

    @admin.action(description="Mark as REQUIRES_REVIEW")
    def mark_as_requires_review(self, request: HttpRequest, queryset) -> None:
        """Mark selected entries as requiring review."""
        count = 0
        for entry in queryset:
            entry.mark_as_requires_review(
                note=f"Escalated by {request.user.username}",
            )
            count += 1

        self.message_user(request, f"Marked {count} entries as requiring review.")


# =============================================================================
# Base Circuit Breaker State Admin
# =============================================================================


class BaseCircuitBreakerStateAdmin(admin.ModelAdmin if DJANGO_AVAILABLE else object):  # type: ignore[misc]
    """
    Base Admin configuration for Circuit Breaker state.

    Base Admin class for managing the Circuit Breaker state of external services.
    Provides operational controls such as force open/close and reset.

    Features:
        - Circuit state list: service name, state, failure/success counts, etc.
        - Filtering: state, manual-control flag, creation date
        - Search: service name, control reason
        - Admin Actions: force open/close, close with DLQ replay, reset

    Example:
        from django.contrib import admin
        from baldur.adapters.django.admin import BaseCircuitBreakerStateAdmin
        from myapp.models import CircuitBreakerState

        @admin.register(CircuitBreakerState)
        class CircuitBreakerStateAdmin(BaseCircuitBreakerStateAdmin):
            pass
    """

    if not DJANGO_AVAILABLE:
        raise ImportError(
            "Django is required to use BaseCircuitBreakerStateAdmin. "
            "Install it with: pip install django"
        )

    # =========================================================================
    # List View Configuration
    # =========================================================================

    list_display = [
        "service_name",
        "state_display",
        "failure_count",
        "success_count",
        "manually_controlled_display",
        "controlled_by_id",
        "opened_at",
        "updated_at",
    ]

    list_filter = [
        "state",
        "manually_controlled",
        "created_at",
    ]

    search_fields = [
        "service_name",
        "control_reason",
    ]

    ordering = ["-updated_at"]

    # =========================================================================
    # Detail View Configuration
    # =========================================================================

    readonly_fields = [
        "failure_count",
        "success_count",
        "last_failure_at",
        "opened_at",
        "created_at",
        "updated_at",
    ]

    actions = [
        "force_open_selected",
        "force_close_selected",
        "force_close_with_replay",
        "reset_selected",
    ]

    fieldsets = (
        (
            "Service Information",
            {
                "fields": ("service_name", "state"),
            },
        ),
        (
            "Counters",
            {
                "fields": ("failure_count", "success_count", "last_failure_at"),
                "classes": ("collapse",),
            },
        ),
        (
            "Manual Control",
            {
                "fields": ("manually_controlled", "controlled_by_id", "control_reason"),
            },
        ),
        (
            "Timestamps",
            {
                "fields": ("opened_at", "created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    # =========================================================================
    # Display Methods
    # =========================================================================

    def state_display(self, obj: Any) -> str:
        """
        Display state with a color indicator.

        Args:
            obj: CircuitBreakerState instance

        Returns:
            HTML-formatted state string
            - closed: green (normal)
            - open: red (blocked)
            - half_open: orange (testing)
        """
        colors = {
            "closed": "green",
            "open": "red",
            "half_open": "orange",
        }
        color = colors.get(obj.state, "gray")
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color,
            obj.get_state_display(),
        )

    state_display.short_description = "State"
    state_display.admin_order_field = "state"

    def manually_controlled_display(self, obj: Any) -> str:
        """
        Display the manual-control state.

        Args:
            obj: CircuitBreakerState instance

        Returns:
            HTML-formatted control-state string
        """
        if obj.manually_controlled:
            return mark_safe('<span style="color: blue;">✓ Manual</span>')
        return mark_safe('<span style="color: gray;">Auto</span>')

    manually_controlled_display.short_description = "Control"

    # =========================================================================
    # Admin Actions
    # =========================================================================

    @admin.action(description="Force OPEN selected circuits (block requests)")
    def force_open_selected(self, request: HttpRequest, queryset) -> None:
        """
        Force the selected circuit breakers open.

        An open circuit blocks all requests to the corresponding service.
        """
        from baldur.services import get_circuit_breaker_service

        service = get_circuit_breaker_service()
        count = 0

        for circuit in queryset:
            result = service.force_open(
                service_name=circuit.service_name,
                reason=f"Admin action by {request.user.username}",
            )
            if result.success:
                count += 1

        self.message_user(
            request,
            f"Successfully opened {count} circuit breaker(s).",
        )

    @admin.action(description="Force CLOSE selected circuits (allow requests)")
    def force_close_selected(self, request: HttpRequest, queryset) -> None:
        """
        Force the selected circuit breakers closed (without DLQ replay).

        A closed circuit permits requests to the corresponding service.
        """
        from baldur.services import get_circuit_breaker_service

        service = get_circuit_breaker_service()
        count = 0

        for circuit in queryset:
            result = service.force_close(
                service_name=circuit.service_name,
                reason=f"Admin action by {request.user.username}",
                trigger_replay=False,
            )
            if result.success:
                count += 1

        self.message_user(
            request,
            f"Successfully closed {count} circuit breaker(s).",
        )

    @admin.action(description="Force CLOSE with DLQ replay")
    def force_close_with_replay(self, request: HttpRequest, queryset) -> None:
        """
        Close the selected circuit breakers and replay DLQ entries.

        After closing the circuit, automatically replays DLQ entries for that service.
        """
        from baldur.services import get_circuit_breaker_service

        service = get_circuit_breaker_service()
        count = 0

        for circuit in queryset:
            result = service.force_close(
                service_name=circuit.service_name,
                reason=f"Admin action with replay by {request.user.username}",
                trigger_replay=True,
            )
            if result.success:
                count += 1

        self.message_user(
            request,
            f"Successfully closed {count} circuit breaker(s) with DLQ replay triggered.",
        )

    @admin.action(description="Reset selected circuits to initial state")
    def reset_selected(self, request: HttpRequest, queryset) -> None:
        """
        Reset the selected circuit breakers to their initial state.

        Both counters and state are reset.
        """
        from baldur.services import get_circuit_breaker_service

        service = get_circuit_breaker_service()
        count = 0

        for circuit in queryset:
            result = service.reset(
                service_name=circuit.service_name,
                reason=f"Admin reset by {request.user.username}",
                controlled_by=getattr(request.user, "pk", None),
            )
            if result.success:
                count += 1

        self.message_user(
            request,
            f"Successfully reset {count} circuit breaker(s).",
        )


# =============================================================================
# Auto-register concrete models (223 Host App Decoupling)
# =============================================================================


def _auto_register_concrete_admin():
    """
    Auto-register concrete models provided by the baldur package.

    This is called at module-load time and registers admin classes
    for FailedOperation, FailedExternalRequest, SecurityIncident,
    and PostmortemRecord provided by the package.

    Uses admin.site.is_registered() to avoid double registration
    when the host app already registers its own admin classes.
    """
    if not DJANGO_AVAILABLE:
        return

    try:
        from baldur.adapters.django.models import (
            FailedExternalRequest,
            FailedOperation,
            SecurityIncident,
        )

        # FailedOperation Admin
        if not admin.site.is_registered(FailedOperation):

            @admin.register(FailedOperation)
            class FailedOperationAdmin(BaseDLQEntryAdmin):
                """Auto-registered FailedOperation admin from baldur package."""

                def get_user_admin_url(self, user: Any) -> str:
                    """Dynamic user admin URL based on AUTH_USER_MODEL."""
                    from django.conf import settings as django_settings

                    user_model = django_settings.AUTH_USER_MODEL
                    app_label, model_name = user_model.split(".")
                    return reverse(
                        f"admin:{app_label}_{model_name.lower()}_change",
                        args=[user.pk],
                    )

        # FailedExternalRequest Admin
        if not admin.site.is_registered(FailedExternalRequest):

            @admin.register(FailedExternalRequest)
            class FailedExternalRequestAdmin(admin.ModelAdmin):
                """Auto-registered FailedExternalRequest admin."""

                list_display = [
                    "id",
                    "domain",
                    "failure_type",
                    "status",
                    "entity_type",
                    "entity_id",
                    "retry_count",
                    "created_at",
                ]
                list_filter = ["domain", "status", "failure_type", "created_at"]
                search_fields = [
                    "error_code",
                    "error_message",
                    "entity_type",
                    "entity_id",
                ]
                ordering = ["-created_at"]
                readonly_fields = [
                    "domain",
                    "entity_type",
                    "entity_id",
                    "failure_type",
                    "error_code",
                    "error_message",
                    "retry_count",
                    "last_retry_at",
                    "request_data",
                    "response_data",
                    "metadata",
                    "created_at",
                    "updated_at",
                    "expires_at",
                ]

        # SecurityIncident Admin
        if not admin.site.is_registered(SecurityIncident):

            @admin.register(SecurityIncident)
            class SecurityIncidentAdmin(admin.ModelAdmin):
                """Auto-registered SecurityIncident admin."""

                list_display = [
                    "id",
                    "incident_type",
                    "severity",
                    "status",
                    "source_ip",
                    "detected_at",
                ]
                list_filter = ["incident_type", "severity", "status", "detected_at"]
                search_fields = ["description", "source_ip", "investigation_notes"]
                ordering = ["-detected_at"]
                readonly_fields = [
                    "incident_type",
                    "severity",
                    "source_ip",
                    "user_agent",
                    "raw_request",
                    "detected_at",
                    "updated_at",
                ]

    except Exception as e:
        logger.debug(
            "baldur.admin_auto_registration_skipped",
            error=e,
        )


_auto_register_concrete_admin()
