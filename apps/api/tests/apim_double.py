"""An in-process stand-in for the Azure Resource Manager APIM surface.

Tests drive the real ``ArmClient``/``ApimClient``/``InventoryCollector`` against this transport so
paging, error mapping, policy parsing, and AI detection are all covered by the same fixture.
"""

import json
import re
import time
from typing import Any
from xml.parsers import expat

import httpx
from azure.core.credentials import AccessToken

SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"
RESOURCE_GROUP = "rg-contoso-dev"
SERVICE_NAME = "apim-contoso-dev"
APIM_PRINCIPAL_ID = "11111111-1111-1111-1111-111111111111"
# A documentation address (RFC 5737), so no test ever names a real gateway's egress.
APIM_PUBLIC_IP = "203.0.113.10"
RESOURCE_ID = (
    f"/subscriptions/{SUBSCRIPTION_ID}"
    f"/resourceGroups/{RESOURCE_GROUP}"
    f"/providers/Microsoft.ApiManagement/service/{SERVICE_NAME}"
)

READER_PERMISSIONS = [
    {
        "actions": [
            "Microsoft.ApiManagement/service/*/read",
            "Microsoft.ApiManagement/service/read",
        ],
        "notActions": ["Microsoft.ApiManagement/service/users/keys/read"],
    }
]
CONTRIBUTOR_PERMISSIONS = [
    {"actions": ["Microsoft.ApiManagement/service/*"], "notActions": []}
]

CHAT_API_POLICY = """
<policies>
  <inbound>
    <base />
    <authentication-managed-identity resource="https://cognitiveservices.azure.com" />
    <llm-token-limit counter-key="@(context.Subscription.Id)" tokens-per-minute="10000"
                     estimate-prompt-tokens="true" />
    <set-header name="Authorization" exists-action="override">
      <value>Bearer sk-live-not-a-real-key</value>
    </set-header>
    <set-backend-service
      base-url="https://contoso-fn.azurewebsites.net/api?code=FunctionKeySecret" />
    <acme-custom-guard mode="strict" />
  </inbound>
</policies>
"""

GLOBAL_POLICY = """
<policies>
  <inbound>
    <base />
    <rate-limit calls="600" renewal-period="60" />
  </inbound>
</policies>
"""

MOSAIC_FRAGMENT = """
<fragment>
  <llm-token-limit counter-key="@(context.Subscription.Id)" tokens-per-minute="2000" />
</fragment>
"""

STABLE_API_VERSION = "2024-05-01"
MCP_API_VERSION = "2025-09-01-preview"
OPERATION_PREFIX = "/mosaic-test-operations/"

# Resources the fake serves without anything having written them, so a reference to one resolves.
STATIC_RESOURCES: frozenset[str] = frozenset(
    {
        "apis/chat-api",
        "apis/echo-api",
        "apis/orders-mcp",
        "apis/weather-mcp",
        "backends/foundry-pool",
        "products/gold",
        "policyFragments/mosaic-rate-standard",
    }
)
_BACKEND_REFERENCE = re.compile(r"<set-backend-service\b[^>]*?\bbackend-id=([\"'])(.*?)\1")
_FRAGMENT_REFERENCE = re.compile(r"<include-fragment\b[^>]*?\bfragment-id=([\"'])(.*?)\1")

# API Management parses a multi-statement policy expression, @{ ... }, with Razor. Razor refuses a
# control-flow statement whose body isn't a braced block, and API Management refuses the policy.
_SINGLE_STATEMENT = (
    'Expected a "{{" but found a "{found}". Block statements must be enclosed in "{{" and "}}". '
    "You cannot use single-statement control-flow statements in CSHTML pages. For example, the "
    "following is not allowed:\r\n\r\n@if(isLoggedIn)\r\n    <p>Hello, @user</p>\r\n\r\n"
    'Instead, wrap the contents of the block in "{{}}":\r\n\r\n@if(isLoggedIn) {{\r\n'
    "    <p>Hello, @user</p>\r\n}}"
)
_INVALID_FRAGMENT_EXPRESSION = "The policy fragment contains invalid policy expression."
# Statements whose parenthesized condition, when present, must be followed by a braced body.
_CONDITIONED_STATEMENTS = frozenset({"if", "for", "foreach", "while", "switch", "lock"})
_BRACKETS = {"(": ")", "[": "]", "{": "}"}
# Verbatim strings (@"..." with "" for a quote), then regular strings and characters, whose escapes
# are a backslash and the character after it. Interpolated strings are read as plain ones.
_CSHARP_LITERAL = re.compile(
    r'(?:\$@|@\$?)"(?:[^"]|"")*"|\$?"(?:[^"\\\r\n]|\\.)*"|\'(?:[^\'\\\r\n]|\\.)+\''
)
_CSHARP_LITERAL_START = re.compile(r"(?:\$@|@\$?|\$)?[\"']")
# Identifiers, keywords and numbers. A verbatim identifier such as @if keeps its @, so it is never
# mistaken for the keyword.
_CSHARP_WORD = re.compile(r"@?[A-Za-z_][A-Za-z0-9_]*|[0-9][A-Za-z0-9_.]*")


