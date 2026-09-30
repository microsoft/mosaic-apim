"""API keys a gateway sends to a backend, read from Key Vault through a named value (ADR 0018).

MOSAIC never gives API Management a key. It creates a Key Vault-backed secret named value that
holds only the secret's versionless identifier, and the gateway fetches the value with its own
system-assigned managed identity, refreshing it within four hours of a rotation. The publication's
policy fragment then removes every credential a caller sent and sets the backend's key header from
that named value, after MOSAIC's authorization and limits have run. A caller can neither supply
nor override the key, and nothing outside MOSAIC's policy attaches it: a customer API that routes to
the same backend gets no key.

Model publications use this today. It is written to be reused by any publication whose backend
takes a key, such as an MCP server: own one named value, render it with :func:`set_backend_key`
after :func:`strip_caller_credentials`, and delete it after the policy that names it.
"""

import re
import xml.etree.ElementTree as ET
from collections.abc import Collection

from mosaic_api.domain import ApiShape, PolicyFacet, PolicyFacetKind
from mosaic_api.errors import ValidationError

NAMED_VALUE_SUFFIX = "-key"

# The header each curated shape authenticates with. Azure OpenAI and the Foundry Models API take
# ``api-key``. Microsoft documents ``x-api-key`` for Claude in Foundry, as Anthropic's API does.
BACKEND_KEY_HEADERS: dict[str, str] = {
    ApiShape.AZURE_OPENAI: "api-key",
    ApiShape.FOUNDRY_MODELS: "api-key",
    ApiShape.ANTHROPIC_MESSAGES: "x-api-key",
}

# Every credential a caller could present that an Azure AI endpoint might honour: its own key
# headers, a bearer token, and the Cognitive Services subscription key, which shares its header
# and query parameter names with API Management's own subscription key.
CALLER_CREDENTIAL_HEADERS: tuple[str, ...] = (
    "Ocp-Apim-Subscription-Key",
    "api-key",
    "x-api-key",
    "Authorization",
)
CALLER_CREDENTIAL_QUERY_PARAMETERS: tuple[str, ...] = ("subscription-key", "api-key")

# API Management's rule for a named value's display name, which is what ``{{...}}`` refers to.
# Braces can never appear, so a reference can't be broken out of.
_NAMED_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
BACKEND_KEY_SUMMARY = (
    "Sends the model endpoint's API key, which API Management reads from Key Vault through a "
    "named value."
)


def backend_key_name(backend_name: str) -> str:
    """The named value a publication's backend key is read from, beside its backend."""

    return f"{backend_name}{NAMED_VALUE_SUFFIX}"


def backend_key_header(shape: str | None) -> str:
    header = BACKEND_KEY_HEADERS.get(shape or "")
    if header is None:
        raise ValidationError(
            "MOSAIC doesn't know which header this API shape takes an API key in.",
            details={"apiShape": shape},
        )
    return header


def named_value_reference(name: str) -> str:
    if not _NAMED_VALUE_PATTERN.fullmatch(name):
        raise ValidationError(
            "A backend key's named value must be a literal API Management name.",
            details={"name": name},
        )
    return "{{" + name + "}}"


def strip_caller_credentials(
    parent: ET.Element,
    *,
    removed_headers: Collection[str] = (),
    removed_query_parameters: Collection[str] = (),
) -> None:
    """Remove every caller credential not already removed, so only the gateway's key goes on.

    ``removed_*`` name what the surrounding policy already deletes, or deletes right after this,
    so no header is removed twice.
    """

    headers = {name.casefold() for name in removed_headers}
    for name in CALLER_CREDENTIAL_HEADERS:
        if name.casefold() not in headers:
            ET.SubElement(parent, "set-header", {"name": name, "exists-action": "delete"})
    parameters = {name.casefold() for name in removed_query_parameters}
    for name in CALLER_CREDENTIAL_QUERY_PARAMETERS:
        if name.casefold() not in parameters:
            ET.SubElement(
                parent, "set-query-parameter", {"name": name, "exists-action": "delete"}
            )


def set_backend_key(parent: ET.Element, *, shape: str | None, named_value: str) -> None:
    """Set the shape's key header from the named value, replacing anything already there."""

    header = ET.SubElement(
        parent,
        "set-header",
        {"name": backend_key_header(shape), "exists-action": "override"},
    )
    ET.SubElement(header, "value").text = named_value_reference(named_value)


def is_backend_key_facet(facet: PolicyFacet, shape: str | None) -> bool:
    return (
        facet.element == "set-header"
        and facet.attributes.get("exists-action") == "override"
        and facet.attributes.get("name", "").casefold()
        == BACKEND_KEY_HEADERS.get(shape or "", "").casefold()
    )


def describe_backend_key(facet: PolicyFacet) -> None:
    """Word the key header's facet as backend authentication rather than a header write."""

    facet.kind = PolicyFacetKind.AUTHENTICATION
    facet.summary = BACKEND_KEY_SUMMARY
    facet.details = [
        "The key never passes through MOSAIC. Every credential a caller sent is removed first, "
        "so nothing a caller sends can replace it.",
    ]


def named_value_properties(name: str, secret_identifier: str) -> dict[str, object]:
    """A secret named value that API Management resolves from Key Vault with its own identity.

    ``identityClientId`` is left out, so the gateway uses its system-assigned identity: the only
    one API Management can use through a Key Vault firewall.
    """

    named_value_reference(name)
    return {
        "displayName": name,
        "secret": True,
        "keyVault": {"secretIdentifier": secret_identifier},
    }
