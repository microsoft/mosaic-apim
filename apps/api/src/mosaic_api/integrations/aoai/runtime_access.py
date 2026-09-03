"""Can a registered gateway actually call this model endpoint?

This is the second of the two access relationships a model endpoint has, and the one the codebase
had no machinery for. Gateway preflight asks "what may *I* do here?" via effective permissions — a
question an identity can only ask about itself. Runtime readiness asks "does *that* principal hold
a role here?", which is a role-assignment read against a different principal.

MOSAIC reports the answer and never grants the role, exactly as it does for its own access.
"""

from dataclasses import dataclass

from mosaic_api.domain import (
    AZURE_OPENAI_USER_ROLE_ID,
    AZURE_OPENAI_USER_ROLE_NAME,
    COGNITIVE_SERVICES_USER_ROLE_ID,
    COGNITIVE_SERVICES_USER_ROLE_NAME,
    FOUNDRY_USER_ROLE_ID,
    FOUNDRY_USER_ROLE_NAME,
    AccessRemediation,
    CognitiveServicesResourceId,
    Gateway,
    GatewayRuntimeAccess,
    RuntimeAccessEvaluation,
    utc_now,
)
from mosaic_api.integrations.aoai.client import CognitiveServicesClient
from mosaic_api.integrations.apim.client import JsonObject


def required_runtime_role(
    resource: CognitiveServicesResourceId, kind: str | None
) -> tuple[str, str]:
    """The role a gateway's managed identity needs to invoke models here.

    Selected by resource shape, and matched by role definition ID rather than name: Microsoft
    renamed the Foundry roles in 2026 ("Azure AI User" became "Foundry User") and advises binding to
    the GUID, which the rename left unchanged.
    """

    if resource.project_name:
        return FOUNDRY_USER_ROLE_NAME, FOUNDRY_USER_ROLE_ID
    if (kind or "").casefold() == "openai":
        return AZURE_OPENAI_USER_ROLE_NAME, AZURE_OPENAI_USER_ROLE_ID
    return COGNITIVE_SERVICES_USER_ROLE_NAME, COGNITIVE_SERVICES_USER_ROLE_ID


def _runtime_remediation(
    scope: str, *, principal_id: str | None, role_name: str, role_definition_id: str
) -> AccessRemediation:
    assignee = principal_id or "<gateway-managed-identity-object-id>"
    command = (
        "az role assignment create"
        f' --assignee-object-id "{assignee}"'
        " --assignee-principal-type ServicePrincipal"
        f' --role "{role_name}"'
        f' --scope "{scope}"'
    )
    return AccessRemediation(
        role_name=role_name,
        role_definition_id=role_definition_id,
        scope=scope,
        principal_id=principal_id,
        command=command,
    )


def _scope_covers(assignment_scope: str, target: str) -> bool:
    """Does an assignment made at ``assignment_scope`` apply at ``target``?

    RBAC inherits downward only. A grant on a subscription or resource group reaches every resource
    inside it, but a grant on a child resource — a Foundry project, say — confers nothing at the
    parent account. Treating a narrower grant as if it applied would tell an administrator the
    gateway can call every model on the account when it can call none of them.
    """

    assigned = assignment_scope.casefold().rstrip("/")
    wanted = target.casefold().rstrip("/")
    return wanted == assigned or wanted.startswith(f"{assigned}/")


@dataclass(frozen=True)
class _AssignmentMatch:
    assignment: JsonObject | None
    narrower_scopes: tuple[str, ...]


def _matching_assignment(
    assignments: list[JsonObject], role_definition_id: str, scope: str
) -> _AssignmentMatch:
    """Find an assignment of the required role that actually applies at ``scope``.

    ``$filter=principalId eq`` returns assignments at, above **and below** the requested scope, so
    the results must be filtered by ancestry rather than trusted wholesale. An exact match wins over
    an inherited one, otherwise an endpoint that is directly assigned would be reported as merely
    inheriting the role whenever a broader grant sorted first. Scopes strictly below the endpoint
    are collected separately so the operator can be told why their grant does not count.
    """

    target = scope.casefold().rstrip("/")
    direct: JsonObject | None = None
    inherited: JsonObject | None = None
    narrower: list[str] = []
    for assignment in assignments:
        properties = assignment.get("properties")
        if not isinstance(properties, dict):
            continue
        definition = properties.get("roleDefinitionId")
        if not isinstance(definition, str):
            continue
        if not definition.casefold().endswith(role_definition_id.casefold()):
            continue
        assignment_scope = properties.get("scope")
        if not isinstance(assignment_scope, str) or not assignment_scope:
            continue
        if assignment_scope.casefold().rstrip("/") == target:
            if direct is None:
                direct = assignment
        elif _scope_covers(assignment_scope, scope):
            if inherited is None:
                inherited = assignment
        else:
            narrower.append(assignment_scope)
    return _AssignmentMatch(direct or inherited, tuple(narrower))


