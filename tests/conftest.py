import copy
import json
import sys
from pathlib import Path

import pytest

CORE = Path(__file__).parent.parent / "core"
sys.path.insert(0, str(CORE))

_FACTS = json.loads((CORE / "facts.json").read_text(encoding="utf-8"))
_POLICY = json.loads((CORE / "policy.json").read_text(encoding="utf-8"))


@pytest.fixture
def facts():
    return copy.deepcopy(_FACTS)


@pytest.fixture
def policy():
    """테스트가 권장 여유·기본값을 바꿔도 다른 테스트에 새지 않게 매번 복사한다."""
    return copy.deepcopy(_POLICY)
