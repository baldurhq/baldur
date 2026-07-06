# baldur.interfaces — Eventing, Notification & Audit

The unified event-bus protocol (with its Kafka-shaped sub-protocols and NoOp
defaults), the notification and alert adapter contracts, the audit-log adapter
surface, and the traffic-routing adapter for multi-region failover.

## Event bus

::: baldur.interfaces.EventBusProtocol

::: baldur.interfaces.ConsumedEventProtocol

::: baldur.interfaces.KafkaEventBusProtocol

::: baldur.interfaces.NoOpKafkaEventBus

## Notification

::: baldur.interfaces.NotificationAdapter

::: baldur.interfaces.NotificationChannel

::: baldur.interfaces.NotificationSeverity

## Alerting

::: baldur.interfaces.AlertSeverity

::: baldur.interfaces.AlertCategory

::: baldur.interfaces.Alert

::: baldur.interfaces.AlertAdapter

## Audit

::: baldur.interfaces.AuditAction

::: baldur.interfaces.AuditEntry

::: baldur.interfaces.AuditLogAdapter

::: baldur.interfaces.NoOpKafkaAuditAdapter

::: baldur.interfaces.NoOpWormAdapter

## Traffic routing

::: baldur.interfaces.RoutingChange

::: baldur.interfaces.TrafficRoutingAdapter
