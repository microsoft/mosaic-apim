"""Can a registered gateway actually call this model endpoint?

This is the second of the two access relationships a model endpoint has. Gateway preflight asks
"what may *I* do here?" via effective permissions, a question an identity can only ask about
itself. Runtime readiness asks "can *that* principal call what MOSAIC publishes?", which is a
role-assignment read against a different principal.

ADR 0013 fixed how the answer is reached:

- The scope evaluated is the account the published API calls, including for an endpoint registered
  by Foundry project. Models are deployed on the parent resource, and a grant on the project does
  not reach it.
- Any role whose data actions cover the published operations counts, not one role definition ID.
  Role definitions are read and evaluated with Azure's semantics, and built-ins known to suffice
  stand in when a definition cannot be read.
- A conditional grant is never enough to claim "can invoke", and neither is a role when a deny
  assignment or the network stands in the way.

MOSAIC reports the answer and never grants the role, exactly as it does for its own access.
"""

import ipaddress
from dataclasses import dataclass
from enum import Enum

from mosaic_api.domain import (
    AZURE_AI_DEVELOPER_ROLE_ID,
    AZURE_AI_DEVELOPER_ROLE_NAME,
    AZURE_OPENAI_CONTRIBUTOR_ROLE_ID,
    AZURE_OPENAI_CONTRIBUTOR_ROLE_NAME,
    AZURE_OPENAI_USER_ROLE_ID,
    AZURE_OPENAI_USER_ROLE_NAME,
    COGNITIVE_SERVICES_DATA_CONTRIBUTOR_ROLE_ID,
    COGNITIVE_SERVICES_DATA_CONTRIBUTOR_ROLE_NAME,
    COGNITIVE_SERVICES_USER_ROLE_ID,
    COGNITIVE_SERVICES_USER_ROLE_NAME,
    FOUNDRY_OWNER_ROLE_ID,
    FOUNDRY_OWNER_ROLE_NAME,
    FOUNDRY_PROJECT_MANAGER_ROLE_ID,
    FOUNDRY_PROJECT_MANAGER_ROLE_NAME,
    FOUNDRY_USER_ROLE_ID,
    FOUNDRY_USER_ROLE_NAME,
    AccessRemediation,
    ApiShape,
    CognitiveServicesResourceId,
    Gateway,
    GatewayRuntimeAccess,
    ModelEndpointCapabilities,
    ModelProvider,
    NetworkReachability,
    RuntimeAccessEvaluation,
    RuntimeAccessReason,
    RuntimeRoleFinding,
    RuntimeRoleFindingKind,
    utc_now,
)
from mosaic_api.integrations.aoai.client import CognitiveServicesClient
from mosaic_api.integrations.apim.client import JsonObject
from mosaic_api.integrations.apim.model_apis import required_data_actions
from mosaic_api.integrations.rbac import condition_may_hold, condition_permits, grants_data_action

_ALL_SHAPES: tuple[ApiShape, ...] = (
    ApiShape.AZURE_OPENAI,
    ApiShape.FOUNDRY_MODELS,
    ApiShape.ANTHROPIC_MESSAGES,
)
# An AI Services (Foundry) account can host Foundry Models deployments and Claude side by side.
# MOSAIC publishes each through its own shape, so readiness needs both.
_FOUNDRY_SHAPES: tuple[ApiShape, ...] = (ApiShape.FOUNDRY_MODELS, ApiShape.ANTHROPIC_MESSAGES)

# Built-ins that cover every data action of a curated shape, taken from their real definitions
# (``az role definition list --name <id>``) and pinned against them by tests. They are consulted
# only when MOSAIC cannot read a role definition; otherwise the definition itself is evaluated.
# Foundry Owner and Foundry Project Manager carry an ABAC condition, but it constrains only which
# roles they may assign, so it holds for every inference call. Owner and Contributor are absent
# on purpose: they carry no data actions and cannot call a model.
#
# These grant ``Microsoft.CognitiveServices/*`` data actions, so they cover every shape.
_SERVICE_WIDE_ROLES: dict[str, str] = {
    FOUNDRY_USER_ROLE_ID: FOUNDRY_USER_ROLE_NAME,
    COGNITIVE_SERVICES_USER_ROLE_ID: COGNITIVE_SERVICES_USER_ROLE_NAME,
    COGNITIVE_SERVICES_DATA_CONTRIBUTOR_ROLE_ID: COGNITIVE_SERVICES_DATA_CONTRIBUTOR_ROLE_NAME,
    FOUNDRY_PROJECT_MANAGER_ROLE_ID: FOUNDRY_PROJECT_MANAGER_ROLE_NAME,
    FOUNDRY_OWNER_ROLE_ID: FOUNDRY_OWNER_ROLE_NAME,
}
KNOWN_SUFFICIENT_ROLES: dict[ApiShape, dict[str, str]] = {
    ApiShape.AZURE_OPENAI: {
        AZURE_OPENAI_USER_ROLE_ID: AZURE_OPENAI_USER_ROLE_NAME,
        AZURE_OPENAI_CONTRIBUTOR_ROLE_ID: AZURE_OPENAI_CONTRIBUTOR_ROLE_NAME,
        AZURE_AI_DEVELOPER_ROLE_ID: AZURE_AI_DEVELOPER_ROLE_NAME,
        **_SERVICE_WIDE_ROLES,
    },
    ApiShape.FOUNDRY_MODELS: {
        AZURE_AI_DEVELOPER_ROLE_ID: AZURE_AI_DEVELOPER_ROLE_NAME,
        **_SERVICE_WIDE_ROLES,
    },
    # Azure AI Developer grants the OpenAI and MaaS data actions but not the provider-model action
    # the Anthropic routes need.
    ApiShape.ANTHROPIC_MESSAGES: dict(_SERVICE_WIDE_ROLES),
}

