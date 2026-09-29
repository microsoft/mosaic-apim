"""Fictional Azure surfaces behind the screenshot demo estate.

Everything here extends the API's own test doubles in ``apps/api/tests`` with a richer, entirely
fictional Contoso inventory. The real MOSAIC services talk to these fakes over ``httpx``'s mock
transport exactly as they would talk to Azure Resource Manager, so the screenshots show what the
product really renders without touching a tenant, a subscription, or a real person's data.
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

TESTS_DIR = Path(__file__).resolve().parents[2] / "apps" / "api" / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from aoai_double import (  # noqa: E402
    AI_RESOURCE_ID,
    AI_SUBSCRIPTION_ID,
    AZURE_OPENAI_USER_ROLE_ID,
    COGNITIVE_SERVICES_USER_ROLE_ID,
    FakeCognitiveServices,
    role_assignment,
)
from apim_double import (  # noqa: E402
    APIM_PRINCIPAL_ID,
    CONTRIBUTOR_PERMISSIONS,
    RESOURCE_ID,
    SUBSCRIPTION_ID,
    FakeApim,
    FakeCredential,
)
from mcp_double import FakeMcpServer  # noqa: E402

__all__ = [
    "AI_RESOURCE_ID",
    "FOUNDRY_RESOURCE_ID",
    "GATEWAY_RESOURCE_ID",
    "PARTNER_GATEWAY_RESOURCE_ID",
    "DemoApim",
    "DemoCognitiveAccount",
    "DemoMcpServer",
    "FakeCredential",
    "build_cognitive_accounts",
    "build_mcp_servers",
    "cognitive_handler",
    "gateway_handler",
    "mcp_handler",
]

GATEWAY_RESOURCE_ID = RESOURCE_ID
# Registered but unreadable, so the console shows how MOSAIC reports a gateway it cannot see yet.
PARTNER_GATEWAY_RESOURCE_ID = (
    f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-contoso-partners"
    "/providers/Microsoft.ApiManagement/service/apim-contoso-partners"
)
FOUNDRY_RESOURCE_ID = (
    f"/subscriptions/{AI_SUBSCRIPTION_ID}/resourceGroups/rg-contoso-ai"
    "/providers/Microsoft.CognitiveServices/accounts/contoso-foundry"
)

_POLICY_TAIL = """
  <backend>
    <base />
  </backend>
  <outbound>
    <base />
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>
"""

GLOBAL_POLICY = """
<policies>
  <inbound>
    <cors allow-credentials="false">
      <allowed-origins>
        <origin>https://portal.contoso.com</origin>
      </allowed-origins>
      <allowed-methods>
        <method>GET</method>
        <method>POST</method>
      </allowed-methods>
    </cors>
    <rate-limit calls="1200" renewal-period="60" />
  </inbound>
  <backend>
    <forward-request />
  </backend>
  <outbound />
  <on-error />
</policies>
"""

AZURE_OPENAI_POLICY = """
<policies>
  <inbound>
    <base />
    <validate-azure-ad-token tenant-id="{{tenant-id}}">
      <client-application-ids>
        <application-id>{{support-copilot-app-id}}</application-id>
      </client-application-ids>
    </validate-azure-ad-token>
    <authentication-managed-identity resource="https://cognitiveservices.azure.com" />
    <set-backend-service backend-id="aoai-eastus2" />
    <llm-token-limit counter-key="@(context.Subscription.Id)" tokens-per-minute="50000"
                     estimate-prompt-tokens="true"
                     remaining-tokens-header-name="x-ratelimit-remaining-tokens" />
    <llm-emit-token-metric namespace="contoso-ai">
      <dimension name="Subscription ID" />
      <dimension name="API ID" />
    </llm-emit-token-metric>
    <llm-semantic-cache-lookup score-threshold="0.05"
                               embeddings-backend-id="aoai-eastus2"
                               embeddings-backend-auth="system-assigned" />
  </inbound>""" + _POLICY_TAIL.replace(
    "<outbound>\n    <base />",
    '<outbound>\n    <base />\n    <llm-semantic-cache-store duration="300" />',
)

FOUNDRY_POLICY = (
    """
