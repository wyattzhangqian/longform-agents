-- PostgreSQL 补充表（SQLite 迁移脚本中创建、base schema 未包含）

CREATE TABLE IF NOT EXISTS llm_usage (
    id                SERIAL PRIMARY KEY,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    latency_ms        INTEGER DEFAULT 0,
    ok                INTEGER DEFAULT 1,
    created_at        TEXT DEFAULT to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_model ON llm_usage(model, created_at DESC);

CREATE TABLE IF NOT EXISTS pending_tool_approvals (
    tool_call_id TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT DEFAULT to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_pending_tool_project ON pending_tool_approvals(project_id);

CREATE TABLE IF NOT EXISTS run_locks (
    project_id   TEXT PRIMARY KEY,
    owner        TEXT NOT NULL,
    acquired_at  DOUBLE PRECISION NOT NULL,
    heartbeat_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'operator',
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT DEFAULT to_char(NOW(), 'YYYY-MM-DD HH24:MI:SS'),
    last_login_at TEXT DEFAULT ''
);

-- agents 扩展列（SQLite 通过 ALTER 迁移）
ALTER TABLE agents ADD COLUMN IF NOT EXISTS skill_ids TEXT DEFAULT '[]';
ALTER TABLE agents ADD COLUMN IF NOT EXISTS tool_ids TEXT DEFAULT '[]';
ALTER TABLE agents ADD COLUMN IF NOT EXISTS model_profile TEXT DEFAULT '{}';
ALTER TABLE agents ADD COLUMN IF NOT EXISTS input_schema TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS output_schema TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS tool_namespaces TEXT DEFAULT '[]';

-- conversations 全文检索（替代 SQLite FTS5）
CREATE INDEX IF NOT EXISTS idx_conversations_content_fts
    ON conversations USING gin(to_tsvector('simple', content));