# How a message names the published API that needs a missing data action.
_SHAPE_LABELS: dict[ApiShape, str] = {
    ApiShape.AZURE_OPENAI: "the Azure OpenAI API",
    ApiShape.FOUNDRY_MODELS: "the Foundry Models API",
    ApiShape.ANTHROPIC_MESSAGES: (
        "the Anthropic Messages API that MOSAIC publishes for Claude models"
    ),
}

# The "All Principals" entry Azure uses in a deny assignment's ``principals``.
_ALL_PRINCIPALS = "00000000-0000-0000-0000-000000000000"
_MANAGEMENT_GROUP_PREFIX = "/providers/microsoft.management/managementgroups/"
_MAX_FINDINGS = 20


def published_shapes(
    kind: str | None, provider: ModelProvider | None = None
) -> tuple[ApiShape, ...]:
    """The curated API shapes a gateway may be asked to call on this endpoint.

    The resource's kind decides what MOSAIC publishes. An Azure OpenAI resource gets the Azure
    OpenAI shape. An AI Services (Foundry) resource gets the Foundry Models shape, and the
    Anthropic Messages shape for Claude deployments. Both are required whichever models are
    deployed today, so a Claude deployment added later does not turn a "can invoke" into a failed
    call. Until MOSAIC can read the resource it does not know the kind, so every shape is
    required: a grant judged sufficient now must still be sufficient once the kind is known, or
    MOSAIC would contradict itself.
    """

    normalized = (kind or "").casefold()
    if not normalized:
        return _ALL_SHAPES
    if normalized == "openai" or provider == ModelProvider.AZURE_OPENAI:
        return (ApiShape.AZURE_OPENAI,)
    return _FOUNDRY_SHAPES


def _shape_data_actions(shapes: tuple[ApiShape, ...]) -> tuple[str, ...]:
    actions: dict[str, None] = {}
    for shape in shapes:
        actions.update(dict.fromkeys(required_data_actions(shape)))
    return tuple(actions)


def required_runtime_data_actions(
    kind: str | None, provider: ModelProvider | None = None
) -> tuple[str, ...]:
    """Every data action the gateway needs for the operations MOSAIC would publish here."""

    return _shape_data_actions(published_shapes(kind, provider))


def recommended_runtime_role(kind: str | None) -> tuple[str, str]:
    """The built-in MOSAIC recommends, as ``(role name, role definition ID)``.

    Cognitive Services OpenAI User for an Azure OpenAI resource: its data actions are limited to
    the OpenAI surface. Foundry User for anything else, including an endpoint registered by Foundry
    project, because Microsoft's Foundry RBAC guidance names Foundry User on the Foundry resource
    as the minimum for calling its models. Its data actions are the same as Cognitive Services
    User's, which the check also accepts, and they cover every shape, the Anthropic Messages
    routes included. Azure AI Developer does not cover those routes. Foundry User is also the
    answer while the kind is unknown, because whatever MOSAIC recommends the check must later
    accept.

    Roles are compared by definition ID rather than name. The Foundry roles were renamed in 2026
    ("Azure AI User" became "Foundry User") and the GUIDs were not.
    """

    if (kind or "").casefold() == "openai":
        return AZURE_OPENAI_USER_ROLE_NAME, AZURE_OPENAI_USER_ROLE_ID
    return FOUNDRY_USER_ROLE_NAME, FOUNDRY_USER_ROLE_ID


def known_sufficient_roles(
    kind: str | None, provider: ModelProvider | None = None
) -> dict[str, str]:
    """Built-ins that cover every shape this endpoint might publish, keyed by lowercase GUID."""

    tables = [KNOWN_SUFFICIENT_ROLES[shape] for shape in published_shapes(kind, provider)]
    first, rest = tables[0], tables[1:]
    return {
        guid.casefold(): name
        for guid, name in first.items()
        if all(guid in table for table in rest)
    }


