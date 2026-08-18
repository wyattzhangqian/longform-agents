-- plans 表追加模板复用字段（统一 preset_templates 的编排模板功能）
ALTER TABLE plans ADD COLUMN source TEXT NOT NULL DEFAULT 'project';
ALTER TABLE plans ADD COLUMN name TEXT NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN description TEXT NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN emoji TEXT NOT NULL DEFAULT '📋';
ALTER TABLE plans ADD COLUMN visibility TEXT NOT NULL DEFAULT 'private';
ALTER TABLE plans ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';
ALTER TABLE plans ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE plans ADD COLUMN forked_from TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_plans_source ON plans(source);
CREATE INDEX IF NOT EXISTS idx_plans_domain ON plans(domain_id);
