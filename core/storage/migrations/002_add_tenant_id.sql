-- 002: 预留多租户隔离列
-- 当前不启用（tenant_id 默认 NULL 表示单租户模式），
-- 多租户到来时只需填充 tenant_id 值，不改接口。

-- agents
ALTER TABLE agents ADD COLUMN tenant_id TEXT DEFAULT NULL;

-- projects
ALTER TABLE projects ADD COLUMN tenant_id TEXT DEFAULT NULL;

-- skills
ALTER TABLE skills ADD COLUMN tenant_id TEXT DEFAULT NULL;

-- preset_templates
ALTER TABLE preset_templates ADD COLUMN tenant_id TEXT DEFAULT NULL;

-- quality_domains
ALTER TABLE quality_domains ADD COLUMN tenant_id TEXT DEFAULT NULL;

-- quality_rules
ALTER TABLE quality_rules ADD COLUMN tenant_id TEXT DEFAULT NULL;

-- knowledge_entries
ALTER TABLE knowledge_entries ADD COLUMN tenant_id TEXT DEFAULT NULL;

-- 索引（仅对非 NULL 值建立索引以减少开销）
CREATE INDEX IF NOT EXISTS idx_agents_tenant ON agents(tenant_id) WHERE tenant_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_projects_tenant ON projects(tenant_id) WHERE tenant_id IS NOT NULL;