class RuntimeAccessCheck:
    """Reads shared by every gateway evaluated against one endpoint in one check.

    Gateways in a tenant usually hold the same few roles, so role definitions are cached by ID for
    the duration of one check rather than re-read per gateway. Nothing outlives the check: a role
    an administrator edits is read afresh the next time.
    """

    def __init__(self, client: CognitiveServicesClient) -> None:
        self._client = client
        self._definitions: dict[str, JsonObject | None] = {}
        self._deny_assignments: list[JsonObject] | None = None
        self._deny_assignments_read = False

    async def role_definition(self, role_definition_id: str) -> JsonObject | None:
        key = role_definition_id.casefold()
        if key not in self._definitions:
            self._definitions[key] = await self._client.role_definition(role_definition_id)
        return self._definitions[key]

    async def deny_assignments(self) -> list[JsonObject] | None:
        if not self._deny_assignments_read:
            self._deny_assignments = await self._client.deny_assignments()
            self._deny_assignments_read = True
        return self._deny_assignments


class _Relation(Enum):
    DIRECT = "direct"
    INHERITED = "inherited"
    NARROWER = "narrower"
    UNRELATED = "unrelated"


def _scope_relation(assignment_scope: str, target: str) -> _Relation:
    """How an assignment's scope relates to the account the published API calls.

    RBAC inherits downward only. A grant on the subscription or resource group reaches the account,
    but a grant on a child resource, such as a Foundry project, confers nothing at the parent
    account. Treating a narrower grant as if it applied would report "can invoke" for calls that
    fail with 401. Management groups are not part of a resource ID, but ``$filter=principalId eq``
    and ``atScope()`` return only assignments at, above, or below the account, and nothing below an
    account is a management group.
    """

    assigned = assignment_scope.casefold().rstrip("/")
    wanted = target.casefold().rstrip("/")
    if assigned == wanted:
        return _Relation.DIRECT
    if (
        not assigned
        or wanted.startswith(f"{assigned}/")
        or assigned.startswith(_MANAGEMENT_GROUP_PREFIX)
    ):
        return _Relation.INHERITED
    if assigned.startswith(f"{wanted}/"):
        return _Relation.NARROWER
    return _Relation.UNRELATED


@dataclass(frozen=True)
class RoleVerdict:
    kind: RuntimeRoleFindingKind
    role_name: str | None
    missing: tuple[str, ...] = ()


def _read_definition(definition: JsonObject | None) -> tuple[str | None, list[JsonObject]] | None:
    if not definition:
        return None
    properties = definition.get("properties")
    if not isinstance(properties, dict):
        return None
    permissions = properties.get("permissions")
    if not isinstance(permissions, list):
        return None
    role_name = properties.get("roleName")
    return (
        role_name if isinstance(role_name, str) else None,
        [block for block in permissions if isinstance(block, dict)],
    )


def judge_role(
    permissions: list[JsonObject], actions: tuple[str, ...], *, assignment_condition: object = None
) -> RoleVerdict:
    """Evaluate one role's permission blocks against the data actions the published API needs.

    Each action must be granted by some block's ``dataActions`` and not removed by that block's
    ``notDataActions``. An action is only granted outright if the block's condition and the
    assignment's condition are both provably true for it; otherwise the grant is conditional, and a
    conditional grant never makes MOSAIC claim "can invoke". Roles are judged one assignment at a
    time: two partial roles that together cover the shape are reported as insufficient, which errs
    toward "cannot invoke" rather than away from it.
    """

    missing: list[str] = []
    conditional: list[str] = []
    for action in actions:
        granting = [block for block in permissions if grants_data_action(block, action)]
        if not granting:
            missing.append(action)
        elif not (
            condition_permits(assignment_condition, action)
            and any(condition_permits(block.get("condition"), action) for block in granting)
        ):
            conditional.append(action)
    if missing:
        return RoleVerdict(RuntimeRoleFindingKind.INSUFFICIENT, None, tuple(missing))
    if conditional:
        return RoleVerdict(RuntimeRoleFindingKind.CONDITIONAL, None)
    return RoleVerdict(RuntimeRoleFindingKind.SUFFICIENT, None)


async def _evaluate_role(
    check: RuntimeAccessCheck,
    role_definition_id: str,
    assignment_condition: object,
    actions: tuple[str, ...],
    fallback: dict[str, str],
) -> RoleVerdict:
    read = _read_definition(await check.role_definition(role_definition_id))
    if read is None:
        # Not being able to read a definition says nothing about what it grants. Only a built-in
        # already known to suffice is trusted; anything else stays unknown rather than missing.
        name = fallback.get(role_definition_id.casefold())
        if name is None:
            return RoleVerdict(RuntimeRoleFindingKind.UNREADABLE, None)
        if all(condition_permits(assignment_condition, action) for action in actions):
            return RoleVerdict(RuntimeRoleFindingKind.SUFFICIENT, name)
        return RoleVerdict(RuntimeRoleFindingKind.CONDITIONAL, name)
    role_name, permissions = read
    verdict = judge_role(permissions, actions, assignment_condition=assignment_condition)
    return RoleVerdict(verdict.kind, role_name, verdict.missing)


