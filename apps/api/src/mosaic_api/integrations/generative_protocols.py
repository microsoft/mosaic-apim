"""Offline, versioned external model contracts. Nothing here opens a network connection.

These contracts are deliberately not a gateway transport. Authentication execution, deployed
egress enforcement and native APIM metering remain independent publication gates.
"""

import base64
import binascii
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any
from urllib.parse import urlencode, urlsplit

from PIL import Image, UnidentifiedImageError

CONTRACT_VERSION = "2026-11-18"
VERTEX_ORIGIN = "https://aiplatform.us.rep.googleapis.com"
VERTEX_MODEL = "gemini-3.8-flash"
STS_URL = "https://sts.googleapis.com/v1/token"
OAUTH_URL = "https://oauth2.googleapis.com/token"
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGES = 8
MAX_IMAGE_PIXELS = 16_000_000

VERTEX_ENFORCEMENT_BLOCKER = (
    "Native Vertex APIM schema recognition, separate streaming-operation token accounting and "
    "required enforcement have not been proved. Publication is blocked."
)
CUSTOM_PUBLICATION_BLOCKER = (
    "Custom chat publication requires deployed exact-host admission and DNS-rebinding-safe "
    "egress enforcement evidence. URL validation is not that evidence; credentialed forwarding "
    "is blocked."
)


def vertex_auth_blocker(auth_mode: str) -> str:
    if auth_mode == "workloadIdentityFederation":
        return (
            "APIM managed identity -> Google STS -> service-account impersonation composition "
            "is unproved. The Google Azure VM guide is not APIM runtime evidence."
        )
    if auth_mode == "serviceAccountKey":
        return (
            "Key Vault service-account signing -> Google OAuth JWT-bearer execution is unproved "
            "in APIM. A .NET PFX proof does not establish an APIM signer."
        )
    return (
        "Google service-account-bound API-key authorization and acceptance on the exact us "
        "Vertex route are unproved. x-goog-api-key forwarding is not enabled."
    )


def publication_blockers(provider: str, auth_mode: str | None = None) -> tuple[str, ...]:
    if provider == "googleVertex":
        auth = (
            vertex_auth_blocker(auth_mode)
            if auth_mode is not None
            else "Selected Vertex authentication execution has not been proved in APIM."
        )
        return auth, VERTEX_ENFORCEMENT_BLOCKER
    return (CUSTOM_PUBLICATION_BLOCKER,)


