"""Build a full observed inventory for one gateway.

Collection is deliberately partial-tolerant: a failure reading one collection degrades the snapshot
and is recorded, rather than aborting the sync and leaving the operator with nothing.
Operation-scope policies are not fetched here because that is an N x M call pattern; they are read
on demand.
"""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any, Literal

import structlog

from mosaic_api.domain import (
    CapabilitySupport,
    GatewayInventorySummary,
    McpEndpoint,
    McpServerKind,
    McpTool,
    McpTransportType,
    deterministic_id,
    new_id,
)
from mosaic_api.errors import DomainError, UpstreamUnsupportedError
from mosaic_api.integrations.apim.ai_detection import AI_POLICY_ELEMENTS, classify_api, classify_url
from mosaic_api.integrations.apim.client import ApimClient, JsonObject
from mosaic_api.integrations.apim.policy_semantics import (
    MOSAIC_FRAGMENT_PREFIX,
    analyze_policy,
    sanitize_url,
    summarize_facets,
)
from mosaic_api.observed import (
    AiBackendKind,
    ObservedApi,
    ObservedApimGroup,
    ObservedApimUser,
    ObservedBackend,
    ObservedEntity,
    ObservedMcpServer,
    ObservedNamedValue,
    ObservedOperation,
    ObservedPolicyDocument,
    ObservedPolicyFragment,
    ObservedProduct,
    ObservedSubscription,
    PolicyScope,
)

logger = structlog.get_logger()

DEFAULT_CONCURRENCY = 8

API_TYPE = "observedApi"
OPERATION_TYPE = "observedOperation"
MCP_SERVER_TYPE = "observedMcpServer"
PRODUCT_TYPE = "observedProduct"
SUBSCRIPTION_TYPE = "observedSubscription"
USER_TYPE = "observedApimUser"
GROUP_TYPE = "observedApimGroup"
BACKEND_TYPE = "observedBackend"
NAMED_VALUE_TYPE = "observedNamedValue"
POLICY_TYPE = "observedPolicyDocument"
FRAGMENT_TYPE = "observedPolicyFragment"

MCP_API_TYPE = "mcp"

SubscriptionScopeKind = Literal["allApis", "product", "api", "unknown"]


@dataclass
class InventorySnapshot:
    snapshot_id: str
    apis: list[ObservedApi] = field(default_factory=list)
    operations: list[ObservedOperation] = field(default_factory=list)
    mcp_servers: list[ObservedMcpServer] = field(default_factory=list)
    products: list[ObservedProduct] = field(default_factory=list)
    subscriptions: list[ObservedSubscription] = field(default_factory=list)
    users: list[ObservedApimUser] = field(default_factory=list)
    groups: list[ObservedApimGroup] = field(default_factory=list)
    backends: list[ObservedBackend] = field(default_factory=list)
    named_values: list[ObservedNamedValue] = field(default_factory=list)
    policy_documents: list[ObservedPolicyDocument] = field(default_factory=list)
    policy_fragments: list[ObservedPolicyFragment] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    incomplete_types: set[str] = field(default_factory=set)
    ai_policy_observed: bool = False
    mcp_support: CapabilitySupport = CapabilitySupport.UNKNOWN

    def entities(self) -> list[ObservedEntity]:
        entities: list[ObservedEntity] = []
        entities.extend(self.apis)
        entities.extend(self.operations)
        entities.extend(self.mcp_servers)
        entities.extend(self.products)
        entities.extend(self.subscriptions)
        entities.extend(self.users)
        entities.extend(self.groups)
        entities.extend(self.backends)
        entities.extend(self.named_values)
        entities.extend(self.policy_documents)
        entities.extend(self.policy_fragments)
        return entities

    def summary(self) -> GatewayInventorySummary:
        recognized = 0
        unrecognized = 0
        managed = 0
        for document in self.policy_documents:
            counts = summarize_facets(document.facets)
            recognized += counts[0]
            unrecognized += counts[1]
            managed += counts[2]
        for fragment in self.policy_fragments:
            counts = summarize_facets(fragment.facets)
            recognized += counts[0]
            unrecognized += counts[1]
            if fragment.managed_by_mosaic:
                managed += 1
        return GatewayInventorySummary(
            apis=len(self.apis),
            ai_apis=sum(1 for api in self.apis if api.ai_kind != AiBackendKind.NONE),
            mcp_servers=len(self.mcp_servers),
            operations=len(self.operations),
            products=len(self.products),
            subscriptions=len(self.subscriptions),
            users=len(self.users),
            groups=len(self.groups),
            backends=len(self.backends),
            named_values=len(self.named_values),
            policy_documents=len(self.policy_documents),
            policy_fragments=len(self.policy_fragments),
            recognized_facets=recognized,
            unrecognized_facets=unrecognized,
            mosaic_managed_facets=managed,
        )