@dataclass(frozen=True)
class _Assessed:
    finding: RuntimeRoleFinding
    relation: _Relation
    order: int


def _finding_kind(relation: _Relation, verdict: RoleVerdict) -> RuntimeRoleFindingKind | None:
    if relation is _Relation.NARROWER:
        # A narrower grant only matters when it would have sufficed at the right scope: that is
        # the grant an administrator can see in the portal and is puzzled by.
        if verdict.kind in {RuntimeRoleFindingKind.SUFFICIENT, RuntimeRoleFindingKind.CONDITIONAL}:
            return RuntimeRoleFindingKind.NARROWER_SCOPE
        return None
    return verdict.kind


_FINDING_ORDER = {
    RuntimeRoleFindingKind.SUFFICIENT: 0,
    RuntimeRoleFindingKind.CONDITIONAL: 1,
    RuntimeRoleFindingKind.NARROWER_SCOPE: 2,
    RuntimeRoleFindingKind.UNREADABLE: 3,
    RuntimeRoleFindingKind.INSUFFICIENT: 4,
}


async def _assess_assignments(
    check: RuntimeAccessCheck,
    assignments: list[JsonObject],
    *,
    scope: str,
    actions: tuple[str, ...],
    fallback: dict[str, str],
    recommended_role_id: str,
) -> list[_Assessed]:
    """Judge every assignment that covers the account or sits below it, best first.

    ``$filter=principalId eq`` returns assignments at, above, **and below** the account, so each is
    placed by ancestry before its role is read. A direct grant sorts ahead of an inherited one, or
    a correctly assigned endpoint would read as merely inheriting its role whenever a broader grant
    was listed first, and the recommended role sorts ahead of other sufficient ones.
    """

    assessed: list[_Assessed] = []
    for order, assignment in enumerate(assignments):
        properties = assignment.get("properties")
        if not isinstance(properties, dict):
            continue
        assignment_scope = properties.get("scope")
        definition = properties.get("roleDefinitionId")
        if not isinstance(assignment_scope, str) or not isinstance(definition, str):
            continue
        relation = _scope_relation(assignment_scope, scope)
        if relation is _Relation.UNRELATED:
            continue
        role_definition_id = definition.rstrip("/").rsplit("/", 1)[-1]
        verdict = await _evaluate_role(
            check, role_definition_id, properties.get("condition"), actions, fallback
        )
        kind = _finding_kind(relation, verdict)
        if kind is None:
            continue
        assessed.append(
            _Assessed(
                finding=RuntimeRoleFinding(
                    kind=kind,
                    role_name=verdict.role_name,
                    role_definition_id=role_definition_id,
                    scope=assignment_scope,
                    inherited=relation is _Relation.INHERITED,
                    missing_data_actions=list(verdict.missing),
                ),
                relation=relation,
                order=order,
            )
        )
    assessed.sort(
        key=lambda item: (
            _FINDING_ORDER[item.finding.kind],
            item.relation is not _Relation.DIRECT,
            (item.finding.role_definition_id or "").casefold() != recommended_role_id.casefold(),
            item.order,
        )
    )
    return assessed


def _role_reason(assessed: list[_Assessed]) -> RuntimeAccessReason:
    kinds = {item.finding.kind for item in assessed}
    if RuntimeRoleFindingKind.SUFFICIENT in kinds:
        return RuntimeAccessReason.GRANTED
    if RuntimeRoleFindingKind.UNREADABLE in kinds:
        return RuntimeAccessReason.ROLE_UNREADABLE
    if RuntimeRoleFindingKind.CONDITIONAL in kinds:
        return RuntimeAccessReason.CONDITIONAL
    if RuntimeRoleFindingKind.NARROWER_SCOPE in kinds:
        return RuntimeAccessReason.NARROWER_SCOPE
    return RuntimeAccessReason.MISSING_ROLE


# Findings that leave the answer open rather than negative. They are reported as not evaluated so
# that nothing keyed on ``evaluation`` alone can present them as a denial.
_UNCONFIRMED_ROLE_REASONS = frozenset(
    {RuntimeAccessReason.ROLE_UNREADABLE, RuntimeAccessReason.CONDITIONAL}
)


@dataclass(frozen=True)
class _Deny:
    definite: bool
    name: str
    scope: str
    action: str


def _principal_entries(value: object) -> list[tuple[str, str]]:
    if not isinstance(value, list):
        return []
    return [
        (str(item.get("id") or "").casefold(), str(item.get("type") or "").casefold())
        for item in value
        if isinstance(item, dict)
    ]


