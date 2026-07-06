# baldur — Facade & Bootstrap

The top-level entry points: bootstrap wiring, the resilience facade, the
leader-elected scheduler, SQL transaction scope, the admin server, and the
framework-extra hooks. Every name below is covered by SemVer compatibility
guarantees in v1.x.

## Bootstrap

::: baldur.init

## Resilience facade

::: baldur.protect

::: baldur.aprotect

::: baldur.protected

::: baldur.aprotected

## Scheduler

::: baldur.get_leader_scheduler

## SQL storage

::: baldur.sql_transaction

## Admin server

::: baldur.start_admin_server

::: baldur.stop_admin_server

## Framework extras

::: baldur.fastapi_lifespan

::: baldur.init_flask