class _Refused(Exception):
    """Why Razor refuses an expression."""


def _csharp_tokens(code: str) -> list[str]:
    """C# tokens, with literals read whole and whitespace and comments dropped, as Razor reads them.

    A string or character literal is a single token, so a keyword, bracket or brace inside one is
    never read as code.
    """

    tokens: list[str] = []
    index = 0
    while index < len(code):
        if code[index].isspace():
            index += 1
        elif code.startswith("//", index):
            end = code.find("\n", index)
            index = len(code) if end < 0 else end + 1
        elif code.startswith("/*", index):
            end = code.find("*/", index + 2)
            if end < 0:
                raise _Refused(
                    "End of file was reached before the end of the block comment. All comments "
                    'started with "/*" sequence must be terminated with a matching "*/" sequence.'
                )
            index = end + 2
        elif literal := _CSHARP_LITERAL.match(code, index):
            tokens.append(literal.group())
            index = literal.end()
        elif _CSHARP_LITERAL_START.match(code, index):
            raise _Refused(
                'Unterminated string literal. Strings that start with a quotation mark (") must '
                "be terminated before the end of the line."
            )
        elif word := _CSHARP_WORD.match(code, index):
            tokens.append(word.group())
            index = word.end()
        else:
            tokens.append(code[index])
            index += 1
    return tokens


class _RazorCodeBlock:
    """Just enough of Razor's C# code parser to find a control-flow body that isn't a block.

    Like Razor, it reads statements only at the start of a statement: a lambda or initializer inside
    a statement, or inside a condition, is balanced and skipped rather than parsed.
    """

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self._index = 0

    def _peek(self) -> str:
        return self._tokens[self._index] if self._index < len(self._tokens) else ""

    def _accept(self, token: str) -> bool:
        if self._peek() != token:
            return False
        self._index += 1
        return True

    def block(self) -> None:
        if not self._accept("{"):
            raise _Refused(_SINGLE_STATEMENT.format(found=self._peek()))
        while self._peek() not in {"}", ""}:
            self._statement()
        if not self._accept("}"):
            raise _Refused('The code block is missing a closing "}" character.')

    def _statement(self) -> None:
        token = self._peek()
        if token == "{":
            self.block()
        elif token in _CONDITIONED_STATEMENTS or (token == "using" and self._next_is("(")):
            self._conditioned()
            while token == "if" and self._accept("else"):
                if self._peek() != "if":
                    self.block()
                    break
                self._conditioned()
        elif self._accept("do"):
            self.block()
            if self._accept("while"):
                self._condition()
                self._accept(";")
        elif self._accept("try"):
            self.block()
            # An exception filter, catch (...) when (...), is refused as System.Web.Razor refuses
            # it. Newer Razor reads one, but which parser API Management runs isn't documented.
            while self._peek() == "catch":
                self._conditioned()
            if self._accept("finally"):
                self.block()
        elif token in {"case", "default"}:
            # A switch label is read to its colon, so the statement after it is read as one.
            while self._peek() not in {":", "}", ""}:
                self._index += 1
            self._accept(":")
        else:
            while self._peek() not in {";", "}", ""}:
                if self._peek() in _BRACKETS:
                    self._balance()
                else:
                    self._index += 1
            self._accept(";")

    def _conditioned(self) -> None:
        """A keyword and its condition, then the braced body Razor requires after the condition.

        Razor leaves a keyword without a condition, such as a general ``catch``, to the C# compiler,
        so whatever follows it is read as the next statement.
        """

        self._index += 1
        if self._peek() == "(":
            self._balance()
            self.block()

    def _next_is(self, token: str) -> bool:
        return self._index + 1 < len(self._tokens) and self._tokens[self._index + 1] == token

    def _condition(self) -> None:
        if self._peek() == "(":
            self._balance()

    def _balance(self) -> None:
        left = self._peek()
        right = _BRACKETS[left]
        depth = 0
        while self._index < len(self._tokens):
            token = self._tokens[self._index]
            self._index += 1
            depth += (token == left) - (token == right)
            if depth == 0:
                return
        raise _Refused(f'An opening "{left}" is missing the corresponding closing "{right}".')