def _deny_targets(properties: JsonObject, principal_id: str) -> bool | None:
    """Whether a deny assignment applies to the gateway, or ``None`` if that depends on a group.

    MOSAIC cannot expand group membership, so a deny naming a group, or one exempting a group, may
    or may not apply to the gateway's identity. That is reported as unknown, never as clear.
    """

    wanted = principal_id.casefold()
    principals = _principal_entries(properties.get("principals"))
    excluded = _principal_entries(properties.get("excludePrincipals"))
    if any(entry_id == wanted for entry_id, _ in excluded):
        return False
    named = any(entry_id in {wanted, _ALL_PRINCIPALS} for entry_id, _ in principals)
    if not named:
        return None if any(entry_type == "group" for _, entry_type in principals) else False
    if any(entry_type == "group" for _, entry_type in excluded):
        return None
    return True


def _blocking_deny(
    deny_assignments: list[JsonObject], *, principal_id: str, scope: str, actions: tuple[str, ...]
) -> _Deny | None:
    """The deny assignment that blocks, or may block, one of the published data actions.

    Deny assignments override role assignments. Only Azure creates them, for deployment stacks and
    managed applications, so they are rare, but a role MOSAIC reports as sufficient does nothing
    if one applies.
    """

    possible: _Deny | None = None
    for deny in deny_assignments:
        properties = deny.get("properties")
        if not isinstance(properties, dict):
            continue
        deny_scope = properties.get("scope")
        if not isinstance(deny_scope, str):
            continue
        relation = _scope_relation(deny_scope, scope)
        if relation is _Relation.INHERITED and properties.get("doNotApplyToChildScopes") is True:
            continue
        if relation not in {_Relation.DIRECT, _Relation.INHERITED}:
            continue
        targets = _deny_targets(properties, principal_id)
        if targets is False:
            continue
        blocks = properties.get("permissions")
        name = properties.get("denyAssignmentName") or deny.get("name") or "(unnamed)"
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            conditions = (block.get("condition"), properties.get("condition"))
            for action in actions:
                if not grants_data_action(block, action):
                    continue
                if not all(condition_may_hold(condition, action) for condition in conditions):
                    continue
                definite = targets is True and all(
                    condition_permits(condition, action) for condition in conditions
                )
                found = _Deny(definite, str(name), deny_scope, action)
                if definite:
                    return found
                possible = possible or found
    return possible


@dataclass(frozen=True)
class NetworkPath:
    reachability: NetworkReachability
    message: str | None = None


def _admitted(address: str, rules: list[str]) -> bool:
    """Whether a firewall rule admits every address in ``address``, which may be a prefix."""

    try:
        candidate = ipaddress.ip_network(address.strip(), strict=False)
    except ValueError:
        return False
    for rule in rules:
        try:
            network = ipaddress.ip_network(rule.strip(), strict=False)
        except ValueError:
            continue
        if (
            candidate.version == network.version
            and int(network.network_address) <= int(candidate.network_address)
            and int(candidate.broadcast_address) <= int(network.broadcast_address)
        ):
            return True
    return False


def evaluate_network_path(
    capabilities: ModelEndpointCapabilities | None, gateway: Gateway
) -> NetworkPath:
    """Whether the gateway has a network path to the endpoint, as far as MOSAIC can tell.

    ``unreachable`` is claimed only when it is certain: public network access is disabled and the
    gateway has no virtual network, so no private endpoint can be in its path. A virtual network,
    a firewall MOSAIC cannot match the gateway's addresses against, or a network security perimeter
    is ``unverified``, because MOSAIC cannot see private endpoints, private DNS, or routing.
    """

    if capabilities is None or not capabilities.kind:
        return NetworkPath(NetworkReachability.UNKNOWN)
    name = gateway.name
    access = (capabilities.public_network_access or "Enabled").strip()
    network_type = gateway.capabilities.virtual_network_type
    if access.casefold() == "disabled":
        if network_type is None:
            return NetworkPath(
                NetworkReachability.UNVERIFIED,
                "Public network access to this resource is disabled, and MOSAIC has not "
                f"recorded whether {name} is connected to a virtual network. Re-run the "
                "gateway's access check so MOSAIC can tell whether it has a private path.",
            )
        if network_type.casefold() == "none":
            return NetworkPath(
                NetworkReachability.UNREACHABLE,
                "Public network access to this resource is disabled, and "
                f"{name} is not connected to a virtual network, so it has no network path to "
                "the resource whatever roles it holds. Connect the gateway to a virtual network "
                "that reaches a private endpoint for this resource, or allow public network "
                "access.",
            )
        return NetworkPath(
            NetworkReachability.UNVERIFIED,
            f"Public network access to this resource is disabled. {name} is connected to a "
            f"virtual network ({network_type}), but MOSAIC cannot see private endpoints or "
            "private DNS, so it cannot confirm the gateway reaches the resource. This is not a "
            "denial.",
        )
    if access.casefold() != "enabled":
        return NetworkPath(
            NetworkReachability.UNVERIFIED,
            f"Public network access to this resource is set to {access}, which MOSAIC cannot "
            "evaluate, so it cannot confirm the gateway reaches the resource. This is not a "
            "denial.",
        )
    if (capabilities.network_default_action or "").casefold() == "deny":
        egress = gateway.capabilities.egress_ip_addresses
        if (
            (network_type or "").casefold() == "none"
            and egress
            and all(_admitted(address, capabilities.network_ip_rules) for address in egress)
        ):
            return NetworkPath(NetworkReachability.REACHABLE)
        return NetworkPath(
            NetworkReachability.UNVERIFIED,
            "This resource's firewall admits only listed addresses and virtual networks, and "
            f"MOSAIC could not match {name}'s outbound addresses to those rules, so it cannot "
            "confirm the gateway reaches the resource. This is not a denial.",
        )
    return NetworkPath(NetworkReachability.REACHABLE)


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


