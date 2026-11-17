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


class ChangeNotRecordedError(DomainError):
    """A side effect succeeded, but MOSAIC could not persist its record of the change."""

    status_code = 503
    code = "change_not_recorded"


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


class DirectoryThrottledError(DirectoryError):
    """Microsoft Graph asked a directory reader to wait."""

    status_code = 429
    code = "directory_throttled"

    def __init__(self, message: str, *, retry_after: int) -> None:
        self.retry_after = min(max(retry_after, 1), 300)
        super().__init__(message, details={"retryAfter": self.retry_after})


class ErrorBody(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)

    code: str
    message: str
    details: dict[str, Any]


_CALLER_ACCESS_ROUTES = frozenset(
    {
        ("GET", "/api/v1/me/entitlements"),
        ("GET", "/api/v1/portal/entitlements"),
        ("GET", "/api/v1/me/entitlements/{entitlement_id}/connection"),
        ("GET", "/api/v1/me/entitlements/{entitlement_id}/mcp-connection"),
        ("POST", "/api/v1/me/entitlements/{entitlement_id}/keys/reveal"),
        ("POST", "/api/v1/me/entitlements/{entitlement_id}/keys"),
        ("POST", "/api/v1/me/entitlements/{entitlement_id}/keys/rotate"),
        ("DELETE", "/api/v1/me/entitlements/{entitlement_id}/keys"),
    }
)
_CALLER_ACCESS_REASON_MESSAGES = {
    "notPublished": (
        "This model isn't published in API Management right now, so there's nothing to connect to"
    ),
    "noKey": "This grant has no key yet. Create one first.",
    "keyMissing": (
        "This grant's key is missing from API Management. Delete it and create a new one."
    ),
    "keyExists": "This grant already has a key. Rotate it, or delete it first.",
    "keyNotOwned": (
        "A subscription with this grant's key name already exists in API Management, "
        "and MOSAIC didn't create it."
    ),
    "keyScopeChanged": (
        "This grant's key is no longer scoped to its model in API Management. "
        "Ask an administrator to re-plan its access."
    ),
    "costCenterKeysOff": (
        "Keys are turned off for this grant's cost center. Use a Microsoft Entra token."
    ),
    "notACostCenterMember": "Current Entra membership does not permit this individual key.",
    "costCenterSetupRequired": (
        "This cost center needs a verified Entra group before it can be used."
    ),
    "resourceNotAvailable": "This resource is Not available under the selected cost center.",
    "resourceUnavailable": "Custom cost-center access requires an available, governed publication.",
    "governmentCostCenterUnsupported": (
        "Government resources are not available in Entra-backed cost-center workflows."
    ),
    "automaticEntraRequired": (
        "Automatic access requires caller-side Entra tokens and configured group claims. "
        "Enable them explicitly on the publication and deployment first."
    ),
    "costCenterAuthenticationUnavailable": (
        "No supported credential is enabled for this resource and cost center."
    ),
    "requestOnlyRequired": (
        "Access requests require a Request-only resource under this cost center. "
        "Automatic access is inherited through its Entra group."
    ),
    "managedAutomaticGrantRequired": (
        "Custom-center group grants are managed by its Automatic resource definitions."
    ),
}
# Exact existing literals preserve portal copy; a local type or a supplied summary is not trust.
_CALLER_ACCESS_LOCAL_MESSAGES = frozenset(
    (
        *_CALLER_ACCESS_REASON_MESSAGES.values(),
        "Multiple principals map to this identity; resolve the duplicates",
        "The grant no longer matches its principal's identity kind",
        "Connection details are available for published model grants only",
        "This model API is not linked to a MOSAIC publication",
        "The publication binding changed; re-plan model access",
        "The publication's gateway is no longer registered",
        "Synchronize the gateway to discover its model API endpoint",
        "This model is no longer registered",
        "This model's gateway is no longer registered",
        "This model's API style has no operations governed access supports",
        "Key access requires an enabled grant successfully applied to API Management",
        "This grant has not been applied to API Management",
        "Apply the pending changes to this grant before using its key",
        (
            "A change to this model's access is in progress or needs recovery; "
            "key retrieval is blocked"
        ),
        "Connection details are available for MCP server grants only",
        "This MCP server is no longer registered",
        "The publication binding changed; re-plan MCP access",
        (
            "This MCP server isn't published in API Management right now, "
            "so there's nothing to connect to"
        ),
        "The MCP server's gateway is no longer registered",
        "A publication operation is in progress or needs recovery; key retrieval is blocked",
        "This grant's cost-center access needs review before using its key.",
        "The individual grant's principal no longer exists.",
        "No cost center has that ID",
        "Apply the pending changes to all models sharing this key before using it.",
        "Imported MCP grants do not have MOSAIC-managed keys.",
        "An MCP publication operation is in progress; try again later.",
        "Model access changed during key retrieval; reload and retry",
        "This deployment can't manage keys",
        "The publication disappeared while managing this key",
        "The model disappeared while managing this key",
        "This grant has no key to delete.",
        "APIM has not confirmed this key is active.",
        "This MCP publication supports Entra tokens only, not subscription keys.",
        "MCP keys require an enabled, successfully applied direct grant.",
        "This MCP grant has not been applied to API Management.",
        "Apply pending changes to this MCP grant before using its key.",
        "Keys are turned off for this cost center; Government access is unavailable.",
        "Keys are turned off for this cost center; Government key access is unavailable.",
        "This MCP grant has no key.",
        "The MCP publication disappeared.",
        "MOSAIC did not create this MCP subscription.",
        "The MCP key is missing. Delete and reprovision it.",
        "This MCP grant already has a key.",
        "APIM has not confirmed this MCP key is active.",
        "The managed subscription is no longer available in API Management",
        "The subscription is not active at the expected model API scope. Re-plan access.",
        "APIM returned an invalid Government runtime policy.",
        "APIM did not confirm Government subscription-key enforcement.",
        "The reviewed Government access policy is missing from APIM.",
        "APIM did not identify a Government API operation.",
        "Government API operations must inherit the reviewed policy without overrides.",
        "The Government MCP metadata policy is missing from APIM.",
        "Apply the reviewed Government policy before provisioning a key.",
        "The Government runtime policy changed. Review and apply it again.",
        "Government keys require an eligible direct grant; group grants cannot authorize calls.",
        (
            "Access granted to a security group uses Entra tokens only; "
            "there's no key to reveal."
        ),
    )
)


