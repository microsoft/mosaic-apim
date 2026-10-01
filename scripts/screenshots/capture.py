"""Capture the README screenshots of MOSAIC from a fictional, locally seeded estate.

Starts the demo API (:mod:`scripts.screenshots.demo_api`) and both single-page apps, drives them
with Playwright, blurs anything that looks like an endpoint, identifier, address, or secret, and
writes PNGs to ``docs/images/screenshots``. Run it from the repository root; see
``docs/screenshots.md`` for the workflow::

    uv run --with "playwright>=1.58,<2" --with "pillow>=11,<12" \\
        python -m scripts.screenshots.capture
"""

import argparse
import contextlib
import io
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx
from PIL import Image, ImageFilter
from playwright.sync_api import Locator, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from scripts.screenshots.demo_api import (
    PARTNER_FOUNDRY_DEMO_KEY,
    PARTNER_FOUNDRY_URL,
    SENSITIVE_LITERALS,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = REPO_ROOT / "docs" / "images" / "screenshots"

App = Literal["console", "portal"]
Theme = Literal["light", "dark"]
Action = Callable[[Page], None]

# Resource, account, and host names from the demo estate. The patterns below catch the generic
# shapes; these catch the bare names wherever the UI shows them on their own.
RESOURCE_NAMES = [
    "apim-contoso-dev",
    "apim-contoso-ai-dev",
    "apim-contoso-partners",
    "rg-contoso-dev",
    "rg-contoso-ai-dev",
    "rg-contoso-partners",
    "rg-contoso-ai",
    "contoso-aoai",
    "contoso-foundry",
    "contoso-safety",
    "kv-contoso-ai",
    "fabrikam-foundry",
    "partner-models",
    "log-contoso-ai",
    "api://contoso-servicedesk",
]

# JavaScript regular expressions, matched case-insensitively against every visible text node and
# form value. Anything they match is blurred in the saved image.
REDACTION_PATTERNS = [
    # GUIDs: tenant, subscription, object, principal, and client IDs.
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    # Email addresses.
    r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
    # URLs and URIs, including api:// audiences.
    r"\b(?:https?|api|wss?)://[^\s\"'<>]+",
    # Azure resource IDs.
    r"/subscriptions/[^\s\"'<>]+",
    # Bare hostnames of the services MOSAIC talks to.
    r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:azure-api\.net|openai\.azure\.com|"
    r"cognitiveservices\.azure\.com|services\.ai\.azure\.com|vault\.azure\.net|contoso\.com)\b",
    # IPv4 addresses, such as gateway egress addresses.
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b",
    # MOSAIC record IDs and the APIM names derived from them, such as modelApi_<32 hex>,
    # gateway_<32 hex>, and mosaic-grant-<32 hex>.
    r"\b[a-z][\w-]*?[_-][0-9a-f]{24,}\b",
    r"\b[0-9a-f]{24,}\b",
    # The demo's placeholder keys and tokens.
    r"\bdemo[0-9a-z-]*(?:key|secret|token)[0-9a-z-]*\b",
]

# Pixels added around each blurred region, so anti-aliased glyph edges are covered too.
BLUR_PADDING = 3

FIND_SENSITIVE_TEXT = """
({literals, patterns, padding}) => {
  const expressions = patterns.map((source) => new RegExp(source, 'gi'))
  const needles = literals.map((literal) => literal.toLowerCase())
  const rects = []
  // An open modal dialog covers the page behind it. Regions found on that page are cut back to
  // the parts still visible around the dialog, so their blur doesn't smear over its content. The
  // cut leaves room for the padding the blur adds to each region.
  const dialogs = [...document.querySelectorAll('[role="dialog"], [role="alertdialog"]')]
    .filter((dialog) => dialog.getAttribute('aria-modal') === 'true')
    .filter((dialog) => dialog.getClientRects().length > 0)
    .map((dialog) => {
      const box = dialog.getBoundingClientRect()
      return {
        element: dialog,
        left: box.left - padding,
        top: box.top - padding,
        right: box.right + padding,
        bottom: box.bottom + padding,
      }
    })
  const around = (piece, cover) => {
    const right = piece.x + piece.width
    const bottom = piece.y + piece.height
    const apart = right <= cover.left || piece.x >= cover.right
      || bottom <= cover.top || piece.y >= cover.bottom
    if (apart) {
      return [piece]
    }
    const pieces = []
    if (piece.y < cover.top) {
      pieces.push({x: piece.x, y: piece.y, width: piece.width, height: cover.top - piece.y})
    }
    if (bottom > cover.bottom) {
      pieces.push({x: piece.x, y: cover.bottom, width: piece.width, height: bottom - cover.bottom})
    }
    const top = Math.max(piece.y, cover.top)
    const height = Math.min(bottom, cover.bottom) - top
    if (piece.x < cover.left) {
      pieces.push({x: piece.x, y: top, width: cover.left - piece.x, height})
    }
    if (right > cover.right) {
      pieces.push({x: cover.right, y: top, width: right - cover.right, height})
    }
    return pieces
  }
  const keep = (rect, owner) => {
    // A box with no area paints nothing, and its padded blur would only smear its neighbours.
    if (rect.width <= 0 || rect.height <= 0) return
    let pieces = [{x: rect.left, y: rect.top, width: rect.width, height: rect.height}]
    for (const dialog of dialogs) {
      if (!dialog.element.contains(owner)) {
        pieces = pieces.flatMap((piece) => around(piece, dialog))
      }
    }
    for (const piece of pieces) {
      if (piece.width > 0 && piece.height > 0) rects.push(piece)
    }
  }
  // A closed <details> paints only its summary, yet the browser still reports boxes for the rest
  // of its content. Blurring those smears whatever sits beneath the summary.
  const collapsed = (element) => {
    for (
      let details = element.closest('details:not([open])');
      details;
      details = details.parentElement ? details.parentElement.closest('details:not([open])') : null
    ) {
      const summary = details.querySelector(':scope > summary')
      if (!summary || !summary.contains(element)) return true
    }
    return false
  }
  const spans = (text) => {
    const found = []
    const lower = text.toLowerCase()
    for (const needle of needles) {
      let from = lower.indexOf(needle)
      while (from !== -1) {
        found.push([from, from + needle.length])
        from = lower.indexOf(needle, from + needle.length)
      }
    }
    for (const expression of expressions) {
      expression.lastIndex = 0
      let match
      while ((match = expression.exec(text)) !== null) {
        if (match[0].length === 0) {
          expression.lastIndex += 1
          continue
        }
        found.push([match.index, match.index + match[0].length])
      }
    }
    return found
  }
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.nodeValue
    if (!text || !text.trim() || !node.parentElement || collapsed(node.parentElement)) continue
    for (const [start, end] of spans(text)) {
      const range = document.createRange()
      range.setStart(node, start)
      range.setEnd(node, end)
      for (const rect of range.getClientRects()) keep(rect, node.parentElement)
    }
  }
  for (const field of document.querySelectorAll('input, textarea')) {
    if (collapsed(field)) continue
    if (field.value && spans(field.value).length > 0) keep(field.getBoundingClientRect(), field)
  }
  // A closed select shows its chosen option without a text node that has a layout box.
  for (const field of document.querySelectorAll('select')) {
    if (collapsed(field)) continue
    const text = field.selectedOptions[0]?.text
    if (text && spans(text).length > 0) keep(field.getBoundingClientRect(), field)
  }
  return rects
}
"""


@dataclass(frozen=True)
class Shot:
    """One screenshot: where it is, how to reach the state it shows, and how big it is."""

    name: str
    app: App
    path: str
    theme: Theme
    ready: str
    actions: Sequence[Action] = field(default_factory=tuple)
    height: int = 900


class CaptureError(RuntimeError):
    """A shot could not reach the state it shows; its unredacted page is kept for debugging."""


def _visible_text(page: Page, text: str) -> Locator:
    return page.get_by_text(text, exact=False).filter(visible=True).first


def click_tab(name: str) -> Action:
    def act(page: Page) -> None:
        page.get_by_role("tab", name=name, exact=True).click()

    return act


def click_button(name: str) -> Action:
    def act(page: Page) -> None:
        page.get_by_role("button", name=name).filter(visible=True).first.click()

    return act


def click_link(name: str) -> Action:
    def act(page: Page) -> None:
        page.get_by_role("link", name=name, exact=True).filter(visible=True).first.click()

    return act


def select_option(label: str, starts_with: str) -> Action:
    """Choose the first option of the labelled select whose text starts with ``starts_with``."""

    def act(page: Page) -> None:
        select = page.get_by_label(label, exact=True)
        texts: list[str] = select.locator("option").all_inner_texts()
        choice = next(text for text in texts if text.strip().startswith(starts_with))
        select.select_option(label=choice.strip())

    return act


def fill_text(label: str, text: str) -> Action:
    def act(page: Page) -> None:
        page.get_by_label(label, exact=True).filter(visible=True).first.fill(text)

    return act


def fill_textbox(name: str, text: str) -> Action:
    """Fill the text box with this accessible name, which leaves out a required field's marker."""

    def act(page: Page) -> None:
        page.get_by_role("textbox", name=name, exact=True).filter(visible=True).first.fill(text)

    return act


def fill_labelled(pattern: str, text: str) -> Action:
    """Fill the field whose label matches, such as a password field, which has no textbox role."""

    def act(page: Page) -> None:
        page.get_by_label(re.compile(pattern)).filter(visible=True).first.fill(text)

    return act


def click_card_button_matching(texts: Sequence[str], button_name: str) -> Action:
    def act(page: Page) -> None:
        _card_matching(page, texts).get_by_role("button", name=button_name).click()

    return act


def scroll_to_card_matching(texts: Sequence[str], margin: int = 24) -> Action:
    """Scroll the window so the first access card with all ``texts`` sits below the top."""

    def act(page: Page) -> None:
        box = _card_matching(page, texts).bounding_box()
        if box is None:
            raise CaptureError(f"No access card with {texts!r} has a layout box to scroll to")
        page.evaluate("(top) => window.scrollBy(0, top)", box["y"] - margin)

    return act


def _card_matching(page: Page, texts: Sequence[str]) -> Locator:
    cards = page.locator(".access-card")
    for text in texts:
        cards = cards.filter(has_text=text)
    return cards.first


def wait_for_text(text: str) -> Action:
    def act(page: Page) -> None:
        _visible_text(page, text).wait_for(timeout=30_000)

    return act


def scroll_to_text(text: str, margin: int = 24) -> Action:
    """Scroll the window so the first visible match sits ``margin`` pixels below the top."""

    def act(page: Page) -> None:
        box = _visible_text(page, text).bounding_box()
        if box is None:
            raise CaptureError(f"{text!r} has no layout box to scroll to")
        page.evaluate("(top) => window.scrollBy(0, top)", box["y"] - margin)

    return act


# The README embeds these by name. Keep the two in step: a renamed or removed shot here needs its
# README reference updated, and a new README screenshot needs an entry here.
OPEN_GATEWAY = (click_link("Contoso AI Gateway"), wait_for_text("Published by MOSAIC"))
# The overview's usage panels and gateway health load after its inventory counts.
DASHBOARD_LOADED = (wait_for_text("Contoso Support Copilot"), wait_for_text("Partner Gateway"))
TELEMETRY_READY = "This gateway is ready for usage analytics."

SHOTS: list[Shot] = [
    # The same view in both themes, shown side by side.
    Shot(
        "console-dashboard-light",
        "console",
        "/dashboard",
        "light",
        "MOSAIC groups",
        actions=DASHBOARD_LOADED,
        height=2000,
    ),
    Shot(
        "console-dashboard-dark",
        "console",
        "/dashboard",
        "dark",
        "MOSAIC groups",
        actions=DASHBOARD_LOADED,
        height=2000,
    ),
    Shot("portal-catalog-light", "portal", "/catalog", "light", "Docs search MCP", height=1040),
    Shot("portal-catalog-dark", "portal", "/catalog", "dark", "Docs search MCP", height=1040),
    # Capability galleries: light shots sit in the README's left column and dark in its right.
    Shot("console-gateways", "console", "/gateways", "light", "Partner Gateway"),
    Shot(
        "console-gateway-overview",
        "console",
        "/gateways",
        "dark",
        "Contoso AI Gateway",
        actions=OPEN_GATEWAY,
    ),
    Shot(
        "console-gateway-apis",
        "console",
        "/gateways",
        "light",
        "Contoso AI Gateway",
        actions=(*OPEN_GATEWAY, click_tab("APIs and endpoints")),
    ),
    Shot("console-models", "console", "/models", "dark", "GPT-4o mini"),
    Shot(
        "console-register-key-endpoint",
        "console",
        "/models?register=1",
        "light",
        "Register model endpoint",
        # Filled in and never submitted, so the estate is unchanged for the shots after it.
        actions=(
            click_tab("Azure AI with an API key"),
            fill_textbox("Endpoint URL", PARTNER_FOUNDRY_URL),
            # Masked in the field, as every pasted key is.
            fill_labelled(r"^API key", PARTNER_FOUNDRY_DEMO_KEY),
            fill_text("Deployment 1 name", "claude-sonnet-4-5"),
            fill_text("Deployment 1 model", "claude-sonnet-4-5"),
            click_button("Add a deployment"),
            fill_text("Deployment 2 name", "gpt-4-1-mini"),
            fill_text("Deployment 2 model", "gpt-4.1-mini"),
            select_option("Deployment 2 API", "Azure OpenAI"),
            fill_textbox("Display name", "Fabrikam partner Foundry"),
        ),
        height=1240,
    ),
    Shot(
        "console-key-endpoint",
        "console",
        "/models",
        "dark",
        "Fabrikam partner Foundry",
        actions=(
            click_button("Fabrikam partner Foundry"),
            wait_for_text("The endpoint accepts the key"),
            scroll_to_text("The endpoint accepts the key", margin=140),
        ),
        # Tall enough for the key check, each gateway's verdict, and the declared deployments, and
        # short enough to scroll the endpoints MOSAIC found out of view.
        height=1100,
    ),
    Shot(
        "console-mcps",
        "console",
        "/mcps",
        "light",
        "Contoso Docs Search",
        # Tall enough for the published server and every registered server with its environment.
        height=1500,
    ),
    Shot(
        "console-identity",
        "console",
        "/identity?tab=agents",
        "dark",
        "Market Research Agent",
        # Tall enough for all four seeded agents, ending just below the detail panel's type field.
        height=1012,
    ),
    Shot(
        "console-directory-picker",
        "console",
        "/identity?tab=agents",
        "light",
        "Market Research Agent",
        actions=(
            # On the Agents tab, the dialog opens with the directory search set to agents.
            click_button("Add agent"),
            fill_text("Directory search", "agent"),
            wait_for_text("Benefits Bot Agent"),
        ),
        height=1040,
    ),
    Shot(
        "console-security-group-members",
        "console",
        "/identity?tab=workloads",
        "dark",
        "Applications and security groups",
        actions=(
            fill_text("Filter by label, detail, object ID, or kind", "AI Model Users"),
            wait_for_text("Security group members"),
            wait_for_text("Market Research Agent"),
        ),
        # The Graph member list sits below the local label form, so the viewport takes the page.
        height=1776,
    ),
    Shot(
        "console-entitlements",
        "console",
        "/entitlements",
        "light",
        "Contoso Support Copilot",
        actions=(scroll_to_text("Desired intent and last recorded", margin=96),),
        # Tall enough to include the first APIM binding, on the eighth grant, beside its actions.
        height=1384,
    ),
    Shot(
        "console-overlapping-grants",
        "console",
        "/entitlements",
        "dark",
        "Overlapping grants",
        actions=(scroll_to_text("Overlapping grants", margin=64),),
        # Tall enough to include the group-versus-group entries after the direct-grant ones.
        height=1384,
    ),
    Shot(
        "console-mcp-publish",
        "console",
        "/mcps",
        "light",
        "Published MCP servers",
        actions=(
            scroll_to_text("Published MCP servers", margin=64),
            click_button("Plan and apply"),
            wait_for_text("MCP access changes"),
        ),
        height=1040,
    ),
    Shot(
        "console-unpublish-review",
        "console",
        "/models",
        "light",
        "GPT-4o mini",
        # Unpublish on the first row, GPT-4o, plans the unpublish and opens its review. The shot
        # never confirms it.
        actions=(click_button("Unpublish"), wait_for_text("What MOSAIC deletes")),
        height=1200,
    ),
    Shot(
        "console-analytics",
        "console",
        "/analytics",
        "dark",
        "Contoso Support Copilot",
        # Tall enough for the trend, the three rankings, and the gateway health below them.
        height=1500,
    ),
    Shot(
        "console-analytics-cost",
        "console",
        "/analytics?tab=cost",
        "light",
        "Month-end forecast",
        # Tall enough for this month's spend, the trend, and the cost of each model and deployment.
        height=1700,
    ),
    Shot(
        "console-pricing",
        "console",
        "/pricing",
        "dark",
        "priced today.",
        # One model version's prices, where the demo's scheduled GPT-4o rate shows as a change.
        actions=(fill_text("Find a model", "2024-11-20"), wait_for_text("Changes ")),
        height=1300,
    ),
    Shot(
        "console-analytics-consumers",
        "console",
        "/analytics?tab=consumers",
        "light",
        "Contoso Support Copilot",
        height=1300,
    ),
    Shot(
        "console-analytics-limits",
        "console",
        "/analytics?tab=limits",
        "dark",
        "Invoice Reconciliation Agent",
        height=1100,
    ),
    Shot(
        "console-gateway-telemetry",
        "console",
        "/gateways",
        "light",
        "Contoso AI Gateway",
        actions=(
            *OPEN_GATEWAY,
            wait_for_text(TELEMETRY_READY),
            scroll_to_text(TELEMETRY_READY, margin=110),
        ),
        height=1200,
    ),
    Shot(
        "console-analytics-reliability",
        "console",
        "/analytics?tab=reliability",
        "dark",
        "No grant for this caller",
        height=1200,
    ),
    Shot(
        "console-environments", "console", "/settings", "light", "needs classification", height=1178
    ),
    Shot(
        "console-environment-findings",
        "console",
        "/settings",
        "dark",
        "needs classification",
        height=924,
        actions=(wait_for_text("Limitations:"), scroll_to_text("Findings", margin=80)),
    ),
    Shot(
        "portal-access",
        "portal",
        "/access",
        "light",
        "My access",
        actions=(
            click_card_button_matching(("Model API", "Applied to APIM"), "Connection details"),
            wait_for_text("Base URL"),
            scroll_to_text("Model API", margin=90),
        ),
    ),
    Shot(
        "portal-mcp-connection",
        "portal",
        "/access",
        "dark",
        "Applied to APIM",
        actions=(
            click_card_button_matching(("MCP server", "Applied to APIM"), "Connection details"),
            wait_for_text("VS Code"),
            scroll_to_card_matching(("MCP server", "Applied to APIM"), margin=72),
        ),
        height=1240,
    ),
    Shot("portal-requests", "portal", "/requests", "dark", "Approved for the churn analysis"),
    Shot("portal-usage", "portal", "/usage", "light", "Busiest resource", height=1104),
    Shot(
        "portal-usage-resources",
        "portal",
        "/usage",
        "dark",
        "Busiest resource",
        actions=(scroll_to_text("By resource", margin=96),),
    ),
]


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _wait_until_ready(url: str, process: subprocess.Popen[bytes], name: str, log: Path) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{name} exited early; see {log}\n{log.read_text(errors='replace')}")
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{name} did not become ready at {url}; see {log}")


@contextlib.contextmanager
def _running(
    name: str, command: list[str], cwd: Path, ready_url: str, logs: Path
) -> Iterator[None]:
    log_path = logs / f"{name}.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
        try:
            _wait_until_ready(ready_url, process, name, log_path)
            yield
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _vite(app_dir: Path, port: int) -> list[str]:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node.js is required to serve the console and portal")
    vite = app_dir / "node_modules" / "vite" / "bin" / "vite.js"
    if not vite.exists():
        raise RuntimeError(f"Run `npm ci` in {app_dir.relative_to(REPO_ROOT)} first")
    return [node, str(vite), "--port", str(port), "--strictPort", "--host", "127.0.0.1"]


def _ensure_chromium() -> None:
    """Install Playwright's Chromium build if it is missing; a no-op once it is present."""

    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)


