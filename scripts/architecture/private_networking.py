"""Regenerate the fictional private-network reference (not deployment configuration)."""

from __future__ import annotations

import ipaddress
import json
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESTINATION = ROOT / "docs" / "images" / "architecture"
VNET = "10.42.0.0/24"
SUBNETS = ("10.42.0.0/26", "10.42.0.64/26", "10.42.0.128/25")
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


def generate() -> tuple[dict, str]:
    network = ipaddress.ip_network(VNET)
    subnets = [ipaddress.ip_network(prefix) for prefix in SUBNETS]
    assert all(subnet.subnet_of(network) for subnet in subnets)
    assert not any(a.overlaps(b) for i, a in enumerate(subnets) for b in subnets[i + 1 :])
    elements: list[dict] = []
    svg = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1720" height="1760" '
        'viewBox="0 0 1720 1760" role="img" aria-labelledby="title description">',
        '<title id="title">MOSAIC administrator-managed private networking reference</title>',
        '<desc id="description">Illustrative /24 VNet, three CIDR-labeled subnets, '
        "ten service private endpoints and their DNS zones. "
        "Public Bicep and Terraform do not deploy this topology.</desc>",
        '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" '
        'orient="auto" markerUnits="strokeWidth">'
        '<path d="M0,0 L0,6 L8,3 z" fill="context-stroke"/></marker></defs>',
        '<rect width="1720" height="1760" fill="#ffffff"/>',
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

    text(35, 18, "MOSAIC | Private connectivity reference", 29)
    text(
        35,
        62,
        "ADMINISTRATOR-OWNED DESIGN. "
        "Supported azd/Bicep and Terraform deployments remain PUBLIC ONLY.",
        18,
    )
    box(20, 110, 1680, 1530)
    text(40, 122, "Azure Commercial reference context", 20)
    box(40, 160, 1640, 1460)
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

    box(555, 225, 540, 1330)
    text(575, 240, f"MOSAIC VNet | {VNET}", 23)
    text(575, 278, "Illustrative ranges; size for your scale and region.", 16)
    box(575, 315, 240, 150, "#107C10")
    text(590, 330, "App integration\n10.42.0.0/26", 19)
    text(
        590,
        388,
        "Microsoft.Web/serverFarms\nAPI + console + portal\nOutbound, not Private Link",
        14,
    )
    box(835, 315, 240, 150, "#107C10")
    text(850, 330, "APIM integration\n10.42.0.64/26", 19)
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

    box(575, 500, 500, 1030)
    text(595, 514, "Private endpoint subnet | 10.42.0.128/25", 20)
    text(595, 547, "Endpoint NICs only; PaaS services remain outside the VNet.", 14)
    text(1165, 512, "Azure services (not subnet-hosted)", 20)

    for i, (name, group, dns, service) in enumerate(ROWS):
        y = 585 + i * 83
        height = 152 if i == 9 else 66
        box(65, y, 440, height, "#0078D4", "#CFE4FA")
        text(80, y + 10, dns, 15 if i == 9 else 16)
        box(645, y, 355, height, "#0078D4", "#CFE4FA")
        text(660, y + 9, f"{name}\nPrivate endpoint: {group}", 17)
        box(1155, y, 480, height, "#495057", "#F3F2F1")
        text(1170, y + 9, service, 17)
        arrow([(505, y + 33), (643, y + 33)], "#0078D4", True)
        arrow([(1000, y + 33), (1153, y + 33)], "#0078D4")

    arrow([(695, 465), (695, 482), (1040, 482), (1040, 575)], "#107C10")
    arrow([(955, 465), (955, 472), (1055, 472), (1055, 575)], "#107C10")
    text(
        1160,
        1498,
        "Private Link does not replace identity/RBAC.\n"
        "APIM private endpoint covers gateway traffic only.",
        17,
    )
    arrow([(1635, 784), (1655, 784), (1655, 310), (1637, 310)], "#495057", True)
    text(
        70,
        1570,
        "Example only: /26 APIM subnet exceeds /27 minimum, but /24 is recommended "
        "for APIM growth. Enlarge the VNet as needed.",
        16,
    )

    arrow([(40, 1680), (110, 1680)], "#0078D4")
    text(120, 1665, "Private Link / private client path", 16)
    arrow([(490, 1680), (560, 1680)], "#0078D4", True)
    text(570, 1665, "DNS mapping", 16)
    arrow([(800, 1680), (870, 1680)], "#107C10")
    text(880, 1665, "Outbound VNet integration", 16)
    arrow([(1230, 1680), (1300, 1680)], "#495057", True)
    text(1310, 1665, "Management / identity", 16)
    text(
        40,
        1710,
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
    scene, svg_content = generate()
    DESTINATION.mkdir(parents=True, exist_ok=True)
    (DESTINATION / "mosaic-private-networking.excalidraw").write_text(
        json.dumps(scene, indent=2) + "\n", encoding="utf-8"
    )
    (DESTINATION / "mosaic-private-networking.svg").write_text(svg_content, encoding="utf-8")
