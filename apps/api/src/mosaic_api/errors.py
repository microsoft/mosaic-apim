from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict


class DomainError(Exception):
    status_code = 400
    code = "domain_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.message = message
        self.details = details or {}
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