async def verify_gateway_runtime_access(
    client: CognitiveServicesClient,
    gateway: Gateway,
    *,
    kind: str | None,
) -> GatewayRuntimeAccess:
    """Report whether one gateway's managed identity can invoke models on this endpoint."""

    resource = client.resource
    scope = resource.canonical
    role_name, role_definition_id = required_runtime_role(resource, kind)
    checked_at = utc_now()
    principal_id = gateway.capabilities.principal_id

    if not principal_id:
        if not gateway.capabilities.identity_observed:
            # MOSAIC has not read this gateway's identity block, which is not the same as the
            # gateway having no identity. Saying so is the difference between an accurate gap and
            # a false accusation.
            return GatewayRuntimeAccess(
                gateway_id=gateway.id,
                gateway_name=gateway.name,
                can_invoke=False,
                evaluation=RuntimeAccessEvaluation.NOT_EVALUATED,
                checked_at=checked_at,
                required_role_name=role_name,
                required_role_definition_id=role_definition_id,
                message=(
                    "MOSAIC has not read this gateway's managed identity yet, so it cannot say "
                    "whether the gateway can call this endpoint. Re-run the gateway's access "
                    "check first."
                ),
            )
        return GatewayRuntimeAccess(
            gateway_id=gateway.id,
            gateway_name=gateway.name,
            can_invoke=False,
            evaluation=RuntimeAccessEvaluation.NO_GATEWAY_IDENTITY,
            checked_at=checked_at,
            required_role_name=role_name,
            required_role_definition_id=role_definition_id,
            message=(
                "This gateway has no managed identity, so no role can be assigned to it. Enable a "
                "system-assigned identity on the API Management service first."
            ),
        )

    assignments = await client.role_assignments_for_principal(principal_id)
    if assignments is None:
        return GatewayRuntimeAccess(
            gateway_id=gateway.id,
            gateway_name=gateway.name,
            apim_principal_id=principal_id,
            can_invoke=False,
            evaluation=RuntimeAccessEvaluation.NOT_EVALUATED,
            checked_at=checked_at,
            required_role_name=role_name,
            required_role_definition_id=role_definition_id,
            remediation=_runtime_remediation(
                scope,
                principal_id=principal_id,
                role_name=role_name,
                role_definition_id=role_definition_id,
            ),
            message=(
                "MOSAIC cannot read role assignments on this endpoint, so it cannot confirm "
                "whether the gateway can call it. This is not a denial: grant MOSAIC a role that "
                "includes Microsoft.Authorization/roleAssignments/read to evaluate it."
            ),
        )

    match = _matching_assignment(assignments, role_definition_id, scope)
    if match.assignment is None:
        message = (
            f"The gateway's managed identity does not hold {role_name} on this endpoint, so "
            "it cannot call these models with managed identity."
        )
        if match.narrower_scopes:
            # Reporting this is the difference between an operator re-reading the docs and an
            # operator staring at a role they can see in the portal.
            message = (
                f"The gateway's managed identity holds {role_name} at {match.narrower_scopes[0]}, "
                "which is narrower than this endpoint. Role assignments apply downward only, so "
                "that grant does not let the gateway call models on this endpoint."
            )
        return GatewayRuntimeAccess(
            gateway_id=gateway.id,
            gateway_name=gateway.name,
            apim_principal_id=principal_id,
            can_invoke=False,
            evaluation=RuntimeAccessEvaluation.ROLE_ASSIGNMENTS,
            checked_at=checked_at,
            required_role_name=role_name,
            required_role_definition_id=role_definition_id,
            remediation=_runtime_remediation(
                scope,
                principal_id=principal_id,
                role_name=role_name,
                role_definition_id=role_definition_id,
            ),
            message=message,
        )

    properties = match.assignment.get("properties")
    assignment_scope = (
        properties.get("scope")
        if isinstance(properties, dict) and isinstance(properties.get("scope"), str)
        else None
    )
    inherited = bool(
        assignment_scope and assignment_scope.casefold() != scope.casefold()
    )
    message = f"The gateway's managed identity holds {role_name} on this endpoint."
    if inherited:
        message = (
            f"The gateway's managed identity holds {role_name} through an assignment inherited "
            f"from {assignment_scope}, not one made directly on this endpoint."
        )

    return GatewayRuntimeAccess(
        gateway_id=gateway.id,
        gateway_name=gateway.name,
        apim_principal_id=principal_id,
        can_invoke=True,
        evaluation=RuntimeAccessEvaluation.ROLE_ASSIGNMENTS,
        checked_at=checked_at,
        required_role_name=role_name,
        required_role_definition_id=role_definition_id,
        assignment_scope=assignment_scope,
        inherited=inherited,
        message=message,
    )
