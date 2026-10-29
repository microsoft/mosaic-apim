"""Compile this independent runtime with the repository's existing hash-lock compiler."""

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPILER = HERE.parents[1] / "e2e" / "mcp-servers" / "compile_requirements.py"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pip-tools", action="store_true", help="Use pip's TLS transport.")
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("mcp_requirements", COMPILER)
    if spec is None or spec.loader is None:
        raise RuntimeError("The repository requirements compiler is missing.")
    compiler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compiler)
    for source in ("requirements.in", "requirements-dev.in"):
        if args.pip_tools:
            output = source.replace(".in", ".txt")
            path = HERE / output
            if not path.exists():
                # Direct/runtime constraints deliberately match M-agent. Reuse its known hashes
                # as the compiler seed rather than downloading every wheel for every platform.
                shutil.copyfile(COMPILER.parent / "m-agent" / output, path)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "piptools",
                    "compile",
                    source,
                    "--generate-hashes",
                    "--reuse-hashes",
                    "--allow-unsafe",
                    "--strip-extras",
                    "--no-emit-index-url",
                    "--no-emit-trusted-host",
                    "--quiet",
                    "--output-file",
                    output,
                ],
                cwd=HERE,
                check=True,
            )
            path.write_text(
                compiler.keep_sha256_only(path.read_text(encoding="utf-8")), encoding="utf-8"
            )
        else:
            compiler.compile_file(HERE, source, None)


if __name__ == "__main__":
    main()
