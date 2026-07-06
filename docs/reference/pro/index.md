# PRO API Reference

Auto-generated, symbol-level reference for the PRO `Status: Public` service
packages. PRO ships as the separate, license-gated `baldur-pro` distribution;
its API is documented publicly here (only the code is licensed), so you can
evaluate the surface before subscribing. For the package-level surface map and
the canonical import paths, see the
[PRO Addendum](../index.md#pro-addendum) on the API Reference overview.

!!! info "🔒 PRO Feature — requires a baldur-pro license"
    The packages below ship in the `baldur-pro` distribution. PRO modules import
    normally — there is no `ImportError`. PRO features activate only when
    `baldur.init()` runs with a valid `BALDUR_LICENSE_KEY`; without it the system
    runs with OSS defaults and `register_pro_services()` logs
    `entitlement.pro_registration_skipped`. See the
    [Entitlement Public-Key Note](../index.md#entitlement-public-key-note) for the
    token model and key-rotation policy.

## Reference pages

- [Dead-letter queue](dlq.md) — `baldur_pro.services.dlq`
- [Replay queue](replay.md) — `baldur_pro.services.replay`
- [Emergency mode](emergency-mode.md) — `baldur_pro.services.emergency_mode`
- [Bulkhead](bulkhead.md) — `baldur_pro.services.bulkhead`
- [Adaptive throttle](throttle.md) — `baldur_pro.services.throttle`
- [Canary rollout](canary.md) — `baldur_pro.services.canary`
- [Unified notification](unified-notification.md) — `baldur_pro.services.unified_notification`
