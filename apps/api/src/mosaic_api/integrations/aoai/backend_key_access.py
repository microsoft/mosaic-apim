"""Can MOSAIC and each gateway read the Key Vault secret a key-authenticated endpoint's key is in?

These are the two access relationships of a key-authenticated endpoint (ADR 0018), and they take
the place of ADR 0006's Reader and runtime-role checks, which such an endpoint has no Azure scope
for. MOSAIC proves its own access by reading the secret. A gateway's is read from Key Vault's
role assignments or access policies, exactly as ADR 0013 reads a gateway's roles on a model
endpoint: any role whose data actions cover reading the secret counts, a condition MOSAIC can't
prove never counts, and "MOSAIC couldn't read this" is never reported as a denial. MOSAIC grants
nothing.

Reading assignments needs the vault's resource ID, which a secret URI doesn't carry. MOSAIC knows
its own vault's from configuration and looks any other up by name in the subscriptions it can
read. It needs ``Microsoft.Authorization/roleAssignments/read`` there, which Reader on the vault
grants without any access to secrets.

A secret's name and URI never leave this module: results name the vault.
"""

import re
from dataclasses import dataclass

import structlog

from mosaic_api.domain import (
    AUTHORIZATION_API_VERSION,
    KEY_VAULT_ADMINISTRATOR_ROLE_ID,
    KEY_VAULT_ADMINISTRATOR_ROLE_NAME,
    KEY_VAULT_API_VERSION,
    KEY_VAULT_GET_SECRET_DATA_ACTION,
    KEY_VAULT_SECRETS_OFFICER_ROLE_ID,
    KEY_VAULT_SECRETS_OFFICER_ROLE_NAME,
    KEY_VAULT_SECRETS_USER_ROLE_ID,
    KEY_VAULT_SECRETS_USER_ROLE_NAME,
    ROLE_DEFINITIONS_API_VERSION,
    AccessRemediation,
    Gateway,
    GatewayRuntimeAccess,
    NetworkReachability,
    RuntimeAccessEvaluation,
    RuntimeAccessReason,
    RuntimeRoleFinding,
    RuntimeRoleFindingKind,
    utc_now,
)
from mosaic_api.errors import DomainError, UpstreamAuthorizationError, UpstreamError
from mosaic_api.integrations.aoai.client import SubscriptionScanner
from mosaic_api.integrations.aoai.runtime_access import (
    _UNCONFIRMED_ROLE_REASONS,
    RuntimeAccessCheck,
    _admitted,
    _assess_assignments,
    _Assessed,
    _blocking_deny,
    _join,
    _role_reason,
)
from mosaic_api.integrations.apim.client import ArmClient, JsonObject

logger = structlog.get_logger()

RESOURCES_API_VERSION = "2021-04-01"
KEY_VAULT_ACTIONS: tuple[str, ...] = (KEY_VAULT_GET_SECRET_DATA_ACTION,)
# Built-ins whose data actions cover reading a secret, consulted only when MOSAIC can't read a role
# definition. Key Vault Reader is absent on purpose: it reads metadata, never a secret's value.
KNOWN_SECRET_READER_ROLES: dict[str, str] = {
    KEY_VAULT_SECRETS_USER_ROLE_ID: KEY_VAULT_SECRETS_USER_ROLE_NAME,
    KEY_VAULT_SECRETS_OFFICER_ROLE_ID: KEY_VAULT_SECRETS_OFFICER_ROLE_NAME,
    KEY_VAULT_ADMINISTRATOR_ROLE_ID: KEY_VAULT_ADMINISTRATOR_ROLE_NAME,
}
ACCESS_POLICY_REMEDIATION = "a Key Vault access policy with Get and List secret permissions"

_VAULT_RESOURCE_ID = re.compile(
    r"^/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[^/]{1,90}"
    r"/providers/Microsoft\.KeyVault/vaults/(?P<vault>[A-Za-z0-9-]{3,24})$",
    re.IGNORECASE,
)
_GATEWAY_PLACEHOLDER = "<gateway-managed-identity-object-id>"
_MOSAIC_PLACEHOLDER = "<mosaic-managed-identity-object-id>"