def expression_error(expression: str) -> str | None:
    """Why API Management refuses a multi-statement ``@{ ... }`` expression, or None.

    Razor requires the body of ``if (...)``, ``else``, ``for (...)``, ``foreach (...)``,
    ``while (...)``, ``do``, ``switch (...)``, ``lock (...)``, ``using (...)``, ``try``,
    ``catch (...)`` and ``finally`` to be a braced block: ``if (x) return y;`` is refused and
    ``if (x) { return y; }`` is not. Only Razor's parse is modelled, not the C# compiler that runs
    after it.
    """

    code = expression.strip()
    if not code.startswith("@{"):
        return None
    try:
        _RazorCodeBlock(_csharp_tokens(code[1:])).block()
    except _Refused as refused:
        return str(refused)
    return None


def policy_expression_error(xml: str) -> tuple[str, int, int, str] | None:
    """The first multi-statement expression in a policy that API Management refuses, if any.

    Returns the element holding it, the line and column where that element's name starts, and
    Razor's reason. A document that isn't well-formed XML is not examined.
    """

    found: list[tuple[str, int, int, str]] = []
    open_elements: list[tuple[str, int, int, list[str]]] = []
    parser = expat.ParserCreate()

    def start(name: str, attributes: dict[str, str]) -> None:
        line, column = parser.CurrentLineNumber, parser.CurrentColumnNumber + 2
        open_elements.append((name, line, column, []))
        found.extend((name, line, column, value) for value in attributes.values())

    def text(data: str) -> None:
        if open_elements:
            open_elements[-1][3].append(data)

    def end(_name: str) -> None:
        name, line, column, parts = open_elements.pop()
        found.append((name, line, column, "".join(parts)))

    parser.StartElementHandler = start
    parser.CharacterDataHandler = text
    parser.EndElementHandler = end
    try:
        parser.Parse(xml, True)
    except expat.ExpatError:
        return None
    for name, line, column, value in sorted(found, key=lambda item: item[1:3]):
        reason = expression_error(value)
        if reason is not None:
            return name, line, column, reason
    return None


class FakeCredential:
    def __init__(self) -> None:
        self.token_requests = 0

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        self.token_requests += 1
        return AccessToken("fake-token", int(time.time()) + 3600)

    async def close(self) -> None:
        return None


