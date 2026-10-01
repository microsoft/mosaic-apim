from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict


class DomainError(Exception):
    status_code = 400
    code = "domain_error"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        summary: str | None = None,
    ) -> None:
        self.message = message
        self.details = details or {}
        # MOSAIC's own wording. It differs from the message only when the message also says what
        # an upstream service said, such as the reason Azure gave for refusing a request.
        self.summary = summary or message
        super().__init__(message)


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class ValidationError(DomainError):
    status_code = 422
    code = "validation_error"


class UpstreamAuthorizationError(DomainError):
    """MOSAIC's identity lacks the Azure permissions needed for an upstream call."""

    status_code = 403
    code = "gateway_forbidden"


class UpstreamNotFoundError(DomainError):
    """The upstream Azure resource does not exist or is not visible to MOSAIC."""

    status_code = 404
    code = "gateway_not_found"


class UpstreamError(DomainError):
    """An upstream Azure call failed for a reason MOSAIC cannot resolve on the caller's behalf."""

    status_code = 502
    code = "gateway_unreachable"


class UpstreamUnsupportedError(DomainError):
    """The Azure resource does not implement the API version a MOSAIC feature needs.

    Distinct from ``UpstreamError`` because it is not a failure to resolve: the service answered,
    and the answer is that this capability is not available here. Callers report it as an absent
    capability rather than degrading a whole operation.
    """

    status_code = 501
    code = "gateway_capability_unsupported"


class UpstreamConflictError(DomainError):
    """The upstream resource changed between MOSAIC reading it and writing it.

    Distinct from :class:`ConflictError`, which is about MOSAIC's own records. This one means an
    ``If-Match`` precondition failed, so the plan was built against a state that no longer exists
    and applying it would overwrite somebody else's change.
    """

    status_code = 409
    code = "gateway_precondition_failed"


class DirectoryDisabledError(DomainError):
    """This deployment turned directory lookup off, so object IDs are entered by hand."""

    status_code = 409
    code = "directory_disabled"


class DirectoryForbiddenError(DomainError):
    """MOSAIC's identity lacks the Microsoft Graph permission a directory read needs.

    The message names the missing application permission, so an administrator can grant it.
    """

    status_code = 403
    code = "directory_forbidden"


class DirectoryError(DomainError):
    """Microsoft Graph failed or couldn't be reached for a reason MOSAIC can't resolve."""

    status_code = 502
    code = "directory_unavailable"


class ErrorBody(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)

    code: str
    message: str
    details: dict[str, Any]


async def domain_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DomainError):
        raise exc
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorBody(code=exc.code, message=exc.message, details=exc.details).model_dump(),
    )


async def request_validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's 422 response, without repeating what the request sent.

    Each error normally carries its ``input``: the field's value, or the whole body when a rule
    spans fields. A request can carry an API key (ADR 0021), and an error must never repeat it, so
    no input is repeated at all. Where it went wrong and why are unchanged.
    """

    if not isinstance(exc, RequestValidationError):
        raise exc
    errors = [
        {key: value for key, value in error.items() if key != "input"} for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})
