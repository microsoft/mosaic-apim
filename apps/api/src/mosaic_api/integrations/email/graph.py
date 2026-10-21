"""Microsoft Graph sendMail with the deployment's app-only identity."""

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import jwt
from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import AzureError
from opentelemetry.instrumentation.utils import suppress_http_instrumentation

from .contracts import EmailAccessResult, EmailMessage, EmailSendResult

GRAPH_HOSTS = frozenset(
    {
        "graph.microsoft.com",
        "graph.microsoft.us",
        "dod-graph.microsoft.us",
        "microsoftgraph.chinacloudapi.cn",
    }
)
_GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
_SAFE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,99}")


class EmailAuthenticationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return min(max(int(value), 0), 86400)
    except ValueError:
        try:
            instant = parsedate_to_datetime(value)
            return min(max(int((instant - datetime.now(UTC)).total_seconds()), 0), 86400)
        except (ValueError, TypeError, OverflowError):
            return None


def _refusal(status: int, code: str | None) -> str:
    if status == 401:
        return "Graph authentication failed. Check the API managed identity and Graph cloud."
    if code in {"MailboxNotEnabledForRESTAPI", "ErrorMailboxNotEnabledForRESTAPI"}:
        return (
            "The sender has no usable Exchange Online mailbox. Ask an Exchange administrator "
            "to check mailbox provisioning and licensing."
        )
    if status == 403:
        return (
            "Graph denied this sender. Ask an Exchange administrator to grant Application "
            "Mail.Send to the API managed identity with this mailbox in scope, then run "
            "Test-ServicePrincipalAuthorization for that identity and mailbox."
        )
    if status == 404:
        return (
            "Graph could not find the sending mailbox. Check its Entra object ID, tenant, "
            "and Exchange Online provisioning."
        )
    if status == 429:
        return "Graph throttled email. Queued notifications retry after the provider's delay."
    if status == 408 or status >= 500:
        return "Graph acceptance is uncertain. Queued notifications retry; duplicates are possible."
    return (
        f"Graph refused email (HTTP {status}). Check the sender mailbox and recipient addresses; "
        "use the diagnostic code and request ID when asking your administrator for help."
    )


class GraphEmailClient:
    def __init__(
        self,
        credential: AsyncTokenCredential,
        *,
        tenant_id: str,
        endpoint: str = "https://graph.microsoft.com",
        principal_id: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        parts = urlsplit(endpoint)
        if (
            parts.scheme != "https"
            or parts.hostname not in GRAPH_HOSTS
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
            or parts.port not in {None, 443}
        ):
            raise ValueError("Email requires a supported Microsoft Graph cloud origin.")
        self._endpoint = f"https://{parts.hostname}"
        self._scope = f"{self._endpoint}/.default"
        self._tenant = tenant_id.casefold()
        self._principal = principal_id.casefold() if principal_id else None
        self._credential = credential
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=False
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _token(self) -> tuple[AccessToken, str | None]:
        try:
            token = await self._credential.get_token(self._scope, tenant_id=self._tenant)
        except (AzureError, TimeoutError):
            raise EmailAuthenticationError(
                "graphIdentityUnavailable",
                "The API could not acquire a Graph token. Check its managed identity and host "
                "attachment; a developer's signed-in session is not an email credential.",
            ) from None
        # This is a defensive check on our Azure credential, not authentication of caller input.
        try:
            claims = jwt.decode(token.token, options={"verify_signature": False})
        except jwt.PyJWTError:
            raise EmailAuthenticationError(
                "graphInvalidCredential", "The API's Graph credential could not be inspected."
            ) from None
        principal = claims.get("oid")
        if (
            str(claims.get("tid", "")).casefold() != self._tenant
            or claims.get("scp")
            or claims.get("idtyp") not in {None, "app"}
            or not (claims.get("idtyp") == "app" or isinstance(claims.get("roles"), list))
            or claims.get("aud") not in {_GRAPH_APP_ID, self._endpoint, self._endpoint + "/"}
            or (self._principal and str(principal).casefold() != self._principal)
        ):
            raise EmailAuthenticationError(
                "graphWrongIdentity",
                "Email needs the API managed identity's app-only Graph token in this tenant. "
                "Check the tenant, cloud, and managed identity object ID; delegated tokens "
                "cannot send background notifications.",
            )
        # Exchange Application RBAC can authorize an app-only token without a Mail.Send role.
        return token, principal if isinstance(principal, str) else None

    async def check_access(self) -> EmailAccessResult:
        try:
            _, principal = await self._token()
        except EmailAuthenticationError as error:
            return EmailAccessResult(False, error=str(error), error_code=error.code)
        return EmailAccessResult(True, identity_object_id=principal)

    async def send(
        self, sender_mailbox_id: str, message: EmailMessage, *, operation_id: str
    ) -> EmailSendResult:
        try:
            mailbox = str(UUID(sender_mailbox_id))
        except (ValueError, TypeError, AttributeError):
            return EmailSendResult(
                False,
                operation_id,
                error="Configure the sender mailbox's Entra object ID.",
                error_code="graphInvalidMailbox",
            )
        if not message.to:
            return EmailSendResult(
                False,
                operation_id,
                error="The email names no recipient.",
                error_code="graphNoRecipients",
            )
        try:
            token, _ = await self._token()
        except EmailAuthenticationError as error:
            return EmailSendResult(False, operation_id, error=str(error), error_code=error.code)
        body = {
            "message": {
                "subject": message.subject,
                "body": {
                    "contentType": "HTML" if message.html else "Text",
                    "content": message.html or message.plain_text,
                },
                "toRecipients": [{"emailAddress": {"address": address}} for address in message.to],
            },
            "saveToSentItems": True,
        }
        try:
            with suppress_http_instrumentation():
                response = await self._client.post(
                    f"{self._endpoint}/v1.0/users/{mailbox}/sendMail",
                    headers={
                        "Authorization": f"Bearer {token.token}",
                        "client-request-id": operation_id,
                        "return-client-request-id": "true",
                    },
                    json=body,
                    follow_redirects=False,
                )
        except httpx.ConnectError:
            return EmailSendResult(
                False,
                operation_id,
                error="Graph could not be reached. MOSAIC retries automatically.",
                error_code="graphNetworkUnavailable",
            )
        except httpx.HTTPError:
            return EmailSendResult(
                False,
                operation_id,
                error="Graph acceptance is uncertain. MOSAIC retries; duplicates are possible.",
                error_code="graphOutcomeUnknown",
                outcome_unknown=True,
            )
        request_id = response.headers.get("request-id")
        try:
            request_id = str(UUID(request_id)) if request_id else None
        except ValueError:
            request_id = None
        if response.status_code == 202:
            return EmailSendResult(True, operation_id, 202, request_id=request_id)
        code = None
        try:
            payload = response.json()
            provider_error = payload.get("error") if isinstance(payload, dict) else None
            candidate = provider_error.get("code") if isinstance(provider_error, dict) else None
            if isinstance(candidate, str) and _SAFE_CODE.fullmatch(candidate):
                code = candidate
        except ValueError:
            pass
        return EmailSendResult(
            False,
            operation_id,
            response.status_code,
            error=_refusal(response.status_code, code),
            retry_after_seconds=_retry_after(response.headers.get("Retry-After")),
            outcome_unknown=response.status_code == 408 or response.status_code >= 500,
            error_code=code or f"graphHttp{response.status_code}",
            request_id=request_id,
        )