<policies>
  <inbound>
    <base />
    <authentication-managed-identity resource="https://cognitiveservices.azure.com" />
    <set-backend-service backend-id="foundry-eastus2" />
    <llm-content-safety backend-id="content-safety" shield-prompt="true" />
    <llm-token-limit counter-key="@(context.Subscription.Id)" tokens-per-minute="20000"
                     estimate-prompt-tokens="true" />
  </inbound>"""
    + _POLICY_TAIL
)

ORDERS_POLICY = (
    """
<policies>
  <inbound>
    <base />
    <rate-limit-by-key calls="300" renewal-period="60"
                       counter-key="@(context.Subscription.Id)" />
    <quota-by-key calls="100000" renewal-period="2592000"
                  counter-key="@(context.Subscription.Id)" />
  </inbound>"""
    + _POLICY_TAIL
)

HR_POLICY = (
    """
<policies>
  <inbound>
    <base />
    <ip-filter action="allow">
      <address-range from="10.20.0.0" to="10.20.255.255" />
    </ip-filter>
    <set-header name="X-Contoso-Caller" exists-action="override">
      <value>@(context.Subscription.Name)</value>
    </set-header>
  </inbound>"""
    + _POLICY_TAIL
)

PREMIUM_PRODUCT_POLICY = (
    """
<policies>
  <inbound>
    <base />
    <quota-by-key calls="500000" renewal-period="2592000"
                  counter-key="@(context.Subscription.Id)" />
  </inbound>"""
    + _POLICY_TAIL
)

CONTENT_SAFETY_FRAGMENT = """
<fragment>
  <llm-content-safety backend-id="content-safety" shield-prompt="true">
    <categories output-type="EightSeverityLevels">
      <category name="Hate" threshold="4" />
      <category name="Violence" threshold="4" />
    </categories>
  </llm-content-safety>
