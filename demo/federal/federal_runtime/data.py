import json
from copy import deepcopy
from importlib.resources import files
from typing import Any

from federal_runtime.catalog import DISCLAIMER, PROFILES

MAX_RESULT_BYTES = 48_000


class DataError(Exception):
    """A safe, deterministic data-selection failure."""


class Snapshot:
    def __init__(self, profile: str) -> None:
        if profile not in PROFILES:
            raise ValueError("Unknown FEDERAL_MCP_PROFILE.")
        raw = files("federal_runtime").joinpath("snapshots", f"{profile}.json").read_bytes()
        if len(raw) > 200_000:
            raise ValueError("Snapshot exceeds the runtime limit.")
        payload = json.loads(raw)
        provenance = payload["provenance"]
        if (
            payload["profile"] != profile
            or provenance["mode"] != "snapshot"
            or not provenance["release"]
            or not provenance["retrieved_at"]
            or not provenance["sources"]
            or not payload["records"]
        ):
            raise ValueError("Snapshot has no usable public provenance.")
        self.profile = profile
        self.provenance: dict[str, Any] = provenance
        self.records: list[dict[str, Any]] = payload["records"]

    def envelope(self, **result: Any) -> dict[str, Any]:
        value = {
            "profile": self.profile,
            "provenance": deepcopy(self.provenance),
            "disclaimer": DISCLAIMER,
            **result,
        }
        if len(json.dumps(value).encode("utf-8")) > MAX_RESULT_BYTES:
            raise DataError("The selected result exceeds the bounded response limit.")
        return value

    def get(self, record_id: str) -> dict[str, Any]:
        for record in self.records:
            if record["id"].casefold() == record_id.strip().casefold():
                return deepcopy(record)
        raise DataError("No matching record exists in this selected snapshot.")

    def select(self, record_ids: list[str]) -> list[dict[str, Any]]:
        return [self.get(record_id) for record_id in dict.fromkeys(record_ids)]

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        words = query.casefold().split()
        if not words:
            raise DataError("Enter a nonblank search query.")
        return [
            deepcopy(record)
            for record in self.records
            if all(word in json.dumps(record).casefold() for word in words)
        ][:limit]