def vault_name_of(resource_id: str | None) -> str | None:
    match = _VAULT_RESOURCE_ID.fullmatch((resource_id or "").strip().rstrip("/"))
    return match.group("vault").casefold() if match else None


def vault_scope_argument(vault_name: str, vault_resource_id: str | None) -> str:
    """The ``--scope`` of a role assignment on the vault, resolved by Azure CLI when MOSAIC can't.

    ``"$(...)"`` substitutes the command's output in both Bash and PowerShell, so the command runs
    as shown in either.
    """

    if vault_resource_id:
        return vault_resource_id
    return f"$(az keyvault show --name {vault_name} --query id --output tsv)"


def secret_reader_remediation(
    vault_name: str, vault_resource_id: str | None, *, principal_id: str | None, mosaic: bool
) -> AccessRemediation:
    """Key Vault Secrets User on the vault, for MOSAIC or for a gateway."""

    argument = vault_scope_argument(vault_name, vault_resource_id)
    assignee = principal_id or (_MOSAIC_PLACEHOLDER if mosaic else _GATEWAY_PLACEHOLDER)
    return AccessRemediation(
        role_name=KEY_VAULT_SECRETS_USER_ROLE_NAME,
        role_definition_id=KEY_VAULT_SECRETS_USER_ROLE_ID,
        scope=vault_resource_id or f"Key Vault {vault_name}",
        principal_id=principal_id,
        command=(
            "az role assignment create"
            f' --assignee-object-id "{assignee}"'
            " --assignee-principal-type ServicePrincipal"
            f' --role "{KEY_VAULT_SECRETS_USER_ROLE_NAME}"'
            f' --scope "{argument}"'
        ),
    )


def access_policy_remediation(vault_name: str, principal_id: str | None) -> AccessRemediation:
    assignee = principal_id or _GATEWAY_PLACEHOLDER
    return AccessRemediation(
        role_name=ACCESS_POLICY_REMEDIATION,
        role_definition_id="",
        scope=f"Key Vault {vault_name}",
        principal_id=principal_id,
        command=(
            f'az keyvault set-policy --name "{vault_name}" --object-id "{assignee}" '
            "--secret-permissions get list"
        ),
    )


class KeyVaultArmClient:
    """ARM reads for one Key Vault. Read-only by construction, and never the data plane."""

    def __init__(self, arm: ArmClient, vault_resource_id: str) -> None:
        self._arm = arm
        self._vault = vault_resource_id.rstrip("/")

    @property
    def resource_id(self) -> str:
        return self._vault

    async def get_vault(self) -> JsonObject | None:
        try:
            return await self._arm.get(
                self._vault, params={"api-version": KEY_VAULT_API_VERSION}, allow_not_found=True
            )
        except (UpstreamAuthorizationError, UpstreamError):
            return None

    async def role_assignments_for_principal(self, principal_id: str) -> list[JsonObject] | None:
        """Assignments at, above and below the vault. ``None`` means "could not read"."""

        try:
            return await self._arm.list(
                f"{self._vault}/providers/Microsoft.Authorization/roleAssignments",
                params={
                    "api-version": AUTHORIZATION_API_VERSION,
                    "$filter": f"principalId eq '{principal_id}'",
                },
                allow_not_found=True,
            )
        except (UpstreamAuthorizationError, UpstreamError):
            return None

    async def role_definition(self, role_definition_guid: str) -> JsonObject | None:
        try:
            return await self._arm.get(
                f"{self._vault}/providers/Microsoft.Authorization/roleDefinitions"
                f"/{role_definition_guid}",
                params={"api-version": ROLE_DEFINITIONS_API_VERSION},
                allow_not_found=True,
            )
        except (UpstreamAuthorizationError, UpstreamError):
            return None

    async def deny_assignments(self) -> list[JsonObject] | None:
        try:
            return await self._arm.list(
                f"{self._vault}/providers/Microsoft.Authorization/denyAssignments",
                params={"api-version": AUTHORIZATION_API_VERSION, "$filter": "atScope()"},
                allow_not_found=True,
            )
        except (UpstreamAuthorizationError, UpstreamError):
            return None