def _subject(resource: CognitiveServicesResourceId) -> str:
    if resource.project_name:
        return f"the parent resource {resource.account_name}"
    return "this resource"


def _role_label(finding: RuntimeRoleFinding) -> str:
    return finding.role_name or f"the role {finding.role_definition_id}"


def _labels(findings: list[RuntimeRoleFinding], limit: int = 3) -> str:
    names = list(dict.fromkeys(_role_label(finding) for finding in findings))
    shown = names[:limit]
    if len(names) > limit:
        shown.append(f"{len(names) - limit} more")
    if len(shown) == 1:
        return shown[0]
    return f"{', '.join(shown[:-1])} and {shown[-1]}"


def _needed_by(action: str, shapes: tuple[ApiShape, ...]) -> str | None:
    """Which published API needs a missing action, when this endpoint publishes more than one.

    An AI Services account publishes a shape per model family, so a role can cover one and not
    another. Naming the API tells an administrator why a role that serves their chat models is
    still not enough.
    """

    if len(shapes) < 2:
        return None
    wanted = action.casefold()
    needing = [
        _SHAPE_LABELS[shape]
        for shape in shapes
        if wanted in {needed.casefold() for needed in required_data_actions(shape)}
    ]
    if not needing or len(needing) == len(shapes):
        return None
    names = " and ".join(needing)
    verb = "needs" if len(needing) == 1 else "need"
    return f"{names[0].upper()}{names[1:]} {verb} that action."


def _role_message(
    reason: RuntimeAccessReason,
    assessed: list[_Assessed],
    *,
    resource: CognitiveServicesResourceId,
    role_name: str,
    shapes: tuple[ApiShape, ...],
) -> str:
    subject = _subject(resource)
    findings = [item.finding for item in assessed]

    def of(kind: RuntimeRoleFindingKind) -> list[RuntimeRoleFinding]:
        return [finding for finding in findings if finding.kind == kind]

    if reason == RuntimeAccessReason.GRANTED:
        winner = of(RuntimeRoleFindingKind.SUFFICIENT)[0]
        if winner.inherited:
            return (
                f"The gateway's managed identity holds {_role_label(winner)} through an "
                f"assignment inherited from {winner.scope}, not one made directly on {subject}."
            )
        return (
            f"The gateway's managed identity holds {_role_label(winner)} on {subject}, which "
            "covers every operation MOSAIC publishes from it."
        )
    if reason == RuntimeAccessReason.ROLE_UNREADABLE:
        unreadable = of(RuntimeRoleFindingKind.UNREADABLE)
        return (
            f"The gateway's managed identity holds {_labels(unreadable)} on {subject}, and MOSAIC "
            "cannot read what that role grants, so it cannot confirm whether the gateway can "
            "call these models. This is not a denial: Reader on the resource lets MOSAIC read "
            "role definitions."
        )
    if reason == RuntimeAccessReason.CONDITIONAL:
        conditional = of(RuntimeRoleFindingKind.CONDITIONAL)
        return (
            f"The gateway's managed identity holds {_labels(conditional)} on {subject}, but only "
            "under an ABAC condition MOSAIC cannot prove holds for these calls, so it does not "
            f"count that as access. Grant {role_name} without a condition."
        )
    if reason == RuntimeAccessReason.NARROWER_SCOPE:
        narrower = of(RuntimeRoleFindingKind.NARROWER_SCOPE)[0]
        return (
            f"The gateway's managed identity holds {_role_label(narrower)} at {narrower.scope}, "
            "which is narrower than what the published API needs. Models are deployed on the "
            f"parent resource, {resource.account_name}, and the published API calls that "
            "resource. Role assignments apply downward only, so that grant does not let the "
            f"gateway call them. Grant {role_name} on {resource.account_name} instead."
        )
    message = (
        f"The gateway's managed identity does not hold a role on {subject} that covers the "
        "operations MOSAIC publishes, so it cannot call these models with managed identity."
    )
    insufficient = of(RuntimeRoleFindingKind.INSUFFICIENT)
    if insufficient:
        missing = insufficient[0].missing_data_actions
        message += (
            f" It holds {_labels(insufficient)} there, which does not grant "
            f"{missing[0] if missing else 'every data action it needs'}."
        )
        needed_by = _needed_by(missing[0], shapes) if missing else None
        if needed_by:
            message += f" {needed_by}"
    return message


