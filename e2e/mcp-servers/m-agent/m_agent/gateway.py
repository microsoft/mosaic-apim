"""M-agent's model call: one governed chat completion through MOSAIC's gateway.

It follows docs/mcp-servers-that-call-models.md. The server calls the model as its own
application, with an Entra token from the container app's managed identity, and passes on the
x-mosaic-on-behalf-of value of the MCP request it's serving. Errors name the HTTP status and, when
the gateway gave one, MOSAIC's reason, with anything that could be a credential or an identifier
taken out.
"""

import json
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from azure.core.credentials_async import AsyncTokenCredential

from m_agent.logs import logger

ON_BEHALF_HEADER = "x-mosaic-on-behalf-of"
COST_CENTER_HEADER = "x-mosaic-cost-center"
SYSTEM_PROMPT = "Answer in ten words or fewer."
MAX_QUESTION_CHARS = 500
MAX_REASON_CHARS = 200
TokenParameter = Literal["max_tokens", "max_completion_tokens"]

_DEPLOYMENT = re.compile(r"[A-Za-z0-9._-]{1,64}")
_API_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,31}")
_COST_CENTER = re.compile(r"[A-Za-z0-9._-]{1,64}")
_TOKEN_PARAMETERS: dict[str, TokenParameter] = {
    "max_tokens": "max_tokens",
    "max_completion_tokens": "max_completion_tokens",
}
_FINISH_REASON = re.compile(r"[a-z_]{1,32}")

# What a gateway's error text must never carry into a tool result.
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_JWT = re.compile(r"\beyJ[\w-]*\.[\w-]*\.[\w-]*")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


class ModelCallError(Exception):
    """A model call that failed. Its message is safe to return to the MCP client."""


@dataclass(frozen=True)
class ModelSettings:
    endpoint: str
    deployment: str
    api_version: str
    runtime_scope: str
    cost_center: str | None = None
    token_parameter: TokenParameter = "max_tokens"
    max_tokens: int = 16

    @property
    def chat_url(self) -> str:
        return f"{self.endpoint}/openai/deployments/{quote(self.deployment)}/chat/completions"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "ModelSettings":
        endpoint = _required(env, "MOSAIC_MODEL_ENDPOINT").rstrip("/")
        parts = urlsplit(endpoint)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "MOSAIC_MODEL_ENDPOINT must be the https endpoint from the model's connection "
                "details, with no credentials, query or fragment."
            )
        deployment = _matching(env, "MOSAIC_MODEL_DEPLOYMENT", _DEPLOYMENT)
        api_version = _matching(env, "MOSAIC_MODEL_API_VERSION", _API_VERSION)
        scope = _required(env, "MOSAIC_RUNTIME_SCOPE")
        if not scope.startswith("api://") or not scope.endswith("/.default"):
            raise ValueError(
                "MOSAIC_RUNTIME_SCOPE must be api://<model-runtime-client-id>/.default."
            )
        cost_center = env.get("MOSAIC_COST_CENTER", "").strip() or None
        if cost_center is not None and not _COST_CENTER.fullmatch(cost_center):
            raise ValueError("MOSAIC_COST_CENTER must be a cost center code.")
        parameter = env.get("MOSAIC_MODEL_TOKEN_PARAMETER", "max_tokens").strip()
        if parameter not in _TOKEN_PARAMETERS:
            raise ValueError(
                "MOSAIC_MODEL_TOKEN_PARAMETER must be max_tokens or max_completion_tokens."
            )
        max_tokens = env.get("MOSAIC_MODEL_MAX_TOKENS", "16").strip()
        if not max_tokens.isdigit() or not 1 <= int(max_tokens) <= 256:
            raise ValueError("MOSAIC_MODEL_MAX_TOKENS must be a whole number from 1 to 256.")
        return cls(
            endpoint=endpoint,
            deployment=deployment,
            api_version=api_version,
            runtime_scope=scope,
            cost_center=cost_center,
            token_parameter=_TOKEN_PARAMETERS[parameter],
            max_tokens=int(max_tokens),
        )


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"Set {name}.")
    return value


def _matching(env: Mapping[str, str], name: str, pattern: re.Pattern[str]) -> str:
    value = _required(env, name)
    if not pattern.fullmatch(value):
        raise ValueError(f"{name} isn't valid.")
    return value