</fragment>
"""


def _api(
    name: str,
    display_name: str,
    path: str,
    service_url: str | None,
    *,
    subscription_required: bool = True,
    version: str | None = None,
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "displayName": display_name,
        "path": path,
        "protocols": ["https"],
        "apiRevision": "1",
        "isCurrent": True,
        "subscriptionRequired": subscription_required,
    }
    if service_url:
        properties["serviceUrl"] = service_url
    if version:
        properties["apiVersion"] = version
    return {"name": name, "properties": properties}


def _operation(name: str, display_name: str, method: str, template: str) -> dict[str, Any]:
    return {
        "name": name,
        "properties": {"displayName": display_name, "method": method, "urlTemplate": template},
    }


APIS: list[dict[str, Any]] = [
    _api(
        "azure-openai",
        "Azure OpenAI",
        "openai",
        "https://contoso-aoai.openai.azure.com/openai",
        version="2024-10-21",
    ),
    _api(
        "foundry-inference",
        "Foundry model inference",
        "models",
        "https://contoso-foundry.services.ai.azure.com/models",
    ),
    _api("orders-api", "Orders API", "orders", "https://orders.contoso.com/api"),
    _api("hr-directory", "HR directory", "hr", "https://hr.contoso.com/api"),
    _api(
        "status-page",
        "Service status",
        "status",
        "https://status.contoso.com",
        subscription_required=False,
    ),
]

OPERATIONS: dict[str, list[dict[str, Any]]] = {
    "azure-openai": [
        _operation(
            "chat-completions",
            "Creates a chat completion",
            "POST",
            "/deployments/{deployment-id}/chat/completions",
        ),
        _operation(
            "embeddings", "Creates embeddings", "POST", "/deployments/{deployment-id}/embeddings"
        ),
        _operation("responses", "Creates a model response", "POST", "/responses"),
        _operation(
            "images",
            "Generates images",
            "POST",
            "/deployments/{deployment-id}/images/generations",
        ),
    ],
    "foundry-inference": [
        _operation("chat-completions", "Creates a chat completion", "POST", "/chat/completions"),
        _operation("embeddings", "Creates embeddings", "POST", "/embeddings"),
        _operation("model-info", "Returns model information", "GET", "/info"),
    ],
    "orders-api": [
        _operation("list-orders", "List orders for a customer", "GET", "/customers/{id}/orders"),
        _operation("get-order", "Get an order", "GET", "/orders/{orderId}"),
        _operation("create-return", "Start a return", "POST", "/orders/{orderId}/returns"),
        _operation("track-shipment", "Track a shipment", "GET", "/shipments/{shipmentId}"),
    ],
    "hr-directory": [
        _operation("get-employee", "Get an employee", "GET", "/employees/{id}"),
        _operation("search-employees", "Search the directory", "GET", "/employees"),
        _operation("list-org-units", "List organizational units", "GET", "/org-units"),
    ],
    "status-page": [_operation("status", "Current service status", "GET", "/")],
}

API_POLICIES: dict[str, str] = {
    "azure-openai": AZURE_OPENAI_POLICY,
    "foundry-inference": FOUNDRY_POLICY,
    "orders-api": ORDERS_POLICY,
    "hr-directory": HR_POLICY,
}

MCP_SERVERS: list[dict[str, Any]] = [
    {
        "name": "orders-mcp",
        "properties": {
            "type": "mcp",
            "displayName": "Orders MCP",
            "description": "Order lookup and returns, exposed from the Orders API.",
            "path": "orders-mcp",
            "protocols": ["https"],
            "subscriptionRequired": True,
        },
    },
    {
        "name": "docs-search-mcp",
        "properties": {
            "type": "mcp",
            "displayName": "Docs search MCP",
            "path": "docs-mcp",
            "protocols": ["https"],
            "serviceUrl": "https://mcp.contoso.com/docs",
            "subscriptionRequired": True,
            "mcpProperties": {
                "transportType": "streamable",
                "endpoints": [{"name": "mcp", "uriTemplate": "/mcp"}],
            },
        },
    },
    {
        "name": "service-desk-mcp",
        "properties": {
            "type": "mcp",
            "displayName": "IT service desk MCP",
            "path": "service-desk-mcp",
            "protocols": ["https"],
            "serviceUrl": "https://servicedesk.contoso.com/mcp",
            "subscriptionRequired": True,
            "mcpProperties": {
                "transportType": "sse",
                "endpoints": [
                    {"name": "sse", "uriTemplate": "/sse"},
                    {"name": "message", "uriTemplate": "/messages"},
                ],
            },
        },
    },
]


def _gateway_tool(name: str, description: str, operation: str) -> dict[str, Any]:
    return {
        "name": name,
        "properties": {
            "displayName": name,
            "description": description,
            "operationId": f"{RESOURCE_ID}/apis/orders-api/operations/{operation}",
        },
    }


MCP_TOOLS: dict[str, list[dict[str, Any]]] = {
    "orders-mcp": [
        _gateway_tool("listOrders", "List recent orders for a customer", "list-orders"),
        _gateway_tool("getOrder", "Get the details of one order", "get-order"),
        _gateway_tool("startReturn", "Start a return for an order", "create-return"),
        _gateway_tool("trackShipment", "Track a shipment", "track-shipment"),
    ],
}

PRODUCTS: list[dict[str, Any]] = [
    {
        "name": "ai-starter",
        "properties": {
            "displayName": "AI Starter",
            "description": "Shared models for prototypes and internal tools.",
            "state": "published",
            "subscriptionRequired": True,
            "approvalRequired": False,
            "subscriptionsLimit": 1,
        },
    },
    {
        "name": "ai-premium",
        "properties": {
            "displayName": "AI Premium",
            "description": "Higher limits for production workloads.",
            "state": "published",
            "subscriptionRequired": True,
            "approvalRequired": True,
            "subscriptionsLimit": 5,
        },
    },
    {
        "name": "partner-apis",
        "properties": {
            "displayName": "Partner APIs",
            "description": "Order and shipment APIs for fulfilment partners.",
            "state": "notPublished",
            "subscriptionRequired": True,
            "approvalRequired": True,
        },
    },
]

PRODUCT_APIS: dict[str, list[str]] = {
    "ai-starter": ["azure-openai"],
    "ai-premium": ["azure-openai", "foundry-inference"],
    "partner-apis": ["orders-api", "orders-mcp"],
}

PRODUCT_POLICIES: dict[str, str] = {"ai-premium": PREMIUM_PRODUCT_POLICY}


def _subscription(
    name: str, display_name: str, scope: str, owner: str | None, created: str
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "displayName": display_name,
        "scope": f"{RESOURCE_ID}/{scope}",
        "state": "active",
        "createdDate": created,
    }
    if owner:
        properties["ownerId"] = f"{RESOURCE_ID}/users/{owner}"
    return {"name": name, "properties": properties}


SUBSCRIPTIONS: list[dict[str, Any]] = [
    _subscription(
        "support-copilot-prod",
        "Support Copilot (production)",
        "products/ai-premium",
        "adele-vance",
        "2026-03-02T09:15:00Z",
    ),
    _subscription(
        "claims-triage",
        "Claims triage",
        "products/ai-starter",
        "alex-wilber",
        "2026-04-18T14:02:00Z",
    ),
    _subscription(
        "fulfilment-partner",
        "Fulfilment partner",
        "products/partner-apis",
        "diego-siciliani",
        "2026-05-06T11:40:00Z",
    ),
    _subscription("hr-portal", "HR portal", "apis/hr-directory", None, "2026-01-27T08:05:00Z"),
]

_PEOPLE = [
    ("adele-vance", "Adele", "Vance", "11a0f3c2-4b6d-4e8f-9a1b-2c3d4e5f6a7b"),
    ("alex-wilber", "Alex", "Wilber", "22b1e4d3-5c7e-4f90-8b2c-3d4e5f6a7b8c"),
    ("diego-siciliani", "Diego", "Siciliani", "33c2f5e4-6d8f-4a01-9c3d-4e5f6a7b8c9d"),
    ("megan-bowen", "Megan", "Bowen", "44d3a6f5-7e90-4b12-8d4e-5f6a7b8c9d0e"),
]

USERS: list[dict[str, Any]] = [
    {
        "name": name,
        "properties": {
            "firstName": first,
            "lastName": last,
            "email": f"{first.lower()}.{last.lower()}@contoso.com",
            "state": "active",
            "identities": [{"provider": "Aad", "id": object_id}],
        },
    }
    for name, first, last, object_id in _PEOPLE
]

GROUPS: list[dict[str, Any]] = [
    {
        "name": "administrators",
        "properties": {"displayName": "Administrators", "type": "system", "builtIn": True},
    },
    {
        "name": "developers",
        "properties": {"displayName": "Developers", "type": "system", "builtIn": True},
    },
    {"name": "guests", "properties": {"displayName": "Guests", "type": "system", "builtIn": True}},
    {
        "name": "ai-platform",
        "properties": {
            "displayName": "AI platform team",
            "description": "Owns the shared model gateway.",
            "type": "custom",
            "builtIn": False,
        },
    },
]

GROUP_MEMBERS: dict[str, list[str]] = {
    "administrators": ["adele-vance"],
    "developers": ["alex-wilber", "diego-siciliani", "megan-bowen"],
    "ai-platform": ["adele-vance", "alex-wilber"],
}

BACKENDS: list[dict[str, Any]] = [
    {
        "name": "aoai-eastus2",
        "properties": {
            "title": "Azure OpenAI (East US 2)",
            "url": "https://contoso-aoai.openai.azure.com/openai",
            "protocol": "http",
        },
    },
    {
        "name": "foundry-eastus2",
        "properties": {
            "title": "Foundry models (East US 2)",
            "url": "https://contoso-foundry.services.ai.azure.com/models",
            "protocol": "http",
        },
    },
    {
        "name": "content-safety",
        "properties": {
            "title": "Content safety",
            "url": "https://contoso-safety.cognitiveservices.azure.com",
            "protocol": "http",
        },
    },
    {
        "name": "orders-backend",
        "properties": {
            "title": "Orders service",
            "url": "https://orders.contoso.com/api",
            "protocol": "http",
        },
    },
]

NAMED_VALUES: list[dict[str, Any]] = [
    {
        "name": "tenant-id",
        "properties": {"displayName": "tenant-id", "secret": False, "tags": ["entra"]},
    },
    {
        "name": "support-copilot-app-id",
        "properties": {"displayName": "support-copilot-app-id", "secret": False, "tags": ["entra"]},
    },
    {
        "name": "orders-api-key",
        "properties": {
            "displayName": "orders-api-key",
            "secret": True,
            "tags": ["orders"],
            "keyVault": {
                "secretIdentifier": "https://kv-contoso-ai.vault.azure.net/secrets/orders-api-key"
            },
        },
    },
]

FRAGMENTS: dict[str, tuple[str, str]] = {
    "ai-content-safety": ("Shared prompt shield and content categories", CONTENT_SAFETY_FRAGMENT),
}


class DemoApim(FakeApim):
    """The Contoso AI gateway: several AI and ordinary APIs, MCP servers, and consumers.

    Reads fall back to what MOSAIC itself wrote, so a sync after publishing observes the
    publication's API, product, backend, and subscriptions the way a real gateway would.
    """

    def __init__(self) -> None:
        super().__init__(permissions=CONTRIBUTOR_PERMISSIONS, sku_name="StandardV2")
        self.public_ip_addresses = ["203.0.113.24"]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/listSecrets"):
            # A placeholder shaped like a key, so the reveal flow renders. Captures blur it.
            return httpx.Response(
                200,
                json={
                    "primaryKey": "demo0primary0key0not0a0real0secret0",
                    "secondaryKey": "demo0secondary0key0not0a0real0secret",
                },
            )
        return self.handler(request)

    def _written_children(self, prefix: str) -> list[dict[str, Any]]:
        depth = prefix.count("/") + 1
        return [
            {"name": key.rsplit("/", 1)[-1], **value}
            for key, value in sorted(self.written.items())
            if key.startswith(f"{prefix}/") and key.count("/") == depth
        ]

    def _merged(self, prefix: str, static: list[dict[str, Any]]) -> httpx.Response:
        written = self._written_children(prefix)
        names = {item["name"] for item in written}
        return self._collection([item for item in static if item["name"] not in names] + written)

    def _route(
        self,
        suffix: str,
        page: str | None,
        api_filter: str | None,
    ) -> httpx.Response:
        if suffix == "policies/policy":
            return self._policy(GLOBAL_POLICY)
        if suffix == "apis":
            if api_filter and "mcp" in api_filter:
                return self._collection(MCP_SERVERS)
            return self._merged("apis", [*APIS, *MCP_SERVERS])
        parts = suffix.split("/")
        collections: dict[str, list[dict[str, Any]]] = {
            "products": PRODUCTS,
            "subscriptions": SUBSCRIPTIONS,
            "users": USERS,
            "groups": GROUPS,
            "backends": BACKENDS,
            "namedValues": NAMED_VALUES,
            "policyFragments": [
                {"name": name, "properties": {"description": description}}
                for name, (description, _) in FRAGMENTS.items()
            ],
        }
        if suffix in collections:
            return self._merged(suffix, collections[suffix])
        if parts[0] == "apis" and len(parts) >= 2:
            return self._api_route(parts[1], parts[2:])
        if parts[0] == "products" and len(parts) >= 3:
            product = parts[1]
            if parts[2:] == ["apis"]:
                return self._collection(
                    [{"name": api, "properties": {}} for api in PRODUCT_APIS.get(product, [])]
                )
            if parts[2:] == ["policies", "policy"]:
                policy = PRODUCT_POLICIES.get(product)
                return self._policy(policy) if policy else self._missing(suffix)
        if parts[0] == "groups" and parts[2:] == ["users"]:
            return self._collection(
                [{"name": user, "properties": {}} for user in GROUP_MEMBERS.get(parts[1], [])]
            )
        if parts[0] == "policyFragments" and len(parts) == 2 and parts[1] in FRAGMENTS:
            return self._policy(FRAGMENTS[parts[1]][1])
        if parts[0] in {"apis", "products", "groups", "policyFragments", "users"}:
            return self._missing(suffix)
        return super()._route(suffix, page, api_filter)

    def _api_route(self, name: str, rest: list[str]) -> httpx.Response:
        definition = next((item for item in [*APIS, *MCP_SERVERS] if item["name"] == name), None)
        if not rest:
            return httpx.Response(200, json=definition) if definition else self._missing(name)
        if rest == ["operations"]:
            static = OPERATIONS.get(name, [])
            return self._merged(f"apis/{name}/operations", static)
        if rest == ["policies", "policy"]:
            policy = API_POLICIES.get(name)
            return self._policy(policy) if policy else self._missing(name)
        if rest == ["tools"]:
            return self._collection(MCP_TOOLS.get(name, []))
        if rest == ["products"]:
            return self._collection(
                [
                    {"name": product, "properties": {}}
                    for product, apis in PRODUCT_APIS.items()
                    if name in apis
                ]
            )
        return self._missing(name)

    @staticmethod
    def _missing(what: str) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "ResourceNotFound", "message": what}})


def gateway_handler(apim: DemoApim) -> Any:
    """Route the Contoso gateway to the fake, and refuse the partner gateway outright."""

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.casefold().startswith(PARTNER_GATEWAY_RESOURCE_ID.casefold()):
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": "AuthorizationFailed",
                        "message": (
                            "The client does not have authorization to perform action "
                            "'Microsoft.ApiManagement/service/read' over this scope."
                        ),
                    }
                },
            )
        return await apim.handle(request)

    return handle


def _deployment(
    name: str,
    model: str,
    version: str,
    capabilities: dict[str, str],
    *,
    model_format: str = "OpenAI",
    publisher: str = "OpenAI",
    sku: str = "GlobalStandard",
    capacity: int = 100,
) -> dict[str, Any]:
    return {
        "name": name,
        "type": "Microsoft.CognitiveServices/accounts/deployments",
        "sku": {"name": sku, "capacity": capacity},
        "properties": {
            "model": {
                "format": model_format,
                "name": model,
                "version": version,
                "publisher": publisher,
            },
            "provisioningState": "Succeeded",
            "raiPolicyName": "Microsoft.DefaultV2",
            "capabilities": capabilities,
        },
    }


def _available(
    name: str,
    version: str,
    capabilities: dict[str, str],
    *,
    lifecycle: str = "GenerallyAvailable",
    model_format: str = "OpenAI",
    kind: str = "OpenAI",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "skuName": "GlobalStandard",
        "model": {
            "name": name,
            "format": model_format,
            "version": version,
            "lifecycleStatus": lifecycle,
            "capabilities": capabilities,
        },
    }


CHAT = {"chatCompletion": "true"}
EMBEDDINGS = {"embeddings": "true"}

AOAI_DEPLOYMENTS = [
    _deployment("gpt-4o", "gpt-4o", "2024-11-20", CHAT, capacity=450),
    _deployment("gpt-4o-mini", "gpt-4o-mini", "2024-07-18", CHAT, capacity=900),
    _deployment("o3-mini", "o3-mini", "2025-01-31", CHAT, capacity=150),
    _deployment(
        "text-embedding-3-large",
        "text-embedding-3-large",
        "1",
        EMBEDDINGS,
        sku="Standard",
        capacity=120,
    ),
    _deployment(
        "gpt-4o-realtime",
        "gpt-4o-realtime-preview",
        "2024-12-17",
        {"realtime": "true"},
        capacity=6,
    ),
]

AOAI_MODELS = [
    _available("gpt-4.1", "2025-04-14", CHAT),
    _available("gpt-4o", "2024-11-20", CHAT),
    _available("gpt-4o-mini", "2024-07-18", CHAT),
    _available("o3-mini", "2025-01-31", CHAT),
    _available("o4-mini", "2025-04-16", CHAT),
    _available("text-embedding-3-large", "1", EMBEDDINGS),
    _available("text-embedding-3-small", "1", EMBEDDINGS),
    _available("gpt-35-turbo", "0125", CHAT, lifecycle="Deprecated"),
]

FOUNDRY_DEPLOYMENTS = [
    _deployment(
        "Phi-4", "Phi-4", "7", CHAT, model_format="Microsoft", publisher="Microsoft", capacity=1
    ),
    _deployment(
        "DeepSeek-R1",
        "DeepSeek-R1",
        "1",
        CHAT,
        model_format="DeepSeek",
        publisher="DeepSeek",
        capacity=1,
    ),
    _deployment(
        "Mistral-Large-2411",
        "Mistral-Large-2411",
        "2",
        CHAT,
        model_format="Mistral AI",
        publisher="Mistral AI",
        capacity=1,
    ),
    _deployment(
        "Cohere-embed-v3-multilingual",
        "Cohere-embed-v3-multilingual",
        "1",
        EMBEDDINGS,
        model_format="Cohere",
        publisher="Cohere",
        capacity=1,
    ),
]

FOUNDRY_MODELS = [
    _available("Phi-4", "7", CHAT, model_format="Microsoft", kind="AIServices"),
    _available("DeepSeek-R1", "1", CHAT, model_format="DeepSeek", kind="AIServices"),
    _available("Mistral-Large-2411", "2", CHAT, model_format="Mistral AI", kind="AIServices"),
    _available("Llama-3.3-70B-Instruct", "5", CHAT, model_format="Meta", kind="AIServices"),
    _available(
        "Cohere-embed-v3-multilingual", "1", EMBEDDINGS, model_format="Cohere", kind="AIServices"
    ),
]


class DemoCognitiveAccount(FakeCognitiveServices):
    """One fictional Azure AI account, served under its own resource ID."""

    def __init__(
        self,
        *,
        resource_id: str,
        kind: str,
        endpoint: str,
        deployments: list[dict[str, Any]],
        models: list[dict[str, Any]],
        runtime_role_id: str,
    ) -> None:
        super().__init__(kind=kind)
        self.resource_id = resource_id
        self.account_name = resource_id.rsplit("/", 1)[-1]
        self.endpoint = endpoint
        self.deployments = deployments
        self.models = models
        # The gateway's managed identity can call the account, so runtime access reads "ready".
        self.role_assignments = [role_assignment(runtime_role_id, resource_id, APIM_PRINCIPAL_ID)]
        self.accounts_by_subscription = {
            AI_SUBSCRIPTION_ID: [
                {
                    "id": AI_RESOURCE_ID,
                    "name": "contoso-aoai",
                    "kind": "OpenAI",
                    "location": "eastus2",
                    "properties": {"endpoint": "https://contoso-aoai.openai.azure.com/"},
                },
                {
                    "id": FOUNDRY_RESOURCE_ID,
                    "name": "contoso-foundry",
                    "kind": "AIServices",
                    "location": "eastus2",
                    "properties": {
                        "endpoint": "https://contoso-foundry.cognitiveservices.azure.com/"
                    },
                },
            ]
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.casefold().startswith(self.resource_id.casefold()):
            # The base fake serves one fixed resource ID; present every account under it.
            request = httpx.Request(
                request.method,
                request.url.copy_with(path=AI_RESOURCE_ID + path[len(self.resource_id) :]),
                headers=request.headers,
                content=request.content,
            )
        return super().handler(request)

    def _account(self) -> dict[str, Any]:
        account = super()._account()
        account["id"] = self.resource_id
        account["name"] = self.account_name
        account["properties"]["endpoint"] = self.endpoint
        return account


def build_cognitive_accounts() -> list[DemoCognitiveAccount]:
    return [
        DemoCognitiveAccount(
            resource_id=FOUNDRY_RESOURCE_ID,
            kind="AIServices",
            endpoint="https://contoso-foundry.cognitiveservices.azure.com/",
            deployments=FOUNDRY_DEPLOYMENTS,
            models=FOUNDRY_MODELS,
            runtime_role_id=COGNITIVE_SERVICES_USER_ROLE_ID,
        ),
        DemoCognitiveAccount(
            resource_id=AI_RESOURCE_ID,
            kind="OpenAI",
            endpoint="https://contoso-aoai.openai.azure.com/",
            deployments=AOAI_DEPLOYMENTS,
            models=AOAI_MODELS,
            runtime_role_id=AZURE_OPENAI_USER_ROLE_ID,
        ),
    ]


def cognitive_handler(accounts: list[DemoCognitiveAccount]) -> Any:
    fallback = accounts[-1]

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path.casefold()
        for account in accounts:
            if path.startswith(account.resource_id.casefold()):
                return account.handler(request)
        return fallback.handler(request)

    return handle


@dataclass
class DemoMcpServer(FakeMcpServer):
    """An MCP server that introduces itself by its own name rather than the fixture's."""

    server_name: str = "contoso-mcp"
    server_title: str = "Contoso MCP"
    server_version: str = "1.0.0"

    def handler(self, request: httpx.Request) -> httpx.Response:
        response = super().handler(request)
        if not response.headers.get("content-type", "").startswith("application/json"):
            return response
        payload = json.loads(response.content)
        result = payload.get("result")
        if not isinstance(result, dict) or "serverInfo" not in result:
            return response
        result["serverInfo"] = {
            "name": self.server_name,
            "title": self.server_title,
            "version": self.server_version,
        }
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.casefold() != "content-length"
        }
        return httpx.Response(
            response.status_code, headers=headers, content=json.dumps(payload).encode("utf-8")
        )


