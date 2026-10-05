import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SMOKE_SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "smoke.py"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def smoke() -> ModuleType:
    """The deployment kit's smoke checker, which runs against the real server here."""

    spec = importlib.util.spec_from_file_location("smoke", SMOKE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
