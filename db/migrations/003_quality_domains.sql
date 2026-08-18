-- 003_quality_domains.sql — 质量规则数据模型
-- 所有质量规则（含平台内置）存储在 DB 中，前端通过 API 读取，不硬编码。
-- 权限模型：platform 只读 + user 可 fork + user 可自建

-- 领域定义表
CREATE TABLE IF NOT EXISTS quality_domains (
    id TEXT PRIMARY KEY,                          -- "platform:comic_drama" or "user:{user_id}:my_comic"
    name TEXT NOT NULL,                           -- "漫剧"
    description TEXT DEFAULT '',
    emoji TEXT DEFAULT '📦',
    source TEXT NOT NULL DEFAULT 'platform',      -- 'platform' | 'user'
    owner_id TEXT DEFAULT '',                     -- 空=平台, 非空=用户ID
    forked_from TEXT DEFAULT '',                  -- 如果是 fork 的，记录源 domain_id
    phase_definitions TEXT DEFAULT '[]',          -- JSON: [{id, label, description, agent_role}]
    metadata TEXT DEFAULT '{}',                   -- JSON: 扩展字段
    is_archived INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 质量规则表
CREATE TABLE IF NOT EXISTS quality_rules (
    id TEXT PRIMARY KEY,                          -- "platform:comic_drama:script.dialogue_length"
    domain_id TEXT NOT NULL,                      -- FK → quality_domains.id
    rule_id TEXT NOT NULL,                        -- 短ID "script.dialogue_length"
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    phase_id TEXT DEFAULT '',                     -- 适用的阶段（空=所有阶段）
    check_type TEXT NOT NULL,                     -- 检查器类型
    severity TEXT NOT NULL DEFAULT 'warning',     -- error | warning | info
    enabled INTEGER DEFAULT 1,
    config TEXT DEFAULT '{}',                     -- JSON: 检查参数
    fix_hint TEXT DEFAULT '',                     -- 修复建议模板
    auto_fix_capable INTEGER DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'platform',      -- 'platform' | 'user'
    owner_id TEXT DEFAULT '',
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (domain_id) REFERENCES quality_domains(id) ON DELETE CASCADE
);

-- 规则集（模板绑定用）
CREATE TABLE IF NOT EXISTS quality_rule_sets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domain_id TEXT NOT NULL,
    strictness TEXT DEFAULT 'standard',           -- relaxed | standard | strict
    rule_overrides TEXT DEFAULT '{}',             -- JSON: {rule_id: {config overrides}}
    disabled_rules TEXT DEFAULT '[]',             -- JSON: [rule_id, ...]
    source TEXT NOT NULL DEFAULT 'platform',
    owner_id TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (domain_id) REFERENCES quality_domains(id)
);

-- 规则运行统计（per project）
CREATE TABLE IF NOT EXISTS quality_rule_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    domain_id TEXT NOT NULL,
    project_id TEXT DEFAULT '',
    trigger_count INTEGER DEFAULT 0,
    pass_count INTEGER DEFAULT 0,
    last_triggered TEXT,
    last_violation_message TEXT DEFAULT '',
    UNIQUE(rule_id, domain_id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_quality_rules_domain ON quality_rules(domain_id);
CREATE INDEX IF NOT EXISTS idx_quality_rules_source ON quality_rules(source, owner_id);
CREATE INDEX IF NOT EXISTS idx_quality_domains_source ON quality_domains(source, owner_id);
