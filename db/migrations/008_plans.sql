-- 计划持久化（支持编辑后保存、稍后执行、历史回看）
CREATE TABLE IF NOT EXISTS plans (
    id              TEXT PRIMARY KEY,
    project_id      TEXT,
    task            TEXT NOT NULL,
    domain_id       TEXT NOT NULL DEFAULT '',
    mode            TEXT NOT NULL DEFAULT 'sequential',
    status          TEXT NOT NULL DEFAULT 'draft',  -- draft / confirmed / executed / archived
    phases          TEXT NOT NULL DEFAULT '[]',     -- JSON: [{phase_id, agent_id, label, dependencies, expected_outputs}]
    execution_layers TEXT NOT NULL DEFAULT '[]',    -- JSON: [[phase_ids...], ...]
    match_details   TEXT NOT NULL DEFAULT '[]',     -- JSON: 匹配详情
    reasoning       TEXT NOT NULL DEFAULT '',       -- 规划推理文本
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    executed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_plans_project ON plans(project_id);
CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status);