def _tool(
    name: str,
    title: str,
    description: str,
    *,
    read_only: bool | None = None,
    destructive: bool | None = None,
) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
    }
    annotations: dict[str, Any] = {}
    if read_only is not None:
        annotations["readOnlyHint"] = read_only
        annotations["openWorldHint"] = False
    if destructive is not None:
        annotations["destructiveHint"] = destructive
    if annotations:
        tool["annotations"] = annotations
    return tool


def build_mcp_servers() -> dict[str, DemoMcpServer]:
    """MCP servers keyed by host, each with its own tools and identity."""

    return {
        "mcp.contoso.com": DemoMcpServer(
            server_name="contoso-docs",
            server_title="Contoso Docs Search",
            server_version="2.4.0",
            instructions="Search product and policy documentation before answering.",
            tool_pages=[
                [
                    _tool(
                        "search_docs",
                        "Search documentation",
                        "Full-text and semantic search across product and policy docs.",
                        read_only=True,
                    ),
                    _tool(
                        "get_document",
                        "Get a document",
                        "Fetch one document by ID.",
                        read_only=True,
                    ),
                    _tool(
                        "list_collections",
                        "List collections",
                        "List searchable collections.",
                        read_only=True,
                    ),
                ]
            ],
        ),
        "servicedesk.contoso.com": DemoMcpServer(
            server_name="contoso-servicedesk",
            server_title="IT Service Desk",
            server_version="1.7.2",
            instructions=None,
            tool_pages=[
                [
                    _tool(
                        "create_ticket",
                        "Create a ticket",
                        "Open an IT support ticket.",
                        read_only=False,
                        destructive=False,
                    ),
                    _tool(
                        "get_ticket_status",
                        "Get ticket status",
                        "Look up a ticket's status.",
                        read_only=True,
                    ),
                    _tool(
                        "search_knowledge_base",
                        "Search knowledge base",
                        "Search IT knowledge base articles.",
                        read_only=True,
                    ),
                    _tool(
                        "reset_mfa",
                        "Reset MFA",
                        "Reset a user's multifactor registration.",
                    ),
                ]
            ],
        ),
        "crm.contoso.com": DemoMcpServer(
            server_name="contoso-crm",
            server_title="Sales CRM",
            server_version="0.9.1",
            instructions=None,
            tool_pages=[
                [
                    _tool(
                        "find_account",
                        "Find an account",
                        "Find a customer account by name.",
                        read_only=True,
                    ),
                    _tool(
                        "list_opportunities",
                        "List opportunities",
                        "List open opportunities for an account.",
                        read_only=True,
                    ),
                    _tool(
                        "update_opportunity",
                        "Update an opportunity",
                        "Change an opportunity's stage or amount.",
                        read_only=False,
                        destructive=True,
                    ),
                ]
            ],
        ),
    }


def mcp_handler(servers: dict[str, DemoMcpServer]) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        server = servers.get(request.url.host)
        if server is None:
            return httpx.Response(404)
        return server.handler(request)

    return handle
