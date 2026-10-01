"""An in-process stand-in for the ARM reads MOSAIC makes about a Key Vault.

Serves the vault's own description, role assignments, role definitions and deny assignments at
the vault's scope, and the subscription listing and resource search the vault locator uses. It
never serves a secret: MOSAIC reads secrets through the data plane, which tests replace with a
function. :class:`FakeKeyStore` stands in for the data-plane writes MOSAIC makes to its own vault.
"""

from typing import Any

import httpx
from aoai_double import deny_assignment, role_assignment, role_definition_resource
from mosaic_api.domain import KeyVaultSecretId
from mosaic_api.errors import ConflictError
from pydantic import SecretStr

VAULT_SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"
VAULT_RESOURCE_GROUP = "rg-contoso-ai"
VAULT_NAME = "kv-contoso-ai"
VAULT_ID = (
    f"/subscriptions/{VAULT_SUBSCRIPTION_ID}/resourceGroups/{VAULT_RESOURCE_GROUP}"
    f"/providers/Microsoft.KeyVault/vaults/{VAULT_NAME}"
)
SECRET_NAME = "fabrikam-foundry-key"
SECRET_URI = f"https://{VAULT_NAME}.vault.azure.net/secrets/{SECRET_NAME}"
SECRET_SCOPE = f"{VAULT_ID}/secrets/{SECRET_NAME}"

KEY_VAULT_SECRETS_USER_ROLE_ID = "4633458b-17de-408a-b874-0445c86b69e6"
KEY_VAULT_SECRETS_OFFICER_ROLE_ID = "b86a8fe4-44ce-4948-aee5-eccb2c155cd7"
KEY_VAULT_READER_ROLE_ID = "21090545-7ca7-4776-b22c-e363652d74d2"
READER_ROLE_ID = "acdd72a7-3385-48ef-bd42-f606fba81ae7"

# Taken from the built-in definitions. Key Vault Reader reads metadata, never a secret's value.
KEY_VAULT_ROLE_DEFINITIONS: dict[str, tuple[str, list[dict[str, Any]]]] = {
    KEY_VAULT_SECRETS_USER_ROLE_ID: (
        "Key Vault Secrets User",
        [
            {
                "actions": [],
                "notActions": [],
                "dataActions": [
                    "Microsoft.KeyVault/vaults/secrets/getSecret/action",
                    "Microsoft.KeyVault/vaults/secrets/readMetadata/action",
                ],
                "notDataActions": [],
            }
        ],
    ),
    KEY_VAULT_SECRETS_OFFICER_ROLE_ID: (
        "Key Vault Secrets Officer",
        [
            {
                "actions": ["Microsoft.Authorization/*/read", "Microsoft.KeyVault/vaults/*/read"],
                "notActions": [],
                "dataActions": ["Microsoft.KeyVault/vaults/secrets/*"],
                "notDataActions": [],
            }
        ],
    ),
    KEY_VAULT_READER_ROLE_ID: (
        "Key Vault Reader",
        [
            {
                "actions": ["Microsoft.Authorization/*/read", "Microsoft.KeyVault/vaults/read"],
                "notActions": [],
                "dataActions": [
                    "Microsoft.KeyVault/vaults/*/read",
                    "Microsoft.KeyVault/vaults/secrets/readMetadata/action",
                ],
                "notDataActions": [],
            }
        ],
    ),
    READER_ROLE_ID: (
        "Reader",
        [{"actions": ["*/read"], "notActions": [], "dataActions": [], "notDataActions": []}],
    ),
}


def vault_role_assignment(role_definition_id: str, scope: str, principal_id: str) -> dict[str, Any]:
    return role_assignment(role_definition_id, scope, principal_id)


def vault_deny(principal_id: str) -> dict[str, Any]:
    return deny_assignment(
        VAULT_ID,
        data_actions=["Microsoft.KeyVault/vaults/secrets/*"],
        principals=[{"id": principal_id, "type": "ServicePrincipal"}],
        name="vault-stack-deny",
    )