def _caller_access_message(error: DomainError) -> str:
    if isinstance(error, NotFoundError):
        return "Entitlement was not found"
    if isinstance(error, (ConflictError, ValidationError)):
        for message in (error.summary, error.message):
            if message in _CALLER_ACCESS_LOCAL_MESSAGES:
                return message
        reason = error.details.get("reason")
        if isinstance(reason, str) and reason in _CALLER_ACCESS_REASON_MESSAGES:
            return _CALLER_ACCESS_REASON_MESSAGES[reason]
        if isinstance(error, ValidationError):
            return (
                "This action is not valid for the grant. Ask an administrator to review "
                "its configuration and eligibility."
            )
        return (
            "This grant is not ready for the action. Refresh connection details and ask an "
            "administrator to review its access."
        )
    if isinstance(error, UpstreamAuthorizationError):
        return (
            "MOSAIC cannot access this grant's gateway service. Ask an administrator to check "
            "MOSAIC's gateway permissions."
        )
    if isinstance(error, UpstreamNotFoundError):
        return "The Azure resource was not found"
    if isinstance(error, UpstreamConflictError):
        return "The Azure resource changed since MOSAIC last read it"
    if isinstance(error, ChangeNotRecordedError):
        return (
            "The gateway change could not be recorded in MOSAIC. Refresh its status and ask an "
            "administrator to review it before repeating the action."
        )
    if isinstance(error, UpstreamUnsupportedError):
        return (
            "This gateway does not support the requested action. "
            "Ask an administrator to review it."
        )
    if isinstance(error, DirectoryForbiddenError):
        return (
            "MOSAIC cannot verify this grant's current membership. Ask an administrator to check "
            "MOSAIC's directory permissions."
        )
    if isinstance(error, DirectoryDisabledError):
        return (
            "Directory verification is unavailable. Ask an administrator to review this grant's "
            "cost-center access."
        )
    if isinstance(error, DirectoryError):
        return "Current membership could not be verified. Try again in a moment."
    if isinstance(error, UpstreamError):
        return "The gateway service did not complete this action. Try again in a moment."
    return "This action could not be completed. Ask an administrator to review the grant."


def _caller_access_details(error: DomainError) -> dict[str, Any]:
    details = error.details
    result: dict[str, Any] = {}
    reason = details.get("reason")
    if (
        isinstance(error, (NotFoundError, ConflictError, ValidationError))
        and isinstance(reason, str)
        and reason in _CALLER_ACCESS_REASON_MESSAGES
    ):
        result["reason"] = reason
    status_code = details.get("statusCode")
    if (
        isinstance(status_code, int)
        and not isinstance(status_code, bool)
        and 100 <= status_code <= 599
    ):
        result["statusCode"] = status_code
    retry_after = details.get("retryAfter")
    if (
        isinstance(retry_after, int)
        and not isinstance(retry_after, bool)
        and 1 <= retry_after <= 300
    ):
        result["retryAfter"] = retry_after
    return result


async def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DomainError):
        raise exc
    caller_access = (
        request.method,
        getattr(request.scope.get("route"), "path", None),
    ) in _CALLER_ACCESS_ROUTES
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorBody(
            code=exc.code,
            message=_caller_access_message(exc) if caller_access else exc.message,
            details=_caller_access_details(exc) if caller_access else exc.details,
        ).model_dump(),
        headers=(
            {"Retry-After": str(exc.retry_after)}
            if isinstance(exc, DirectoryThrottledError)
            else None
        ),
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
