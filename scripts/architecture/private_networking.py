"""Regenerate the fictional private-network reference (not deployment configuration)."""

from __future__ import annotations

import ipaddress
import json
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESTINATION = ROOT / "docs" / "images" / "architecture"
VNET = "10.42.0.0/24"
SUBNETS = ("10.42.0.0/26", "10.42.0.128/25")
APIM_SUBNET = "10.42.0.64/26"
ROWS = [
    (
        "Administrator console",
        "sites",
        "privatelink.azurewebsites.net\nInclude console SCM record",
        "Web App: console",
    ),
    (
        "End-user portal",
        "sites",
        "privatelink.azurewebsites.net\nInclude portal SCM record",
        "Web App: portal",
    ),
    (
        "MOSAIC API",
        "sites",
        "privatelink.azurewebsites.net\nInclude API SCM record",
        "Web App: FastAPI API",
    ),
    (
        "Container registry",
        "registry",
        "privatelink.azurecr.io\nRegistry + regional data records",
        "Azure Container Registry\nPremium required for Private Link",
    ),
    ("Governance and usage", "Sql", "privatelink.documents.azure.com", "Azure Cosmos DB for NoSQL"),
    (
        "Backend secrets",
        "vault",
        "privatelink.vaultcore.azure.net",
        "Backend Key Vault\nAPI + gateway roles as configured",
    ),
    (
        "Management credentials",
        "vault",
        "privatelink.vaultcore.azure.net",
        "Management Key Vault\nAPI-only: no gateway secret access",
    ),
    (
        "Runtime gateway",
        "Gateway",
        "privatelink.azure-api.net",
        "API Management: Standard v2\nPrivate endpoint is gateway-only",
    ),
    (
        "Example AI backend",
        "account",
        "privatelink.openai.azure.com",
        "Azure OpenAI (optional/external)\nNot deployed by MOSAIC",
    ),
    (
        "Observability scope",
        "azuremonitor",
        "privatelink.monitor.azure.com\nprivatelink.oms.opinsights.azure.com\nprivatelink.ods.opinsights.azure.com\nprivatelink.agentsvc.azure-automation.net\nprivatelink.blob.core.windows.net",
        "Azure Monitor Private Link Scope\nLinked Application Insights +\nLog Analytics workspace",
    ),
]


