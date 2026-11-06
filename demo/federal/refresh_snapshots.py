"""Explicit, read-only public-source capture. Failures never generate replacement/synthetic data."""

import argparse
import csv
import hashlib
import io
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
DESTINATION = HERE / "federal_runtime" / "snapshots"
FAA_CYCLE = "2026-10-01"
ECFR_AS_OF = "2026-10-27"
NASS_URL = "https://www.nass.usda.gov/Publications/Todays_Reports/reports/cropan26.txt"
MAX_DOWNLOAD_BYTES = 12_000_000
SOURCE_HOSTS = frozenset(
    {
        "www.eia.gov",
        "www.osti.gov",
        "nfdc.faa.gov",
        "www.ecfr.gov",
        "www.faa.gov",
        "www.nass.usda.gov",
        "www.nrcs.usda.gov",
        "images-api.nasa.gov",
        "cmr.earthdata.nasa.gov",
    }
)


class GovernmentRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        parts = urlsplit(new_url)
        if parts.scheme != "https" or parts.hostname not in SOURCE_HOSTS:
            raise ValueError("Public-source redirect is outside the HTTPS allowlist.")
        return super().redirect_request(request, file, code, message, headers, new_url)


class PublicCapture:
    def __init__(self) -> None:
        self.sources: list[dict[str, str]] = []

    def fetch(self, url: str, accept: str = "*/*") -> bytes:
        if urlsplit(url).scheme != "https" or urlsplit(url).hostname not in SOURCE_HOSTS:
            raise ValueError("Source is not on the fixed public-government allowlist.")
        request = urllib.request.Request(
            url, headers={"User-Agent": "MOSAIC-public-demo/1.0", "Accept": accept}
        )
        opener = urllib.request.build_opener(GovernmentRedirect())
        with opener.open(request, timeout=60) as response:
            raw = response.read(MAX_DOWNLOAD_BYTES + 1)
        if len(raw) > MAX_DOWNLOAD_BYTES:
            raise ValueError("Public source exceeds capture limit.")
        self.sources.append(
            {
                "url": url,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "retrieved_at": datetime.now(UTC).isoformat(),
            }
        )
        return raw

    def payload(self, profile: str, release: str, records: list[dict], **extra: str) -> dict:
        if not records:
            raise ValueError("Public source returned no selected records.")
        return {
            "profile": profile,
            "provenance": {
                "mode": "snapshot",
                "release": release,
                "retrieved_at": min(source["retrieved_at"] for source in self.sources),
                "snapshot_generated_at": datetime.now(UTC).isoformat(),
                "sources": self.sources,
                "selection": "Small selected public-data subset, not a complete agency inventory.",
                **extra,
            },
            "records": records,
        }


def _xlsx_rows(raw: bytes) -> list[dict[str, str]]:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        strings = [
            "".join(item.itertext())
            for item in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("m:si", ns)
        ]
        root = ET.fromstring(archive.read("xl/worksheets/sheet7.xml"))
    rows = []
    for row in root.findall(".//m:row", ns):
        values = {}
        for cell in row:
            column = cell.get("r", "").rstrip("0123456789")
            value = cell.findtext("m:v", None, ns)
            if column in {"A", "B", "C"} and value is not None:
                values[column] = strings[int(value)] if cell.get("t") == "s" else value
        rows.append(values)
    if "Table 5." not in rows[0].get("A", ""):
        raise ValueError("EIA generation sheet structure changed.")
    if rows[3].get("B") != "Year\n2024" or rows[3].get("C") != "Year\n2023":
        raise ValueError("EIA release changed; review the year selector before refreshing.")
    return rows


def energy() -> dict:
    capture = PublicCapture()
    records = []
    for state in ("AL", "CA", "TX"):
        url = f"https://www.eia.gov/electricity/state/xls/SEP%20Tables%20for%20{state}.xlsx"
        rows = _xlsx_rows(capture.fetch(url))
        total_index = next(
            i for i, row in enumerate(rows) if row.get("A") == "Total electric industry"
        )
        for year, column in ((2024, "B"), (2023, "C")):
            mix = {}
            for row in rows[total_index + 1 :]:
                fuel, amount = row.get("A", ""), row.get(column)
                if amount is not None and fuel and not fuel.startswith(".."):
                    mix[fuel] = int(amount) if re.fullmatch(r"-?\d+", amount) else None
            records.append(
                {
                    "id": f"{state}-{year}",
                    "state": state,
                    "year": year,
                    "generation_mwh": int(rows[total_index][column]),
                    "mix_mwh": mix,
                    "source_url": url,
                    "note": "Table 5 total electric industry; excludes nested fuel subcategories. "
                    "Null is unavailable, not zero; independent rounding may affect totals.",
                }
            )
    return capture.payload(
        "doe-energy-data", "EIA State Electricity Profiles 2024, Table 5", records
    )