class FakeApim:
    """A small but realistic API Management instance."""

    def __init__(
        self,
        *,
        permissions: list[dict[str, Any]] | None = None,
        service_status: int = 200,
        permissions_status: int = 200,
        supports_mcp: bool = True,
        sku_name: str | None = "Developer",
    ) -> None:
        self.permissions = READER_PERMISSIONS if permissions is None else permissions
        self.service_status = service_status
        self.permissions_status = permissions_status
        # None omits the SKU entirely, as if ARM described the service without one.
        self.sku_name = sku_name
        # Mirrors a service that has not been upgraded to the preview management contract. Such a
        # service rejects the version outright rather than returning an empty list.
        self.supports_mcp = supports_mcp
        self.requests: list[str] = []
        self.http_calls: list[dict[str, Any]] = []
        self.failures: dict[str, int] = {}
        self.persistent_failures: dict[str, int] = {}
        # Writes are recorded in order so tests can assert dependency ordering and rollback
        # direction, and stored so a later read observes what a write created.
        self.writes: list[tuple[str, str]] = []
        self.written: dict[str, dict[str, Any]] = {}
        self.write_failures: dict[str, int] = {}
        self.write_failures_after: dict[str, tuple[int, int]] = {}
        # The error object a refused write's body carries, as Azure says why it refused it.
        self.write_errors: dict[str, dict[str, Any]] = {}
        # A resource removed just before another is written, as if someone else had deleted it.
        self.removals: dict[str, str] = {}
        self.delete_failures: dict[str, int] = {}
        self.async_writes: set[str] = set()
        self.operation_polls: dict[str, int] = {}
        self.operation_result: dict[str, str] = {}
        self.operation_errors: dict[str, dict[str, Any]] = {}
        # A policy fragment write is answered with a Location header and validated afterwards, as
        # API Management does. Each poll body is kept here under the asyncId its URL carries.
        self.location_operations: dict[str, dict[str, Any]] = {}
        self._async_ids = 0
        # Every write that named a resource that did not exist yet, as (written, named). API
        # Management fails a fragment that routes to a missing backend; other references are only
        # recorded, so a test can assert an apply created everything after what it names.
        self.dangling_references: list[tuple[str, str]] = []
        # ARM reports a system-assigned principal at the top level, but a user-assigned one only
        # under userAssignedIdentities. Tests override this to cover both shapes.
        self.identity: dict[str, Any] | None = {
            "type": "SystemAssigned",
            "principalId": APIM_PRINCIPAL_ID,
        }
        # No virtual network, as the live dev gateway has none. Tests override these to cover
        # private and firewalled model endpoints.
        self.virtual_network_type: str | None = "None"
        self.public_ip_addresses: list[str] = [APIM_PUBLIC_IP]
        # Set when a NAT gateway carries the service's outbound calls instead.
        self.outbound_public_ip_addresses: list[str] = []
        self.additional_locations: list[dict[str, Any]] = []
        self.tags: dict[str, str] | None = None

    def fail_once(self, path_suffix: str, status_code: int) -> None:
        self.failures[path_suffix] = status_code

    def fail_always(self, path_suffix: str, status_code: int) -> None:
        self.persistent_failures[path_suffix] = status_code

    def fail_write(
        self, path_suffix: str, status_code: int = 500, *, error: dict[str, Any] | None = None
    ) -> None:
        """Refuse every write of a resource, with ``error`` as the error object Azure returns."""

        self.write_failures[path_suffix] = status_code
        if error is not None:
            self.write_errors[path_suffix] = error

    def fail_write_after(
        self, path_suffix: str, successful_writes: int, status_code: int = 500
    ) -> None:
        self.write_failures_after[path_suffix] = (successful_writes, status_code)

    def remove_before_write(self, path_suffix: str, removed: str) -> None:
        """Delete ``removed`` when ``path_suffix`` is next written, as if someone else just had.

        An operator deleting a backend while an apply runs is how an update of the fragment that
        routes to it comes to name a backend that does not exist.
        """

        self.removals[path_suffix] = removed

    def fail_delete(self, path_suffix: str, status_code: int = 500) -> None:
        """Let a resource be created but refuse to remove it, so rollback itself has to fail."""

        self.delete_failures[path_suffix] = status_code

    def make_async(
        self,
        path_suffix: str,
        *,
        polls: int = 1,
        result: str = "Succeeded",
        error: dict[str, Any] | None = None,
    ) -> None:
        """Make one write return 202 and settle only after ``polls`` in-progress responses.

        ``error`` is the ``error`` object the settled operation reports, as Azure says why an
        operation failed. Policy fragments ignore this: they always answer through a Location
        poll, and fail it only when their routing names a missing backend or a policy expression
        is one API Management refuses.
        """

        self.async_writes.add(path_suffix)
        self.operation_polls[path_suffix] = polls
        self.operation_result[path_suffix] = result
        if error is not None:
            self.operation_errors[path_suffix] = error

    def seed(self, path_suffix: str, payload: dict[str, Any] | None = None) -> None:
        """Pretend a resource already exists, so a plan reports update rather than create."""

        self.written[path_suffix] = payload or {"properties": {}}

    def write_paths(self, method: str | None = None) -> list[str]:
        return [suffix for verb, suffix in self.writes if method is None or verb == method]

    def _maybe_fail(self, suffix: str) -> httpx.Response | None:
        status_code = self.persistent_failures.get(suffix) or self.failures.pop(suffix, None)
        if status_code is None:
            return None
        headers = {"Retry-After": "0"} if status_code == 429 else {}
        return httpx.Response(
            status_code,
            json={"error": {"message": f"injected {status_code}"}},
            headers=headers,
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        if path.startswith(OPERATION_PREFIX):
            return self._operation(path)
        if not path.startswith(RESOURCE_ID):
            return httpx.Response(404, json={"error": {"message": "unknown resource"}})
        suffix = path[len(RESOURCE_ID) :].strip("/")
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = request.content.decode(errors="replace")
        self.http_calls.append(
            {
                "method": request.method,
                "path": suffix,
                "api_version": request.url.params.get("api-version"),
                "body": body,
            }
        )
        if request.method in {"PUT", "DELETE"}:
            return self._write(request, suffix)
        async_id = request.url.params.get("azure-asyncId")
        if async_id is not None:
            return self._location_poll(async_id)
        injected = self._maybe_fail(suffix)
        if injected is not None:
            return injected
        api_version = request.url.params.get("api-version")
        if api_version == MCP_API_VERSION and not self.supports_mcp:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "InvalidApiVersionParameter",
                        "message": (
                            f"The api-version '{MCP_API_VERSION}' is invalid. "
                            "The supported versions are '2024-05-01'."
                        ),
                    }
                },
            )
        if suffix in self.written:
            return httpx.Response(200, json=self._written_resource(suffix))
        product_apis = self._written_product_apis(suffix)
        if product_apis is not None:
            return product_apis
        page = request.url.params.get("page")
        return self._route(suffix, page, request.url.params.get("$filter"))

    def _written_product_apis(self, suffix: str) -> httpx.Response | None:
        """Serve the product/API links a write created, so a re-plan sees its own effects."""

        if not suffix.startswith("products/") or not suffix.endswith("/apis"):
            return None
        product = suffix[: -len("/apis")]
        if product not in self.written:
            return None
        prefix = f"{suffix}/"
        return self._collection(
            [
                {"name": key[len(prefix) :], "properties": {}}
                for key in sorted(self.written)
                if key.startswith(prefix)
            ]
        )

    def _written_resource(self, suffix: str) -> dict[str, Any]:
        stored = self.written[suffix]
        return {"name": suffix.rsplit("/", 1)[-1], **stored}

    def _write(self, request: httpx.Request, suffix: str) -> httpx.Response:
        self.writes.append((request.method, suffix))
        removed = self.removals.pop(suffix, None)
        if removed is not None:
            self.written.pop(removed, None)
        failures = self.delete_failures if request.method == "DELETE" else self.write_failures
        delayed = self.write_failures_after.get(suffix)
        if delayed is not None and request.method == "PUT":
            remaining, delayed_status = delayed
            if remaining <= 0:
                status_code = delayed_status
            else:
                self.write_failures_after[suffix] = (remaining - 1, delayed_status)
                status_code = None
        else:
            status_code = failures.get(suffix)
        if status_code is not None:
            headers = {"Retry-After": "0"} if status_code == 429 else {}
            error = (self.write_errors.get(suffix) if request.method == "PUT" else None) or {
                "message": f"injected write {status_code}"
            }
            return httpx.Response(status_code, json={"error": error}, headers=headers)
        if request.method == "DELETE":
            existed = self.written.pop(suffix, None) is not None
            return httpx.Response(200 if existed else 204)
        try:
            body = json.loads(request.content) if request.content else {}
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        if suffix.startswith("policyFragments/"):
            return self._write_fragment(suffix, body)
        if suffix == "policies/policy" or suffix.endswith("/policies/policy"):
            refused = self._refused_policy(body)
            if refused is not None:
                return refused
        self._record_references(suffix, body)
        self.written[suffix] = body
        if suffix in self.async_writes:
            operation = f"{OPERATION_PREFIX}{len(self.writes)}"
            self.operation_polls[operation] = self.operation_polls.get(suffix, 1)
            self.operation_result[operation] = self.operation_result.get(suffix, "Succeeded")
            if suffix in self.operation_errors:
                self.operation_errors[operation] = self.operation_errors[suffix]
            return httpx.Response(
                202,
                json={},
                headers={
                    "Azure-AsyncOperation": f"https://management.azure.com{operation}",
                    "Retry-After": "0",
                },
            )
        return httpx.Response(200, json=self._written_resource(suffix))

    def _exists(self, suffix: str) -> bool:
        return suffix in self.written or suffix in STATIC_RESOURCES

    def _record_references(self, suffix: str, body: dict[str, Any]) -> None:
        """Note each resource this write names that does not exist yet."""

        parts = suffix.split("/")
        named = [f"{parts[0]}/{parts[1]}"] if len(parts) >= 4 else []
        if len(parts) == 4 and parts[0] == "products" and parts[2] == "apis":
            named.append(f"apis/{parts[3]}")
        properties = body.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        value = properties.get("value")
        if isinstance(value, str):
            named.extend(f"backends/{m.group(2)}" for m in _BACKEND_REFERENCE.finditer(value))
            named.extend(
                f"policyFragments/{m.group(2)}" for m in _FRAGMENT_REFERENCE.finditer(value)
            )
        scope = properties.get("scope")
        if isinstance(scope, str) and scope.casefold().startswith(RESOURCE_ID.casefold()):
            named.append(scope[len(RESOURCE_ID) :].strip("/"))
        self.dangling_references.extend(
            (suffix, reference) for reference in named if not self._exists(reference)
        )

    @staticmethod
    def _policy_xml(body: dict[str, Any]) -> str:
        properties = body.get("properties")
        value = properties.get("value") if isinstance(properties, dict) else None
        return value if isinstance(value, str) else ""

    def _refused_policy(self, body: dict[str, Any]) -> httpx.Response | None:
        """Refuse a policy document with an expression API Management can't parse, as it does.

        A policy is validated as it is written, so the PUT itself fails with a ValidationError
        naming the element, and nothing is stored.
        """

        refusal = policy_expression_error(self._policy_xml(body))
        if refusal is None:
            return None
        element, line, column, reason = refusal
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "ValidationError",
                    "message": "One or more fields contain incorrect values:",
                    "details": [
                        {
                            "code": "ValidationError",
                            "target": element,
                            "message": (
                                f"Error in element '{element}' on line {line}, column {column}: "
                                f"{reason}"
                            ),
                        }
                    ],
                }
            },
        )

    def _write_fragment(self, suffix: str, body: dict[str, Any]) -> httpx.Response:
        """Accept a policy fragment as API Management does, then validate it.

        API Management answers 201, or 200 when replacing, with only a Location header and checks
        the policy afterwards. A multi-statement expression Razor can't parse, or a
        ``set-backend-service`` naming a backend that does not exist, fails the operation: the
        Location poll reports ``Failed`` with the ValidationError Azure returns, and the new content
        is not stored. A failed create leaves no fragment, and a failed update leaves the previous
        one in place. A missing backend's error gives the line and column where the element's name
        starts.
        """

        existed = self._exists(suffix)
        self._async_ids += 1
        async_id = f"{self._async_ids:024x}"
        xml = self._policy_xml(body)
        refusal = policy_expression_error(xml)
        missing = next(
            (
                match
                for match in _BACKEND_REFERENCE.finditer(xml)
                if not self._exists(f"backends/{match.group(2)}")
            ),
            None,
        )
        if refusal is not None:
            # As the live gateway reported it: no element, line or detail, just Razor's reason.
            self.location_operations[async_id] = {
                "status": "Failed",
                "error": {
                    "code": "ValidationError",
                    "message": f"{_INVALID_FRAGMENT_EXPRESSION} {refusal[3]}",
                },
            }
        elif missing is None:
            self._record_references(suffix, body)
            self.written[suffix] = body
            self.location_operations[async_id] = self._written_resource(suffix)
        else:
            backend = missing.group(2)
            self.dangling_references.append((suffix, f"backends/{backend}"))
            line = xml.count("\n", 0, missing.start()) + 1
            column = missing.start() - (xml.rfind("\n", 0, missing.start()) + 1) + 2
            self.location_operations[async_id] = {
                "status": "Failed",
                "error": {
                    "code": "ValidationError",
                    "message": "One or more fields contain incorrect values:",
                    "details": [
                        {
                            "code": "ValidationError",
                            "target": "set-backend-service",
                            "message": (
                                f"Error in element 'set-backend-service' on line {line}, "
                                f"column {column}: Backend with id '{backend}' could not be "
                                "found."
                            ),
                        }
                    ],
                },
            }
        return httpx.Response(
            200 if existed else 201,
            json={"name": suffix.rsplit("/", 1)[-1], **body},
            headers={
                "Location": (
                    f"https://management.azure.com{RESOURCE_ID}/{suffix}"
                    f"?api-version={STABLE_API_VERSION}&azure-asyncId={async_id}"
                    "&format=rawxml"
                )
            },
        )

    def _location_poll(self, async_id: str) -> httpx.Response:
        body = self.location_operations.get(async_id)
        if body is None:
            return httpx.Response(404, json={"error": {"message": "operation not found"}})
        return httpx.Response(200, json=body)

    def _operation(self, path: str) -> httpx.Response:
        remaining = self.operation_polls.get(path, 0)
        if remaining > 0:
            self.operation_polls[path] = remaining - 1
            return httpx.Response(
                200, json={"status": "InProgress"}, headers={"Retry-After": "0"}
            )
        body: dict[str, Any] = {"status": self.operation_result.get(path, "Succeeded")}
        if path in self.operation_errors:
            body["error"] = self.operation_errors[path]
        return httpx.Response(200, json=body)

    def _route(
        self,
        suffix: str,
        page: str | None,
        api_filter: str | None,
    ) -> httpx.Response:
        if suffix == "":
            if self.service_status != 200:
                return httpx.Response(
                    self.service_status, json={"error": {"message": "denied"}}
                )
            return httpx.Response(200, json=self._service())
        if suffix == "providers/Microsoft.Authorization/permissions":
            if self.permissions_status != 200:
                return httpx.Response(
                    self.permissions_status, json={"error": {"message": "denied"}}
                )
            return httpx.Response(200, json={"value": self.permissions})
        if suffix == "apis":
            if api_filter and "mcp" in api_filter:
                return self._collection(self._mcp_servers())
            return self._paged_apis(page)

        routes = {
            "policies/policy": lambda: self._policy(GLOBAL_POLICY),
            "apis/chat-api": lambda: httpx.Response(200, json=self._api_definitions()[0]),
            "apis/chat-api/operations": lambda: self._collection(self._chat_operations()),
            "apis/chat-api/policies/policy": lambda: self._policy(CHAT_API_POLICY),
            "apis/echo-api": lambda: httpx.Response(200, json=self._api_definitions()[1]),
            "apis/echo-api/operations": lambda: self._collection(self._echo_operations()),
            "apis/echo-api/policies/policy": lambda: httpx.Response(
                404, json={"error": {"message": "policy not found"}}
            ),
            "apis/orders-mcp/tools": lambda: self._collection(self._mcp_tools()),
            "apis/weather-mcp/tools": lambda: self._collection([]),
            "products": lambda: self._collection(self._products()),
            "products/gold/apis": lambda: self._collection(
                [{"name": "chat-api", "properties": {}}]
            ),
            "products/gold/policies/policy": lambda: self._policy(GLOBAL_POLICY),
            "subscriptions": lambda: self._collection(self._subscriptions()),
            "users": lambda: self._collection(self._users()),
            "groups": lambda: self._collection(self._groups()),
            "groups/developers/users": lambda: self._collection(
                [{"name": "user-ada", "properties": {}}]
            ),
            "backends": lambda: self._collection(self._backends()),
            "namedValues": lambda: self._collection(self._named_values()),
            "policyFragments": lambda: self._collection(
                [{"name": "mosaic-rate-standard", "properties": {"description": "MOSAIC"}}]
            ),
            "policyFragments/mosaic-rate-standard": lambda: self._policy(MOSAIC_FRAGMENT),
        }
        route = routes.get(suffix)
        if route is None:
            return httpx.Response(404, json={"error": {"message": f"no route for {suffix}"}})
        return route()

    @staticmethod
    def _collection(values: list[dict[str, Any]]) -> httpx.Response:
        return httpx.Response(200, json={"value": values})

    @staticmethod
    def _policy(xml: str) -> httpx.Response:
        return httpx.Response(200, json={"properties": {"value": xml, "format": "rawxml"}})

    def _service(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "provisioningState": "Succeeded",
            "gatewayUrl": f"https://{SERVICE_NAME}.azure-api.net",
            "publicIPAddresses": list(self.public_ip_addresses),
        }
        if self.virtual_network_type is not None:
            properties["virtualNetworkType"] = self.virtual_network_type
        if self.outbound_public_ip_addresses:
            properties["outboundPublicIPAddresses"] = list(self.outbound_public_ip_addresses)
        if self.additional_locations:
            properties["additionalLocations"] = list(self.additional_locations)
        service: dict[str, Any] = {
            "name": SERVICE_NAME,
            "location": "eastus2",
            "properties": properties,
        }
        if self.sku_name is not None:
            service["sku"] = {"name": self.sku_name, "capacity": 1}
        if self.identity is not None:
            service["identity"] = self.identity
        if self.tags is not None:
            service["tags"] = self.tags
        return service

    def _paged_apis(self, page: str | None) -> httpx.Response:
        chat, echo, mcp = self._api_definitions()
        if page is None:
            return httpx.Response(
                200,
                json={
                    "value": [chat],
                    "nextLink": (
                        f"https://management.azure.com{RESOURCE_ID}/apis"
                        "?api-version=2024-05-01&page=2"
                    ),
                },
            )
        return httpx.Response(200, json={"value": [echo, mcp]})

    @staticmethod
    def _api_definitions() -> list[dict[str, Any]]:
        return [
            {
                "name": "chat-api",
                "properties": {
                    "displayName": "Chat completions",
                    "path": "openai",
                    "protocols": ["https"],
                    "serviceUrl": "https://contoso.openai.azure.com/openai",
                    "apiRevision": "1",
                    "isCurrent": True,
                    "subscriptionRequired": True,
                },
            },
            {
                "name": "echo-api",
                "properties": {
                    "displayName": "Echo",
                    "path": "echo",
                    "protocols": ["https"],
                    "serviceUrl": "https://echo.contoso.com",
                    "apiRevision": "1",
                    "isCurrent": True,
                    "subscriptionRequired": True,
                },
            },
            {
                # An MCP server also appears in an unfiltered API listing. It must not be
                # collected as an ordinary API, or one resource shows up twice.
                "name": "orders-mcp",
                "properties": {
                    "type": "mcp",
                    "displayName": "Orders MCP",
                    "path": "orders-mcp",
                    "protocols": ["https"],
                    "isCurrent": True,
                    "subscriptionRequired": True,
                },
            },
        ]

    @staticmethod
    def _mcp_servers() -> list[dict[str, Any]]:
        return [
            {
                "name": "orders-mcp",
                "properties": {
                    "type": "mcp",
                    "displayName": "Orders MCP",
                    "path": "orders-mcp",
                    "protocols": ["https"],
                    "subscriptionRequired": True,
                },
            },
            {
                "name": "weather-mcp",
                "properties": {
                    "type": "mcp",
                    "displayName": "Weather MCP",
                    "path": "weather-mcp",
                    "protocols": ["https"],
                    "serviceUrl": "https://mcp.contoso.com?code=PassthroughSecret",
                    "subscriptionRequired": False,
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

    @staticmethod
    def _mcp_tools() -> list[dict[str, Any]]:
        return [
            {
                "name": "listOrders",
                "properties": {
                    "displayName": "listOrders",
                    "description": "List all orders for a customer",
                    "operationId": f"{RESOURCE_ID}/apis/echo-api/operations/get-echo",
                },
            }
        ]

    @staticmethod
    def _chat_operations() -> list[dict[str, Any]]:
        return [
            {
                "name": "chat-completions",
                "properties": {
                    "displayName": "Create chat completion",
                    "method": "POST",
                    "urlTemplate": "/deployments/{deployment}/chat/completions",
                },
            }
        ]

    @staticmethod
    def _echo_operations() -> list[dict[str, Any]]:
        return [
            {
                "name": "get-echo",
                "properties": {
                    "displayName": "Echo it back",
                    "method": "GET",
                    "urlTemplate": "/resource",
                },
            }
        ]

    @staticmethod
    def _products() -> list[dict[str, Any]]:
        return [
            {
                "name": "gold",
                "properties": {
                    "displayName": "Gold tier",
                    "description": "High volume access",
                    "state": "published",
                    "subscriptionRequired": True,
                    "approvalRequired": True,
                    "subscriptionsLimit": 10,
                },
            }
        ]

    @staticmethod
    def _subscriptions() -> list[dict[str, Any]]:
        return [
            {
                "name": "sub-ada",
                "properties": {
                    "displayName": "Ada research",
                    "scope": f"{RESOURCE_ID}/products/gold",
                    "state": "active",
                    "ownerId": f"{RESOURCE_ID}/users/user-ada",
                    "createdDate": "2026-01-15T10:30:00Z",
                },
            }
        ]

    @staticmethod
    def _users() -> list[dict[str, Any]]:
        return [
            {
                "name": "user-ada",
                "properties": {
                    "firstName": "Ada",
                    "lastName": "Lovelace",
                    "email": "ada@contoso.com",
                    "state": "active",
                    "identities": [
                        {"provider": "Aad", "id": "11111111-2222-3333-4444-555555555555"}
                    ],
                },
            }
        ]

    @staticmethod
    def _groups() -> list[dict[str, Any]]:
        return [
            {
                "name": "developers",
                "properties": {
                    "displayName": "Developers",
                    "type": "system",
                    "builtIn": True,
                },
            }
        ]

    @staticmethod
    def _backends() -> list[dict[str, Any]]:
        return [
            {
                "name": "foundry-pool",
                "properties": {
                    "title": "Foundry pool",
                    "url": "https://contoso.openai.azure.com/openai?sig=SasTokenSecret",
                    "protocol": "http",
                },
            }
        ]

    @staticmethod
    def _named_values() -> list[dict[str, Any]]:
        return [
            {
                "name": "openai-primary-key",
                "properties": {
                    "displayName": "openai-primary-key",
                    "secret": True,
                    "value": "sk-live-should-never-be-stored",
                    "tags": ["ai"],
                    "keyVault": {
                        "secretIdentifier": "https://kv-contoso.vault.azure.net/secrets/openai"
                    },
                },
            }
        ]


def build_client(fake: FakeApim, **kwargs: Any) -> tuple[httpx.AsyncClient, FakeCredential]:
    transport = httpx.MockTransport(fake.handler)
    return httpx.AsyncClient(transport=transport, **kwargs), FakeCredential()
