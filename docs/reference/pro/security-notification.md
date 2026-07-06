# baldur_pro.services.security_notification — Security Notification Service

The PRO delivery service behind the notification channels:
`SecurityNotificationService`. The OSS notification value types it consumes
(`NotificationConfig`, `ChannelDeliveryResult`, `SecurityNotificationResult`,
`NotificationChannel`) live in the
[OSS security & notification reference](../services/security.md).

!!! info "🔒 PRO Feature — requires a baldur-pro license"
    These symbols ship in the `baldur-pro` distribution. PRO modules import
    normally — there is no `ImportError`. PRO features activate only when
    `baldur.init()` runs with a valid `BALDUR_LICENSE_KEY`; without it the system
    runs with OSS defaults and `register_pro_services()` logs
    `entitlement.pro_registration_skipped`.

The package is delivery plumbing rather than an operator API — PRO service
registration wires it automatically, and the unified notification pipeline
delivers through it on your behalf — so it carries no rendered signature
pages. The service class composes one handler mixin per channel, and importing
the package registers the escalation push adapters into the provider seam so
escalation resolves a concrete transport on PRO installs.

The shared instance is obtained from `get_security_notification_service()`
(call it with no arguments); `configure_security_notification_service(service)`
swaps in a prebuilt instance, and `reset_security_notification_service()`
clears it. All three are built by a generic singleton factory.
