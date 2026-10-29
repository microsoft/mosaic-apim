import json
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context
from pydantic import Field

from federal_runtime.data import DataError, Snapshot
from federal_runtime.model import BriefGateway
from federal_runtime.shared import ON_BEHALF_HEADER, ModelCallError

Query = Annotated[str, Field(min_length=1, max_length=80, pattern=r".*\S.*")]
RecordId = Annotated[str, Field(min_length=1, max_length=120, pattern=r".*\S.*")]
RecordIds = Annotated[list[RecordId], Field(min_length=1, max_length=3)]
Limit = Annotated[int, Field(ge=1, le=5)]
Focus = Annotated[str, Field(min_length=1, max_length=160, pattern=r".*\S.*")]
State = Annotated[str, Field(pattern=r"^[A-Za-z]{2}$")]
EnergyYear = Literal[2023, 2024]
CropYear = Literal[2024, 2025]
Crop = Literal["corn", "soybeans"]


class AgencyTools:
    def __init__(self, snapshot: Snapshot, gateway: BriefGateway | None = None) -> None:
        self.data, self.gateway = snapshot, gateway

    async def _brief(self, record_ids: list[str], focus: str, ctx: Context) -> dict[str, Any]:
        records = self.data.select(record_ids)
        if self.gateway is None:
            raise ModelCallError(
                "Summary inference is disabled until protected-backend verification and the "
                "application's separate model grant are configured."
            )
        request = ctx.request_context.request
        reference = request.headers.get(ON_BEHALF_HEADER) if request is not None else None
        evidence = self.data.envelope(records=records)
        prompt = json.dumps({"focus": focus, "evidence": evidence}, ensure_ascii=False)
        answer = await self.gateway.ask(prompt, on_behalf_of=reference)
        return self.data.envelope(
            summary=answer,
            generated=True,
            citations=[
                {"id": record["id"], "source_url": record["source_url"]} for record in records
            ],
            scope=(
                "Model-generated interpretation of the selected snapshot; "
                "not independently verified."
            ),
        )

    def _energy(self, state: str, year: int) -> dict[str, Any]:
        record = self.data.get(f"{state.upper()}-{year}")
        record["mix_percent"] = {
            fuel: round(100 * amount / record["generation_mwh"], 3) if amount is not None else None
            for fuel, amount in record["mix_mwh"].items()
        }
        return record

    async def state_energy_profile(self, state: State, year: EnergyYear = 2024) -> dict[str, Any]:
        """EIA state generation in MWh and derived generation shares; selected AL/CA/TX only."""
        return self.data.envelope(record=self._energy(state, year))

    async def compare_generation_mix(
        self, state_a: State, state_b: State, year: EnergyYear = 2024
    ) -> dict[str, Any]:
        """Compare two states in the same EIA year, retaining missing values and units."""
        if state_a.upper() == state_b.upper():
            raise DataError("Select two distinct states.")
        return self.data.envelope(
            records=[self._energy(state_a, year), self._energy(state_b, year)],
            calculation="Fuel share = reported fuel MWh / reported total MWh * 100.",
        )

    async def source_details(self) -> dict[str, Any]:
        """Describe the exact EIA release, source files, units and selected states/years."""
        return self.data.envelope(
            available=[record["id"] for record in self.data.records],
            units="MWh; derived shares are percentages. Null is not zero.",
        )

    async def search_research(self, query: Query, limit: Limit = 5) -> dict[str, Any]:
        """Search selected OSTI titles, organizations and subjects, not full text or abstracts."""
        return self.data.envelope(records=self.data.search(query, limit))

    async def get_record(self, record_id: RecordId) -> dict[str, Any]:
        """Get an OSTI bibliographic record and citation URL from the snapshot."""
        return self.data.envelope(record=self.data.get(record_id))

    async def summarize_research(
        self, record_ids: RecordIds, focus: Focus, ctx: Context
    ) -> dict[str, Any]:
        """One governed model call for a cited metadata-only brief; no paper-content claims."""
        return await self._brief(record_ids, focus, ctx)

    async def find_airport(self, query: Query, limit: Limit = 5) -> dict[str, Any]:
        """Find selected NASR airports by FAA/ICAO ID, name or city; non-operational only."""
        return self.data.envelope(records=self.data.search(query, limit))

    async def airport_profile(self, airport_id: RecordId) -> dict[str, Any]:
        """Read a historical airport reference record, not navigation or NOTAM information."""
        normalized = airport_id.strip().upper()
        for record in self.data.records:
            if normalized in (record["id"], record["icao"]):
                return self.data.envelope(record=record.copy())
        raise DataError("Airport is not included in this selected NASR snapshot.")

    async def source_cycle(self) -> dict[str, Any]:
        """Return the FAA source subscription cycle and the explicitly historical scope."""
        return self.data.envelope(available=[record["id"] for record in self.data.records])

    async def lookup_citation(
        self, section: Annotated[str, Field(pattern=r"^\d{1,3}\.\d{1,3}$")]
    ) -> dict[str, Any]:
        """Retrieve a selected Title 14 section as of the snapshot date, not legal advice."""
        return self.data.envelope(record=self.data.get(section))

    async def find_guidance(self, query: Query, limit: Limit = 5) -> dict[str, Any]:
        """Search selected FAA guidance-page metadata and related Title 14 references."""
        return self.data.envelope(records=self.data.search(query, limit))

    async def summarize_reference(
        self, record_ids: RecordIds, focus: Focus, ctx: Context
    ) -> dict[str, Any]:
        """Draft a cited educational reference summary; never operational aviation guidance."""
        return await self._brief(record_ids, focus, ctx)

    async def crop_summary(self, crop: Crop, year: CropYear = 2025) -> dict[str, Any]:
        """Read selected national NASS yields and rounded acreage; no farm/state estimates."""
        return self.data.envelope(record=self.data.get(f"{crop}-{year}"))

    async def compare_yields(
        self, crop: Crop, first_year: CropYear = 2024, second_year: CropYear = 2025
    ) -> dict[str, Any]:
        """Compare a crop across available years; only corn has both years in this subset."""
        if first_year == second_year:
            raise DataError("Select two distinct crop years.")
        first = self.data.get(f"{crop}-{first_year}")
        second = self.data.get(f"{crop}-{second_year}")
        return self.data.envelope(
            records=[first, second],
            change_bushels_per_acre=round(second["yield"] - first["yield"], 3),
            percent_change=round((second["yield"] / first["yield"] - 1) * 100, 3),
            calculation="Second reported national yield minus first; not a forecast.",
        )

    async def source_release(self) -> dict[str, Any]:
        """Describe the NASS release, national-only scope, precision and available records."""
        return self.data.envelope(available=[record["id"] for record in self.data.records])

    async def lookup_practice(
        self, practice_code: Annotated[str, Field(pattern=r"^\d{3}$")]
    ) -> dict[str, Any]:
        """Find a selected national NRCS practice-page record; no eligibility determination."""
        return self.data.envelope(record=self.data.get(practice_code))

    async def find_practices(self, query: Query, limit: Limit = 5) -> dict[str, Any]:
        """Search NRCS practice titles/descriptions; check current local standards separately."""
        return self.data.envelope(records=self.data.search(query, limit))

    async def summarize_practices(
        self, record_ids: RecordIds, focus: Focus, ctx: Context
    ) -> dict[str, Any]:
        """One model call to explain selected national reference pages, not site-specific advice."""
        return await self._brief(record_ids, focus, ctx)

    async def search_missions(self, query: Query, limit: Limit = 5) -> dict[str, Any]:
        """Search selected NASA Artemis I public media metadata, not current mission telemetry."""
        return self.data.envelope(records=self.data.search(query, limit))

    async def mission_sources(self, record_id: RecordId) -> dict[str, Any]:
        """Return NASA media-source metadata and a link; do not download/rehost media."""
        return self.data.envelope(record=self.data.get(record_id))

    async def build_mission_brief(
        self, record_ids: RecordIds, focus: Focus, ctx: Context
    ) -> dict[str, Any]:
        """One model call to build a cited historical briefing from selected NASA metadata."""
        return await self._brief(record_ids, focus, ctx)

    async def search_collections(self, query: Query, limit: Limit = 5) -> dict[str, Any]:
        """Search public CMR MODIS collection metadata without authenticated data downloads."""
        return self.data.envelope(records=self.data.search(query, limit))

    async def describe_collection(self, collection_id: RecordId) -> dict[str, Any]:
        """Describe one CMR collection's version and declared spatial/temporal metadata."""
        return self.data.envelope(record=self.data.get(collection_id))

    async def compare_coverage(
        self, collection_ids: Annotated[list[RecordId], Field(min_length=2, max_length=3)]
    ) -> dict[str, Any]:
        """Compare declared bounding boxes and dates, not observed granule availability."""
        if len(set(collection_ids)) < 2:
            raise DataError("Select at least two distinct collections.")
        records = self.data.select(collection_ids)
        return self.data.envelope(
            records=records,
            coverage={
                "same_declared_boxes": all(
                    record["boxes"] == records[0]["boxes"] for record in records
                ),
                "earliest_declared_start": min(
                    (record["time_start"] for record in records if record["time_start"]),
                    default=None,
                ),
                "unspecified_end_count": sum(record["time_end"] is None for record in records),
            },
            scope=(
                "CMR collection-level metadata only. Null end dates do not prove current coverage."
            ),
        )
