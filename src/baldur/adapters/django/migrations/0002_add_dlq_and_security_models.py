# 223 Host App Decoupling: Add concrete DLQ and Security models
# Uses SeparateDatabaseAndState for existing installations where
# tables already exist (managed by host app migrations).
# For fresh installations, the tables will be created automatically.


import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Add concrete models for Host App Decoupling (223).

    Strategy: SeparateDatabaseAndState
    - Existing DB: tables already exist (from host app migrations) → state only
    - Fresh DB: tables created by database_operations

    Models:
    - FailedOperation (db_table="failed_operations")
    - FailedExternalRequest (db_table="baldur_failed_external_request")
    - SecurityIncident (db_table="security_incidents")
    """

    dependencies = [
        ("baldur", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # =====================================================================
        # FailedOperation - concrete DLQ model
        # =====================================================================
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="FailedOperation",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        (
                            "domain",
                            models.CharField(
                                choices=[
                                    ("payment", "Payment"),
                                    ("point", "Point"),
                                    ("inventory", "Inventory"),
                                    ("webhook", "Webhook"),
                                    ("notification", "Notification"),
                                ],
                                db_index=True,
                                help_text="Business domain where the failure occurred",
                                max_length=50,
                                verbose_name="Domain",
                            ),
                        ),
                        (
                            "failure_type",
                            models.CharField(
                                db_index=True,
                                help_text="Specific failure classification (e.g., TIMEOUT, VALIDATION_ERROR)",
                                max_length=100,
                                verbose_name="Failure Type",
                            ),
                        ),
                        (
                            "status",
                            models.CharField(
                                choices=[
                                    ("pending", "Pending Review"),
                                    ("reviewing", "Under Review"),
                                    ("replayed", "Replay Queued"),
                                    ("requires_review", "Requires Human Review"),
                                    ("resolved", "Resolved"),
                                    ("rejected", "Rejected (Unrecoverable)"),
                                    ("archived", "Archived"),
                                    ("expired", "Retention Expired"),
                                ],
                                db_index=True,
                                default="pending",
                                max_length=30,
                                verbose_name="Status",
                            ),
                        ),
                        (
                            "entity_type",
                            models.CharField(
                                blank=True,
                                db_index=True,
                                help_text="Type of related entity (e.g., 'order', 'payment', 'subscription')",
                                max_length=100,
                                verbose_name="Entity Type",
                            ),
                        ),
                        (
                            "entity_id",
                            models.CharField(
                                blank=True,
                                db_index=True,
                                help_text="ID of related entity",
                                max_length=100,
                                verbose_name="Entity ID",
                            ),
                        ),
                        (
                            "entity_refs",
                            models.JSONField(
                                blank=True,
                                default=dict,
                                help_text="Additional entity references as {type: id} mapping",
                                verbose_name="Entity References",
                            ),
                        ),
                        (
                            "snapshot_data",
                            models.JSONField(
                                blank=True,
                                default=dict,
                                help_text="Complete state snapshot for recovery without accessing original records",
                                verbose_name="Snapshot Data",
                            ),
                        ),
                        (
                            "error_code",
                            models.CharField(
                                blank=True, max_length=100, verbose_name="Error Code"
                            ),
                        ),
                        (
                            "error_message",
                            models.TextField(blank=True, verbose_name="Error Message"),
                        ),
                        (
                            "retry_count",
                            models.PositiveIntegerField(
                                default=0,
                                help_text="Number of replay attempts from DLQ",
                                verbose_name="Retry Count",
                            ),
                        ),
                        (
                            "max_retries",
                            models.PositiveIntegerField(
                                default=2,
                                help_text="Maximum allowed replay attempts (default: 2)",
                                verbose_name="Max Retries",
                            ),
                        ),
                        (
                            "last_retry_at",
                            models.DateTimeField(
                                blank=True, null=True, verbose_name="Last Retry At"
                            ),
                        ),
                        (
                            "request_data",
                            models.JSONField(
                                blank=True,
                                default=dict,
                                help_text="Original request payload",
                                verbose_name="Request Data",
                            ),
                        ),
                        (
                            "response_data",
                            models.JSONField(
                                blank=True,
                                default=dict,
                                help_text="External system response",
                                verbose_name="Response Data",
                            ),
                        ),
                        (
                            "metadata",
                            models.JSONField(
                                blank=True,
                                default=dict,
                                help_text="Additional debug info: timing, retry history, state snapshots",
                                verbose_name="Metadata",
                            ),
                        ),
                        (
                            "resolved_at",
                            models.DateTimeField(
                                blank=True, null=True, verbose_name="Resolved At"
                            ),
                        ),
                        (
                            "resolution_type",
                            models.CharField(
                                blank=True,
                                choices=[
                                    ("auto_replay", "Automatic Replay"),
                                    ("manual_fix", "Manual Fix"),
                                    ("rejected", "Rejected"),
                                    ("expired", "Expired"),
                                    ("internal_error", "Internal Error"),
                                    ("archived", "Archived"),
                                ],
                                max_length=30,
                                verbose_name="Resolution Type",
                            ),
                        ),
                        (
                            "resolution_note",
                            models.TextField(
                                blank=True, verbose_name="Resolution Note"
                            ),
                        ),
                        (
                            "next_action_hint",
                            models.CharField(
                                blank=True,
                                help_text="Guidance for operators (e.g., 'Verify payment in PG admin')",
                                max_length=200,
                                verbose_name="Next Action Hint",
                            ),
                        ),
                        (
                            "recommended_action",
                            models.CharField(
                                blank=True,
                                choices=[
                                    ("replay", "Replay Operation"),
                                    ("manual_check", "Manual Verification"),
                                    ("escalate", "Escalate to Senior"),
                                    ("archive", "Archive (No Action)"),
                                ],
                                max_length=30,
                                verbose_name="Recommended Action",
                            ),
                        ),
                        (
                            "created_at",
                            models.DateTimeField(
                                auto_now_add=True,
                                db_index=True,
                                verbose_name="Created At",
                            ),
                        ),
                        (
                            "updated_at",
                            models.DateTimeField(
                                auto_now=True, verbose_name="Updated At"
                            ),
                        ),
                        (
                            "expires_at",
                            models.DateTimeField(
                                blank=True,
                                db_index=True,
                                help_text="Auto-archive after retention period",
                                null=True,
                                verbose_name="Expires At",
                            ),
                        ),
                        (
                            "user",
                            models.ForeignKey(
                                blank=True,
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="baldur_failed_operations",
                                to=settings.AUTH_USER_MODEL,
                                verbose_name="User",
                            ),
                        ),
                        (
                            "resolved_by",
                            models.ForeignKey(
                                blank=True,
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="baldur_resolved_operations",
                                to=settings.AUTH_USER_MODEL,
                                verbose_name="Resolved By",
                            ),
                        ),
                    ],
                    options={
                        "verbose_name": "Failed Operation (DLQ)",
                        "verbose_name_plural": "Failed Operations (DLQ)",
                        "db_table": "failed_operations",
                        "ordering": ["-created_at"],
                        "abstract": False,
                    },
                ),
            ],
            database_operations=[],  # Table already exists in existing installations
        ),
        # =====================================================================
        # FailedExternalRequest - external API DLQ model
        # =====================================================================
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="FailedExternalRequest",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        (
                            "domain",
                            models.CharField(
                                choices=[
                                    ("external_api", "External API"),
                                    ("payment", "Payment"),
                                    ("point", "Point"),
                                    ("inventory", "Inventory"),
                                    ("webhook", "Webhook"),
                                    ("notification", "Notification"),
                                ],
                                default="external_api",
                                max_length=50,
                                verbose_name="Domain",
                            ),
                        ),
                        (
                            "entity_type",
                            models.CharField(
                                blank=True,
                                db_index=True,
                                help_text="Related entity type (e.g., 'order', 'payment', 'subscription')",
                                max_length=100,
                                verbose_name="Entity Type",
                            ),
                        ),
                        (
                            "entity_id",
                            models.CharField(
                                blank=True,
                                db_index=True,
                                help_text="Related entity ID",
                                max_length=100,
                                verbose_name="Entity ID",
                            ),
                        ),
                        (
                            "entity_refs",
                            models.JSONField(
                                blank=True,
                                default=dict,
                                help_text="Additional entity references (e.g., {'user_id': 123, 'tenant_id': 'abc'})",
                                verbose_name="Entity References",
                            ),
                        ),
                        (
                            "user_id",
                            models.PositiveIntegerField(
                                blank=True,
                                db_index=True,
                                null=True,
                                verbose_name="User ID",
                            ),
                        ),
                        (
                            "external_request_id",
                            models.CharField(
                                blank=True,
                                max_length=200,
                                verbose_name="External Request ID",
                            ),
                        ),
                        (
                            "external_transaction_id",
                            models.CharField(
                                blank=True,
                                max_length=100,
                                verbose_name="External Transaction ID",
                            ),
                        ),
                        (
                            "amount",
                            models.DecimalField(
                                decimal_places=0,
                                default=0,
                                max_digits=10,
                                verbose_name="Amount",
                            ),
                        ),
                        (
                            "failure_type",
                            models.CharField(
                                choices=[
                                    ("max_retries_exceeded", "Max Retries Exceeded"),
                                    ("non_retryable_error", "Non-Retryable Error"),
                                    ("sla_timeout", "SLA Timeout Exceeded"),
                                    ("circuit_breaker_open", "Circuit Breaker Open"),
                                    ("manual_abort", "Manual Abort"),
                                    ("unknown", "Unknown Error"),
                                ],
                                default="unknown",
                                max_length=30,
                                verbose_name="Failure Type",
                            ),
                        ),
                        (
                            "error_code",
                            models.CharField(
                                blank=True, max_length=100, verbose_name="Error Code"
                            ),
                        ),
                        (
                            "error_message",
                            models.TextField(blank=True, verbose_name="Error Message"),
                        ),
                        (
                            "retry_count",
                            models.PositiveIntegerField(
                                default=0, verbose_name="Retry Count"
                            ),
                        ),
                        (
                            "last_retry_at",
                            models.DateTimeField(
                                blank=True, null=True, verbose_name="Last Retry At"
                            ),
                        ),
                        (
                            "status",
                            models.CharField(
                                choices=[
                                    ("pending", "Pending Review"),
                                    ("reviewing", "Reviewing"),
                                    ("resolved", "Resolved"),
                                    ("rejected", "Rejected"),
                                    ("expired", "Expired"),
                                ],
                                default="pending",
                                max_length=20,
                                verbose_name="Status",
                            ),
                        ),
                        (
                            "resolved_at",
                            models.DateTimeField(
                                blank=True, null=True, verbose_name="Resolved At"
                            ),
                        ),
                        (
                            "resolved_by_id",
                            models.PositiveIntegerField(
                                blank=True, null=True, verbose_name="Resolved By ID"
                            ),
                        ),
                        (
                            "resolution_note",
                            models.TextField(
                                blank=True, verbose_name="Resolution Note"
                            ),
                        ),
                        (
                            "request_data",
                            models.JSONField(
                                blank=True, default=dict, verbose_name="Request Data"
                            ),
                        ),
                        (
                            "response_data",
                            models.JSONField(
                                blank=True, default=dict, verbose_name="Response Data"
                            ),
                        ),
                        (
                            "metadata",
                            models.JSONField(
                                blank=True, default=dict, verbose_name="Metadata"
                            ),
                        ),
                        (
                            "created_at",
                            models.DateTimeField(
                                auto_now_add=True, verbose_name="Created At"
                            ),
                        ),
                        (
                            "updated_at",
                            models.DateTimeField(
                                auto_now=True, verbose_name="Updated At"
                            ),
                        ),
                        (
                            "expires_at",
                            models.DateTimeField(
                                blank=True, null=True, verbose_name="Expires At"
                            ),
                        ),
                    ],
                    options={
                        "verbose_name": "Failed External Request (DLQ)",
                        "verbose_name_plural": "Failed External Requests (DLQ)",
                        "db_table": "baldur_failed_external_request",
                        "ordering": ["-created_at"],
                        "abstract": False,
                    },
                ),
            ],
            database_operations=[],
        ),
        # =====================================================================
        # SecurityIncident - security violation tracking model
        # =====================================================================
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="SecurityIncident",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        (
                            "incident_type",
                            models.CharField(
                                choices=[
                                    (
                                        "webhook_signature_invalid",
                                        "Webhook Signature Invalid",
                                    ),
                                    (
                                        "payment_amount_tampered",
                                        "Payment Amount Tampered",
                                    ),
                                    ("token_forged", "Token Forged"),
                                    ("unauthorized_access", "Unauthorized Access"),
                                    ("rate_limit_abuse", "Rate Limit Abuse"),
                                    ("suspicious_activity", "Suspicious Activity"),
                                    ("replay_attack", "Replay Attack Detected"),
                                    ("injection_attempt", "Injection Attempt"),
                                ],
                                db_index=True,
                                max_length=100,
                                verbose_name="Incident Type",
                            ),
                        ),
                        (
                            "severity",
                            models.CharField(
                                choices=[
                                    ("critical", "Critical"),
                                    ("high", "High"),
                                    ("medium", "Medium"),
                                ],
                                db_index=True,
                                max_length=20,
                                verbose_name="Severity",
                            ),
                        ),
                        (
                            "status",
                            models.CharField(
                                choices=[
                                    ("open", "Open"),
                                    ("investigating", "Investigating"),
                                    ("resolved", "Resolved"),
                                    ("false_positive", "False Positive"),
                                ],
                                db_index=True,
                                default="open",
                                max_length=30,
                                verbose_name="Status",
                            ),
                        ),
                        (
                            "source_ip",
                            models.GenericIPAddressField(
                                blank=True,
                                db_index=True,
                                help_text="IP address of the request origin",
                                null=True,
                                verbose_name="Source IP",
                            ),
                        ),
                        (
                            "user_agent",
                            models.TextField(blank=True, verbose_name="User Agent"),
                        ),
                        (
                            "description",
                            models.TextField(
                                help_text="Detailed description of the security incident",
                                verbose_name="Description",
                            ),
                        ),
                        (
                            "raw_request",
                            models.JSONField(
                                blank=True,
                                default=dict,
                                help_text="Sanitized request data for forensic analysis",
                                verbose_name="Raw Request",
                            ),
                        ),
                        (
                            "action_taken",
                            models.TextField(
                                blank=True,
                                help_text="Immediate protective action taken",
                                verbose_name="Action Taken",
                            ),
                        ),
                        (
                            "investigation_notes",
                            models.TextField(
                                blank=True, verbose_name="Investigation Notes"
                            ),
                        ),
                        (
                            "resolved_at",
                            models.DateTimeField(
                                blank=True, null=True, verbose_name="Resolved At"
                            ),
                        ),
                        (
                            "detected_at",
                            models.DateTimeField(
                                auto_now_add=True,
                                db_index=True,
                                verbose_name="Detected At",
                            ),
                        ),
                        (
                            "updated_at",
                            models.DateTimeField(
                                auto_now=True, verbose_name="Updated At"
                            ),
                        ),
                        (
                            "user_id",
                            models.PositiveIntegerField(
                                blank=True,
                                db_index=True,
                                help_text="Associated user ID (domain-neutral, no FK)",
                                null=True,
                                verbose_name="User ID",
                            ),
                        ),
                        (
                            "related_entity_type",
                            models.CharField(
                                blank=True,
                                max_length=100,
                                verbose_name="Related Entity Type",
                            ),
                        ),
                        (
                            "related_entity_id",
                            models.CharField(
                                blank=True,
                                max_length=100,
                                verbose_name="Related Entity ID",
                            ),
                        ),
                    ],
                    options={
                        "verbose_name": "Security Incident",
                        "verbose_name_plural": "Security Incidents",
                        "db_table": "security_incidents",
                        "ordering": ["-detected_at"],
                        "abstract": False,
                    },
                ),
            ],
            database_operations=[],
        ),
    ]