class KeyVaultLocator:
    """Finds a vault's resource ID from its name, which is all a secret URI says about it.

    Vault names are unique in a cloud, so a name identifies one vault. MOSAIC's own vault is known
    from configuration; any other is looked up in the subscriptions MOSAIC can read, which finds
    it exactly when MOSAIC could read its role assignments anyway. A vault found once is
    remembered; one not found is looked for again next time, because it may be visible by then.
    """

    def __init__(
        self,
        arm: ArmClient,
        *,
        scanner: SubscriptionScanner | None = None,
        known_vault_ids: list[str] | None = None,
    ) -> None:
        self._arm = arm
        self._scanner = scanner
        self._found: dict[str, str] = {}
        for resource_id in known_vault_ids or []:
            name = vault_name_of(resource_id)
            if name:
                self._found[name] = resource_id.strip().rstrip("/")

    def client(self, vault_resource_id: str) -> KeyVaultArmClient:
        return KeyVaultArmClient(self._arm, vault_resource_id)

    async def locate(self, vault_name: str) -> str | None:
        name = vault_name.casefold()
        if name in self._found:
            return self._found[name]
        if self._scanner is None or not re.fullmatch(r"[a-z0-9-]{3,24}", name):
            return None
        try:
            subscriptions = await self._scanner.list_subscriptions()
        except DomainError:
            logger.warning("key_vault_locate_subscriptions_failed")
            return None
        for subscription in subscriptions:
            subscription_id = subscription.get("subscriptionId")
            if not isinstance(subscription_id, str) or not re.fullmatch(
                r"[0-9a-fA-F-]{36}", subscription_id
            ):
                continue
            try:
                resources = await self._arm.list(
                    f"/subscriptions/{subscription_id}/resources",
                    params={
                        "api-version": RESOURCES_API_VERSION,
                        "$filter": (
                            f"resourceType eq 'Microsoft.KeyVault/vaults' and name eq '{name}'"
                        ),
                    },
                    allow_not_found=True,
                )
            except DomainError:
                continue
            for resource in resources:
                resource_id = resource.get("id")
                if isinstance(resource_id, str) and vault_name_of(resource_id) == name:
                    self._found[name] = resource_id
                    return resource_id
        return None


@dataclass(frozen=True)
class VaultFacts:
    """What MOSAIC read about the vault, shared by every gateway in one check."""

    vault_name: str
    resource_id: str | None
    properties: JsonObject | None

    @property
    def uses_access_policies(self) -> bool:
        """True only when the vault says so. An unreadable vault is assumed to use RBAC."""

        properties = self.properties or {}
        return properties.get("enableRbacAuthorization") is False


def _network(facts: VaultFacts, gateway: Gateway) -> tuple[NetworkReachability, str | None]:
    """Whether API Management gets through the vault's network rules, as far as MOSAIC can tell.

    API Management reads a named value from Key Vault as a trusted Microsoft service, with its
    system-assigned identity, so a firewall that admits trusted services lets it through.
    """

    properties = facts.properties
    if properties is None:
        return NetworkReachability.UNKNOWN, None
    vault = facts.vault_name
    public = str(properties.get("publicNetworkAccess") or "Enabled").casefold()
    acls = properties.get("networkAcls")
    acls = acls if isinstance(acls, dict) else {}
    default_action = str(acls.get("defaultAction") or "Allow").casefold()
    bypass = str(acls.get("bypass") or "").casefold()
    if public == "disabled":
        return (
            NetworkReachability.UNVERIFIED,
            f"Public network access to Key Vault {vault} is disabled, and MOSAIC can't see "
            f"whether {gateway.name} has a private path to it. This isn't a denial.",
        )
    if default_action != "deny" or "azureservices" in bypass:
        return NetworkReachability.REACHABLE, None
    egress = gateway.capabilities.egress_ip_addresses
    rules = [
        str(rule.get("value"))
        for rule in acls.get("ipRules") or []
        if isinstance(rule, dict) and rule.get("value")
    ]
    if egress and all(_admitted(address, rules) for address in egress):
        return NetworkReachability.REACHABLE, None
    return (
        NetworkReachability.UNVERIFIED,
        f"Key Vault {vault}'s firewall admits only listed networks and doesn't allow trusted "
        f"Microsoft services, so MOSAIC can't confirm {gateway.name} gets through. Allow trusted "
        "Microsoft services on the vault's firewall. This isn't a denial.",
    )


