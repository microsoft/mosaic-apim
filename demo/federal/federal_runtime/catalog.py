from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    name: str
    purpose: str
    tools: tuple[str, str, str]
    model_tool: str | None = None


PROFILES = {
    "doe-energy-data": Profile(
        "DOE Energy Data Desk",
        "Compare selected state electricity generation and energy mix from EIA.",
        ("state_energy_profile", "compare_generation_mix", "source_details"),
    ),
    "doe-lab-research": Profile(
        "DOE Lab Research Library",
        "Find public OSTI research metadata; generate a cited metadata-only research brief.",
        ("search_research", "get_record", "summarize_research"),
        "summarize_research",
    ),
    "faa-airport-reference": Profile(
        "FAA Airport Reference",
        "Look up airport reference metadata from a dated FAA NASR subset.",
        ("find_airport", "airport_profile", "source_cycle"),
    ),
    "faa-regulatory-reference": Profile(
        "FAA Regulatory Reference",
        "Find Title 14 eCFR citations and curated FAA guidance; summarize with citations.",
        ("lookup_citation", "find_guidance", "summarize_reference"),
        "summarize_reference",
    ),
    "usda-crop-insights": Profile(
        "USDA Crop Insights",
        "Compare selected published NASS national crop acreage and yield statistics.",
        ("crop_summary", "compare_yields", "source_release"),
    ),
    "usda-conservation-guide": Profile(
        "USDA Conservation Guide",
        "Discover NRCS conservation practices; never determine benefits or eligibility.",
        ("lookup_practice", "find_practices", "summarize_practices"),
        "summarize_practices",
    ),
    "nasa-mission-archive": Profile(
        "NASA Mission Archive",
        "Find NASA mission/media metadata and build a cited briefing; link, never rehost media.",
        ("search_missions", "mission_sources", "build_mission_brief"),
        "build_mission_brief",
    ),
    "nasa-earthdata-explorer": Profile(
        "NASA Earthdata Explorer",
        "Discover public CMR collections and compare spatial and temporal metadata coverage.",
        ("search_collections", "describe_collection", "compare_coverage"),
    ),
}

DISCLAIMER = (
    "Unofficial MOSAIC demonstration using selected public snapshots, not an agency service "
    "or a compliance claim. Not current operational guidance. FAA results must not be used "
    "for flight planning, navigation, NOTAM decisions, aircraft control or safety-critical advice. "
    "USDA results do not determine benefits or eligibility."
)
