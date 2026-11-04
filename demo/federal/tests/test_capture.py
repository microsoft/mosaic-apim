import hashlib
import json
import sys
import urllib.request
from pathlib import Path

import pytest

import refresh_snapshots as capture


def test_capture_failure_is_nonzero_and_never_replaces_existing_snapshots(monkeypatch, capsys):
    root = Path(__file__).resolve().parents[1] / "federal_runtime" / "snapshots"
    before = {path.name: path.read_bytes() for path in root.glob("*.json")}

    def fail():
        raise OSError("private network URL/details")

    monkeypatch.setattr(capture, "CAPTURES", {"doe-energy-data": fail})
    monkeypatch.setattr(sys, "argv", ["refresh_snapshots.py", "--profile", "doe-energy-data"])
    assert capture.main() == 1
    after = {path.name: path.read_bytes() for path in root.glob("*.json")}
    assert before == after
    output = capsys.readouterr().out
    assert "outcome=error" in output and "No selected snapshots written" in output
    assert "private" not in output


def test_reprocessing_public_nass_excerpt_does_not_change_its_retrieval_date():
    excerpt = Path(__file__).resolve().parents[1] / "source_excerpts" / "nass-cropan26.txt"
    data = capture.crops(excerpt)
    assert data["provenance"]["retrieved_at"] == "2026-10-29"
    assert data["provenance"]["snapshot_generated_at"]
    source = data["provenance"]["sources"][0]
    assert source["excerpt_sha256"] == hashlib.sha256(excerpt.read_bytes()).hexdigest()
    assert "sha256" not in source
    assert data["records"][0]["yield"] == 179.3
    assert data["records"][1]["yield"] == 186.5


@pytest.mark.parametrize(
    "url",
    ["https://attacker.example/file", "file:///etc/passwd", "http://www.eia.gov/file"],
)
def test_capture_rejects_non_allowlisted_sources_before_network(url):
    with pytest.raises(ValueError, match="allowlist"):
        capture.PublicCapture().fetch(url)


@pytest.mark.parametrize("url", ["http://www.eia.gov/file", "https://attacker.example/file"])
def test_capture_rejects_redirect_outside_https_allowlist_before_following(url):
    request = urllib.request.Request("https://www.eia.gov/file")
    with pytest.raises(ValueError, match="allowlist"):
        capture.GovernmentRedirect().redirect_request(request, None, 302, "", {}, url)


def test_excerpt_and_metadata_sources_are_not_synthetic_or_mislabeled_live():
    root = Path(__file__).resolve().parents[1]
    for path in (root / "federal_runtime" / "snapshots").glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["provenance"]["mode"] == "snapshot"
        for source in payload["provenance"]["sources"]:
            if "excerpt_sha256" in source:
                assert "capture_note" in source
                assert "sha256" not in source