def research() -> dict:
    capture = PublicCapture()
    url = "https://www.osti.gov/api/v1/records?search=solar&rows=5"
    root = ET.fromstring(capture.fetch(url, "application/xml"))
    records = []
    for item in root.findall("record"):
        record_id = item.findtext("osti_id")
        records.append(
            {
                "id": record_id,
                "title": item.findtext("title"),
                "publication_date": item.findtext("publication_date"),
                "doi": item.findtext("doi"),
                "research_organizations": [
                    r.text for r in item.findall("research_orgs/research_org")
                ],
                "subjects": [r.text for r in item.findall("subjects/subject")][:12],
                "source_url": f"https://www.osti.gov/biblio/{record_id}",
                "scope": (
                    "Bibliographic metadata only. Abstract/full text not rehosted or reviewed."
                ),
            }
        )
    return capture.payload("doe-lab-research", "OSTI API v1 solar metadata capture", records)


def airports() -> dict:
    capture = PublicCapture()
    url = "https://nfdc.faa.gov/webContent/28DaySub/extra/01_Oct_2026_APT_CSV.zip"
    raw = capture.fetch(url)
    records = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        with archive.open("APT_BASE.csv") as data:
            for row in csv.DictReader(io.TextIOWrapper(data, encoding="utf-8-sig")):
                if row["ARPT_ID"] not in {"IAD", "DEN", "SFO", "DFW", "JFK"}:
                    continue
                if row["EFF_DATE"] != "2026/10/01":
                    raise ValueError("FAA NASR cycle differs from the selected version.")
                records.append(
                    {
                        "id": row["ARPT_ID"],
                        "icao": row["ICAO_ID"],
                        "name": row["ARPT_NAME"],
                        "city": row["CITY"],
                        "state": row["STATE_CODE"],
                        "effective_date": FAA_CYCLE,
                        "elevation_ft": float(row["ELEV"]),
                        "source_url": url,
                    }
                )
    return capture.payload(
        "faa-airport-reference",
        "FAA NASR 28-day subscription effective 2026-10-01, APT_BASE.csv",
        records,
        expires="2026-10-29; historical reference only, never current operational data",
    )


class PageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.description = ""
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self.in_title = True
        values = dict(attrs)
        if tag == "meta" and values.get("name", "").lower() == "description":
            self.description = values.get("content") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data


def web_record(capture: PublicCapture, record_id: str, url: str) -> dict:
    page = PageText()
    page.feed(capture.fetch(url).decode("utf-8"))
    if not page.title.strip():
        raise ValueError("Public page has no title.")
    return {
        "id": record_id,
        "title": unescape(page.title.strip())[:240],
        "description": unescape(page.description.strip())[:700],
        "source_url": url,
        "version": "Web page captured at provenance.retrieved_at; no publication date asserted.",
    }


def regulatory(excerpts: Path | None = None) -> dict:
    capture = PublicCapture()
    records = []
    for section in ("107.19", "107.31", "107.39"):
        url = (
            f"https://www.ecfr.gov/api/versioner/v1/full/{ECFR_AS_OF}/title-14.xml"
            f"?part=107&section={section}"
        )
        if excerpts is None:
            raw = capture.fetch(url, "text/xml")
        else:
            raw = (excerpts / f"ecfr-{section}.xml").read_bytes()
            capture.sources.append(
                {
                    "url": url,
                    "excerpt_sha256": hashlib.sha256(raw).hexdigest(),
                    "retrieved_at": "2026-10-29",
                    "capture_note": "Selected XML elements transcribed from the dated public "
                    "eCFR API response retrieved through web_fetch. Direct urllib API access "
                    "returned HTTP 406 in the capture environment; no automatic fallback.",
                }
            )
        root = ET.fromstring(raw)
        records.append(
            {
                "id": section,
                "kind": "regulation",
                "citation": f"14 CFR {section}",
                "title": root.findtext("HEAD"),
                "text": " ".join(" ".join(item.itertext()) for item in root.findall("P")),
                "as_of": ECFR_AS_OF,
                "source_url": url,
            }
        )
    records.append(
        {
            **web_record(
                capture,
                "part107-waivers",
                "https://www.faa.gov/uas/commercial_operators/part_107_waivers",
            ),
            "kind": "guidance",
        }
    )
    return capture.payload(
        "faa-regulatory-reference", f"eCFR Title 14 as of {ECFR_AS_OF}; FAA curated page", records
    )