def _no_items() -> list[JsonObject]:
    """A fresh, correctly typed empty fallback for a collection MOSAIC could not read."""

    return []


def _properties(item: JsonObject) -> JsonObject:
    properties = item.get("properties")
    return properties if isinstance(properties, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _flag(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _named(items: list[JsonObject]) -> list[tuple[str, JsonObject]]:
    """Pair each item with its name, dropping anything unnamed so later zips stay aligned."""

    return [(name, item) for item in items if (name := _text(item.get("name")))]


def _api_type(item: JsonObject) -> str | None:
    properties = _properties(item)
    value = _text(properties.get("type")) or _text(properties.get("apiType"))
    return value.casefold() if value else None


def _mcp_transport(value: object) -> McpTransportType:
    text = _text(value)
    if not text:
        return McpTransportType.UNKNOWN
    try:
        return McpTransportType(text.casefold())
    except ValueError:
        return McpTransportType.UNKNOWN


def _mcp_endpoints(value: object) -> list[McpEndpoint]:
    if not isinstance(value, list):
        return []
    endpoints: list[McpEndpoint] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        name = _text(entry.get("name"))
        template = _text(entry.get("uriTemplate"))
        if name and template:
            endpoints.append(McpEndpoint(name=name, uri_template=template))
    return endpoints


def _split_operation_id(value: object) -> tuple[str | None, str | None]:
    """Pull the API and operation names out of a tool's ARM operation ID.

    Tools reference their backing operation by full resource ID. Only the two trailing names are
    useful to an administrator, and keeping the whole ID would put a subscription ID in the UI.
    """

    text = _text(value)
    if not text:
        return None, None
    trimmed = text.rstrip("/")
    operation_index = trimmed.rfind("/operations/")
    if operation_index == -1:
        return None, None
    operation_name = trimmed[operation_index + len("/operations/") :] or None
    api_segment = trimmed[:operation_index]
    api_index = api_segment.rfind("/apis/")
    api_name = api_segment[api_index + len("/apis/") :] if api_index != -1 else None
    return api_name or None, operation_name


def _subscription_scope(scope: str) -> tuple[SubscriptionScopeKind, str | None]:
    trimmed = scope.rstrip("/")
    if trimmed.endswith("/apis"):
        return "allApis", None
    product_index = trimmed.rfind("/products/")
    if product_index != -1:
        return "product", trimmed[product_index + len("/products/") :]
    api_index = trimmed.rfind("/apis/")
    if api_index != -1:
        return "api", trimmed[api_index + len("/apis/") :]
    return "unknown", None


class InventoryCollector:
    def __init__(
        self,
        client: ApimClient,
        *,
        tenant_id: str,
        gateway_id: str,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self._client = client
        self._tenant_id = tenant_id
        self._gateway_id = gateway_id
        self._snapshot_id = new_id("snapshot")
        self._semaphore = asyncio.Semaphore(concurrency)
        self._snapshot = InventorySnapshot(snapshot_id=self._snapshot_id)

    def _id(self, prefix: str, *parts: str) -> str:
        return deterministic_id(prefix, self._tenant_id, self._gateway_id, *parts)

    async def _guard[T](
        self,
        label: str,
        work: Callable[[], Coroutine[Any, Any, T]],
        fallback: T,
        *,
        affects: tuple[str, ...] = (),
    ) -> T:
        # ``work`` is a factory rather than a coroutine so that nothing is constructed until it is
        # about to be awaited. Cancelling a sync at shutdown would otherwise discard coroutines that
        # were created eagerly and never started, producing RuntimeWarning noise.
        try:
            async with self._semaphore:
                return await work()
        except DomainError as error:
            self._snapshot.errors.append(f"{label}: {error.message}")
            # A failed read is indistinguishable from an empty collection once it falls back, so
            # record which entity types are untrustworthy. The caller must not treat their absence
            # from this snapshot as deletion.
            self._snapshot.incomplete_types.update(affects)
            logger.warning("inventory_section_failed", section=label, reason=error.message)
            return fallback

    async def collect(self) -> InventorySnapshot:
        collections = await asyncio.gather(
            self._guard(
                "APIs",
                partial(self._client.list_apis),
                _no_items(),
                affects=(API_TYPE, OPERATION_TYPE, POLICY_TYPE),
            ),
            self._guard(
                "products",
                partial(self._client.list_products),
                _no_items(),
                affects=(PRODUCT_TYPE, POLICY_TYPE, API_TYPE),
            ),
            self._guard(
                "subscriptions",
                partial(self._client.list_subscriptions),
                _no_items(),
                affects=(SUBSCRIPTION_TYPE,),
            ),
            self._guard(
                "users", partial(self._client.list_users), _no_items(), affects=(USER_TYPE,)
            ),
            self._guard(
                "groups",
                partial(self._client.list_groups),
                _no_items(),
                affects=(GROUP_TYPE, USER_TYPE),
            ),
            self._guard(
                "backends",
                partial(self._client.list_backends),
                _no_items(),
                affects=(BACKEND_TYPE,),
            ),
            self._guard(
                "named values",
                partial(self._client.list_named_values),
                _no_items(),
                affects=(NAMED_VALUE_TYPE,),
            ),
            self._guard(
                "policy fragments",
                partial(self._client.list_policy_fragments),
                _no_items(),
                affects=(FRAGMENT_TYPE,),
            ),
        )
        (
            api_items,
            product_items,
            subscription_items,
            user_items,
            group_items,
            backend_items,
            named_value_items,
            fragment_items,
        ) = collections

        backend_kinds = self._collect_backends(backend_items)
        self._collect_named_values(named_value_items)
        self._collect_groups(group_items)
        self._collect_subscriptions(subscription_items)

        await self._collect_service_policy()
        product_api_map = await self._collect_products(product_items)
        await self._collect_apis(api_items, product_api_map, backend_kinds)
        await self._collect_mcp_servers(product_api_map)
        await self._collect_fragments(fragment_items)
        await self._collect_users(user_items, group_items)

        self._snapshot.ai_policy_observed = self._observed_ai_policies()
        return self._snapshot

    def _observed_ai_policies(self) -> bool:
        for document in self._snapshot.policy_documents:
            if any(facet.element in AI_POLICY_ELEMENTS for facet in document.facets):
                return True
        for fragment in self._snapshot.policy_fragments:
            if any(facet.element in AI_POLICY_ELEMENTS for facet in fragment.facets):
                return True
        return False

    def _collect_backends(self, items: list[JsonObject]) -> dict[str, AiBackendKind]:
        kinds: dict[str, AiBackendKind] = {}
        for name, item in _named(items):
            properties = _properties(item)
            url = _text(properties.get("url"))
            kind = classify_url(url)
            kinds[name] = kind
            self._snapshot.backends.append(
                ObservedBackend(
                    id=self._id("obsBackend", name),
                    tenant_id=self._tenant_id,
                    gateway_id=self._gateway_id,
                    snapshot_id=self._snapshot_id,
                    name=name,
                    title=_text(properties.get("title")),
                    url=sanitize_url(url),
                    protocol=_text(properties.get("protocol")),
                    ai_kind=kind,
                )
            )
        return kinds

    def _collect_named_values(self, items: list[JsonObject]) -> None:
        for name, item in _named(items):
            properties = _properties(item)
            key_vault = properties.get("keyVault")
            secret_identifier = (
                _text(key_vault.get("secretIdentifier")) if isinstance(key_vault, dict) else None
            )
            self._snapshot.named_values.append(
                ObservedNamedValue(
                    id=self._id("obsNamedValue", name),
                    tenant_id=self._tenant_id,
                    gateway_id=self._gateway_id,
                    snapshot_id=self._snapshot_id,
                    name=name,
                    display_name=_text(properties.get("displayName")) or name,
                    secret=_flag(properties.get("secret"), False),
                    tags=_string_list(properties.get("tags")),
                    key_vault_secret_identifier=secret_identifier,
                )
            )

    def _collect_groups(self, items: list[JsonObject]) -> None:
        for name, item in _named(items):
            properties = _properties(item)
            self._snapshot.groups.append(
                ObservedApimGroup(
                    id=self._id("obsApimGroup", name),
                    tenant_id=self._tenant_id,
                    gateway_id=self._gateway_id,
                    snapshot_id=self._snapshot_id,
                    name=name,
                    display_name=_text(properties.get("displayName")) or name,
                    description=_text(properties.get("description")),
                    group_type=_text(properties.get("type")),
                    built_in=_flag(properties.get("builtIn"), False),
                )
            )

    def _collect_subscriptions(self, items: list[JsonObject]) -> None:
        for name, item in _named(items):
            properties = _properties(item)
            scope = _text(properties.get("scope")) or ""
            scope_kind, scope_name = _subscription_scope(scope)
            owner_id = _text(properties.get("ownerId"))
            self._snapshot.subscriptions.append(
                ObservedSubscription(
                    id=self._id("obsSubscription", name),
                    tenant_id=self._tenant_id,
                    gateway_id=self._gateway_id,
                    snapshot_id=self._snapshot_id,
                    name=name,
                    display_name=_text(properties.get("displayName")),
                    scope=scope,
                    scope_kind=scope_kind,
                    scope_name=scope_name,
                    state=_text(properties.get("state")),
                    owner_id=owner_id,
                    owner_label=owner_id.rsplit("/", 1)[-1] if owner_id else None,
                    created_date=_parse_timestamp(properties.get("createdDate")),
                )
            )

    async def _collect_users(
        self, user_items: list[JsonObject], group_items: list[JsonObject]
    ) -> None:
        group_names = [name for name, _ in _named(group_items)]
        member_lists = await asyncio.gather(
            *(
                self._guard(
                    f"members of group {name}",
                    partial(self._client.list_group_users, name),
                    _no_items(),
                    affects=(USER_TYPE,),
                )
                for name in group_names
            )
        )
        memberships: dict[str, list[str]] = {}
        for group_name, members in zip(group_names, member_lists, strict=True):
            for member_name, _ in _named(members):
                memberships.setdefault(member_name, []).append(group_name)

        for name, item in _named(user_items):
            properties = _properties(item)
            providers: list[str] = []
            entra_object_id: str | None = None
            identities = properties.get("identities")
            if isinstance(identities, list):
                for identity in identities:
                    if not isinstance(identity, dict):
                        continue
                    provider = _text(identity.get("provider"))
                    if not provider:
                        continue
                    providers.append(provider)
                    if provider.casefold() == "aad":
                        entra_object_id = _text(identity.get("id")) or entra_object_id
            first = _text(properties.get("firstName")) or ""
            last = _text(properties.get("lastName")) or ""
            display = " ".join(part for part in (first, last) if part)
            self._snapshot.users.append(
                ObservedApimUser(
                    id=self._id("obsApimUser", name),
                    tenant_id=self._tenant_id,
                    gateway_id=self._gateway_id,
                    snapshot_id=self._snapshot_id,
                    name=name,
                    display_name=display or None,
                    email=_text(properties.get("email")),
                    state=_text(properties.get("state")),
                    identity_providers=providers,
                    entra_object_id=entra_object_id,
                    group_names=memberships.get(name, []),
                )
            )

    async def _collect_service_policy(self) -> None:
        xml = await self._guard(
            "gateway policy",
            partial(self._client.get_service_policy),
            None,
            affects=(POLICY_TYPE,),
        )
        if xml:
            self._append_policy(PolicyScope.GLOBAL, "global", "All APIs on this gateway", xml)

    def _append_policy(
        self, scope: PolicyScope, scope_id: str, scope_label: str, xml: str
    ) -> list[str]:
        analysis = analyze_policy(xml)
        self._snapshot.policy_documents.append(
            ObservedPolicyDocument(
                id=self._id("obsPolicy", scope.value, scope_id),
                tenant_id=self._tenant_id,
                gateway_id=self._gateway_id,
                snapshot_id=self._snapshot_id,
                scope=scope,
                scope_id=scope_id,
                scope_label=scope_label,
                content_sha256=analysis.content_sha256,
                element_count=analysis.element_count,
                facets=analysis.facets,
                unrecognized_elements=sorted(set(analysis.unrecognized_elements)),
            )
        )
        return [facet.element for facet in analysis.facets]

    async def _collect_products(self, items: list[JsonObject]) -> dict[str, list[str]]:
        entries = _named(items)
        policies = await asyncio.gather(
            *(
                self._guard(
                    f"policy for product {name}",
                    partial(self._client.get_product_policy, name),
                    None,
                    affects=(POLICY_TYPE,),
                )
                for name, _ in entries
            )
        )
        api_lists = await asyncio.gather(
            *(
                self._guard(
                    f"APIs in product {name}",
                    partial(self._client.list_product_apis, name),
                    _no_items(),
                    affects=(PRODUCT_TYPE, API_TYPE),
                )
                for name, _ in entries
            )
        )

        product_api_map: dict[str, list[str]] = {}
        for (name, item), policy_xml, api_items in zip(entries, policies, api_lists, strict=True):
            properties = _properties(item)
            api_names = [api_name for api_name, _ in _named(api_items)]
            for api_name in api_names:
                product_api_map.setdefault(api_name, []).append(name)
            display_name = _text(properties.get("displayName")) or name
            self._snapshot.products.append(
                ObservedProduct(
                    id=self._id("obsProduct", name),
                    tenant_id=self._tenant_id,
                    gateway_id=self._gateway_id,
                    snapshot_id=self._snapshot_id,
                    name=name,
                    display_name=display_name,
                    description=_text(properties.get("description")),
                    state=_text(properties.get("state")),
                    subscription_required=_flag(properties.get("subscriptionRequired"), True),
                    approval_required=_flag(properties.get("approvalRequired"), False),
                    subscriptions_limit=_int_or_none(properties.get("subscriptionsLimit")),
                    api_names=api_names,
                )
            )
            if policy_xml:
                self._append_policy(
                    PolicyScope.PRODUCT, name, f"Product: {display_name}", policy_xml
                )
        return product_api_map

    async def _collect_apis(
        self,
        items: list[JsonObject],
        product_api_map: dict[str, list[str]],
        backend_kinds: dict[str, AiBackendKind],
    ) -> None:
        # MCP servers are APIs of type ``mcp`` in the ARM model. They are collected separately, so
        # exclude them here rather than listing one resource twice under two different shapes.
        entries = [
            (name, item)
            for name, item in _named(items)
            if _api_type(item) != MCP_API_TYPE
        ]
        operation_lists = await asyncio.gather(
            *(
                self._guard(
                    f"operations for API {name}",
                    partial(self._client.list_operations, name),
                    _no_items(),
                    affects=(OPERATION_TYPE,),
                )
                for name, _ in entries
            )
        )
        policies = await asyncio.gather(
            *(
                self._guard(
                    f"policy for API {name}",
                    partial(self._client.get_api_policy, name),
                    None,
                    affects=(POLICY_TYPE,),
                )
                for name, _ in entries
            )
        )

        for (name, item), operation_items, policy_xml in zip(
            entries, operation_lists, policies, strict=True
        ):
            properties = _properties(item)
            display_name = _text(properties.get("displayName")) or name
            templates = self._collect_operations(name, operation_items)

            policy_elements: list[str] = []
            if policy_xml:
                policy_elements = self._append_policy(
                    PolicyScope.API, name, f"API: {display_name}", policy_xml
                )

            service_url = _text(properties.get("serviceUrl"))
            referenced = [
                kind
                for backend_name, kind in backend_kinds.items()
                if service_url and backend_name in service_url
            ]
            ai_kind, signals = classify_api(
                service_url=service_url,
                path=_text(properties.get("path")),
                operation_templates=templates,
                policy_elements=policy_elements,
                backend_kinds=referenced,
            )
            self._snapshot.apis.append(
                ObservedApi(
                    id=self._id("obsApi", name),
                    tenant_id=self._tenant_id,
                    gateway_id=self._gateway_id,
                    snapshot_id=self._snapshot_id,
                    name=name,
                    display_name=display_name,
                    path=_text(properties.get("path")) or "",
                    protocols=_string_list(properties.get("protocols")),
                    service_url=sanitize_url(service_url),
                    api_type=_text(properties.get("type")) or _text(properties.get("apiType")),
                    api_revision=_text(properties.get("apiRevision")),
                    api_version=_text(properties.get("apiVersion")),
                    is_current=_flag(properties.get("isCurrent"), True),
                    subscription_required=_flag(properties.get("subscriptionRequired"), True),
                    ai_kind=ai_kind,
                    ai_signals=signals,
                    operation_count=len(operation_items),
                    product_names=product_api_map.get(name, []),
                )
            )

    def _collect_operations(self, api_name: str, items: list[JsonObject]) -> list[str]:
        templates: list[str] = []
        for name, item in _named(items):
            properties = _properties(item)
            template = _text(properties.get("urlTemplate")) or ""
            templates.append(template)
            self._snapshot.operations.append(
                ObservedOperation(
                    id=self._id("obsOperation", api_name, name),
                    tenant_id=self._tenant_id,
                    gateway_id=self._gateway_id,
                    snapshot_id=self._snapshot_id,
                    api_name=api_name,
                    name=name,
                    display_name=_text(properties.get("displayName")) or name,
                    method=_text(properties.get("method")) or "GET",
                    url_template=template,
                )
            )
        return templates

    async def _collect_mcp_servers(self, product_api_map: dict[str, list[str]]) -> None:
        """Collect MCP servers, treating an unsupported preview contract as an absent capability.

        Three outcomes are deliberately distinct. A service that answers is ``available``. A
        service that rejects the preview API version is ``unavailable`` and produces no error,
        because "this gateway is too old for MCP" is not a failure an operator can act on. Any
        other failure leaves support ``unknown``, records the error, and exempts the type from the
        sweep, so a transient outage never reads as "the MCP servers were deleted".
        """

        try:
            async with self._semaphore:
                items = await self._client.list_mcp_servers()
        except UpstreamUnsupportedError:
            self._snapshot.mcp_support = CapabilitySupport.UNAVAILABLE
            logger.info("inventory_mcp_unsupported", gateway_id=self._gateway_id)
            return
        except DomainError as error:
            self._snapshot.errors.append(f"MCP servers: {error.message}")
            self._snapshot.incomplete_types.add(MCP_SERVER_TYPE)
            logger.warning("inventory_section_failed", section="MCP servers", reason=error.message)
            return

        self._snapshot.mcp_support = CapabilitySupport.AVAILABLE
        entries = _named(items)
        tool_lists = await asyncio.gather(
            *(
                self._guard(
                    f"tools for MCP server {name}",
                    partial(self._client.list_mcp_tools, name),
                    _no_items(),
                    affects=(MCP_SERVER_TYPE,),
                )
                for name, _ in entries
            )
        )

        for (name, item), tool_items in zip(entries, tool_lists, strict=True):
            properties = _properties(item)
            mcp_properties = properties.get("mcpProperties")
            mcp_properties = mcp_properties if isinstance(mcp_properties, dict) else {}
            service_url = _text(properties.get("serviceUrl"))
            tools = self._read_mcp_tools(tool_items)
            self._snapshot.mcp_servers.append(
                ObservedMcpServer(
                    id=self._id("obsMcpServer", name),
                    tenant_id=self._tenant_id,
                    gateway_id=self._gateway_id,
                    snapshot_id=self._snapshot_id,
                    name=name,
                    display_name=_text(properties.get("displayName")) or name,
                    path=_text(properties.get("path")) or "",
                    protocols=_string_list(properties.get("protocols")),
                    service_url=sanitize_url(service_url),
                    # A passthrough server declares the external backend it forwards to; a
                    # REST-backed one has no service URL of its own and reaches its tools through
                    # operations on other APIs.
                    kind=(
                        McpServerKind.PASSTHROUGH
                        if mcp_properties
                        else McpServerKind.REST_API_BACKED
                    ),
                    transport_type=_mcp_transport(mcp_properties.get("transportType")),
                    endpoints=_mcp_endpoints(mcp_properties.get("endpoints")),
                    tools=tools,
                    tool_count=len(tools),
                    subscription_required=_flag(properties.get("subscriptionRequired"), True),
                    product_names=product_api_map.get(name, []),
                )
            )

    def _read_mcp_tools(self, items: list[JsonObject]) -> list[McpTool]:
        tools: list[McpTool] = []
        for name, item in _named(items):
            properties = _properties(item)
            api_name, operation_name = _split_operation_id(properties.get("operationId"))
            tools.append(
                McpTool(
                    name=name,
                    display_name=_text(properties.get("displayName")) or name,
                    description=_text(properties.get("description")),
                    backing_api_name=api_name,
                    backing_operation_name=operation_name,
                )
            )
        return tools

    async def _collect_fragments(self, items: list[JsonObject]) -> None:
        entries = _named(items)
        contents = await asyncio.gather(
            *(
                self._guard(
                    f"policy fragment {name}",
                    partial(self._client.get_policy_fragment, name),
                    None,
                    affects=(FRAGMENT_TYPE,),
                )
                for name, _ in entries
            )
        )
        for (name, item), xml in zip(entries, contents, strict=True):
            properties = _properties(item)
            analysis = analyze_policy(xml or "")
            self._snapshot.policy_fragments.append(
                ObservedPolicyFragment(
                    id=self._id("obsFragment", name),
                    tenant_id=self._tenant_id,
                    gateway_id=self._gateway_id,
                    snapshot_id=self._snapshot_id,
                    name=name,
                    description=_text(properties.get("description")),
                    content_sha256=analysis.content_sha256,
                    managed_by_mosaic=name.casefold().startswith(MOSAIC_FRAGMENT_PREFIX),
                    facets=analysis.facets,
                    unrecognized_elements=sorted(set(analysis.unrecognized_elements)),
                )
            )