def scrub(text: str, secrets: Iterable[str] = ()) -> str | None:
    """Text from the gateway, without credentials, URLs, addresses or IDs, on one short line."""

    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _JWT.sub("[redacted]", text)
    text = _URL.sub("[url]", text)
    text = _EMAIL.sub("[email]", text)
    text = _GUID.sub("[id]", text)
    text = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    if len(text) > MAX_REASON_CHARS:
        text = text[: MAX_REASON_CHARS - 1].rstrip() + "…"
    return text or None


def deny_reason(response: httpx.Response, *, secrets: Iterable[str] = ()) -> str | None:
    """MOSAIC's reason for refusing a call, when the gateway's response gives one.

    MOSAIC's own refusals are plain text. API Management's, such as a failed token check or a
    spent call limit, are JSON with a message. A model's own errors are JSON with error.message.
    """

    body = response.content[:16384].decode("utf-8", "replace")
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    candidate: Any = None
    if content_type.endswith("json") or body.lstrip().startswith("{"):
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            candidate = payload.get("message")
            error = payload.get("error")
            if not isinstance(candidate, str) and isinstance(error, dict):
                candidate = error.get("message")
            elif not isinstance(candidate, str) and isinstance(error, str):
                candidate = error
    elif content_type in {"", "text/plain"}:
        candidate = body
    return scrub(candidate, secrets) if isinstance(candidate, str) else None


def _answer(payload: Any) -> str:
    choice: Any = None
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("choices"), list)
        and payload["choices"]
    ):
        choice = payload["choices"][0]
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str) and content.strip():
        return content.strip()
    finish = choice.get("finish_reason") if isinstance(choice, dict) else None
    if isinstance(finish, str) and _FINISH_REASON.fullmatch(finish):
        raise ModelCallError(f"The model returned no answer text (finish reason: {finish}).")
    raise ModelCallError("The model returned no answer text.")


class ModelGateway:
    """Calls one governed model through MOSAIC's gateway, as the server's own application."""

    def __init__(
        self, settings: ModelSettings, credential: AsyncTokenCredential, http: httpx.AsyncClient
    ) -> None:
        self.settings = settings
        self._credential = credential
        self._http = http

    async def _token(self) -> str:
        try:
            token = (await self._credential.get_token(self.settings.runtime_scope)).token
        except Exception as error:
            # The credential's own message can name endpoints and client IDs, so it stays here.
            raise ModelCallError(
                f"The server couldn't get a token for the model gateway ({type(error).__name__})."
            ) from None
        if not token:
            raise ModelCallError("The server got an empty token for the model gateway.")
        return token

    async def ask(self, question: str, *, on_behalf_of: str | None) -> str:
        token = await self._token()
        headers = {"Authorization": f"Bearer {token}"}
        if on_behalf_of:
            headers[ON_BEHALF_HEADER] = on_behalf_of
        if self.settings.cost_center:
            headers[COST_CENTER_HEADER] = self.settings.cost_center
        body = {
            "model": self.settings.deployment,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ],
            self.settings.token_parameter: self.settings.max_tokens,
            "stream": False,
        }
        started = time.perf_counter()
        try:
            response = await self._http.post(
                self.settings.chat_url,
                params={"api-version": self.settings.api_version},
                headers=headers,
                json=body,
            )
        except httpx.TimeoutException:
            logger.info("model_call status=timeout duration_ms=%d", _elapsed_ms(started))
            raise ModelCallError("The model call timed out.") from None
        except httpx.HTTPError as error:
            logger.info("model_call status=unreachable duration_ms=%d", _elapsed_ms(started))
            # httpx's messages name the URL, so only the error's type is reported.
            raise ModelCallError(
                f"The model call failed before the gateway answered ({type(error).__name__})."
            ) from None
        logger.info(
            "model_call status=%d duration_ms=%d", response.status_code, _elapsed_ms(started)
        )
        if not response.is_success:
            reason = deny_reason(response, secrets=[token])
            suffix = f": {reason}" if reason else "."
            raise ModelCallError(f"The model call failed with HTTP {response.status_code}{suffix}")
        try:
            payload = response.json()
        except ValueError:
            raise ModelCallError("The gateway's answer wasn't JSON.") from None
        # Only a broken gateway would echo the token, but it must never reach a tool result.
        return _answer(payload).replace(token, "[redacted]")

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._credential.close()


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
