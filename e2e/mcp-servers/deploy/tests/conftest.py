import json
import os
import subprocess
from typing import Any

import pytest

import deploy


@pytest.fixture(scope="session")
def template() -> dict[str, Any]:
    """main.bicep compiled to ARM JSON, as CI's Bicep build compiles it."""

    try:
        command = deploy.az_command()
    except deploy.KitError:
        command = None
    if command is not None:
        result = subprocess.run(
            [*command, "bicep", "build", "--file", str(deploy.TEMPLATE), "--stdout"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    if os.environ.get("MCP_SERVERS_REQUIRE_BICEP"):
        pytest.fail("The Azure CLI couldn't build main.bicep, and CI requires it.")
    pytest.skip("The Azure CLI and Bicep aren't available here.")
