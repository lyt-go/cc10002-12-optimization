"""测试夹具：每个用例重置内存仓库，避免用例之间互相污染版本账本。"""
from __future__ import annotations

import pytest

from app.seed import SEED_ROWS
from app.store import store


@pytest.fixture(autouse=True)
def reset_store() -> None:
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