def generate(*, include_apim: bool = False) -> tuple[dict, str]:
    network = ipaddress.ip_network(VNET)
    prefixes = (*SUBNETS, APIM_SUBNET) if include_apim else SUBNETS
    subnets = [ipaddress.ip_network(prefix) for prefix in prefixes]
    assert all(subnet.subnet_of(network) for subnet in subnets)
    assert not any(a.overlaps(b) for i, a in enumerate(subnets) for b in subnets[i + 1 :])
    rows = ROWS if include_apim else [row for row in ROWS if row[1] not in ("Gateway", "account")]
    offset = (len(ROWS) - len(rows)) * 83
    height = 1760 - offset
    gateway_description = (
        "Includes optional co-located APIM."
        if include_apim
        else "No co-located APIM or APIM subnet."
    )
    elements: list[dict] = []
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1720" height="{height}" '
        f'viewBox="0 0 1720 {height}" role="img" aria-labelledby="title description">',
        '<title id="title">MOSAIC administrator-managed private networking reference</title>',
        f'<desc id="description">Illustrative /24 VNet, {len(subnets)} CIDR-labeled subnets, '
        f"{len(rows)} service private endpoints and their DNS zones. "
        f"{gateway_description} "
        "Public Bicep and Terraform do not deploy this topology.</desc>",
        '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" '
        'orient="auto" markerUnits="strokeWidth">'
        '<path d="M0,0 L0,6 L8,3 z" fill="context-stroke"/></marker></defs>',
        f'<rect width="1720" height="{height}" fill="#ffffff"/>',
    ]

    def base(kind: str, x: int, y: int, width: int, height: int, color: str) -> dict:
        return {
            "id": f"element-{len(elements)}",
            "type": kind,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "angle": 0,
            "strokeColor": color,
            "backgroundColor": "transparent",
            "fillStyle": "solid",
            "strokeWidth": 1.5,
            "strokeStyle": "solid",
            "roughness": 0,
            "opacity": 100,
            "groupIds": [],
            "frameId": None,
            "index": None,
            "roundness": None,
            "seed": len(elements) + 1,
            "version": 1,
            "versionNonce": 1,
            "isDeleted": False,
            "boundElements": None,
            "updated": 1,
            "link": None,
            "locked": False,
        }

    def text(x: int, y: int, value: str, size: int = 17) -> None:
        lines = value.splitlines()
        item = base(
            "text",
            x,
            y,
            max(len(line) for line in lines) * size * 0.62,
            size * 2.5 * len(lines),
            "#000000",
        )
        item.update(
            text=value,
            originalText=value,
            fontSize=size,
            fontFamily=2,
            textAlign="left",
            verticalAlign="top",
            containerId=None,
            lineHeight=1.25,
            autoResize=True,
        )
        elements.append(item)
        for i, line in enumerate(lines):
            svg.append(
                f'<text x="{x}" y="{y + size + i * size * 1.25}" '
                f'font-family="Arial, Helvetica, sans-serif" font-size="{size}" '
                f'fill="#000000">{escape(line)}</text>'
            )

    def box(
        x: int, y: int, width: int, height: int, color: str = "#0078D4", fill: str = "transparent"
    ) -> None:
        item = base("rectangle", x, y, width, height, color)
        item.update(backgroundColor=fill, roundness={"type": 3})
        elements.append(item)
        svg.append(
            f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="8" '
            f'stroke="{color}" stroke-width="1.5" '
            f'fill="{fill if fill != "transparent" else "none"}"/>'
        )

    def arrow(points: list[tuple[int, int]], color: str, dashed: bool = False) -> None:
        x, y = points[0]
        item = base(
            "arrow",
            x,
            y,
            max(p[0] for p in points) - min(p[0] for p in points),
            max(p[1] for p in points) - min(p[1] for p in points),
            color,
        )
        item.update(
            points=[[a - x, b - y] for a, b in points],
            startArrowhead=None,
            endArrowhead="arrow",
            startBinding=None,
            endBinding=None,
            strokeStyle="dashed" if dashed else "solid",
        )
        elements.append(item)
        coordinates = " ".join(f"{a},{b}" for a, b in points)
        dash = ' stroke-dasharray="6 5"' if dashed else ""
        svg.append(
            f'<polyline points="{coordinates}" fill="none" stroke="{color}" '
            f'stroke-width="1.5"{dash} marker-end="url(#arrow)"/>'
        )

    title = (
        "Private hosting + optional APIM"
        if include_apim
        else "Private hosting (no co-located APIM)"
    )
    text(35, 18, f"MOSAIC | {title}", 29)
    text(
        35,
        62,
        "ADMINISTRATOR-OWNED DESIGN. "
        "Supported azd/Bicep and Terraform deployments remain PUBLIC ONLY.",
        18,
    )
    box(20, 110, 1680, 1530 - offset)
    text(40, 122, "Azure Commercial reference context", 20)
    box(40, 160, 1640, 1460 - offset)
    text(60, 170, "MOSAIC deployment and external dependencies (logical grouping)", 18)

    box(65, 225, 440, 170, "#0078D4", "#CFE4FA")
    text(85, 242, "Administrators, portal users,\nruntime clients and deployment runners", 19)
    text(
        85,
        302,
        "Private reachability + DNS required\n"
        "VPN / ExpressRoute / peered connectivity\n"
        "provided and operated by administrators",
        16,
    )
    arrow([(505, 310), (555, 310)], "#0078D4")
    text(75, 414, "Private DNS zones / records", 20)
    text(
        75,
        446,
        "VNet links; corporate DNS needs forwarding.\nDNS zones are not placed inside a subnet.",
        16,
    )

    box(555, 225, 540, 1330 - offset)
    text(575, 240, f"MOSAIC VNet | {VNET}", 23)
    text(575, 278, "Illustrative ranges; size for your scale and region.", 16)
    box(575, 315, 240 if include_apim else 500, 150, "#107C10")
    text(590, 330, f"App integration\n{SUBNETS[0]}", 19)
    text(
        590,
        388,
        "Microsoft.Web/serverFarms\nAPI + console + portal\nOutbound, not Private Link",
        14,
    )
    if include_apim:
        box(835, 315, 240, 150, "#107C10")
        text(850, 330, f"Optional APIM\n{APIM_SUBNET}", 19)
        text(
            850,
            388,
            "Standard v2; dedicated\nMicrosoft.Web/serverFarms\nOutbound, not Private Link",
            14,
        )

    box(1155, 225, 480, 170, "#495057", "#F3F2F1")
    text(1175, 240, "Required public / Azure service endpoints", 19)
    text(
        1175,
        280,
        "Entra ID, Microsoft Graph, Azure ARM\n"
        "Existing APIMs can remain in their own networks\n"
        "Build/package sources and optional integrations\nNot an air-gapped architecture",
        16,
    )
    box(1155, 410, 480, 80, "#5C2D91", "#E8DAEF")
    text(
        1170,
        422,
        "APIM/resource logs: Azure platform delivery\nSeparate from client Monitor ingestion/query",
        17,
    )

    box(575, 500, 500, 1030 - offset)
    text(595, 514, f"Private endpoint subnet | {SUBNETS[1]}", 20)
    text(595, 547, "Endpoint NICs only; PaaS services remain outside the VNet.", 14)
    text(1165, 512, "Azure services (not subnet-hosted)", 20)

    for i, (name, group, dns, service) in enumerate(rows):
        y = 585 + i * 83
        is_monitor = group == "azuremonitor"
        height = 152 if is_monitor else 66
        box(65, y, 440, height, "#0078D4", "#CFE4FA")
        text(80, y + 10, dns, 15 if is_monitor else 16)
        box(645, y, 355, height, "#0078D4", "#CFE4FA")
        text(660, y + 9, f"{name}\nPrivate endpoint: {group}", 17)
        box(1155, y, 480, height, "#495057", "#F3F2F1")
        text(1170, y + 9, service, 17)
        arrow([(505, y + 33), (643, y + 33)], "#0078D4", True)
        arrow([(1000, y + 33), (1153, y + 33)], "#0078D4")

    arrow([(695, 465), (695, 482), (1040, 482), (1040, 575)], "#107C10")
    if include_apim:
        arrow([(955, 465), (955, 472), (1055, 472), (1055, 575)], "#107C10")
    text(
        1160,
        1498 - offset,
        "Private Link does not replace identity/RBAC.\n"
        + (
            "APIM private endpoint covers gateway traffic only."
            if include_apim
            else "No local APIM subnet or endpoint is required."
        ),
        17,
    )
    arrow([(1635, 784), (1655, 784), (1655, 310), (1637, 310)], "#495057", True)
    text(
        70,
        1570 - offset,
        (
            "Optional APIM: /26 exceeds /27 minimum, but /24 is recommended for APIM growth. "
            "Enlarge the VNet as needed."
            if include_apim
            else "MOSAIC-only hosting: no APIM address space reserved. "
            "Add gateway subnets, endpoints and DNS only if your design needs them."
        ),
        16,
    )

    arrow([(40, 1680 - offset), (110, 1680 - offset)], "#0078D4")
    text(120, 1665 - offset, "Private Link / private client path", 16)
    arrow([(490, 1680 - offset), (560, 1680 - offset)], "#0078D4", True)
    text(570, 1665 - offset, "DNS mapping", 16)
    arrow([(800, 1680 - offset), (870, 1680 - offset)], "#107C10")
    text(880, 1665 - offset, "Outbound VNet integration", 16)
    arrow([(1230, 1680 - offset), (1300, 1680 - offset)], "#495057", True)
    text(1310, 1665 - offset, "Management / identity", 16)
    text(
        40,
        1710 - offset,
        "Purple: platform telemetry. Each service row maps DNS -> private endpoint -> service, "
        "not an extra inference-time hop.",
        17,
    )
    svg.append("</svg>")
    return {
        "type": "excalidraw",
        "version": 2,
        "source": "MOSAIC",
        "elements": elements,
        "appState": {"viewBackgroundColor": "#ffffff"},
        "files": {},
    }, "\n".join(svg) + "\n"


if __name__ == "__main__":
    DESTINATION.mkdir(parents=True, exist_ok=True)
    for with_apim in (False, True):
        scene, svg_content = generate(include_apim=with_apim)
        name = "mosaic-private-networking-with-apim" if with_apim else "mosaic-private-networking"
        (DESTINATION / f"{name}.excalidraw").write_text(
            json.dumps(scene, indent=2) + "\n", encoding="utf-8"
        )
        (DESTINATION / f"{name}.svg").write_text(svg_content, encoding="utf-8")
