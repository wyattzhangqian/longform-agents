-- Artifact 状态机：为 artifacts 表增加 status 字段
ALTER TABLE artifacts ADD COLUMN status TEXT NOT NULL DEFAULT 'draft';
-- 可选值: draft | review | approved | deprecated

ALTER TABLE artifacts ADD COLUMN approved_by TEXT DEFAULT '';
ALTER TABLE artifacts ADD COLUMN approved_at TEXT DEFAULT '';

-- 为查询优化添加索引
CREATE INDEX IF NOT EXISTS idx_artifacts_status ON artifacts(project_id, status);