def _unknown_kind_note(role_name: str) -> str:
    return (
        "MOSAIC cannot read this resource yet, so it does not know whether it is Azure OpenAI or "
        "Foundry, and an Azure OpenAI resource can use a narrower role. Grant MOSAIC Reader on the "
        f"resource and it will recommend the exact role. Meanwhile, {role_name} is accepted for "
        "either kind."
    )


def _join(*parts: str | None) -> str:
    return " ".join(part for part in parts if part)


@dataclass(frozen=True)
class _Context:
    gateway: Gateway
    scope: str
    role_name: str
    role_definition_id: str
    actions: tuple[str, ...]
    network: NetworkPath


def _result(
    context: _Context,
    *,
    can_invoke: bool,
    evaluation: RuntimeAccessEvaluation,
    reason: RuntimeAccessReason,
    message: str,
    principal_id: str | None = None,
    remediation: AccessRemediation | None = None,
    assessed: list[_Assessed] | None = None,
) -> GatewayRuntimeAccess:
    findings = [item.finding for item in assessed or []]
    winner = next(
        (finding for finding in findings if finding.kind == RuntimeRoleFindingKind.SUFFICIENT),
        None,
    )
    return GatewayRuntimeAccess(
        gateway_id=context.gateway.id,
        gateway_name=context.gateway.name,
        apim_principal_id=principal_id,
        can_invoke=can_invoke,
        evaluation=evaluation,
        reason=reason,
        checked_at=utc_now(),
        required_role_name=context.role_name,
        required_role_definition_id=context.role_definition_id,
        granted_role_name=winner.role_name if winner else None,
        granted_role_definition_id=winner.role_definition_id if winner else None,
        assignment_scope=winner.scope if winner else None,
        inherited=winner.inherited if winner else False,
        evaluated_scope=context.scope,
        required_data_actions=list(context.actions),
        role_findings=findings[:_MAX_FINDINGS],
        network_reachability=context.network.reachability,
        remediation=remediation,
        message=message,
    )