def admit_custom_chat_url(url: str) -> str:
    """Validate public-host syntax only; never claim this proves DNS or gateway egress."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.port not in {None, 443}
        or parts.path not in {"", "/", "/v1", "/v1/"}
        or not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", host)
        or "." not in host
        or host.lower().endswith(
            (
                ".localhost",
                ".local",
                ".internal",
                ".invalid",
                ".test",
                ".lan",
                ".home",
            )
        )
        or not re.fullmatch(r"[a-zA-Z]{2,63}", host.rsplit(".", 1)[-1])
    ):
        raise ValueError(
            "Custom chat requires a public HTTPS DNS host, optional /v1, no URL secrets"
        )
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if all(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
            for label in host.split(".")
        ):
            return url.rstrip("/")
    raise ValueError("Custom chat requires a public DNS hostname, not an IP address")


@dataclass(frozen=True)
class AuthRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(repr=False)
    body: str = field(repr=False)


def sts_exchange(audience: str, subject_token: str) -> AuthRequest:
    """RFC 8693 exchange: deliberately no Authorization header and no credential fallback."""
    if not re.fullmatch(
        r"//iam\.googleapis\.com/projects/[0-9]+/locations/global/"
        r"workloadIdentityPools/[a-z0-9-]+/providers/[a-z0-9-]+",
        audience,
    ):
        raise ValueError("Invalid workload identity provider audience")
    if not subject_token or any(c in subject_token for c in "\r\n"):
        raise ValueError("A fresh managed-identity subject token is required")
    return AuthRequest(
        "POST",
        STS_URL,
        {"Content-Type": "application/x-www-form-urlencoded"},
        urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "audience": audience,
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
                "scope": "https://www.googleapis.com/auth/cloud-platform",
                "subject_token": subject_token,
            }
        ),
    )


def _service_account(value: str) -> str:
    if not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9._-]*@[a-z][a-z0-9-]*\.iam\.gserviceaccount\.com", value
    ):
        raise ValueError("Invalid Google service-account email")
    return value


def impersonation_request(service_account: str, sts_token: str) -> AuthRequest:
    account = _service_account(service_account)
    if not sts_token or any(c in sts_token for c in "\r\n"):
        raise ValueError("A fresh STS access token is required")
    return AuthRequest(
        "POST",
        f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        f"{account}:generateAccessToken",
        {"Content-Type": "application/json", "Authorization": f"Bearer {sts_token}"},
        json.dumps(
            {"scope": ["https://www.googleapis.com/auth/cloud-platform"], "lifetime": "3600s"}
        ),
    )


def service_account_claims(service_account: str, issued_at: int) -> dict[str, str | int]:
    """Claims for an external, separately proved signer; this helper does not sign a JWT."""
    return {
        "iss": _service_account(service_account),
        "aud": OAUTH_URL,
        "scope": "https://www.googleapis.com/auth/cloud-platform",
        "iat": issued_at,
        "exp": issued_at + 3600,
    }


def jwt_bearer_exchange(signed_assertion: str) -> AuthRequest:
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", signed_assertion):
        raise ValueError("A separately signed service-account JWT is required")
    return AuthRequest(
        "POST",
        OAUTH_URL,
        {"Content-Type": "application/x-www-form-urlencoded"},
        urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": signed_assertion,
            }
        ),
    )


def vertex_auth_headers(auth_mode: str, credential: str) -> dict[str, str]:
    """Local wire contract only, not authorization to execute any of these unproved routes."""
    if not credential or any(character in credential for character in "\r\n"):
        raise ValueError("A fresh credential for the explicitly selected auth mode is required")
    if auth_mode == "apiKey":
        return {"x-goog-api-key": credential}
    if auth_mode in {"workloadIdentityFederation", "serviceAccountKey"}:
        return {"Authorization": f"Bearer {credential}"}
    raise ValueError("Unsupported explicit Vertex authentication mode")


def vertex_operation_url(
    project_id: str, model_id: str, *, streaming: bool, query: Mapping[str, str] | None = None
) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project_id) or model_id != VERTEX_MODEL:
        raise ValueError("Unsupported exact Vertex project/model binding")
    expected = {"alt": "sse"} if streaming else {}
    if query is not None and dict(query) != expected:
        raise ValueError("Only alt=sse on streamGenerateContent is allowed")
    operation = "streamGenerateContent" if streaming else "generateContent"
    return (
        f"{VERTEX_ORIGIN}/v1/projects/{project_id}/locations/us/publishers/google/"
        f"models/{model_id}:{operation}" + ("?alt=sse" if streaming else "")
    )


def custom_operation_url(endpoint: str) -> str:
    origin = admit_custom_chat_url(endpoint)
    return origin + ("/chat/completions" if origin.endswith("/v1") else "/v1/chat/completions")


def _image(data: object, mime: object) -> None:
    if mime not in {"image/png", "image/jpeg"} or not isinstance(data, str):
        raise ValueError("Only inline base64 PNG/JPEG images are supported")
    if len(data) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        raise ValueError("Inline image exceeds the contract byte limit")
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Invalid inline image encoding") from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("Inline image exceeds the contract byte limit")
    try:
        with Image.open(BytesIO(raw)) as image:
            expected = "PNG" if mime == "image/png" else "JPEG"
            if image.format != expected or image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("Inline image does not match the permitted MIME/dimension bound")
            image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("Invalid inline PNG/JPEG image") from None


def validate_request(
    shape: str, model_id: str, body: dict[str, Any], *, streaming: bool = False
) -> dict[str, Any]:
    """Validate the bounded envelope without translating, dropping defaults or opaque signatures.

    The unchanged body is returned. Function calls/results belong to the application, not MOSAIC.
    This local validator is not claimed to be an enforced APIM inbound policy.
    """
    if len(json.dumps(body, allow_nan=False).encode()) > MAX_REQUEST_BYTES:
        raise ValueError("Request exceeds the contract byte limit")
    images = 0
    if shape == "vertexGemini":
        if model_id != VERTEX_MODEL or ("model" in body and body["model"] != model_id):
            raise ValueError("Request does not match the exact native model binding")
        if "stream" in body:
            raise ValueError("Native streaming uses a separate operation, not body.stream")
        if set(body) - {
            "contents",
            "systemInstruction",
            "generationConfig",
            "safetySettings",
            "tools",
            "toolConfig",
            "model",
        }:
            raise ValueError("Unsupported native request field")
        config = body.get("generationConfig", {})
        if not isinstance(config, dict):
            raise ValueError("Invalid native generationConfig")
        modalities = config.get("responseModalities", ["TEXT"])
        if (
            not isinstance(modalities, list)
            or any(modality != "TEXT" for modality in modalities)
            or "speechConfig" in config
        ):
            raise ValueError("Audio/video/image output is outside this contract")
        contents = body.get("contents")
        if not isinstance(contents, list) or not contents:
            raise ValueError("Native contents must be a nonempty array")
        instructions = [body["systemInstruction"]] if "systemInstruction" in body else []
        for content in [*contents, *instructions]:
            if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
                raise ValueError("Native content requires parts")
            for part in content["parts"]:
                if not isinstance(part, dict) or set(part) - {
                    "text",
                    "inlineData",
                    "functionCall",
                    "functionResponse",
                    "thoughtSignature",
                    "thought",
                }:
                    raise ValueError(
                        "Only text, inline images and application function parts are allowed"
                    )
                if "inlineData" in part:
                    inline = part["inlineData"]
                    if not isinstance(inline, dict) or set(inline) != {"mimeType", "data"}:
                        raise ValueError("Invalid inline image part")
                    _image(inline["data"], inline["mimeType"])
                    images += 1
        tools = body.get("tools", [])
        if not isinstance(tools, list) or any(
            not isinstance(tool, dict) or set(tool) != {"functionDeclarations"} for tool in tools
        ):
            raise ValueError("Only application-executed function declarations are supported")
        tool_config = body.get("toolConfig", {})
        if not isinstance(tool_config, dict) or set(tool_config) - {"functionCallingConfig"}:
            raise ValueError("Only application function-calling configuration is supported")
    elif shape == "openAiChat":
        if body.get("model") != model_id or not isinstance(body.get("messages"), list):
            raise ValueError("Chat requires messages and the exact declared body.model")
        if body.get("stream", False) is not streaming:
            raise ValueError("Chat streaming flag does not match the selected operation")
        if set(body) - {
            "model",
            "messages",
            "stream",
            "temperature",
            "top_p",
            "n",
            "max_tokens",
            "max_completion_tokens",
            "stop",
            "presence_penalty",
            "frequency_penalty",
            "logit_bias",
            "user",
            "seed",
            "response_format",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "stream_options",
            "logprobs",
            "top_logprobs",
            "reasoning_effort",
            "metadata",
            "store",
            "service_tier",
            "modalities",
        }:
            raise ValueError("Unsupported custom chat request field")
        modalities = body.get("modalities", ["text"])
        if not isinstance(modalities, list) or any(modality != "text" for modality in modalities):
            raise ValueError("Audio/video/provider-hosted tools are outside this contract")
        for message in body["messages"]:
            if not isinstance(message, dict):
                raise ValueError("Invalid chat message")
            if set(message) & {"audio", "video", "input_audio", "file", "attachments"}:
                raise ValueError("Audio/video/files are outside this chat contract")
            parts = message.get("content")
            if isinstance(parts, list):
                for part in parts:
                    if not isinstance(part, dict) or part.get("type") not in {"text", "image_url"}:
                        raise ValueError(
                            "Only text and inline PNG/JPEG content parts are supported"
                        )
                    if part["type"] == "image_url":
                        image = part.get("image_url")
                        url = image.get("url") if isinstance(image, dict) else None
                        match = (
                            re.fullmatch(
                                r"data:(image/png|image/jpeg);base64,([A-Za-z0-9+/=]+)", url
                            )
                            if isinstance(url, str)
                            else None
                        )
                        if match is None:
                            raise ValueError("Remote media is not supported; use inline PNG/JPEG")
                        _image(match[2], match[1])
                        images += 1
        tools = body.get("tools", [])
        if not isinstance(tools, list) or any(
            not isinstance(tool, dict) or tool.get("type") != "function" for tool in tools
        ):
            raise ValueError("Only application-executed function tools are supported")
        choice = body.get("tool_choice", "auto")
        if not (
            (isinstance(choice, str) and choice in {"auto", "none", "required"})
            or (
                isinstance(choice, dict)
                and choice.get("type") == "function"
                and set(choice) == {"type", "function"}
                and isinstance(choice["function"], dict)
                and set(choice["function"]) == {"name"}
                and isinstance(choice["function"]["name"], str)
            )
        ):
            raise ValueError("tool_choice can select only application-executed functions")
    else:
        raise ValueError("Unsupported versioned generative contract")
    if images > MAX_IMAGES:
        raise ValueError("Too many inline images")
    return body


def usage_counts(shape: str, response: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """Missing usage is unknown, never a fabricated zero (including streamed responses)."""
    usage = response.get("usageMetadata" if shape == "vertexGemini" else "usage")
    if not isinstance(usage, dict):
        return None, None
    names = (
        ("promptTokenCount", "candidatesTokenCount")
        if shape == "vertexGemini"
        else ("prompt_tokens", "completion_tokens")
    )
    values = tuple(usage.get(name) for name in names)
    return (
        values[0] if type(values[0]) is int and values[0] >= 0 else None,
        values[1] if type(values[1]) is int and values[1] >= 0 else None,
    )
