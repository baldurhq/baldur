# baldur.interfaces — Task Queue

The task-queue contract (sync and async) with its status/priority enums,
result and option DTOs, and the queue exception hierarchy. Adapter authors
implement these to back Baldur on Celery, RQ, arq, and others.

## Enums

::: baldur.interfaces.TaskStatus

::: baldur.interfaces.TaskPriority

## DTOs

::: baldur.interfaces.TaskResult

::: baldur.interfaces.TaskOptions

::: baldur.interfaces.ScheduleInfo

## Exceptions

::: baldur.interfaces.TaskQueueError

::: baldur.interfaces.TaskNotFoundError

::: baldur.interfaces.TaskTimeoutError

::: baldur.interfaces.TaskRevokedError

## Interfaces

::: baldur.interfaces.TaskQueueInterface

::: baldur.interfaces.AsyncTaskQueueInterface
