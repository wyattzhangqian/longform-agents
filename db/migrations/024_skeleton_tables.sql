-- 长内容支持：在 run_context 的 checkpoint 中已有存储（JSON 字段）
-- 此迁移为常用查询添加辅助索引和统计表

-- 连续性线索统计（可选，用于 dashboard 展示）
CREATE TABLE IF NOT EXISTS continuity_stats (
    project_id TEXT NOT NULL,
    total_threads INTEGER DEFAULT 0,
    introduced INTEGER DEFAULT 0,
    referenced INTEGER DEFAULT 0,
    resolved INTEGER DEFAULT 0,
    overdue INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT '',
    PRIMARY KEY (project_id)
);
