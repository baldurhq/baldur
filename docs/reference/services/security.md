# baldur.services — Security & Notification

Security-violation detection (service, config, violation/severity enums, the
severity lookup table) and the OSS notification value types that surface
incidents. The delivery service (`SecurityNotificationService`) ships in PRO —
see [its reference](../pro/security-notification.md).

## Violation detection

::: baldur.services.SecurityViolationService

::: baldur.services.SecurityViolationResult

::: baldur.services.SecurityConfig

::: baldur.services.ViolationType

::: baldur.services.Severity

::: baldur.services.SEVERITY_BY_VIOLATION_TYPE

::: baldur.services.handle_security_violation

## Notification

The notification *value types* are OSS; the delivery service that consumes them
(`SecurityNotificationService`) ships in PRO — see
[the PRO notification service reference](../pro/security-notification.md).

::: baldur.services.SecurityNotificationResult

::: baldur.services.NotificationConfig

::: baldur.services.NotificationChannel

::: baldur.services.ChannelDeliveryResult

## Singleton accessors

One process-wide singleton accessor rounds out this surface. It is built by a
generic singleton factory, so it carries no standalone signature page; call it
with no arguments to obtain the shared instance:

- `get_security_violation_service()` → the shared
  [`SecurityViolationService`][baldur.services.SecurityViolationService].

The notification service accessor (`get_security_notification_service()`) ships
in PRO — see [the PRO reference](../pro/security-notification.md).
