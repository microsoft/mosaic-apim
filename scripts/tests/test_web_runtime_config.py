import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def shell_path(path: Path) -> str:
    if path.drive:
        return f"/{path.drive[0].lower()}/" + "/".join(path.parts[1:])
    return path.as_posix()


@pytest.mark.parametrize(
    "portal",
    [
        None,
        "https://portal.example.test",
        "'; injected = true; //",
        'https://portal.example.test/"\\\r\n\t',
    ],
)
def test_portal_configuration_remains_data_at_container_startup(
    tmp_path: Path, portal: str | None
) -> None:
    shell = shutil.which("sh")
    if os.name == "nt":
        git = shutil.which("git")
        if git is not None:
            git_exec = subprocess.check_output([git, "--exec-path"], text=True).strip()
            candidate = Path(git_exec).parents[2] / "bin" / "bash.exe"
            if candidate.exists():
                shell = str(candidate)
    node = shutil.which("node")
    if shell is None or node is None:
        pytest.skip("Entrypoint verification requires a POSIX shell and Node.js")
    if subprocess.run([shell, "-c", "command -v envsubst"], capture_output=True).returncode:
        pytest.skip("Entrypoint verification requires envsubst")
    output = tmp_path / "config.js"
    entrypoint = (ROOT / "apps" / "web" / "40-runtime-config.sh").read_text()
    script = entrypoint.replace(
        "/opt/mosaic/config.template.js",
        shlex.quote(shell_path(ROOT / "apps" / "web" / "config.template.js")),
    ).replace("/usr/share/nginx/html/config.js", shlex.quote(shell_path(output)))
    env = {
        **os.environ,
        "MOSAIC_API_BASE_URL": "https://api.example.test",
        "MOSAIC_ENTRA_TENANT_ID": "fictional-tenant",
        "MOSAIC_ENTRA_CLIENT_ID": "fictional-client",
        "MOSAIC_ENTRA_API_SCOPE": "api://fictional/access_as_user",
        "MOSAIC_APPLICATIONINSIGHTS_CONNECTION_STRING": "",
    }
    env.pop("MOSAIC_PORTAL_URL", None)
    if os.name == "nt":
        # Git Bash must not rewrite escaped configuration as a Windows filesystem path.
        env["MSYS2_ENV_CONV_EXCL"] = (
            env.get("MSYS2_ENV_CONV_EXCL", "") + ";MOSAIC_PORTAL_URL"
        ).lstrip(";")
    if portal is not None:
        env["MOSAIC_PORTAL_URL"] = portal
    subprocess.run([shell, "-c", script], env=env, check=True, capture_output=True)
    result = subprocess.check_output(
        [
            node,
            "-e",
            "const fs=require('node:fs'),vm=require('node:vm');"
            "const context={window:{},injected:false};"
            "vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);"
            "process.stdout.write(JSON.stringify(context));",
            str(output),
        ],
        text=True,
    )
    context = json.loads(result)
    assert context["injected"] is False
    assert context["window"]["__MOSAIC_CONFIG__"]["portalUrl"] == (portal or "")
    assert context["window"]["__MOSAIC_CONFIG__"]["authMode"] == "entra"
