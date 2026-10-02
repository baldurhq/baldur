# baldur.interfaces — Statistics & Event Journal

The statistics repository surface (summary DTOs + audit-trail records) and the
event-journal interface. Baldur fills the journal from its event bus in a Django
app, whose app config starts the subscriber; on other frameworks it stays empty
unless your code calls `init_event_journal()` from `baldur.services.event_journal`.

## Statistics DTOs

::: baldur.interfaces.StatusCounts

::: baldur.interfaces.DomainDistribution

::: baldur.interfaces.FailureTypeDistribution

::: baldur.interfaces.RecentActivity

::: baldur.interfaces.CleanupStats

::: baldur.interfaces.PaginatedResult

::: baldur.interfaces.CircuitBreakerSummary

::: baldur.interfaces.CircuitBreakerInfo

## Audit-trail DTOs

::: baldur.interfaces.AuditTrailEntry

::: baldur.interfaces.EntityAuditTrail

## Interfaces

::: baldur.interfaces.StatisticsRepositoryInterface

::: baldur.interfaces.EventJournalRepository

::: baldur.interfaces.JournalEntry

::: baldur.interfaces.JournalQueryFilter

::: baldur.interfaces.JournalQueryResult
