import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))          # make `zoterocite` importable
FIXTURES = ROOT / "fixtures"


@pytest.fixture
def fixtures():
    return FIXTURES