def _result(
    gateway: Gateway,
    facts: VaultFacts,
    *,
    can_invoke: bool,
    evaluation: RuntimeAccessEvaluation,
    reason: RuntimeAccessReason,
    message: str,
    remediation: AccessRemediation | None,
    network: NetworkReachability = NetworkReachability.UNKNOWN,
    findings: list[RuntimeRoleFinding] | None = None,
    granted: RuntimeRoleFinding | None = None,
    granted_name: str | None = None,
    required_role: tuple[str, str] = (
        KEY_VAULT_SECRETS_USER_ROLE_NAME,
        KEY_VAULT_SECRETS_USER_ROLE_ID,
    ),
) -> GatewayRuntimeAccess:
    return GatewayRuntimeAccess(
        gateway_id=gateway.id,
        gateway_name=gateway.name,
        apim_principal_id=gateway.capabilities.principal_id,
        can_invoke=can_invoke,
        evaluation=evaluation,
        reason=reason,
        checked_at=utc_now(),
        required_role_name=required_role[0],
        required_role_definition_id=required_role[1] or None,
        granted_role_name=granted_name or (granted.role_name if granted else None),
        granted_role_definition_id=granted.role_definition_id if granted else None,
        assignment_scope=granted.scope if granted else None,
        inherited=granted.inherited if granted else False,
        evaluated_scope=facts.resource_id,
        required_data_actions=list(KEY_VAULT_ACTIONS),
        role_findings=(findings or [])[:20],
        network_reachability=network,
        remediation=remediation,
        message=message,
    )


def _visible(finding: RuntimeRoleFinding, vault_resource_id: str) -> RuntimeRoleFinding:
    """Name the vault rather than the secret when a role was assigned on the secret itself."""

    vault = vault_resource_id.casefold().rstrip("/")
    scope = finding.scope.casefold().rstrip("/")
    if scope == vault or scope.startswith(f"{vault}/"):
        return finding.model_copy(update={"scope": vault_resource_id, "inherited": False})
    return finding


def _role_label(finding: RuntimeRoleFinding) -> str:
    return finding.role_name or f"the role {finding.role_definition_id}"


def _access_policy_check(
    gateway: Gateway,
    facts: VaultFacts,
    principal_id: str,
    network: NetworkReachability,
    network_message: str | None,
) -> GatewayRuntimeAccess:
    vault = facts.vault_name
    policies = (facts.properties or {}).get("accessPolicies")
    granted = False
    for policy in policies if isinstance(policies, list) else []:
        if not isinstance(policy, dict):
            continue
        if str(policy.get("objectId") or "").casefold() != principal_id.casefold():
            continue
        permissions = policy.get("permissions")
        secrets = permissions.get("secrets") if isinstance(permissions, dict) else None
        allowed = {str(item).casefold() for item in secrets or []}
        if {"get", "all"} & allowed:
            granted = True
            break
    remediation = access_policy_remediation(vault, principal_id)
    required = (ACCESS_POLICY_REMEDIATION, "")
    if not granted:
        return _result(
            gateway,
            facts,
            can_invoke=False,
            evaluation=RuntimeAccessEvaluation.ROLE_ASSIGNMENTS,
            reason=RuntimeAccessReason.MISSING_ROLE,
            message=_join(
                f"Key Vault {vault} uses access policies, and none lets {gateway.name}'s managed "
                "identity get secrets, so the gateway can't read this endpoint's API key.",
                network_message,
            ),
            remediation=remediation,
            network=network,
            required_role=required,
        )
    unverified = network == NetworkReachability.UNVERIFIED
    return _result(
        gateway,
        facts,
        can_invoke=not unverified,
        evaluation=(
            RuntimeAccessEvaluation.NOT_EVALUATED
            if unverified
            else RuntimeAccessEvaluation.ROLE_ASSIGNMENTS
        ),
        reason=(
            RuntimeAccessReason.NETWORK_UNVERIFIED if unverified else RuntimeAccessReason.GRANTED
        ),
        message=_join(
            f"An access policy on Key Vault {vault} lets {gateway.name}'s managed identity get "
            "secrets, so the gateway can read this endpoint's API key.",
            network_message,
        ),
        remediation=None,
        network=network,
        granted_name="Key Vault access policy",
        required_role=required,
    )


