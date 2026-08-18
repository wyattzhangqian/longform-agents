-- 知识效果统计表：记录每条知识的注入和引用情况
CREATE TABLE IF NOT EXISTS knowledge_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_id    TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    project_id      TEXT DEFAULT '',
    run_id          TEXT DEFAULT '',
    injected        BOOLEAN DEFAULT 1,
    referenced      BOOLEAN DEFAULT 0,
    injection_time  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_kstats_agent ON knowledge_stats(agent_id);
CREATE INDEX IF NOT EXISTS idx_kstats_knowledge ON knowledge_stats(knowledge_id);
CREATE INDEX IF NOT EXISTS idx_kstats_time ON knowledge_stats(injection_time);
