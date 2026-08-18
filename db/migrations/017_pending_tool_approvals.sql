-- 补 pending_tool_approvals 表（待审批工具持久化）。
-- database.py 的内联建表在 _run_migrations 的 return 之后（死代码），从不执行；migrations 里也缺，
-- 导致 SQLite 无此表，pending_tools 落库/恢复失败（test_register_persists_and_reloads: assert 0>=1，
-- 后端启动日志也报 "no such table: pending_tool_approvals"）。
-- 建表语句照搬自 database.py 死代码区，与 db/schema.pg.extras.sql 一致。
CREATE TABLE IF NOT EXISTS pending_tool_approvals (
    tool_call_id TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_pending_tool_project ON pending_tool_approvals(project_id);
