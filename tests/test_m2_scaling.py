"""M2 规模化测试 — Postgres SQL 兼容 / OTel / Redis Worker 发布 / 沙箱"""

import pytest


class TestPostgresSqlCompat:
    def test_named_params_conversion(self):
        from core.storage.postgres import prepare_sql

        sql = "SELECT * FROM t WHERE a = :x AND b = :y"
        converted, params = prepare_sql(sql, {"x": 1, "y": "two"})
        assert "$1" in converted and "$2" in converted
        assert params == (1, "two")

    def test_qmark_conversion(self):
        from core.storage.postgres import prepare_sql

        sql = "SELECT * FROM t WHERE id = ?"
        converted, params = prepare_sql(sql, (42,))
        assert converted == "SELECT * FROM t WHERE id = $1"
        assert params == (42,)

    def test_datetime_dialect(self):
        from core.storage.postgres import prepare_sql

        sql = "INSERT INTO t (created_at) VALUES (datetime('now', 'localtime'))"
        converted, _ = prepare_sql(sql, ())
        assert "datetime(" not in converted
        assert "NOW()" in converted

    def test_insert_or_ignore(self):
        from core.storage.postgres import prepare_sql

        sql = "INSERT OR IGNORE INTO users (id, username) VALUES (?, ?)"
        converted, params = prepare_sql(sql, ("u1", "alice"))
        assert "OR IGNORE" not in converted.upper()
        assert "$1" in converted
        assert params == ("u1", "alice")

    def test_pending_tools_upsert(self):
        from core.storage.postgres import prepare_sql

        sql = """INSERT OR REPLACE INTO pending_tool_approvals
            (tool_call_id, project_id, payload) VALUES (?, ?, ?)"""
        converted, _ = prepare_sql(sql, ("tc1", "p1", "{}"))
        assert "ON CONFLICT (tool_call_id)" in converted

    def test_should_returning_id_for_conversations(self):
        from core.storage.postgres import _should_returning_id

        assert _should_returning_id("INSERT INTO conversations (content) VALUES (?)")
        assert not _should_returning_id("UPDATE conversations SET content = ?")


class TestOtel:
    def test_disabled_by_default(self):
        from core.observability.otel import otel_status

        status = otel_status()
        assert status["enabled"] is False
        assert status["initialized"] is False


class TestSandbox:
    def test_deny_sudo(self):
        from core.gateway.sandbox import check_command_denied

        assert check_command_denied("sudo rm -rf /") is not None

    def test_allow_safe_echo(self):
        from core.gateway.sandbox import check_command_denied

        assert check_command_denied("echo hello") is None


@pytest.mark.asyncio
class TestRedisPublisherOnly:
    async def test_start_publisher_only_no_subscribe(self, monkeypatch):
        from core.events import EventBus
        from core.observability.redis_bridge import RedisSSEBridge

        monkeypatch.setenv("SSE_REDIS_ENABLED", "false")
        import config
        monkeypatch.setattr(config, "SSE_REDIS_ENABLED", False)

        bridge = RedisSSEBridge(EventBus())
        await bridge.start_publisher_only()
        assert bridge._pubsub is None