async def verify_gateway_runtime_access(
    client: CognitiveServicesClient,
    gateway: Gateway,
    *,
    kind: str | None,
    provider: ModelProvider | None = None,
    capabilities: ModelEndpointCapabilities | None = None,
    check: RuntimeAccessCheck | None = None,
) -> GatewayRuntimeAccess:
    """Report whether one gateway's managed identity can invoke models on this endpoint.

    ``check`` lets several gateways share role-definition reads; one is created when omitted.
    ``capabilities`` carries the endpoint's network settings; without them the network path is
    reported as unknown rather than guessed.
    """

    check = check or RuntimeAccessCheck(client)
    resource = client.resource
    scope = resource.account_scope
    role_name, role_definition_id = recommended_runtime_role(kind)
    shapes = published_shapes(kind, provider)
    actions = _shape_data_actions(shapes)
    network = evaluate_network_path(capabilities, gateway)
    context = _Context(gateway, scope, role_name, role_definition_id, actions, network)
    kind_note = None if kind else _unknown_kind_note(role_name)
    principal_id = gateway.capabilities.principal_id

    if not principal_id:
        if not gateway.capabilities.identity_observed:
            # MOSAIC has not read this gateway's identity block, which is not the same as the
            # gateway having no identity. Saying so is the difference between an accurate gap and
            # a false accusation.
            return _result(
                context,
                can_invoke=False,
                evaluation=RuntimeAccessEvaluation.NOT_EVALUATED,
                reason=RuntimeAccessReason.IDENTITY_NOT_OBSERVED,
                message=_join(
                    "MOSAIC has not read this gateway's managed identity yet, so it cannot say "
                    "whether the gateway can call this endpoint. Re-run the gateway's access "
                    "check first.",
                    network.message,
                ),
            )
        return _result(
            context,
            can_invoke=False,
            evaluation=RuntimeAccessEvaluation.NO_GATEWAY_IDENTITY,
            reason=RuntimeAccessReason.NO_GATEWAY_IDENTITY,
            message=_join(
                "This gateway has no managed identity, so no role can be assigned to it. Enable "
                "a system-assigned identity on the API Management service first.",
                network.message,
            ),
        )

    remediation = _runtime_remediation(
        scope,
        principal_id=principal_id,
        role_name=role_name,
        role_definition_id=role_definition_id,
    )
    unreachable = network.reachability == NetworkReachability.UNREACHABLE

    assignments = await client.role_assignments_for_principal(principal_id)
    if assignments is None:
        unreadable = (
            "MOSAIC cannot read role assignments on this endpoint, so it cannot confirm whether "
            "the gateway can call it. This is not a denial: grant MOSAIC a role that includes "
            "Microsoft.Authorization/roleAssignments/read, such as Reader, to evaluate it."
        )
        return _result(
            context,
            can_invoke=False,
            evaluation=RuntimeAccessEvaluation.NOT_EVALUATED,
            reason=(
                RuntimeAccessReason.NETWORK_UNREACHABLE
                if unreachable
                else RuntimeAccessReason.ASSIGNMENTS_UNREADABLE
            ),
            message=(
                _join(network.message, unreadable, kind_note)
                if unreachable
                else _join(unreadable, network.message, kind_note)
            ),
            principal_id=principal_id,
            remediation=remediation,
        )

    assessed = await _assess_assignments(
        check,
        assignments,
        scope=scope,
        actions=actions,
        fallback=known_sufficient_roles(kind, provider),
        recommended_role_id=role_definition_id,
    )
    role_reason = _role_reason(assessed)
    role_message = _role_message(
        role_reason, assessed, resource=resource, role_name=role_name, shapes=shapes
    )

    if role_reason != RuntimeAccessReason.GRANTED:
        return _result(
            context,
            can_invoke=False,
            evaluation=(
                RuntimeAccessEvaluation.NOT_EVALUATED
                if role_reason in _UNCONFIRMED_ROLE_REASONS
                else RuntimeAccessEvaluation.ROLE_ASSIGNMENTS
            ),
            reason=RuntimeAccessReason.NETWORK_UNREACHABLE if unreachable else role_reason,
            message=(
                _join(network.message, role_message, kind_note)
                if unreachable
                else _join(role_message, network.message, kind_note)
            ),
            principal_id=principal_id,
            remediation=remediation,
            assessed=assessed,
        )

    # Deny assignments are only worth reading once a role would otherwise suffice. If they cannot
    # be read, the verdict stands: only Azure creates them, and "cannot read" is not evidence of
    # one, so treating it as a block would turn every Reader-less check into a false negative.
    deny_assignments = await check.deny_assignments()
    deny = (
        _blocking_deny(deny_assignments, principal_id=principal_id, scope=scope, actions=actions)
        if deny_assignments is not None
        else None
    )
    if deny is not None:
        if deny.definite:
            deny_message = (
                f"{role_message} However, the deny assignment {deny.name} at {deny.scope} blocks "
                f"{deny.action}, and deny assignments override role assignments. Azure creates "
                "them for deployment stacks and managed applications, so the fix is in whatever "
                "created it, not another role."
            )
        else:
            deny_message = (
                f"{role_message} However, the deny assignment {deny.name} at {deny.scope} may "
                f"block {deny.action}: it depends on a group membership or condition MOSAIC "
                "cannot evaluate, so MOSAIC cannot confirm the gateway can call these models. "
                "This is not a denial."
            )
        return _result(
            context,
            can_invoke=False,
            evaluation=(
                RuntimeAccessEvaluation.ROLE_ASSIGNMENTS
                if deny.definite
                else RuntimeAccessEvaluation.NOT_EVALUATED
            ),
            reason=(
                RuntimeAccessReason.NETWORK_UNREACHABLE
                if unreachable
                else RuntimeAccessReason.DENY_ASSIGNMENT
            ),
            message=(
                _join(network.message, deny_message)
                if unreachable
                else _join(deny_message, network.message)
            ),
            principal_id=principal_id,
            assessed=assessed,
        )

    if network.reachability in {NetworkReachability.UNREACHABLE, NetworkReachability.UNVERIFIED}:
        # An unverified path is "cannot confirm", not "no", so it is reported as not evaluated,
        # exactly like a deny assignment that may or may not apply.
        return _result(
            context,
            can_invoke=False,
            evaluation=(
                RuntimeAccessEvaluation.ROLE_ASSIGNMENTS
                if unreachable
                else RuntimeAccessEvaluation.NOT_EVALUATED
            ),
            reason=(
                RuntimeAccessReason.NETWORK_UNREACHABLE
                if unreachable
                else RuntimeAccessReason.NETWORK_UNVERIFIED
            ),
            message=(
                _join(network.message, role_message)
                if unreachable
                else _join(role_message, network.message)
            ),
            principal_id=principal_id,
            assessed=assessed,
        )

    return _result(
        context,
        can_invoke=True,
        evaluation=RuntimeAccessEvaluation.ROLE_ASSIGNMENTS,
        reason=RuntimeAccessReason.GRANTED,
        message=role_message,
        principal_id=principal_id,
        assessed=assessed,
    )