def crops(excerpt: Path | None = None) -> dict:
    capture = PublicCapture()
    if excerpt is None:
        text = capture.fetch(NASS_URL).decode("utf-8")
    else:
        raw = excerpt.read_bytes()
        text = raw.decode("utf-8")
        capture.sources.append(
            {
                "url": NASS_URL,
                "excerpt_sha256": hashlib.sha256(raw).hexdigest(),
                "retrieved_at": "2026-10-29",
                "capture_note": "Selected public report text transcribed from web_fetch. "
                "Direct refresh is not a fallback: it fails explicitly on source/network errors.",
            }
        )
    corn = re.search(
        r"record high ([\d.]+) bushels per acre,.*?2024 yield of ([\d.]+) bushels per acre",
        text,
        re.DOTALL,
    )
    acreage = re.search(r"for grain was estimated at ([\d.]+) million acres", text)
    soy = re.search(r"Soybean production.*?record high ([\d.]+) bushels", text, re.DOTALL)
    soy_area = re.search(r"Harvested area, at ([\d.]+) million acres", text)
    if "January 2026" not in text or not all((corn, acreage, soy, soy_area)):
        raise ValueError("NASS release or summary structure changed; review the source.")
    records = [
        {
            "id": "corn-2024",
            "crop": "corn",
            "geography": "United States",
            "year": 2024,
            "yield": float(corn[2]),
            "yield_unit": "bushels/acre",
            "harvested_million_acres": None,
            "source_url": NASS_URL,
        },
        {
            "id": "corn-2025",
            "crop": "corn",
            "geography": "United States",
            "year": 2025,
            "yield": float(corn[1]),
            "yield_unit": "bushels/acre",
            "harvested_million_acres": float(acreage[1]),
            "source_url": NASS_URL,
        },
        {
            "id": "soybeans-2025",
            "crop": "soybeans",
            "geography": "United States",
            "year": 2025,
            "yield": float(soy[1]),
            "yield_unit": "bushels/acre",
            "harvested_million_acres": float(soy_area[1]),
            "source_url": NASS_URL,
        },
    ]
    return capture.payload(
        "usda-crop-insights",
        "USDA NASS Crop Production 2025 Summary, approved 2026-01-12",
        records,
        precision="National summary estimates; acreage is rounded in millions. "
        "2024 corn acreage is not included, not zero. No state/county/farm inference.",
    )


def conservation() -> dict:
    capture = PublicCapture()
    records = []
    for code, slug in (
        ("340", "cover-crop-ac-340-conservation-practice-standard"),
        ("590", "nutrient-management-ac-590-conservation-practice-standard"),
        ("330", "contour-farming-ac-330-conservation-practice-standard"),
    ):
        records.append(
            web_record(
                capture, code, f"https://www.nrcs.usda.gov/resources/guides-and-instructions/{slug}"
            )
        )
    return capture.payload(
        "usda-conservation-guide",
        "NRCS national conservation practice page metadata capture",
        records,
        scope="National reference pages only; state/local standards and eligibility not evaluated.",
    )


def missions() -> dict:
    capture = PublicCapture()
    url = "https://images-api.nasa.gov/search?q=Artemis%20I%20Orion&media_type=image&page_size=3"
    payload = json.loads(capture.fetch(url))
    records = []
    for item in payload["collection"]["items"]:
        data = item["data"][0]
        records.append(
            {
                "id": data["nasa_id"],
                "title": data["title"],
                "date_created": data["date_created"],
                "center": data.get("center"),
                "media_type": data["media_type"],
                "description": data.get("description", "")[:700],
                "source_url": item["href"],
                "note": "Public media metadata, not a mission-status feed. Media not rehosted.",
            }
        )
    return capture.payload(
        "nasa-mission-archive", "NASA Images API v1.1 Artemis I metadata", records
    )


def earthdata() -> dict:
    capture = PublicCapture()
    url = "https://cmr.earthdata.nasa.gov/search/collections.json?keyword=MODIS&page_size=3"
    payload = json.loads(capture.fetch(url))
    records = []
    for item in payload["feed"]["entry"]:
        records.append(
            {
                "id": item["id"],
                "title": item["title"],
                "short_name": item["short_name"],
                "version": item["version_id"],
                "time_start": item.get("time_start"),
                "time_end": item.get("time_end"),
                "boxes": item.get("boxes", []),
                "source_url": f"https://cmr.earthdata.nasa.gov/search/concepts/{item['id']}.html",
                "note": "Null time_end means not specified, not a promise of current coverage. "
                "Metadata discovery only; authenticated granule downloads not performed.",
            }
        )
    return capture.payload(
        "nasa-earthdata-explorer", "NASA CMR collection metadata capture", records
    )


CAPTURES = {
    "doe-energy-data": energy,
    "doe-lab-research": research,
    "faa-airport-reference": airports,
    "faa-regulatory-reference": regulatory,
    "usda-crop-insights": crops,
    "usda-conservation-guide": conservation,
    "nasa-mission-archive": missions,
    "nasa-earthdata-explorer": earthdata,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=CAPTURES, action="append")
    parser.add_argument(
        "--nass-excerpt", type=Path, help="Explicit previously retrieved report excerpt."
    )
    parser.add_argument(
        "--ecfr-excerpts", type=Path, help="Explicit previously retrieved dated eCFR XML excerpts."
    )
    args = parser.parse_args()
    results = []
    for profile in args.profile or CAPTURES:
        try:
            if profile == "usda-crop-insights":
                payload = crops(args.nass_excerpt)
            elif profile == "faa-regulatory-reference":
                payload = regulatory(args.ecfr_excerpts)
            else:
                payload = CAPTURES[profile]()
            results.append((profile, payload))
        except Exception as error:
            print(f"capture profile={profile} outcome=error kind={type(error).__name__}")
            print(
                "No selected snapshots written; no synthetic or stale-data substitution performed."
            )
            return 1
    DESTINATION.mkdir(parents=True, exist_ok=True)
    for profile, payload in results:
        (DESTINATION / f"{profile}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"capture profile={profile} outcome=ok records={len(payload['records'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
