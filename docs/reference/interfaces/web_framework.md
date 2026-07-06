# baldur.interfaces — Web Framework

The web-framework abstraction (request/response context, method/content enums,
permission levels, the handler type alias) plus the framework exception
hierarchy. Adapter authors implement these to integrate Django, FastAPI, and
Flask.

## Enums

::: baldur.interfaces.HttpMethod

::: baldur.interfaces.ContentType

::: baldur.interfaces.PermissionLevel

## DTOs

::: baldur.interfaces.RequestContext

::: baldur.interfaces.ResponseContext

## Exceptions

::: baldur.interfaces.WebFrameworkError

::: baldur.interfaces.RouteNotFoundError

::: baldur.interfaces.AuthenticationError

::: baldur.interfaces.PermissionDeniedError

## Interface & types

::: baldur.interfaces.WebFrameworkInterface

::: baldur.interfaces.HandlerFunc
