"""CLI encoding and failure handling without Azure, credentials or network access."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import deploy


@pytest.fixture
def windows_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    launcher = tmp_path / "CLI with spaces" / "wbin" / "az.cmd"
    launcher.parent.mkdir(parents=True)
    python = launcher.parent.parent / "python.exe"
    python.touch()
    monkeypatch.setattr(deploy.shutil, "which", lambda name: str(launcher))
    # Replace only deploy's reference, not os.name used by pathlib and pytest.
    monkeypatch.setattr(deploy, "os", SimpleNamespace(name="nt"))
    return python


def test_windows_bundled_python_explicitly_enables_utf8(windows_cli: Path) -> None:
    assert deploy.az_command() == [str(windows_cli), "-I", "-B", "-X", "utf8", "-m", "azure.cli"]


def test_windows_cmd_without_bundled_python_is_preserved(windows_cli: Path) -> None:
    windows_cli.unlink()
    assert deploy.az_command() == [str(windows_cli.parent / "wbin" / "az.cmd")]


@pytest.mark.parametrize(("platform", "launcher"), [("nt", "az.exe"), ("posix", "az")])
def test_other_launchers_are_preserved(
    monkeypatch: pytest.MonkeyPatch, platform: str, launcher: str
) -> None:
    monkeypatch.setattr(deploy, "os", SimpleNamespace(name=platform))
    monkeypatch.setattr(deploy.shutil, "which", lambda name: launcher)
    assert deploy.az_command() == [launcher]


def test_missing_cli_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploy.shutil, "which", lambda name: None)
    with pytest.raises(deploy.KitError, match="isn't on PATH"):
        deploy.az_command()


@pytest.mark.parametrize(
    ("status", "stdout", "stderr", "expected"),
    [
        (0, '{"message": "構築 ✓ café"}', "", {"message": "構築 ✓ café"}),
        (0, "", "", None),
        (1, "", "earlier line\n構築 failed ✓\n", "構築 failed ✓"),
        (2, "build failed: café", "", "build failed: café"),
        (1, "", "", "no output"),
    ],
)
def test_captured_output_uses_utf8_and_preserves_failures(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    stdout: str,
    stderr: str,
    expected: object,
) -> None:
    monkeypatch.setattr(deploy, "az_command", lambda: ["fake-az"])
    runner = Mock(return_value=subprocess.CompletedProcess([], status, stdout, stderr))
    monkeypatch.setattr(deploy.subprocess, "run", runner)
    args = ["deployment", "sub", "show"]
    if status:
        with pytest.raises(deploy.KitError) as error:
            deploy.run_az(args, capture=True)
        assert str(error.value) == f"az deployment sub show failed: {expected}"
    else:
        assert deploy.run_az(args, capture=True) == expected
    runner.assert_called_once_with(
        ["fake-az", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@pytest.mark.parametrize("status", [0, 1])
def test_build_logs_still_stream_and_failures_still_raise(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr(deploy, "az_command", lambda: ["fake-az"])
    runner = Mock(return_value=subprocess.CompletedProcess([], status))
    monkeypatch.setattr(deploy.subprocess, "run", runner)
    args = ["acr", "build", "--registry", "example", "."]
    if status:
        with pytest.raises(deploy.KitError, match="az acr build failed; its output is above"):
            deploy.run_az(args, capture=False)
    else:
        assert deploy.run_az(args, capture=False) is None
    runner.assert_called_once_with(["fake-az", *args], check=False)


@pytest.mark.parametrize("capture", [False, True])
@pytest.mark.parametrize("status", [0, 1])
def test_isolated_python_handles_unicode_even_with_legacy_encoding_environment(
    windows_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    capture: bool,
    status: int,
) -> None:
    # Exercise the constructed interpreter flags with a local script, never azure.cli.
    flags = deploy.az_command()[1:-2]
    payload = {"message": "構築 ✓ café"}
    script = (
        "import sys; assert sys.stdout.encoding == 'utf-8'; "
        "assert sys.stderr.encoding == 'utf-8'; "
        f"print({json.dumps(payload, ensure_ascii=False)!a}); "
        f"sys.stderr.buffer.write(b'diagnostic: \\xff\\n'); sys.exit({status})"
    )
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setattr(deploy, "az_command", lambda: [sys.executable, *flags, "-c", script])
    if status:
        with pytest.raises(deploy.KitError) as error:
            deploy.run_az(["acr", "build"], capture=capture)
        expected = (
            "az acr build failed: diagnostic: \ufffd"
            if capture
            else "az acr build failed; its output is above."
        )
        assert str(error.value) == expected
    else:
        result = deploy.run_az(["acr", "build"], capture=capture)
        assert result == (payload if capture else None)
    if not capture:
        assert json.loads(capfd.readouterr().out) == payload