async def verify_gateway_key_access(
    gateway: Gateway,
    facts: VaultFacts,
    *,
    client: KeyVaultArmClient | None,
    check: RuntimeAccessCheck | None,
    secret_name: str,
) -> GatewayRuntimeAccess:
    """Report whether one gateway's managed identity can read the endpoint's key from Key Vault.

    ``client`` and ``check`` are None when MOSAIC couldn't find the vault. The secret itself is the
    scope evaluated, so a role assigned on it counts, and so does one on the vault or above.
    """

    vault = facts.vault_name
    principal_id = gateway.capabilities.principal_id
    remediation = secret_reader_remediation(
        vault, facts.resource_id, principal_id=principal_id, mosaic=False
    )
    if not principal_id:
        if not gateway.capabilities.identity_observed:
            return _result(
                gateway,
                facts,
                can_invoke=False,
                evaluation=RuntimeAccessEvaluation.NOT_EVALUATED,
                reason=RuntimeAccessReason.IDENTITY_NOT_OBSERVED,
                message=(
                    "MOSAIC hasn't read this gateway's managed identity yet, so it can't say "
                    "whether the gateway can read this endpoint's API key. Re-run the gateway's "
                    "access check first."
                ),
                remediation=None,
            )
        return _result(
            gateway,
            facts,
            can_invoke=False,
            evaluation=RuntimeAccessEvaluation.NO_GATEWAY_IDENTITY,
            reason=RuntimeAccessReason.NO_GATEWAY_IDENTITY,
            message=(
                "This gateway has no managed identity, so it can't read this endpoint's API key "
                "from Key Vault. Turn on the API Management service's system-assigned managed "
                "identity first."
            ),
            remediation=None,
        )
    network, network_message = _network(facts, gateway)
    if facts.uses_access_policies:
        return _access_policy_check(gateway, facts, principal_id, network, network_message)
    if client is None or check is None or facts.resource_id is None:
        return _result(
            gateway,
            facts,
            can_invoke=False,
            evaluation=RuntimeAccessEvaluation.NOT_EVALUATED,
            reason=RuntimeAccessReason.ASSIGNMENTS_UNREADABLE,
            message=(
                f"MOSAIC can't find Key Vault {vault} in the subscriptions it can read, so it "
                f"can't check whether {gateway.name} can read this endpoint's API key. This isn't "
                "a denial: Reader on the vault lets MOSAIC check."
            ),
            remediation=remediation,
        )
    assignments = await client.role_assignments_for_principal(principal_id)
    if assignments is None:
        return _result(
            gateway,
            facts,
            can_invoke=False,
            evaluation=RuntimeAccessEvaluation.NOT_EVALUATED,
            reason=RuntimeAccessReason.ASSIGNMENTS_UNREADABLE,
            message=_join(
                f"MOSAIC can't read role assignments on Key Vault {vault}, so it can't confirm "
                f"{gateway.name} can read this endpoint's API key. This isn't a denial: Reader on "
                "the vault lets MOSAIC check.",
                network_message,
            ),
            remediation=remediation,
            network=network,
        )
    secret_scope = f"{facts.resource_id}/secrets/{secret_name}"
    assessed: list[_Assessed] = await _assess_assignments(
        check,
        assignments,
        scope=secret_scope,
        actions=KEY_VAULT_ACTIONS,
        fallback={key.casefold(): value for key, value in KNOWN_SECRET_READER_ROLES.items()},
        recommended_role_id=KEY_VAULT_SECRETS_USER_ROLE_ID,
    )
    findings = [_visible(item.finding, facts.resource_id) for item in assessed]
    reason = _role_reason(assessed)
    if reason == RuntimeAccessReason.NARROWER_SCOPE:
        # Nothing is narrower than the secret, so this can only be a finding on another secret,
        # which grants nothing here.
        reason = RuntimeAccessReason.MISSING_ROLE
    winner = next(
        (item for item in findings if item.kind == RuntimeRoleFindingKind.SUFFICIENT), None
    )
    if reason != RuntimeAccessReason.GRANTED:
        if reason == RuntimeAccessReason.ROLE_UNREADABLE:
            unreadable = [f for f in findings if f.kind == RuntimeRoleFindingKind.UNREADABLE]
            role_message = (
                f"{gateway.name}'s managed identity holds {_role_label(unreadable[0])} on Key "
                f"Vault {vault}, and MOSAIC can't read what that role grants, so it can't confirm "
                "the gateway can read this endpoint's API key. This isn't a denial."
            )
        elif reason == RuntimeAccessReason.CONDITIONAL:
            conditional = [f for f in findings if f.kind == RuntimeRoleFindingKind.CONDITIONAL]
            role_message = (
                f"{gateway.name}'s managed identity holds {_role_label(conditional[0])} on Key "
                f"Vault {vault}, but only under a condition MOSAIC can't prove holds, so it "
                f"doesn't count that as access. Grant {KEY_VAULT_SECRETS_USER_ROLE_NAME} without "
                "a condition."
            )
        else:
            role_message = (
                f"{gateway.name}'s managed identity holds no role on Key Vault {vault} that reads "
                "secrets, so the gateway can't read this endpoint's API key."
            )
        return _result(
            gateway,
            facts,
            can_invoke=False,
            evaluation=(
                RuntimeAccessEvaluation.NOT_EVALUATED
                if reason in _UNCONFIRMED_ROLE_REASONS
                else RuntimeAccessEvaluation.ROLE_ASSIGNMENTS
            ),
            reason=reason,
            message=_join(role_message, network_message),
            remediation=remediation,
            network=network,
            findings=findings,
        )
    assert winner is not None
    if winner.inherited:
        granted_message = (
            f"{gateway.name}'s managed identity holds {_role_label(winner)} through an assignment "
            f"inherited from {winner.scope}, which lets it read this endpoint's API key from Key "
            f"Vault {vault}."
        )
    else:
        granted_message = (
            f"{gateway.name}'s managed identity holds {_role_label(winner)} on Key Vault {vault}, "
            "which lets it read this endpoint's API key."
        )
    deny_assignments = await check.deny_assignments()
    deny = (
        _blocking_deny(
            deny_assignments,
            principal_id=principal_id,
            scope=secret_scope,
            actions=KEY_VAULT_ACTIONS,
        )
        if deny_assignments is not None
        else None
    )
    if deny is not None:
        return _result(
            gateway,
            facts,
            can_invoke=False,
            evaluation=(
                RuntimeAccessEvaluation.ROLE_ASSIGNMENTS
                if deny.definite
                else RuntimeAccessEvaluation.NOT_EVALUATED
            ),
            reason=RuntimeAccessReason.DENY_ASSIGNMENT,
            message=_join(
                granted_message,
                f"However, the deny assignment {deny.name} "
                f"{'blocks' if deny.definite else 'may block'} reading secrets, and deny "
                "assignments override role assignments.",
                network_message,
            ),
            remediation=None,
            network=network,
            findings=findings,
            granted=winner,
        )
    if network == NetworkReachability.UNVERIFIED:
        return _result(
            gateway,
            facts,
            can_invoke=False,
            evaluation=RuntimeAccessEvaluation.NOT_EVALUATED,
            reason=RuntimeAccessReason.NETWORK_UNVERIFIED,
            message=_join(granted_message, network_message),
            remediation=None,
            network=network,
            findings=findings,
            granted=winner,
        )
    return _result(
        gateway,
        facts,
        can_invoke=True,
        evaluation=RuntimeAccessEvaluation.ROLE_ASSIGNMENTS,
        reason=RuntimeAccessReason.GRANTED,
        message=granted_message,
        remediation=None,
        network=network,
        findings=findings,
        granted=winner,
    )
