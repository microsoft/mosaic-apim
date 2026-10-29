"""Target-bound Azure/Graph calls. Secrets stay in memory and HTTPS request bodies."""

import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from scripts.federal_demo.estate import validate_target
from scripts.mosaic_entra import OperationFailed, run_json_command


class OperationError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def command(args: list[str], *, allow_text=False) -> object:
    try:
        return run_json_command(args, f"{args[0]} {args[1]}", allow_non_json=allow_text)
    except OperationFailed as error:
        # CLI output may contain credentials; retain the failed operation, not its raw body.
        match = re.search(r"ERROR:\s*\(?([A-Za-z][A-Za-z0-9_.-]+)", str(error))
        code = f" ({match[1]})" if match else ""
        raise OperationError(
            f"{args[0]} {args[1]} failed{code}; inspect the CLI operation diagnostics"
        ) from None


class Operator:
    def __init__(self, target: dict, state_path: Path):
        validate_target(target)
        self.target = target
        self.state_path = state_path
        self.state = (
            json.loads(state_path.read_text())
            if state_path.exists()
            else {
                "tenantId": target["tenantId"],
                "subscriptionId": target["subscriptionId"],
                "estate": target["estate"],
                "objects": {},
            }
        )
        for key in ("tenantId", "subscriptionId", "estate"):
            if self.state[key] != target[key]:
                raise OperationError("Ownership ledger belongs to a different target")
        self._tokens: dict[str, tuple[str, float]] = {}

    def verify_target(self) -> None:
        account = command(["az", "account", "show", "--output", "json"])
        if not isinstance(account, dict) or (
            account["tenantId"] != self.target["tenantId"]
            or account["id"] != self.target["subscriptionId"]
            or account["environmentName"] != "AzureCloud"
        ):
            raise OperationError("Azure CLI is not on the approved commercial tenant/subscription")

    def token(self, resource: str) -> str:
        if resource not in self._tokens or self._tokens[resource][1] <= time.time() + 120:
            token = command(
                [
                    "az",
                    "account",
                    "get-access-token",
                    "--tenant",
                    self.target["tenantId"],
                    "--resource",
                    resource,
                    "--output",
                    "json",
                ]
            )
            if not isinstance(token, dict) or not token.get("accessToken"):
                raise OperationError("Azure CLI returned no access token")
            if not token.get("expires_on"):
                raise OperationError("Azure CLI returned no token expiry; update the CLI")
            self._tokens[resource] = (token["accessToken"], float(token["expires_on"]))
        return self._tokens[resource][0]

    def request(self, method: str, url: str, resource: str, body=None, *, _attempt=0):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
            raise OperationError("Refusing non-HTTPS or credential-bearing URL")
        allowed = {
            "https://graph.microsoft.com": {"graph.microsoft.com"},
            "https://management.azure.com/": {"management.azure.com"},
            "https://vault.azure.net": {
                f"{self.target.get('onboardingVault', '')}.vault.azure.net"
            },
        }
        if parsed.hostname not in allowed.get(resource, set()):
            raise OperationError("Destination is outside the configured service boundary")
        headers = {"Authorization": f"Bearer {self.token(resource)}"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        try:
            with build_opener(NoRedirect()).open(
                Request(url, data=data, headers=headers, method=method), timeout=60
            ) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise OperationError("Response exceeds the bounded size")
                return json.loads(raw) if raw else None
        except HTTPError as error:
            raise OperationError(
                f"{parsed.hostname} rejected {method}: HTTP {error.code}"
            ) from None
        except (URLError, OSError):
            if method == "GET" and _attempt < 2:
                logging.getLogger(__name__).warning(
                    "Read interrupted; retrying %s", parsed.hostname
                )
                time.sleep(_attempt + 1)
                return self.request(method, url, resource, body, _attempt=_attempt + 1)
            detail = "read failed" if method == "GET" else "write outcome may be uncertain"
            raise OperationError(f"{parsed.hostname} is unreachable; {detail}") from None

    def graph(self, method: str, path: str, body=None):
        if not path.startswith("/") or path.startswith("//"):
            raise OperationError("Graph path must be relative")
        return self.request(
            method, f"https://graph.microsoft.com/v1.0{path}", "https://graph.microsoft.com", body
        )

    def items(self, path: str) -> list:
        page = self.graph("GET", path)
        result = []
        seen = set()
        while True:
            if not isinstance(page, dict) or not isinstance(page.get("value"), list):
                raise OperationError("Malformed Graph collection")
            result.extend(page["value"])
            next_url = page.get("@odata.nextLink")
            if not next_url:
                return result
            if next_url in seen or len(seen) >= 100:
                raise OperationError("Invalid or excessive Graph pagination")
            seen.add(next_url)
            if not next_url.startswith("https://graph.microsoft.com/v1.0/"):
                raise OperationError("Graph pagination left the approved endpoint")
            page = self.request("GET", next_url, "https://graph.microsoft.com")

    def remember(self, key: str, value: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.state_path.with_suffix(".write.lock")
        for attempt in range(50):
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if attempt == 49:
                    raise OperationError(
                        "Ownership ledger is locked; inspect before retrying"
                    ) from None
                time.sleep(0.1)
        try:
            latest = (
                json.loads(self.state_path.read_text()) if self.state_path.exists() else self.state
            )
            for identity in ("tenantId", "subscriptionId", "estate"):
                if latest[identity] != self.target[identity]:
                    raise OperationError("Ownership ledger target changed")
            latest["objects"][key] = value
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(latest, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.state_path)
            self.state = latest
        finally:
            os.close(descriptor)
            lock.unlink()
