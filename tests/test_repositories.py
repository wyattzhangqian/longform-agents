"""Repository 层测试 — 覆盖所有聚合根的基本 CRUD"""
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def mock_db():
    """Mock 数据库连接"""
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    # fetchone 返回 None（空结果）
    db.execute.return_value.fetchone = AsyncMock(return_value=None)
    db.execute.return_value.fetchall = AsyncMock(return_value=[])
    return db


@pytest.mark.asyncio
async def test_base_repository_crud(mock_db):
    from core.storage.repositories.base import BaseRepository

    repo = BaseRepository(db=mock_db)

    # find_by_id
    result = await repo.find_by_id("test_1")
    assert result is None  # mock 返回 None

    # find_all
    results = await repo.find_all()
    assert results == []

    # insert
    id_ = await repo.insert({"id": "new_1", "name": "test"})
    assert id_ == "new_1"

    # count
    count = await repo.count()
    assert count == 0

    # delete
    ok = await repo.delete("test_1")
    assert ok is True


@pytest.mark.asyncio
async def test_project_repository(mock_db):
    from core.storage.repositories.project_repo import ProjectRepository

    repo = ProjectRepository(db=mock_db)

    results = await repo.find_by_status("active")
    assert results == []

    id_ = await repo.upsert({"id": "proj_1", "name": "test"})
    assert id_ == "proj_1"


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_tenant_id_reservation(mock_db):
    """验证 tenant_id 参数预留"""
    from core.storage.repositories.base import BaseRepository

    repo = BaseRepository(db=mock_db)
    mock_db.execute.reset_mock()

    await repo.find_by_id("x", tenant_id="tenant_1")
    call_args = mock_db.execute.call_args
    assert call_args is not None
    sql = call_args[0][0]
    assert "tenant_id" in sql
