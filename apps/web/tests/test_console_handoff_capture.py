"""Port validation for the isolated handoff capture, without starting any servers."""

from pathlib import Path

import pytest

from scripts.screenshots import capture


class PortValidationComplete(Exception):
    pass


@pytest.fixture
def capture_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    monkeypatch.setattr(capture, "REPO_ROOT", tmp_path)

    def stop_before_browser() -> None:
        raise PortValidationComplete

    monkeypatch.setattr(capture, "_ensure_chromium", stop_before_browser)
    return [
        "--only",
        "console-portal-handoff-light",
        "--output",
        str(tmp_path / "images"),
        "--logs-dir",
        str(tmp_path / "logs"),
    ]


def test_handoff_only_does_not_require_unused_app_ports(
    monkeypatch: pytest.MonkeyPatch, capture_args: list[str]
) -> None:
    checked: list[int] = []

    def port_free(port: int) -> bool:
        checked.append(port)
        return port not in {15173, 15174}

    monkeypatch.setattr(capture, "_port_free", port_free)

    with pytest.raises(PortValidationComplete):
        capture.main(capture_args)

    assert checked == [18000, 15175]


@pytest.mark.parametrize(
    ("port", "message"),
    [
        (18000, "port 18000 is in use"),
        (15175, "--console-access-port is in use"),
    ],
)
def test_handoff_only_still_rejects_occupied_required_ports(
    monkeypatch: pytest.MonkeyPatch,
    capture_args: list[str],
    capsys: pytest.CaptureFixture[str],
    port: int,
    message: str,
) -> None:
    monkeypatch.setattr(capture, "_port_free", lambda candidate: candidate != port)

    with pytest.raises(SystemExit) as error:
        capture.main(capture_args)

    assert error.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("port", [15173, 15174])
def test_mixed_shots_still_require_normal_app_ports(
    monkeypatch: pytest.MonkeyPatch,
    capture_args: list[str],
    capsys: pytest.CaptureFixture[str],
    port: int,
) -> None:
    monkeypatch.setattr(capture, "_port_free", lambda candidate: candidate != port)

    with pytest.raises(SystemExit) as error:
        capture.main([*capture_args, "--only", "console-dashboard-light"])

    assert error.value.code == 2
    assert f"port {port} is in use" in capsys.readouterr().err