def _stale_images(output: Path) -> list[Path]:
    names = {f"{shot.name}.png" for shot in SHOTS}
    return sorted(path for path in output.glob("*.png") if path.name not in names)


def _config_script(api_port: int) -> str:
    return (
        "window.__MOSAIC_CONFIG__ = {"
        f"apiBaseUrl: 'http://127.0.0.1:{api_port}', authMode: 'local', "
        "entraTenantId: 'organizations', entraClientId: 'local-development', "
        "entraApiScope: 'api://local-development/access_as_user'};"
    )


def _blur(
    image: Image.Image, rects: list[dict[str, float]], padding: int = BLUR_PADDING
) -> Image.Image:
    """Pixelate then blur each region, so no glyph shape survives to be read back."""

    width, height = image.size
    for rect in rects:
        left = max(int(rect["x"]) - padding, 0)
        top = max(int(rect["y"]) - padding, 0)
        right = min(int(rect["x"] + rect["width"]) + padding, width)
        bottom = min(int(rect["y"] + rect["height"]) + padding, height)
        if right <= left or bottom <= top:
            continue
        region = image.crop((left, top, right, bottom))
        small = region.resize(
            (max((right - left) // 8, 1), max((bottom - top) // 8, 1)), Image.Resampling.BILINEAR
        )
        smeared = small.resize(region.size, Image.Resampling.BILINEAR)
        image.paste(smeared.filter(ImageFilter.GaussianBlur(3)), (left, top))
    return image


SPINNERS_GONE = """
() => ![...document.querySelectorAll('.fui-Spinner')].some(
  (element) => element.getClientRects().length > 0
)
"""


def _settle(page: Page) -> None:
    page.wait_for_load_state("networkidle")
    with contextlib.suppress(PlaywrightTimeoutError):
        page.wait_for_function(SPINNERS_GONE, timeout=15_000)
    page.wait_for_timeout(400)


def _capture_one(
    page: Page, shot: Shot, url: str, literals: list[str], output: Path, logs: Path
) -> Path:
    problems: list[str] = []
    page.on(
        "console",
        lambda message: (
            problems.append(f"console {message.type}: {message.text}")
            if message.type in {"error", "warning"}
            else None
        ),
    )
    page.on("pageerror", lambda error: problems.append(f"page error: {error}"))
    page.on(
        "response",
        lambda response: (
            problems.append(f"HTTP {response.status} {response.url}")
            if response.status >= 400
            else None
        ),
    )
    page.on("requestfailed", lambda request: problems.append(f"request failed: {request.url}"))
    try:
        page.goto(url + shot.path)
        _settle(page)
        _visible_text(page, shot.ready).wait_for(timeout=30_000)
        for action in shot.actions:
            action(page)
            _settle(page)
    except Exception as error:
        # Debug artefacts stay in the temporary log directory and are never redacted, so they
        # must not be copied into the repository.
        debug = logs / f"{shot.name}.failed.png"
        with contextlib.suppress(Exception):
            page.screenshot(path=debug, full_page=True)
        details = "\n".join(problems[-20:]) or "no browser errors were reported"
        raise CaptureError(f"{shot.name}: {error}\nPage at failure: {debug}\n{details}") from error
    rects: list[dict[str, float]] = page.evaluate(
        FIND_SENSITIVE_TEXT,
        {"literals": literals, "patterns": REDACTION_PATTERNS, "padding": BLUR_PADDING},
    )
    png = page.screenshot(animations="disabled", caret="hide")
    image = _blur(Image.open(io.BytesIO(png)).convert("RGB"), rects)
    target = output / f"{shot.name}.png"
    image.save(target, optimize=True)
    shown = target.resolve()
    if shown.is_relative_to(REPO_ROOT):
        shown = shown.relative_to(REPO_ROOT)
    print(f"captured {shown} ({len(rects)} regions blurred)")
    return target


def capture(
    shots: Sequence[Shot],
    *,
    output: Path,
    urls: dict[App, str],
    api_port: int,
    logs: Path,
    headed: bool = False,
) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    literals = sorted({*SENSITIVE_LITERALS, *RESOURCE_NAMES}, key=len, reverse=True)
    written: list[Path] = []
    failures: list[CaptureError] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        try:
            for shot in shots:
                context = browser.new_context(
                    viewport={"width": 1440, "height": shot.height},
                    device_scale_factor=1,
                    color_scheme=shot.theme,
                    reduced_motion="reduce",
                    locale="en-US",
                    timezone_id="UTC",
                )
                try:
                    context.add_init_script(f"localStorage.setItem('mosaic-theme', '{shot.theme}')")
                    context.route(
                        "**/config.js",
                        lambda route: route.fulfill(
                            content_type="application/javascript", body=_config_script(api_port)
                        ),
                    )
                    page = context.new_page()
                    page.set_default_timeout(45_000)
                    page.set_default_navigation_timeout(60_000)
                    written.append(_capture_one(page, shot, urls[shot.app], literals, output, logs))
                except CaptureError as error:
                    failures.append(error)
                    print(f"FAILED {error}", file=sys.stderr)
                finally:
                    context.close()
        finally:
            browser.close()
    if failures:
        raise CaptureError(f"{len(failures)} of {len(shots)} shots failed; see the errors above")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture MOSAIC's README screenshots.")
    parser.add_argument(
        "--only", action="append", default=[], metavar="SHOT", help="Capture only this shot"
    )
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--api-port", type=int, default=18000)
    parser.add_argument("--console-port", type=int, default=15173)
    parser.add_argument("--portal-port", type=int, default=15174)
    parser.add_argument("--headed", action="store_true", help="Show the browser while capturing")
    parser.add_argument(
        "--reuse-servers",
        action="store_true",
        help="Use a demo API and apps already running on the given ports instead of starting them",
    )
    parser.add_argument("--list", action="store_true", help="List the shots and exit")
    args = parser.parse_args(argv)

    if args.list:
        for shot in SHOTS:
            print(f"{shot.name:28} {shot.app:8} {shot.theme:6} {shot.path}")
        return 0
    unknown = set(args.only) - {shot.name for shot in SHOTS}
    if unknown:
        parser.error(f"unknown shots: {', '.join(sorted(unknown))}")
    shots = [shot for shot in SHOTS if not args.only or shot.name in args.only]
    if not args.reuse_servers:
        for port in (args.api_port, args.console_port, args.portal_port):
            if not _port_free(port):
                parser.error(f"port {port} is in use; pick another with the --*-port options")

    logs = Path(tempfile.mkdtemp(prefix="mosaic-screenshots-")).resolve()
    _ensure_chromium()
    api = [
        sys.executable,
        "-m",
        "scripts.screenshots.demo_api",
        "--port",
        str(args.api_port),
        "--console-port",
        str(args.console_port),
        "--portal-port",
        str(args.portal_port),
    ]
    urls: dict[App, str] = {
        "console": f"http://127.0.0.1:{args.console_port}",
        "portal": f"http://127.0.0.1:{args.portal_port}",
    }
    apps: dict[App, tuple[Path, int]] = {
        "console": (REPO_ROOT / "apps" / "web", args.console_port),
        "portal": (REPO_ROOT / "apps" / "portal", args.portal_port),
    }
    try:
        with contextlib.ExitStack() as stack:
            if not args.reuse_servers:
                stack.enter_context(
                    _running(
                        "api", api, REPO_ROOT, f"http://127.0.0.1:{args.api_port}/healthz", logs
                    )
                )
                for app, (app_dir, port) in apps.items():
                    stack.enter_context(
                        _running(app, _vite(app_dir, port), app_dir, urls[app], logs)
                    )
            capture(
                shots,
                output=args.output,
                urls=urls,
                api_port=args.api_port,
                logs=logs,
                headed=args.headed,
            )
    except Exception:
        print(f"Screenshot capture failed. Server logs and debug captures: {logs}", file=sys.stderr)
        raise
    shutil.rmtree(logs, ignore_errors=True)
    for stale in _stale_images(args.output):
        print(f"warning: {stale.name} is not in SHOTS; delete it if nothing embeds it anymore")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
