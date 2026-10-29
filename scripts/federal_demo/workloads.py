"""The five approved manually triggered Production application identities."""

JOBS = {
    "nasa-brief": (
        "NASA Mission Briefing",
        "NASA-MISSION",
        "gpt-5.6-terra",
        "nasa-mission-archive",
    ),
    "faa-airspace": ("FAA Airspace Briefing", "FAA-OPS", "gpt-5.6-luna", "faa-airport-reference"),
    "faa-standards": (
        "FAA Flight Standards Review",
        "FAA-RND",
        "gpt-5.6-luna",
        "faa-regulatory-reference",
    ),
    "doe-research": ("DOE Lab Research", "DOE-LABS", "gpt-5.1", "doe-lab-research"),
    "usda-field": ("USDA Field Insights", "USDA-DATA", "gpt-5.6-luna", "usda-crop-insights"),
}