class FakeKeyVaultArm:
    """One vault in one subscription, as MOSAIC's ARM identity sees it."""

    def __init__(self) -> None:
        self.properties: dict[str, Any] = {
            "tenantId": "00000000-0000-0000-0000-000000000000",
            "enableRbacAuthorization": True,
            "publicNetworkAccess": "Enabled",
            "networkAcls": {"defaultAction": "Allow", "bypass": "AzureServices"},
        }
        self.assignments: list[dict[str, Any]] = []
        self.deny_assignments: list[dict[str, Any]] = []
        self.vault_readable = True
        self.assignments_readable = True
        self.definitions_readable = True
        # Whether a subscription-wide resource search finds the vault, as it does only for an
        # identity that can read it.
        self.searchable = True
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(f"{request.method} {path}")
        if request.method != "GET":
            return httpx.Response(405, json={"error": {"message": "read-only double"}})
        if path == "/subscriptions":
            return _collection(
                [{"subscriptionId": VAULT_SUBSCRIPTION_ID, "displayName": "Contoso AI"}]
            )
        if path == f"/subscriptions/{VAULT_SUBSCRIPTION_ID}/resources":
            wanted = request.url.params.get("$filter", "")
            found = self.searchable and f"name eq '{VAULT_NAME}'" in wanted
            return _collection(
                [{"id": VAULT_ID, "name": VAULT_NAME, "type": "Microsoft.KeyVault/vaults"}]
                if found
                else []
            )
        if path.casefold() == VAULT_ID.casefold():
            if not self.vault_readable:
                return _denied()
            return httpx.Response(
                200,
                json={"id": VAULT_ID, "name": VAULT_NAME, "properties": self.properties},
            )
        authorization = f"{VAULT_ID}/providers/Microsoft.Authorization/"
        if path.casefold().startswith(authorization.casefold()):
            rest = path[len(authorization) :]
            if rest == "roleAssignments":
                if not self.assignments_readable:
                    return _denied()
                wanted = request.url.params.get("$filter", "")
                return _collection(
                    [
                        item
                        for item in self.assignments
                        if f"'{item['properties']['principalId']}'" in wanted
                    ]
                )
            if rest == "denyAssignments":
                return _collection(self.deny_assignments)
            if rest.startswith("roleDefinitions/"):
                if not self.definitions_readable:
                    return _denied()
                guid = rest.rsplit("/", 1)[-1]
                known = KEY_VAULT_ROLE_DEFINITIONS.get(guid)
                if known is None:
                    return httpx.Response(404, json={"error": {"message": "no such role"}})
                return httpx.Response(200, json=role_definition_resource(guid, *known))
        return httpx.Response(404, json={"error": {"message": f"unknown resource {path}"}})


def _collection(values: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"value": values})


def _denied() -> httpx.Response:
    return httpx.Response(403, json={"error": {"code": "AuthorizationFailed", "message": "no"}})


class FakeKeyStore:
    """MOSAIC's own Key Vault as its key store writes it: each secret's versions, and every call.

    Deleting a secret keeps its name taken, as purge protection does, so writing a new secret
    under a deleted name fails the way Key Vault fails it.
    """

    def __init__(self, vault_name: str = VAULT_NAME) -> None:
        self.vault_name = vault_name
        self.versions: dict[str, list[str]] = {}
        self.tags: dict[str, dict[str, str]] = {}
        self.deleted: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.put_error: Exception | None = None
        self.put_error_after_write: Exception | None = None
        self.delete_error: Exception | None = None

    def new_secret(self, name: str) -> KeyVaultSecretId:
        return KeyVaultSecretId.parse(f"https://{self.vault_name}.vault.azure.net/secrets/{name}")

    async def put(
        self, secret: KeyVaultSecretId, value: SecretStr, *, tags: dict[str, str]
    ) -> None:
        self.calls.append(("put", secret.secret_name))
        if self.put_error is not None:
            raise self.put_error
        if secret.secret_name in self.deleted:
            raise ConflictError("A deleted secret of that name is still kept.")
        self.versions.setdefault(secret.secret_name, []).append(value.get_secret_value())
        self.tags[secret.secret_name] = dict(tags)
        if self.put_error_after_write is not None:
            raise self.put_error_after_write

    async def delete(self, secret: KeyVaultSecretId) -> bool:
        self.calls.append(("delete", secret.secret_name))
        if self.delete_error is not None:
            raise self.delete_error
        if self.versions.pop(secret.secret_name, None) is None:
            return False
        self.deleted.add(secret.secret_name)
        return True

    def current(self, uri: str) -> str | None:
        """The newest version of the secret a versionless identifier names, if MOSAIC wrote it."""

        secret = KeyVaultSecretId.parse(uri)
        if secret.vault_name != self.vault_name:
            return None
        versions = self.versions.get(secret.secret_name)
        return versions[-1] if versions else None
