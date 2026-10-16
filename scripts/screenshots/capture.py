"""Capture the README screenshots of MOSAIC from a fictional, locally seeded estate.

Starts the demo API (:mod:`scripts.screenshots.demo_api`) and both single-page apps, drives them
with Playwright, blurs anything that looks like an endpoint, identifier, address, or secret, and
writes PNGs to ``docs/images/screenshots``. Run it from the repository root; see
``docs/screenshots.md`` for the workflow::

    uv run --with "playwright>=1.58,<2" \\
        python -m scripts.screenshots.capture
"""

import argparse
import contextlib
import io
import json
import re
import shutil
import socket
import subprocess
import sys
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
    EXTRA_DIRECTORY_OBJECTS,
    GROUPS,
    PARTNER_FOUNDRY_DEMO_KEY,
    PARTNER_FOUNDRY_URL,
    PEOPLE,
    SENSITIVE_LITERALS,
)
from scripts.screenshots.selectors import grant_option_matches

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = REPO_ROOT / "docs" / "images" / "screenshots"

App = Literal["console", "portal"]
Theme = Literal["light", "dark"]
Action = Callable[[Page], None]

# Resource, account, and host names from the demo estate. The patterns below catch the generic
# shapes; these catch the bare names wherever the UI shows them on their own.
RESOURCE_NAMES = [
    *(person.label for person in PEOPLE),
    *(person.label.lower().replace(" ", "-") for person in PEOPLE),
    *(person.display_name for person in EXTRA_DIRECTORY_OBJECTS),
    *(name for name, _description, _members in GROUPS),
    "Contoso AI Gateway",
    "Contoso Government Gateway",
    "Contoso Government Azure OpenAI",
    "Government Docs Search",
    "contoso-government-docs",
    "Contoso AI Dev Gateway",
    "Partner Gateway",
    "Contoso AI Foundry",
    "Contoso Azure OpenAI",
    "Fabrikam partner Foundry",
    "Contoso Docs Search",
    "IT Service Desk",
    "Sales CRM",
    "Docs search MCP",
    "Docs MCP",
    "Orders MCP",
    "contoso-docs",
    "contoso-servicedesk",
    "contoso-crm",
    "mosaic-mcp-metadata",
    "mosaic-blocked-cost-centers",
    "ai-starter",
    "ai-premium",
    "apim-contoso-dev",
    "apim-contoso-ai-dev",
    "apim-contoso-partners",
    "rg-contoso-dev",
    "rg-contoso-ai-dev",
    "rg-contoso-partners",
    "rg-contoso-ai",
    "contoso-aoai",
    # Longer names first, so a suffix of a regional account's name is never left showing.
    "contoso-foundry-ncus",
    "contoso-foundry-west",
    "contoso-foundry",
    "contoso-safety",
    "kv-contoso-ai",
    "kv-contoso-management",
    "apim-contoso-gov",
    "contoso-gov-aoai",
    "rg-contoso-gov",
    "fabrikam-foundry",
    "partner-models",
    "log-contoso-ai",
    "api://contoso-servicedesk",
    "Contoso Docs Search",
    "Docs search MCP",
    "Orders MCP",
    "IT service desk MCP",
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
    r"cognitiveservices\.azure\.com|services\.ai\.azure\.com|vault\.azure\.net|contoso\.com|"
    r"azure-api\.us|openai\.azure\.us|cognitiveservices\.azure\.us|"
    r"vault\.usgovcloudapi\.net|"
    r"communication\.azure\.com|azurecomm\.net)\b",
    # IPv4 addresses, such as gateway egress addresses.
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b",
    # MOSAIC record IDs and the APIM names derived from them, such as modelApi_<32 hex>,
    # gateway_<32 hex>, and mosaic-grant-<32 hex>.
    r"\b[a-z][\w-]*?[_-][0-9a-f]{24,}\b",
    r"\bmosaic-(?:contoso|mcp|pool)-[a-z0-9-]+\b",
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
  // A fixed or sticky bar, such as each app's top bar, paints over the page scrolled beneath it.
  // A region it hides is cut back as one behind a dialog is, but only where the bar is opaque and
  // really on top at that spot, so text it doesn't hide is still blurred.
  const opaque = (style) => {
    if (style.visibility !== 'visible' || Number(style.opacity) < 0.99) return false
    const match = style.backgroundColor.match(/^rgba?\\(([^)]*)\\)$/)
    if (!match) return false
    const channels = match[1].split(/[\\s,/]+/).filter(Boolean)
    return channels.length === 3 || (channels.length === 4 && Number(channels[3]) >= 0.99)
  }
  const bars = [...document.body.querySelectorAll('*')]
    .filter((element) => {
      const style = getComputedStyle(element)
      return (style.position === 'fixed' || style.position === 'sticky') && opaque(style)
    })
    .filter((element) => element.getClientRects().length > 0)
    .map((element) => {
      const box = element.getBoundingClientRect()
      return {
        element,
        box,
        left: box.left - padding,
        top: box.top - padding,
        right: box.right + padding,
        bottom: box.bottom + padding,
      }
    })
  const hiddenBy = (piece, bar) => {
    const left = Math.max(piece.x, bar.box.left)
    const right = Math.min(piece.x + piece.width, bar.box.right)
    const top = Math.max(piece.y, bar.box.top)
    const bottom = Math.min(piece.y + piece.height, bar.box.bottom)
    if (right <= left || bottom <= top) return false
    const topmost = document.elementFromPoint((left + right) / 2, (top + bottom) / 2)
    return topmost !== null && bar.element.contains(topmost)
  }
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
    for (const bar of bars) {
      if (!bar.element.contains(owner)) {
        pieces = pieces.flatMap((piece) => (hiddenBy(piece, bar) ? around(piece, bar) : [piece]))
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
  for (const element of document.querySelectorAll('.resource-offering')) {
    if (collapsed(element)) continue
    const range = document.createRange()
    range.selectNodeContents(element)
    for (const rect of range.getClientRects()) keep(rect, element)
  }
  // Runtime values may be placeholders rather than GUIDs or names from the seeded directory.
  const privateRuntimeLabels = ['Administrator context', 'Tenant ID', 'API base URL']
  for (const label of document.querySelectorAll('dt')) {
    if (!privateRuntimeLabels.includes(label.textContent.trim())) {
      continue
    }
    for (const value of label.parentElement.querySelectorAll(':scope > dd')) {
      if (collapsed(value)) continue
      const range = document.createRange()
      range.selectNodeContents(value)
      for (const rect of range.getClientRects()) keep(rect, value)
    }
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
  for (const avatar of document.querySelectorAll('.identity-avatar-user, .topbar-avatar')) {
    if (avatar.getClientRects().length > 0 && !collapsed(avatar)) {
      keep(avatar.getBoundingClientRect(), avatar)
    }
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
    width: int = 1440


class CaptureError(RuntimeError):
    """A shot could not reach the state it shows; its unredacted page is kept for debugging."""


def _visible_text(page: Page, text: str) -> Locator:
    return page.get_by_text(text, exact=False).filter(visible=True).first


def click_tab(name: str) -> Action:
    def act(page: Page) -> None:
        page.get_by_role("tab", name=name, exact=True).or_(
            page.get_by_role("button", name=name, exact=True)
        ).filter(visible=True).first.click()

    return act


def click_button(name: str | re.Pattern[str]) -> Action:
    def act(page: Page) -> None:
        page.get_by_role("button", name=name).filter(visible=True).first.click()

    return act


def select_first_resource(list_name: str) -> Action:
    def act(page: Page) -> None:
        page.get_by_role("list", name=list_name, exact=True).get_by_role("button").first.click()

    return act


def click_detail_menu_action(name: str) -> Action:
    def act(page: Page) -> None:
        page.get_by_role("button", name=re.compile(r"^More actions")).filter(
            visible=True
        ).first.click()
        page.get_by_role("menuitem", name=name, exact=True).click()

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


def open_grant_connection(model: str, cost_center: str, *, cloud: str | None = None) -> Action:
    def act(page: Page) -> None:
        page.get_by_role("button", name=f"View access for {model}", exact=True).click()
        _settle(page)
        dialog = page.get_by_role("dialog")
        select = dialog.get_by_role("combobox", name="Grant", exact=True)
        if select.count():
            choices = [
                text.strip()
                for text in select.locator("option").all_inner_texts()
                if grant_option_matches(text.strip(), cost_center, cloud)
            ]
            if len(choices) != 1:
                raise CaptureError(f"Expected one ready direct {model} grant in {cost_center}")
            select.select_option(label=choices[0])
        dialog.get_by_text(re.compile(rf"^{re.escape(cost_center)} ·")).filter(
            visible=True
        ).first.wait_for()
        dialog.get_by_role("button", name="Connection details", exact=True).click()

    return act


def select_access_offering(cloud: str) -> Action:
    def act(page: Page) -> None:
        select = page.get_by_role("dialog").get_by_role(
            "combobox", name="Access option", exact=True
        )
        choices = [
            text.strip()
            for text in select.locator("option").all_inner_texts()
            if cloud in text
        ]
        if len(choices) != 1:
            raise CaptureError(f"Expected one {cloud} access offering")
        select.select_option(label=choices[0])

    return act


def wait_for_text(text: str) -> Action:
    def act(page: Page) -> None:
        _visible_text(page, text).wait_for(timeout=30_000)

    return act


def review_demo_requests(page: Page) -> None:
    click_button("Bulk approval review")(page)
    _settle(page)
    # Two explicit requests keep the facts readable without changing any approval state.
    for checkbox in page.get_by_role("checkbox").all()[:2]:
        checkbox.check()
    click_button("Review 2 requests")(page)


def scroll_to_text(text: str, margin: int = 24) -> Action:
    """Scroll the window or open drawer so the text sits below its header."""

    def act(page: Page) -> None:
        box = _visible_text(page, text).bounding_box()
        if box is None:
            raise CaptureError(f"{text!r} has no layout box to scroll to")
        target = _visible_text(page, text)
        target.evaluate(
            """(element, margin) => {
              const drawer = element.closest('.resource-drawer-body')
              if (drawer) {
                drawer.scrollBy(0, element.getBoundingClientRect().top
                  - drawer.getBoundingClientRect().top - margin)
              } else {
                window.scrollBy(0, element.getBoundingClientRect().top - margin)
              }
            }""",
            margin,
        )

    return act


# The README embeds these by name. Keep the two in step: a renamed or removed shot here needs its
# README reference updated, and a new README screenshot needs an entry here.
OPEN_GATEWAY = (click_button("Contoso AI Gateway"), wait_for_text("Read-only"))
# The overview's usage panels, gateway health, and pools load after its inventory counts.
DASHBOARD_LOADED = (
    wait_for_text("Contoso Support Copilot"),
    wait_for_text("Partner Gateway"),
    wait_for_text("published pool"),
)
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
        height=2450,
    ),
    Shot(
        "console-dashboard-dark",
        "console",
        "/dashboard",
        "dark",
        "MOSAIC groups",
        actions=DASHBOARD_LOADED,
        height=2450,
    ),
    Shot("portal-catalog-light", "portal", "/catalog", "light", "Docs search MCP", height=1040),
    Shot("portal-catalog-dark", "portal", "/catalog", "dark", "Docs search MCP", height=1040),
    # Capability galleries: light shots sit in the README's left column and dark in its right.
    Shot("console-gateways", "console", "/gateways", "light", "Partner Gateway",
         actions=OPEN_GATEWAY, height=1600),
    Shot(
        "console-government-gateway", "console", "/gateways", "light",
        "Contoso Government Gateway",
        actions=(click_button("Contoso Government Gateway"), wait_for_text("Read-only")),
        height=1600,
    ),
    Shot(
        "console-gateway-overview",
        "console",
        "/gateways",
        "dark",
        "Contoso AI Gateway",
        actions=OPEN_GATEWAY,
        height=1600,
    ),
    Shot(
        "console-gateway-apis",
        "console",
        "/gateways",
        "light",
        "Contoso AI Gateway",
        actions=(
            *OPEN_GATEWAY, click_tab("APIs and endpoints"),
            scroll_to_text("Read-only · Synced from APIM.", margin=110),
        ),
        height=1300,
    ),
    Shot("console-models", "console", "/models", "dark", "GPT-4o mini",
         actions=(click_button("GPT-4o mini"),)),
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
        "/models?tab=endpoints",
        "dark",
        "Fabrikam partner Foundry",
        actions=(
            click_button("Fabrikam partner Foundry"),
            wait_for_text("The endpoint accepts the key"),
            scroll_to_text("The endpoint accepts the key", margin=140),
        ),
        # Include the key check, gateway verdicts, and declared deployments.
        height=1650,
    ),
    Shot(
        "console-pools",
        "console",
        "/pools",
        "light",
        "Claude evaluation",
        actions=(click_button("Anthropic Claude"),),
        height=1000,
    ),
    Shot(
        "console-pool-detail",
        "console",
        "/pools",
        "dark",
        "Anthropic Claude",
        actions=(
            click_button("Anthropic Claude"),
            click_button("Collapse Pools sections"), click_button("Collapse navigation"),
            click_tab("Models and deployments"),
            scroll_to_text("served by 5 active deployments", margin=160),
        ),
        # Include both models' deployments in the selected section.
        height=1482,
    ),
    Shot(
        "console-pool-access",
        "console",
        "/pools",
        "light",
        "Anthropic Claude",
        # The governed Claude pool's access card: how callers sign in, the grants the gateway
        # enforces on each model, and the cost center's pooled quota.
        actions=(
            click_button("Anthropic Claude"),
            click_button("Collapse Pools sections"), click_button("Collapse navigation"),
            click_tab("Access and connection"),
            wait_for_text("Grants in force"),
            scroll_to_text("Who can call it", margin=110),
        ),
        # Include authentication, grants, and pooled quotas in the selected detail.
        height=1500,
    ),
    Shot(
        "console-pool-health",
        "console",
        "/pools",
        "dark",
        "Anthropic Claude",
        # The Claude pool's last day from its attempt traces: how calls to each model ended, and
        # how each deployment answered, with North Central US throttling Opus and tripping its
        # breaker while the other regions take the retries.
        actions=(
            click_button("Anthropic Claude"),
            click_button("Collapse Pools sections"), click_button("Collapse navigation"),
            click_tab("Health"),
            wait_for_text("Breaker tripped"),
            scroll_to_text("calls to each model ended", margin=168),
        ),
        # Include both models' deployment attempts and the health footnote.
        height=1500,
    ),
    Shot(
        "console-pool-editor",
        "console",
        "/pools?tab=suggestions",
        "light",
        "Suggested pools",
        # Create pool on the Claude Haiku 4.5 suggestion opens the editor with the deployments the
        # gateway can use. The shot never saves the pool, so the estate is unchanged for the shots
        # after it.
        actions=(
            click_button("Anthropic"),
            click_button("Create pool for Anthropic on Contoso AI Gateway"),
            wait_for_text("Create a model pool"),
            click_tab("3. Models"),
            wait_for_text("Name callers send"),
        ),
        height=1200,
    ),
    Shot(
        "console-endpoint-pools",
        "console",
        "/models?tab=endpoints",
        "dark",
        "Model endpoints",
        # The selected endpoint serves both pools. Focus on their dependency summaries.
        actions=(
            click_button("Contoso AI Foundry"),
            wait_for_text("Used by pools"),
            scroll_to_text("Used by pools", margin=110),
        ),
        # Tall enough to end just below the card listing the pools.
        height=1080,
    ),
    Shot(
        "console-mcps",
        "console",
        "/mcps",
        "light",
        "Contoso Docs Search",
        actions=(click_button("Contoso Docs Search"),),
        height=1500,
    ),
    Shot(
        "console-identity",
        "console",
        "/identity?tab=agents",
        "dark",
        "Market Research Agent",
        actions=(click_button("Market Research Agent"),),
        height=1000,
    ),
    Shot(
        "console-people-search",
        "console",
        "/identity?tab=users",
        "light",
        "Megan Bowen",
        actions=(
            click_button("Add person"),
            fill_text("Directory search", "hen"),
            click_button("Select Henrietta Mueller"),
            wait_for_text("Selected:"),
        ),
        height=1000,
    ),
    Shot(
        "console-identity-detail",
        "console",
        "/identity?tab=agents",
        "dark",
        "Market Research Agent",
        actions=(
            click_button("Market Research Agent"),
            wait_for_text("Edit local metadata"),
        ),
        height=1000,
    ),
    Shot(
        "console-directory-picker",
        "console",
        "/identity?tab=agents",
        "light",
        "Market Research Agent",
        actions=(
            # The onboarding workspace starts with the directory search set to agents.
            click_button("Add agent"),
            fill_text("Directory search", "agent"),
            wait_for_text("Benefits Bot Agent"),
            click_button("Select Benefits Bot Agent"),
        ),
        height=1040,
    ),
    Shot(
        "console-security-group-members",
        "console",
        "/identity?tab=security-groups",
        "dark",
        "Entra security groups",
        actions=(
            fill_text("Filter by label, detail, object ID, or kind", "AI Model Users"),
            click_button("AI Model Users"),
            wait_for_text("Security group members"),
            wait_for_text("Market Research Agent"),
            scroll_to_text("Security group members", margin=110),
        ),
        height=1300,
    ),
    Shot(
        "console-entitlements",
        "console",
        "/entitlements",
        "light",
        "Contoso Support Copilot",
        actions=(select_first_resource("Grants"),),
        height=1100,
    ),
    Shot(
        "console-access-requests",
        "console",
        "/entitlements?tab=requests",
        "light",
        "Access requests",
        actions=(select_first_resource("Access requests"),),
        height=1250,
    ),
    Shot(
        "console-bulk-approval",
        "console",
        "/entitlements?tab=requests",
        "dark",
        "Access requests",
        actions=(review_demo_requests, wait_for_text("Compatible shared limits")),
        height=1950,
    ),
    Shot(
        "console-approval-policy",
        "console",
        "/entitlements?tab=approvalPolicies",
        "light",
        "Development office hours",
        actions=(
            click_button("Development office hours"),
            click_button("Edit policy and dry-run"),
            wait_for_text("Bounded grant template"),
        ),
        height=2780,
    ),
    Shot(
        "console-notification-inbox",
        "console",
        "/notifications",
        "dark",
        "Administrator notifications",
        actions=(select_first_resource("Notifications"),),
        height=1150,
    ),
    Shot(
        "console-notification-preferences",
        "console",
        "/profile",
        "light",
        "Personal notifications",
        actions=(click_button("Edit preferences"),),
        height=2070,
    ),
    Shot(
        "console-notification-readiness",
        "console",
        "/settings?tab=notifications&notificationView=health",
        "dark",
        "Notification readiness",
        actions=(wait_for_text("Refresh checks"),),
        height=2200,
    ),
    Shot(
        "console-model-access-settings",
        "console",
        "/entitlements?tab=settings",
        "dark",
        "Published model",
        actions=(select_option("Published model", "GPT-4o"),),
        height=1250,
    ),
    Shot(
        "console-overlapping-grants",
        "console",
        "/entitlements?tab=effective",
        "dark",
        "Overlapping grants",
        actions=(scroll_to_text("Overlapping grants", margin=64),),
        # Tall enough to include the group-versus-group entries after the direct-grant ones.
        height=1384,
    ),
    Shot(
        "console-cost-centers",
        "console",
        "/cost-centers",
        "light",
        "Customer Insights",
        actions=(click_button("Customer Insights"),),
        height=1050,
    ),
    Shot(
        "console-cost-center-detail",
        "console",
        "/cost-centers",
        "dark",
        "Customer Insights",
        actions=(
            click_button("Customer Insights"),
            click_tab("Members"),
            wait_for_text("Membership"),
        ),
        height=1400,
    ),
    Shot(
        "console-mcp-publish",
        "console",
        "/mcps",
        "light",
        "Published MCP servers",
        actions=(
            click_button("Contoso Docs Search"),
            click_button("Review changes"),
            wait_for_text("MCP access changes"),
        ),
        height=3000,
    ),
    Shot(
        "console-unpublish-review",
        "console",
        "/models",
        "light",
        "GPT-4o mini",
        # Unpublish on the first row, GPT-4o, plans the unpublish and opens its review. The shot
        # never confirms it.
        actions=(
            select_first_resource("Published models"),
            click_detail_menu_action("Unpublish"),
            wait_for_text("What MOSAIC deletes"),
        ),
        height=1200,
    ),
    Shot(
        "console-budgets",
        "console",
        "/cost-centers",
        "dark",
        "Customer Support",
        # Customer Support's budget, past its limit and blocked: the gateway refusing its calls and
        # this month's emails, beside the amount, thresholds, recipients, and action.
        actions=(
            click_button("Customer Support"),
            click_tab("Budget"),
            wait_for_text("Refusing calls"),
            scroll_to_text("A monthly amount in US dollars", margin=110),
        ),
        height=1200,
    ),
    Shot(
        "console-analytics-filters",
        "console",
        "/analytics",
        "light",
        "Filters",
        actions=(click_button("Filters"),),
        height=1000,
    ),
    Shot(
        "console-request-notifications",
        "console",
        "/dashboard",
        "light",
        "MOSAIC groups",
        actions=(*DASHBOARD_LOADED, click_button(re.compile(r"^Pending approvals"))),
        height=1000,
    ),
    Shot(
        "console-new-cost-center",
        "console",
        "/cost-centers",
        "dark",
        "Cost centers",
        actions=(click_button("New cost center"),),
        height=1000,
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
        "console-analytics-mcp-model-use",
        "console",
        "/analytics?tab=consumers",
        "dark",
        "Contoso Support Copilot",
        # The people the Docs Search server's model calls were made for, below the consumers that
        # already count those calls as its own, and the references MOSAIC couldn't use.
        actions=(
            wait_for_text("Docs Search service"),
            # Below the sticky top bar, with the card's heading above its description.
            scroll_to_text("Model calls an MCP server", margin=150),
        ),
        height=760,
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
        "console-azure-connections", "console", "/settings?tab=connections", "dark",
        "Government management", height=1100,
    ),
    Shot(
        "console-environment-findings",
        "console",
        "/settings?tab=findings",
        "dark",
        "Findings",
        height=924,
        actions=(wait_for_text("Limitations:"), scroll_to_text("Findings", margin=80)),
    ),
    Shot(
        "console-runtime-configuration",
        "console",
        "/settings?tab=runtime",
        "light",
        "Directory lookup is enabled with Entra authentication",
        height=1200,
    ),
    Shot(
        "console-email-settings",
        "console",
        "/settings?tab=email",
        "light",
        "Send test email",
        # Budget email through Communication Services, turned on, with its last test email.
        actions=(
            wait_for_text("Send test email"),
            scroll_to_text("Budget warnings and blocks are emailed", margin=150),
        ),
        height=548,
    ),
    Shot(
        "portal-notifications",
        "portal",
        "/notifications",
        "light",
        "Your persistent inbox",
        actions=(wait_for_text("Mark read"),),
        height=1280,
    ),
    Shot(
        "portal-notification-preferences",
        "portal",
        "/notification-preferences",
        "dark",
        "Categories & channels",
        height=1800,
    ),
    Shot(
        "portal-notification-digests",
        "portal",
        "/notification-preferences",
        "light",
        "Categories & channels",
        actions=(scroll_to_text("Digest schedules", margin=105),),
        height=1430,
    ),
    Shot(
        "portal-request-access",
        "portal",
        "/catalog",
        "light",
        "Docs search MCP",
        actions=(
            click_button("View access options for gpt-4o-mini"),
            select_access_offering("Azure Commercial"),
        ),
        height=1100,
    ),
    Shot(
        "portal-access",
        "portal",
        "/access",
        "light",
        "My access",
        # Megan's GPT-4o grant under Customer Insights, the first applied model grant on the page.
        actions=(
            open_grant_connection("gpt-4o", "Customer Insights"),
            wait_for_text("Base URL"),
        ),
    ),
    Shot(
        "portal-connection-cost-center",
        "portal",
        "/access",
        "light",
        "My access",
        # Megan's own GPT-4o grant under General: the cost-center header and token call sample.
        actions=(
            open_grant_connection("gpt-4o", "General"),
            wait_for_text("Cost center header"),
            scroll_to_text("Cost center header", margin=96),
        ),
        height=1240,
    ),
    Shot(
        "portal-pool-connection",
        "portal",
        "/access",
        "dark",
        "My access",
        # Authentication, the key also used for Claude Opus 4.5, and call samples. Nothing names
        # the pool, its deployments, or their regions.
        actions=(
            open_grant_connection("claude-sonnet-4-5", "Customer Insights"),
            wait_for_text("This key also works for"),
            scroll_to_text("Authentication", margin=24),
        ),
        # The shared-key note and the curl and Python examples.
        height=1700,
    ),
    Shot(
        "portal-mcp-connection",
        "portal",
        "/access",
        "dark",
        "Applied to APIM",
        actions=(
            open_grant_connection("Contoso Docs Search", "Customer Insights"),
            wait_for_text("VS Code"),
        ),
        height=1240,
    ),
    Shot(
        "portal-government-connection", "portal", "/access", "light", "My access",
        actions=(
            open_grant_connection("gpt-4o-mini", "Customer Insights", cloud="Azure Government"),
            wait_for_text("Authentication"),
        ),
        height=1420,
    ),
    Shot(
        "portal-government-mcp-connection", "portal", "/access", "dark", "My access",
        actions=(
            open_grant_connection(
                "Government Docs Search", "Customer Insights", cloud="Azure Government"
            ),
            wait_for_text("VS Code"),
        ),
        height=1420,
    ),
    Shot("portal-requests", "portal", "/requests", "dark", "Approved for the churn analysis"),
    # Tall enough for the budget banner, the figures, the daily trend, and the last 24 hours.
    Shot("portal-usage", "portal", "/usage", "light", "Busiest resource", height=1400),
    Shot(
        "portal-usage-resources",
        "portal",
        "/usage?tab=resources",
        "dark",
        "By resource",
        # Each of Megan's cost centers' totals this month, then her own usage by resource.
        actions=(),
        height=1240,
    ),
    Shot(
        "portal-usage-breakdowns",
        "portal",
        "/usage?tab=breakdowns",
        "light",
        "Cost centers",
        actions=(scroll_to_text("Each cost center's total this month", margin=150),),
        height=1100,
    ),
    # Customer Insights, which Megan charges, has used most of its budget, and blocks at 100%.
    Shot(
        "portal-budget-banner", "portal", "/access", "light", "near its monthly budget", height=720
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


def _vite(app_dir: Path, port: int, preview: bool = False) -> list[str]:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node.js is required to serve the console and portal")
    vite = app_dir / "node_modules" / "vite" / "bin" / "vite.js"
    if not vite.exists():
        raise RuntimeError(f"Run `npm ci` in {app_dir.relative_to(REPO_ROOT)} first")
    if preview and not (app_dir / "dist" / "index.html").exists():
        raise RuntimeError(f"Run `npm run build` in {app_dir.relative_to(REPO_ROOT)} first")
    mode = ["preview"] if preview else []
    return [node, str(vite), *mode, "--port", str(port), "--strictPort", "--host", "127.0.0.1"]


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


def _assert_no_document_overflow(
    page: Page, context: str, *, expected_width: int | None = None
) -> None:
    geometry = page.evaluate(
        """() => {
          const root = document.documentElement
          const width = root.clientWidth
          const scrollX = window.scrollX
          if (root.scrollWidth <= width + 1 && scrollX === 0) {
            return {clientWidth: width, scrollWidth: root.scrollWidth, scrollX, offenders: []}
          }
          const offenders = [...document.querySelectorAll('body *')].flatMap(element => {
            if (!element.getClientRects().length) return []
            const rect = element.getBoundingClientRect()
            if (rect.right + scrollX <= width + 1 && rect.left + scrollX >= -1) return []
            return [{
              element: element.tagName.toLowerCase() + '.' + [...element.classList].join('.'),
              left: Math.round(rect.left + scrollX),
              right: Math.round(rect.right + scrollX),
            }]
          }).slice(0, 8)
          return {clientWidth: width, scrollWidth: root.scrollWidth, scrollX, offenders}
        }"""
    )
    if (
        geometry["scrollWidth"] > geometry["clientWidth"] + 1
        or geometry["scrollX"] != 0
        or expected_width is not None and (
            geometry["clientWidth"] != expected_width
            or geometry["scrollWidth"] != expected_width
        )
    ):
        raise CaptureError(
            f"{context}: document overflows horizontally: {json.dumps(geometry)}"
        )


def _check_local_table_scroll(page: Page, context: str, *, require_scroll: bool = False) -> None:
    checked = 0
    for container in page.locator(".table-scroll").all():
        if not container.is_visible() or not container.evaluate(
            "(element) => element.scrollWidth > element.clientWidth + 1"
        ):
            continue
        for operation in ("direct scrolling", "scrollIntoView"):
            container.evaluate(
                """(element, operation) => {
                  element.scrollLeft = 0
                  if (operation === 'direct scrolling') element.scrollLeft = element.scrollWidth
                  else element.querySelector('table tr:last-child > :last-child')
                    .scrollIntoView({block: 'nearest', inline: 'end'})
                }""",
                operation,
            )
            _settle(page)
            if container.evaluate("(element) => element.scrollLeft") <= 0:
                raise CaptureError(f"{context}: local table {operation} did not reveal its last column")
            _assert_no_document_overflow(
                page, f"{context}: table {operation}", expected_width=1440
            )
        container.evaluate("(element) => { element.scrollLeft = 0 }")
        _assert_no_document_overflow(page, f"{context}: table reset", expected_width=1440)
        checked += 1
    if require_scroll and checked == 0:
        raise CaptureError(f"{context}: expected a locally scrollable table")
    print(f"verified {context}: document width 1440px; {checked} local tables and scrollIntoView")


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
        page.mouse.move(0, 0)
        _settle(page)
        if shot.app == "console":
            _assert_no_document_overflow(page, shot.name, expected_width=shot.width)
    except Exception as error:
        # Debug artefacts stay in the capture log directory and are never redacted, so they
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
    staged = output / f".{shot.name}.capture.png"
    try:
        image.save(staged, optimize=True)
        staged.replace(target)
    finally:
        staged.unlink(missing_ok=True)
    shown = target.resolve()
    if shown.is_relative_to(REPO_ROOT):
        shown = shown.relative_to(REPO_ROOT)
    print(f"captured {shown} ({len(rects)} regions blurred)")
    return target


def _check_portal_appearance(page: Page, theme: Theme) -> None:
    origin = "/".join(page.url.split("/")[:3])
    expected = "rgb(255, 255, 255)" if theme == "light" else "rgb(28, 37, 49)"
    session = page.context.new_cdp_session(page)
    try:
        for width, height in ((1440, 900), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            for route, surface in (
                ("/usage", ".usage-kpi-card"),
                ("/catalog", ".resource-list"),
                ("/access", ".resource-list"),
                ("/requests", ".request-card"),
            ):
                page.goto(origin + route)
                _settle(page)
                page.locator(surface).first.wait_for()
                if page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
                    raise CaptureError(f"portal: {route} overflows at {width}px in {theme}")
                for selector in (".sidebar", ".topbar"):
                    chrome = page.locator(selector).evaluate(
                        """element => {
                          const style = getComputedStyle(element);
                          const probe = document.createElement('span');
                          probe.style.backgroundColor = 'var(--mosaic-glass)';
                          element.append(probe);
                          const glass = getComputedStyle(probe).backgroundColor;
                          probe.remove();
                          return { blur: style.backdropFilter, background: style.backgroundColor,
                            glass };
                        }"""
                    )
                    if (
                        chrome["blur"] != "blur(20px) saturate(1.4)"
                        or chrome["background"] != chrome["glass"]
                    ):
                        raise CaptureError(
                            f"portal: {route} {selector} does not use {theme} glass chrome: "
                            f"{chrome}"
                        )
                for selector in (surface, ".fui-FluentProvider"):
                    style = page.locator(selector).first.evaluate(
                        """element => {
                          const style = getComputedStyle(element);
                          return {background: style.backgroundColor, radius: style.borderRadius,
                            padding: style.padding};
                        }"""
                    )
                    if style["background"] != expected:
                        raise CaptureError(
                            f"portal: {route} {selector} is not an opaque {theme} surface"
                        )
                    if selector == surface and style["radius"] != "12px":
                        raise CaptureError(f"portal: {route} does not use rounded panels")
                    if (
                        surface in (".usage-kpi-card", ".request-card")
                        and selector == surface
                        and (style["padding"] != "20px")
                    ):
                        raise CaptureError(
                            f"portal: {route} Fluent card styles override panel spacing"
                        )
            page.goto(origin + "/catalog")
            _settle(page)
            page.get_by_role("button", name=re.compile("^View access options for ")).first.click()
            drawer = page.get_by_role("dialog")
            drawer.wait_for()
            background = drawer.evaluate("element => getComputedStyle(element).backgroundColor")
            if background != expected:
                raise CaptureError(f"portal: {theme} detail drawer is not opaque")
            page.keyboard.press("Escape")
            drawer.wait_for(state="hidden")

        for feature, value in (
            ("prefers-reduced-transparency", "reduce"),
            ("forced-colors", "active"),
        ):
            session.send(
                "Emulation.setEmulatedMedia",
                {"features": [{"name": feature, "value": value}]},
            )
            for selector in (".sidebar", ".topbar"):
                style = page.locator(selector).evaluate(
                    """element => {
                      const style = getComputedStyle(element);
                      return {blur: style.backdropFilter, background: style.backgroundColor};
                    }"""
                )
                if style["blur"] != "none":
                    raise CaptureError(f"portal: {feature} did not disable {selector} blur")
                if feature == "prefers-reduced-transparency" and style["background"] != expected:
                    raise CaptureError(f"portal: reduced transparency left {selector} translucent")
            session.send("Emulation.setEmulatedMedia", {"features": []})

        page.evaluate(
            """() => {
              for (const sheet of document.styleSheets) {
                for (let index = sheet.cssRules.length - 1; index >= 0; index--) {
                  const rule = sheet.cssRules[index];
                  if (rule instanceof CSSSupportsRule &&
                      rule.conditionText.includes('backdrop-filter')) {
                    sheet.deleteRule(index);
                  }
                }
              }
            }"""
        )
        for selector in (".sidebar", ".topbar"):
            style = page.locator(selector).evaluate(
                """element => {
                  const style = getComputedStyle(element);
                  return {blur: style.backdropFilter, background: style.backgroundColor};
                }"""
            )
            if style["blur"] != "none" or style["background"] != expected:
                raise CaptureError(f"portal: unsupported blur did not leave {selector} solid")
    finally:
        session.send("Emulation.setEmulatedMedia", {"features": []})
        session.detach()
    print(f"verified portal {theme} glass, solid surfaces, mobile reflow, and blur fallbacks")


def _check_navigation(page: Page, app: App) -> None:
    """Exercise shell geometry and focus using the same fictional estate as the captures."""
    origin = "/".join(page.url.split("/")[:3])
    original_preference = page.evaluate("localStorage.getItem('mosaic-theme')")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(origin + ("/catalog" if app == "portal" else "/dashboard"))
    _settle(page)
    content_before = page.locator(".content-inner").bounding_box()
    page.get_by_role("button", name="Collapse navigation", exact=True).click()
    sidebar = page.locator(".sidebar")
    collapsed = sidebar.bounding_box()
    if collapsed is None or collapsed["width"] > 90:
        raise CaptureError(f"{app}: collapsed primary navigation is not a compact rail")
    content_after = page.locator(".content-inner").bounding_box()
    if content_before is None or content_after is None or (
        content_after["width"] - content_before["width"] < 120
    ):
        raise CaptureError(f"{app}: collapsing primary navigation did not release its width")
    if app == "portal":
        appearance = page.get_by_role("button", name=re.compile("^Appearance: "))
        appearance.click()
        page.get_by_role("menuitem", name=re.compile(r"^Dark(?: \(selected\))?$")).click()
        if page.locator("html").get_attribute("data-theme") != "dark":
            raise CaptureError("portal: compact appearance control did not select dark")
    page.get_by_role("button", name="Expand navigation", exact=True).click()
    expanded = sidebar.bounding_box()
    if expanded is None or expanded["width"] < 200:
        raise CaptureError(f"{app}: expanding primary navigation did not restore its labels")

    if app == "portal":
        appearance = page.get_by_role("group", name="Appearance")
        for preference in ("Light", "Dark", "System"):
            appearance.get_by_role("button", name=preference, exact=True).click()
            if page.evaluate("localStorage.getItem('mosaic-theme')") != preference.lower():
                raise CaptureError(f"portal: appearance did not persist {preference}")
        page.goto(origin + "/usage")
        _settle(page)
        section_navigation = page.get_by_role("navigation", name="Usage sections")
        section_content = section_navigation.locator("..").locator("> div").last
        before = section_content.bounding_box()
        page.get_by_role("button", name="Collapse Usage sections", exact=True).click()
        after = section_content.bounding_box()
        if before is None or after is None or after["width"] - before["width"] < 150:
            raise CaptureError("portal: collapsing usage sections did not release its width")
        page.get_by_role("button", name="Expand Usage sections", exact=True).click()
        if original_preference:
            page.evaluate(
                "preference => localStorage.setItem('mosaic-theme', preference)",
                original_preference,
            )
        page.goto(origin + "/")
        page.wait_for_url(origin + "/usage")
        page.goto(origin + "/access")
        _settle(page)
        rows = page.locator(".access-list")
        before = rows.bounding_box()
        grant_opener = page.get_by_role("button", name=re.compile("^View access for ")).first
        grant_opener.click()
        dialog = page.get_by_role("dialog")
        dialog.wait_for()
        after = rows.bounding_box()
        if (
            before is None
            or after is None
            or any(abs(before[axis] - after[axis]) > 1 for axis in ("x", "y", "width", "height"))
        ):
            raise CaptureError("portal: opening grant details moved the access list")
        if not dialog.evaluate("(element) => element.contains(document.activeElement)"):
            raise CaptureError("portal: opening grant details did not move focus inside")
        page.keyboard.press("Escape")
        dialog.wait_for(state="hidden")
        page.wait_for_function(
            "(element) => element === document.activeElement", arg=grant_opener.element_handle()
        )
        if rows.bounding_box() != before:
            raise CaptureError("portal: closing grant details changed the access list")
        open_grant_connection("gpt-4o", "Customer Insights")(page)
        page.get_by_role("button", name="Show primary key", exact=True).click()
        page.locator("[data-secret]").wait_for()
        select = page.get_by_role("combobox", name="Grant", exact=True)
        selected_grant = select.input_value()
        general = next(
            text.strip()
            for text in select.locator("option").all_inner_texts()
            if text.startswith("General ·")
        )
        select.select_option(label=general)
        _settle(page)
        if (
            page.locator("[data-secret]").count()
            or page.get_by_role("button", name="Connection details", exact=True).get_attribute(
                "aria-expanded"
            )
            != "false"
        ):
            raise CaptureError("portal: switching grants retained connection details or a key")
        select.select_option(value=selected_grant)
        page.get_by_role("button", name="Connection details", exact=True).click()
        page.get_by_role("button", name="Show primary key", exact=True).click()
        page.locator("[data-secret]").wait_for()
        page.get_by_role("button", name="Close details", exact=True).click()
        page.get_by_role("dialog").wait_for(state="hidden")
        if page.locator("[data-secret]").count():
            raise CaptureError("portal: closing grant details retained a key")
        page.get_by_role("button", name="View access for gpt-4o", exact=True).click()
        _settle(page)
        if page.get_by_role("combobox", name="Grant", exact=True).input_value() != "":
            raise CaptureError("portal: reopening grant details retained the previous selection")
        page.keyboard.press("Escape")
        page.get_by_role("dialog").wait_for(state="hidden")
        page.goto(origin + "/usage?tab=resources")
        page.get_by_role("table", name="Usage by resource", exact=True).wait_for()
        if page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
            raise CaptureError("portal: resource usage overflows the desktop viewport horizontally")
    else:
        _check_console_reflow(page)
        search = page.get_by_role("textbox", name="Search MOSAIC pages", exact=True)
        search.fill("model endpoints")
        page.get_by_role("link", name="Models · Model endpoints", exact=True).click()
        page.get_by_role("heading", name="Model endpoints", exact=True).wait_for()

    page.set_viewport_size({"width": 390, "height": 844})
    opener = page.get_by_role("button", name="Open navigation", exact=True)
    opener.click()
    dialog = page.get_by_role("dialog", name="Navigation", exact=True)
    dialog.wait_for()
    if not dialog.evaluate("(element) => element.contains(document.activeElement)"):
        raise CaptureError(f"{app}: opening mobile navigation did not move focus inside")
    if not page.locator("main").evaluate("(element) => element.inert"):
        raise CaptureError(f"{app}: mobile navigation did not make the background inert")
    page.keyboard.press("Escape")
    if not opener.evaluate("(element) => element === document.activeElement"):
        raise CaptureError(f"{app}: closing mobile navigation did not restore focus")
    if page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
        raise CaptureError(f"{app}: the mobile shell overflows the viewport horizontally")
    if app == "console":
        _check_identity_workspace(page)
    elif app == "portal":
        page.goto(origin + "/catalog")
        _settle(page)
        page.get_by_role("button", name=re.compile("^View access options for ")).first.click()
        dialog = page.get_by_role("dialog")
        dialog.wait_for()
        box = dialog.bounding_box()
        if box is None or box["x"] < -1 or box["width"] > 391:
            raise CaptureError("portal: mobile access options do not fit the viewport")
        if dialog.evaluate("(element) => element.scrollWidth > element.clientWidth + 1"):
            raise CaptureError("portal: mobile access options overflow horizontally")
        page.keyboard.press("Escape")
        dialog.wait_for(state="hidden")
    print(f"verified {app} desktop collapse, mobile navigation, and contextual layout")


def _check_identity_workspace(page: Page) -> None:
    origin = "/".join(page.url.split("/")[:3])
    page.goto(origin + "/identity?tab=users")
    _settle(page)
    row = page.get_by_role("button", name=re.compile(r"^Megan Bowen"))
    row.wait_for()
    if page.get_by_role("dialog").count():
        raise CaptureError("console: identity details opened without a selection")
    row.click()
    page.get_by_role("button", name="Edit local metadata", exact=True).wait_for()
    if page.get_by_role("dialog").count():
        raise CaptureError("console: selecting identity details opened a modal")
    if page.get_by_role("textbox", name="Local label", exact=True).count():
        raise CaptureError("console: identity overview immediately exposes an editor")
    page.get_by_role("button", name="Edit local metadata", exact=True).click()
    editor = page.get_by_role("region", name="Edit Megan Bowen", exact=True)
    editor.wait_for()
    label = editor.get_by_role("textbox", name="Local label", exact=True)
    page.wait_for_function(
        "(element) => element === document.activeElement", arg=label.element_handle()
    )
    label.fill("Megan Bowen review draft")
    page.locator("[data-workspace-editor]").get_by_role("button", name="Back", exact=True).click()
    page.get_by_role("dialog", name="Discard unsaved changes?", exact=True).wait_for()
    page.get_by_role("button", name="Stay", exact=True).click()
    page.get_by_role("dialog", name="Discard unsaved changes?", exact=True).wait_for(
        state="hidden"
    )
    page.wait_for_function(
        "(element) => element === document.activeElement", arg=label.element_handle()
    )
    if label.input_value() != "Megan Bowen review draft":
        raise CaptureError("console: keeping identity edits lost the draft")
    editor.get_by_role("button", name="Cancel", exact=True).click()
    page.get_by_role("dialog", name="Discard unsaved changes?", exact=True).wait_for()
    page.get_by_role("button", name="Discard changes", exact=True).click()
    overview_action = page.get_by_role("button", name="Edit local metadata", exact=True)
    editor.wait_for(state="hidden")
    page.wait_for_function(
        "(element) => element === document.activeElement", arg=overview_action.element_handle()
    )
    page.get_by_role("button", name="Back to list", exact=True).click()
    page.wait_for_function(
        "(element) => element === document.activeElement", arg=row.element_handle()
    )
    if page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
        raise CaptureError("console: mobile identity workspace overflows the viewport")

    page.get_by_role("button", name="Add person", exact=True).click()
    add = page.get_by_role("region", name="Add person", exact=True)
    add.wait_for()
    add.get_by_role("textbox", name="Directory search", exact=True).fill("hen")
    add.get_by_role("button", name="Select Henrietta Mueller", exact=True).click()
    add.get_by_role("button", name="Add Henrietta Mueller", exact=True).wait_for()
    add.locator(".identity-avatar-user img").first.wait_for()
    if page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
        raise CaptureError("console: mobile directory picker overflows the viewport")
    add.get_by_role("button", name="Cancel", exact=True).click()
    confirmation = page.get_by_role("dialog", name="Discard unsaved changes?", exact=True)
    confirmation.wait_for()
    confirmation.get_by_role("button", name="Discard changes", exact=True).click()
    add.wait_for(state="hidden")

    for width in (768, 1440):
        page.set_viewport_size({"width": width, "height": 900})
        if page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
            raise CaptureError(f"console: identity workspace overflows at {width}px")

    for section, name, edit, field_label in (
        ("users", "Megan Bowen", "Edit local metadata", "Local label"),
        ("groups", "Data Science", "Edit group", "Description"),
    ):
        page.goto(origin + f"/identity?tab={section}")
        _settle(page)
        page.get_by_role("button", name=re.compile(r"^" + re.escape(name))).click()
        page.get_by_role("button", name=edit, exact=True).click()
        draft = page.get_by_role("textbox", name=field_label, exact=True)
        draft.fill(f"{name} section draft")
        page.set_viewport_size({"width": 768, "height": 900})
        page.get_by_label("Identity section", exact=True).select_option("agents")
        page.get_by_role("dialog", name="Discard unsaved changes?", exact=True).wait_for()
        page.get_by_role("button", name="Stay", exact=True).click()
        page.get_by_role("dialog", name="Discard unsaved changes?", exact=True).wait_for(
            state="hidden"
        )
        page.wait_for_function(
            "(element) => element === document.activeElement", arg=draft.element_handle()
        )
        if f"tab={section}" not in page.url or draft.input_value() != f"{name} section draft":
            raise CaptureError("console: cancelling a section change lost the section or draft")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.get_by_role("navigation", name="Identity sections", exact=True).get_by_role(
            "button", name="Agents", exact=True
        ).click()
        page.get_by_role("dialog", name="Discard unsaved changes?", exact=True).wait_for()
        page.get_by_role("button", name="Discard changes", exact=True).click()
        page.wait_for_function("new URLSearchParams(location.search).get('tab') === 'agents'")
        page.locator("[data-workspace-editor]").wait_for(state="hidden")
        if page.get_by_role("dialog", name="Discard unsaved changes?", exact=True).count():
            raise CaptureError("console: discarding a section draft left the confirmation open")

    page.emulate_media(forced_colors="active")
    glass = page.locator(".sidebar").evaluate(
        "(element) => getComputedStyle(element).backdropFilter"
    )
    if glass != "none":
        raise CaptureError("console: forced colors did not disable translucent navigation")
    page.emulate_media(forced_colors="none")
    print(
        "verified identity focus, dirty drafts, section navigation, picker selection, "
        "reflow, and forced colors"
    )


def _check_console_reflow(page: Page) -> None:
    origin = "/".join(page.url.split("/")[:3])
    page.goto(origin + "/analytics")
    _settle(page)
    content_width = "() => document.querySelector('nav[aria-label=\"Analytics sections\"]').parentElement.lastElementChild.getBoundingClientRect().width"
    wrapped_controls = """() => [...document.querySelectorAll('.fui-Button, .fui-Badge')]
      .filter(element => element.getClientRects().length && !element.closest('[aria-pressed]')
        && !element.querySelector('time') && !element.getAttribute('aria-label')?.startsWith('Select '))
      .flatMap(element => {
        const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT)
        const failures = []
        while (walker.nextNode()) {
          const text = walker.currentNode
          if (!text.textContent.trim()) continue
          const range = document.createRange()
          range.selectNodeContents(text)
          const tops = [...range.getClientRects()].filter(r => r.width > 0 && r.height > 0)
            .map(r => Math.round(r.top))
          if (new Set(tops).size > 1) failures.push(text.textContent.trim())
        }
        return failures
      })"""
    for theme in ("light", "dark"):
        page.set_viewport_size({"width": 1440, "height": 900})
        page.reload()
        _settle(page)
        page.get_by_role("group", name="Appearance", exact=True).get_by_role(
            "button", name=theme.capitalize(), exact=True
        ).click()
        page.wait_for_function(
            "(theme) => document.documentElement.dataset.theme === theme", arg=theme
        )
        nav = page.get_by_role("navigation", name="Analytics sections", exact=True)
        expanded = page.evaluate(content_width)
        secondary_expanded = nav.bounding_box()
        primary_expanded = page.locator(".sidebar").bounding_box()
        page.get_by_role("button", name="Collapse Analytics sections", exact=True).click()
        collapsed = page.evaluate(content_width)
        secondary_collapsed = nav.bounding_box()
        if secondary_expanded is None or secondary_collapsed is None or (
            collapsed - expanded < 150
            or abs(collapsed - expanded - (
                secondary_expanded["width"] - secondary_collapsed["width"]
            )) > 1
        ):
            raise CaptureError(f"console: {theme} secondary collapse did not release its full width")
        for item in nav.get_by_role("button").all()[1:]:
            if not item.locator("svg").count() or not item.get_attribute("aria-label"):
                raise CaptureError("console: collapsed section is missing an icon or accessible label")
        page.get_by_role("button", name="Collapse navigation", exact=True).click()
        both = page.evaluate(content_width)
        primary_collapsed = page.locator(".sidebar").bounding_box()
        if primary_expanded is None or primary_collapsed is None or (
            both - collapsed < 120
            or abs(both - collapsed - (
                primary_expanded["width"] - primary_collapsed["width"]
            )) > 1
        ):
            raise CaptureError(f"console: {theme} primary collapse did not release its full width")
        page.get_by_role("button", name="Expand Analytics sections", exact=True).click()
        primary_only = page.evaluate(content_width)
        if both - primary_only < 150 or abs(
            both - primary_only - (collapsed - expanded)
        ) > 1:
            raise CaptureError(f"console: {theme} secondary expansion did not restore its width")
        page.get_by_role("button", name="Expand navigation", exact=True).click()
        if abs(page.evaluate(content_width) - expanded) > 1:
            raise CaptureError(f"console: {theme} both expanded rails did not restore the workspace")
        for width in (1920, 1440, 1280, 768, 720, 390):
            page.set_viewport_size({"width": width, "height": 900})
            if page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
                raise CaptureError(f"console: {theme} workspace overflows at {width}px")
            if failures := page.evaluate(wrapped_controls):
                raise CaptureError(f"console: wrapped controls at {width}px: {failures}")
    page.set_viewport_size({"width": 1440, "height": 900})
    for theme in ("light", "dark"):
        page.goto(origin + "/pools")
        _settle(page)
        page.get_by_role("group", name="Appearance", exact=True).get_by_role(
            "button", name=theme.capitalize(), exact=True
        ).click()
        page.wait_for_function(
            "(theme) => document.documentElement.dataset.theme === theme", arg=theme
        )
        primary = page.locator(".sidebar").bounding_box()
        secondary = page.get_by_role(
            "navigation", name="Pools sections", exact=True
        ).bounding_box()
        if primary is None or primary["width"] < 200 or (
            secondary is None or secondary["width"] < 200
        ):
            raise CaptureError(f"console: {theme} pool checks require both expanded rails")
        page.get_by_role("list", name="Model pools", exact=True).get_by_role(
            "button", name=re.compile(r"^Anthropic Claude")
        ).click()
        sections = page.get_by_role("navigation", name="Pool sections", exact=True)
        for section in (
            "Overview", "Models and deployments", "Access and connection", "Health",
            "Policy and runs",
        ):
            sections.get_by_role("button", name=section, exact=True).click()
            _settle(page)
            if section == "Health":
                wait_for_text("Breaker tripped")(page)
                scroll_to_text("calls to each model ended", margin=168)(page)
                _settle(page)
            _assert_no_document_overflow(
                page, f"console: {theme} pool {section} with both rails expanded",
                expected_width=1440,
            )
            _check_local_table_scroll(page, f"{theme} pool {section}")
        page.goto(origin + "/pricing")
        _settle(page)
        page.get_by_role("group", name="Appearance", exact=True).get_by_role(
            "button", name=theme.capitalize(), exact=True
        ).click()
        page.wait_for_function(
            "(theme) => document.documentElement.dataset.theme === theme", arg=theme
        )
        page.get_by_role("table", name="Prices", exact=True).wait_for()
        primary = page.locator(".sidebar").bounding_box()
        secondary = page.get_by_role(
            "navigation", name="Pricing sections", exact=True
        ).bounding_box()
        if primary is None or primary["width"] < 200 or (
            secondary is None or secondary["width"] < 200
        ):
            raise CaptureError(f"console: {theme} pricing checks require both expanded rails")
        _assert_no_document_overflow(page, f"{theme} pricing", expected_width=1440)
        _check_local_table_scroll(page, f"{theme} pricing", require_scroll=True)
        fill_text("Find a model", "2024-11-20")(page)
        wait_for_text("Changes ")(page)
        _settle(page)
        _assert_no_document_overflow(page, f"{theme} filtered pricing", expected_width=1440)
        _check_local_table_scroll(page, f"{theme} filtered pricing", require_scroll=True)
        page.goto(origin + "/analytics")
        _settle(page)
    print("verified every pool section, scrolled Health, and Pricing with both expanded rails in both themes")
    print("verified both themes, all rail combinations, width recovery, reflow, and control lines")


def capture(
    shots: Sequence[Shot],
    *,
    output: Path,
    urls: dict[App, str],
    api_port: int,
    logs: Path,
    headed: bool = False,
    check_navigation: bool = False,
) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    literals = sorted(
        {
            *SENSITIVE_LITERALS,
            *RESOURCE_NAMES,
            *(name.lower().replace(" ", "-") for name in RESOURCE_NAMES),
        },
        key=len,
        reverse=True,
    )
    written: list[Path] = []
    failures: list[CaptureError] = []
    verified: set[App] = set()
    verified_portal_themes: set[Theme] = set()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        try:
            for shot in shots:
                context = browser.new_context(
                    viewport={"width": shot.width, "height": shot.height},
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
                    if check_navigation and shot.app not in verified:
                        verified.add(shot.app)
                        _check_navigation(page, shot.app)
                    if (
                        check_navigation
                        and shot.app == "portal"
                        and shot.theme not in (verified_portal_themes)
                    ):
                        _check_portal_appearance(page, shot.theme)
                        verified_portal_themes.add(shot.theme)
                except (CaptureError, PlaywrightTimeoutError) as error:
                    failures.append(CaptureError(str(error)))
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
    parser.add_argument(
        "--logs-dir", type=Path,
        help="Keep server logs and unredacted failure captures in this dedicated directory",
    )
    parser.add_argument("--api-port", type=int, default=18000)
    parser.add_argument("--console-port", type=int, default=15173)
    parser.add_argument("--portal-port", type=int, default=15174)
    parser.add_argument("--headed", action="store_true", help="Show the browser while capturing")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Serve existing production builds instead of Vite dev",
    )
    parser.add_argument(
        "--check-navigation", action="store_true", help="Verify desktop and mobile shell behavior"
    )
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

    # Keep scratch data in the project's ignored cache, never in a shared system temp folder.
    logs = (
        args.logs_dir.resolve()
        if args.logs_dir
        else REPO_ROOT / "scripts" / "screenshots" / "__pycache__" / f"capture-{time.time_ns()}"
    )
    if not logs.is_relative_to(REPO_ROOT) or logs.is_relative_to(args.output.resolve()):
        parser.error("--logs-dir must be a dedicated project-local directory outside --output")
    logs.mkdir(parents=True, exist_ok=False)
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
                        _running(app, _vite(app_dir, port, args.preview), app_dir, urls[app], logs)
                    )
            capture(
                shots,
                output=args.output,
                urls=urls,
                api_port=args.api_port,
                logs=logs,
                headed=args.headed,
                check_navigation=args.check_navigation,
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
