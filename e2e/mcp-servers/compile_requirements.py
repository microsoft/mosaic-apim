"""Regenerate each server's pinned, hashed requirements files.

Run it from anywhere with Python 3.12 or later and uv installed:

    python e2e/mcp-servers/compile_requirements.py

Pass --index-url to resolve from a mirror instead of PyPI. The files keep only sha256 hashes:
some mirrors also publish md5 hashes, and pip refuses those in hash-checking mode.

The servers aren't part of the repository's uv workspace, so uv runs with --no-config. The
workspace's settings, such as its exclude-newer cutoff, don't apply to them.
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVERS = ("m-tools", "m-agent", "deploy")
PYTHON_VERSION = "3.13"
_HASH_LINE = re.compile(r"^\s+--hash=(?P<algorithm>[a-z0-9]+):")


def uv_command() -> list[str]:
    uv = shutil.which("uv")
    return [uv] if uv else [sys.executable, "-m", "uv"]


def keep_sha256_only(text: str) -> str:
    """Drop hash lines that aren't sha256, keeping each requirement's line continuations valid."""

    lines = [
        line
        for line in text.splitlines()
        if not ((match := _HASH_LINE.match(line)) and match["algorithm"] != "sha256")
    ]
    for index, line in enumerate(lines):
        continues = index + 1 < len(lines) and _HASH_LINE.match(lines[index + 1])
        stripped = line.rstrip().removesuffix("\\").rstrip()
        lines[index] = f"{stripped} \\" if continues else stripped
    return "\n".join(lines) + "\n"


def compile_file(folder: Path, source: str, index_url: str | None) -> None:
    output = source.replace(".in", ".txt")
    arguments = [
        "pip",
        "compile",
        source,
        "--no-config",
        "--universal",
        "--python-version",
        PYTHON_VERSION,
        "--generate-hashes",
        "--output-file",
        output,
    ]
    command = [*uv_command(), *arguments]
    if index_url:
        command += ["--index-url", index_url]
    command += ["--custom-compile-command", " ".join(["uv", *arguments])]
    subprocess.run(command, cwd=folder, check=True)
    path = folder / output
    path.write_text(keep_sha256_only(path.read_text(encoding="utf-8")), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index-url", help="A package index to resolve from instead of PyPI.")
    arguments = parser.parse_args()
    for server in SERVERS:
        folder = HERE / server
        for source in ("requirements.in", "requirements-dev.in"):
            if (folder / source).exists():
                compile_file(folder, source, arguments.index_url)


if __name__ == "__main__":
    main()
