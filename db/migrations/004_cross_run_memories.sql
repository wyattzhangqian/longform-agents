-- ============================================================
-- 004: 跨 Run 记忆系统（Memory Evolution）
-- ============================================================

-- 跨 run 记忆表：从已完成 run 中提取的持久化记忆
CREATE TABLE IF NOT EXISTS cross_run_memories (
    id              TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    memory_type     TEXT NOT NULL DEFAULT 'user_preference',  -- user_preference | quality_pattern | domain_knowledge | workflow_insight
    content         TEXT NOT NULL,                             -- 提取的记忆内容（自然语言）
    source_run_id   TEXT DEFAULT '',
    source_phase    TEXT DEFAULT '',
    confidence      REAL DEFAULT 0.6,                          -- 0-1
    created_at      TEXT DEFAULT (datetime('now', 'localtime')),
    last_accessed   TEXT DEFAULT (datetime('now', 'localtime')),
    access_count    INTEGER DEFAULT 0,
    decay_score     REAL DEFAULT 1.0,                           -- 衰减分数，< threshold 时 GC
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE INDEX IF NOT EXISTS idx_cross_run_agent ON cross_run_memories(agent_id);
CREATE INDEX IF NOT EXISTS idx_cross_run_decay ON cross_run_memories(agent_id, decay_score);
CREATE INDEX IF NOT EXISTS idx_cross_run_type ON cross_run_memories(agent_id, memory_type);

-- GC 统计表：记录每个 agent 的 GC 执行历史
CREATE TABLE IF NOT EXISTS cross_run_memory_gc_stats (
    agent_id        TEXT PRIMARY KEY,
    total_gc_runs   INTEGER DEFAULT 0,
    total_removed   INTEGER DEFAULT 0,
    last_gc_at      TEXT DEFAULT '',
    current_capacity INTEGER DEFAULT 0
);
