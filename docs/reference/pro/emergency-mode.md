# baldur_pro.services.emergency_mode — Emergency Mode

System-wide emergency levels and the manager that activates them:
`EmergencyLevel`, `get_emergency_manager`, and `is_emergency_active`.

!!! info "🔒 PRO Feature — requires a baldur-pro license"
    These symbols ship in the `baldur-pro` distribution. PRO modules import
    normally — there is no `ImportError`. PRO features activate only when
    `baldur.init()` runs with a valid `BALDUR_LICENSE_KEY`; without it the system
    runs with OSS defaults and `register_pro_services()` logs
    `entitlement.pro_registration_skipped`.

::: baldur_pro.services.emergency_mode
