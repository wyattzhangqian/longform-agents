-- db/migrations/006_agent_templates.sql
-- Agent 模板系统 — 存储 Agent 模板定义和 LLM 即时生成的临时 Agent

CREATE TABLE IF NOT EXISTS agent_templates (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    domain          TEXT NOT NULL,
    phase           TEXT NOT NULL DEFAULT '',
    emoji           TEXT NOT NULL DEFAULT '🤖',
    role            TEXT NOT NULL DEFAULT '',
    system_prompt   TEXT NOT NULL DEFAULT '',
    capability_tags TEXT NOT NULL DEFAULT '[]',
    recommended_model TEXT NOT NULL DEFAULT 'deepseek-v4-flash',
    temperature     REAL NOT NULL DEFAULT 0.7,
    max_tokens      INTEGER NOT NULL DEFAULT 4096,
    tool_ids        TEXT NOT NULL DEFAULT '[]',
    input_schema    TEXT DEFAULT NULL,
    output_schema   TEXT DEFAULT NULL,
    source          TEXT NOT NULL DEFAULT 'user',
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_agent_templates_domain ON agent_templates(domain);
CREATE INDEX IF NOT EXISTS idx_agent_templates_source ON agent_templates(source);

CREATE TABLE IF NOT EXISTS agent_temporary (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    subtask_title   TEXT NOT NULL DEFAULT '',
    name            TEXT NOT NULL,
    emoji           TEXT NOT NULL DEFAULT '🤖',
    role            TEXT NOT NULL DEFAULT '',
    system_prompt   TEXT NOT NULL DEFAULT '',
    capability_tags TEXT NOT NULL DEFAULT '[]',
    recommended_model TEXT NOT NULL DEFAULT 'deepseek-v4-flash',
    temperature     REAL NOT NULL DEFAULT 0.7,
    max_tokens      INTEGER NOT NULL DEFAULT 4096,
    match_score     REAL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT NOT NULL,
    saved_as_template_id TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_temporary_project ON agent_temporary(project_id);
CREATE INDEX IF NOT EXISTS idx_agent_temporary_expires ON agent_temporary(expires_at);
