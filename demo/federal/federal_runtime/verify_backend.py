"""Verify valid-token, wrong-client EasyAuth rejection without invoking tools or models."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import ManagedIdentityCredential

from federal_runtime.shared import configure_logging

TARGET_COUNT = 24
AUDIENCE_COUNT = 3
MAX_INPUT_BYTES = 16_384
MAX_RESPONSE_BYTES = 65_536
TOTAL_DEADLINE_SECONDS = 180.0
TOKEN_TIMEOUT_SECONDS = 20.0
CLOSE_TIMEOUT_SECONDS = 5.0
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+azurecontainerapps\.io")


class TargetError(ValueError):
    """Invalid input; never includes any supplied value."""


class QuietParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise TargetError("Invalid verifier arguments.")


@dataclass(frozen=True)
class Target:
    url: str
    audience: str


@dataclass(frozen=True)
class Targets:
    items: tuple[Target, ...]
    sha256: str


@dataclass
class Report:
    targets_sha256: str | None = None
    target_count: int = 0
    attempted: int = 0
    status_counts: Counter[str] = field(default_factory=Counter)
    error_counts: Counter[str] = field(default_factory=Counter)

    @property
    def success(self) -> bool:
        return (
            self.target_count == TARGET_COUNT
            and self.attempted == TARGET_COUNT
            and self.status_counts == {"403": TARGET_COUNT}
            and not self.error_counts
        )

    def payload(self) -> dict:
        return {
            "outcome": "ok" if self.success else "error",
            "targets_sha256": self.targets_sha256,
            "target_count": self.target_count,
            "attempted": self.attempted,
            "status_counts": dict(sorted(self.status_counts.items())),
            "error_counts": dict(sorted(self.error_counts.items())),
        }


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise TargetError("Duplicate target fields.")
        result[key] = value
    return result


def parse_targets(raw: str) -> Targets:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_INPUT_BYTES:
        raise TargetError("Invalid targets input size.")
    try:
        values = json.loads(raw, object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError):
        raise TargetError("Invalid targets JSON.") from None
    if not isinstance(values, list) or len(values) != TARGET_COUNT:
        raise TargetError("Exactly 24 backend targets are required.")
    targets = []
    for value in values:
        if (
            not isinstance(value, dict)
            or set(value) != {"url", "audience"}
            or not isinstance(value["url"], str)
            or not isinstance(value["audience"], str)
        ):
            raise TargetError("Invalid target fields.")
        url, audience = value["url"], value["audience"]
        if url != url.strip() or any(character.isspace() for character in url):
            raise TargetError("Invalid backend URL.")
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
            if (
                parts.scheme != "https"
                or not _HOST.fullmatch(host)
                or len(host) > 253
                or parts.port is not None
                or parts.username is not None
                or parts.password is not None
                or parts.path != "/mcp"
                or "?" in url
                or "#" in url
                or "\\" in url
                or any(ord(character) < 33 or ord(character) > 126 for character in url)
            ):
                raise TargetError("Invalid backend URL.")
        except ValueError:
            raise TargetError("Invalid backend URL.") from None
        if not audience.startswith("api://") or not _UUID.fullmatch(audience[6:]):
            raise TargetError("Invalid backend audience.")
        targets.append(Target(f"https://{host}/mcp", f"api://{audience[6:].lower()}"))
    if len({target.url for target in targets}) != TARGET_COUNT:
        raise TargetError("Every backend URL must be unique.")
    counts = Counter(target.audience for target in targets)
    if len(counts) != AUDIENCE_COUNT or set(counts.values()) != {TARGET_COUNT // AUDIENCE_COUNT}:
        raise TargetError("Exactly three audiences with eight backends each are required.")
    targets.sort(key=lambda target: (target.url, target.audience))
    canonical = json.dumps(
        [{"url": target.url, "audience": target.audience} for target in targets],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return Targets(tuple(targets), hashlib.sha256(canonical.encode("utf-8")).hexdigest())


async def verify_targets(
    targets: Targets,
    client_id: str,
    *,
    credential: AsyncTokenCredential | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Report:
    report = Report(targets.sha256, len(targets.items))
    identity = credential
    http = None
    try:
        async with asyncio.timeout(TOTAL_DEADLINE_SECONDS):
            if not _UUID.fullmatch(client_id):
                raise TargetError(
                    "Set AZURE_CLIENT_ID to the verifier job's user-assigned identity."
                )
            identity = credential or ManagedIdentityCredential(client_id=client_id, retry_total=0)
            http = httpx.AsyncClient(
                transport=transport or httpx.AsyncHTTPTransport(retries=0),
                follow_redirects=False,
                timeout=HTTP_TIMEOUT,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
                trust_env=False,
            )
            for audience in sorted({target.audience for target in targets.items}):
                token = None
                acquired = None
                try:
                    async with asyncio.timeout(TOKEN_TIMEOUT_SECONDS):
                        acquired = await identity.get_token(f"{audience}/.default")
                    token = acquired.token
                    if (
                        not isinstance(token, str)
                        or not token.strip()
                        or len(token) > 16_384
                        or acquired.expires_on <= time.time()
                    ):
                        raise ValueError("No current managed identity token.")
                except Exception:
                    report.error_counts["token"] += 1
                    token = None
                    acquired = None
                    continue
                try:
                    for target in targets.items:
                        if target.audience != audience:
                            continue
                        report.attempted += 1
                        try:
                            async with http.stream(
                                "GET",
                                target.url,
                                headers={
                                    "Authorization": f"Bearer {token}",
                                    "Accept": "application/json, text/event-stream",
                                    "Accept-Encoding": "identity",
                                },
                            ) as response:
                                report.status_counts[str(response.status_code)] += 1
                                if response.status_code != 403:
                                    report.error_counts["unexpected_status"] += 1
                                    continue
                                length = 0
                                async for chunk in response.aiter_bytes(chunk_size=4096):
                                    length += len(chunk)
                                    if length > MAX_RESPONSE_BYTES:
                                        report.error_counts["response_limit"] += 1
                                        break
                        except httpx.HTTPError:
                            report.error_counts["network"] += 1
                        except Exception:
                            report.error_counts["response"] += 1
                finally:
                    # Do not persist tokens or reuse one for a different backend audience.
                    token = None
                    acquired = None
    except TimeoutError:
        report.error_counts["deadline"] += 1
    except TargetError:
        report.error_counts["configuration"] += 1
    except Exception:
        report.error_counts["verification"] += 1
    finally:
        for resource in (http, identity):
            if resource is not None:
                try:
                    async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                        if resource is http:
                            await resource.aclose()
                        else:
                            await resource.close()
                except Exception:
                    report.error_counts["cleanup"] += 1
    return report


def main(argv: list[str] | None = None) -> int:
    # SDK/HTTP warnings are filtered, withheld and kept off the single-JSON stdout contract.
    configure_logging(sys.stderr)
    report = Report()
    try:
        parser = QuietParser(add_help=False)
        parser.add_argument("--targets-json", required=True)
        args = parser.parse_args(argv)
        targets = parse_targets(args.targets_json)
        report = Report(targets.sha256, len(targets.items))
        client_id = os.environ.get("AZURE_CLIENT_ID", "").strip()
        if not _UUID.fullmatch(client_id):
            raise TargetError("Set AZURE_CLIENT_ID to the verifier job's user-assigned identity.")
        report = asyncio.run(verify_targets(targets, client_id))
    except TargetError:
        report.error_counts["configuration"] += 1
    except Exception:
        report.error_counts["verification"] += 1
    print(json.dumps(report.payload(), sort_keys=True, separators=(",", ":")))
    return 0 if report.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
