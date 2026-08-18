-- Agent 协作平台 - 数据库 Schema
-- 基于旧版 comic-workflow-backup/db/schema.sql 设计，完整迁移

-- ============================================================
-- Agent 定义（6层完整定义）
-- ============================================================
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT 'custom',     -- domain | custom | template
    status      TEXT NOT NULL DEFAULT 'active',     -- active | inactive | training
    emoji       TEXT DEFAULT '🤖',
    role        TEXT DEFAULT '',

    -- 能力层
    capabilities        TEXT DEFAULT '[]',          -- JSON: [讲故事, 角色设计, ...]
    current_version     TEXT DEFAULT '1.0.0',
    creator             TEXT DEFAULT 'user',
    model               TEXT DEFAULT 'deepseek-v4-flash',
    specialized_knowledge TEXT DEFAULT '[]',        -- JSON: [编剧理论, ...]
    reasoning_engine    TEXT DEFAULT 'chain-of-thought',

    -- 记忆层
    memory_config       TEXT DEFAULT '{}',          -- JSON: { episodic, semantic, procedural }

    -- 工具层
    tools               TEXT DEFAULT '[]',          -- JSON: [{ name, description, endpoint }]
    tool_ids            TEXT DEFAULT '[]',          -- JSON: ToolCatalog 直接挂载的工具 id
    skill_ids           TEXT DEFAULT '[]',          -- JSON: 指令/可执行/mcp 技能包 id
    tool_namespaces     TEXT DEFAULT '[]',          -- JSON: 需要访问的工具命名空间
    tool_configs        TEXT DEFAULT '{}',          -- JSON: per-tool 预设 (如{"generate_image":{"default_size":"1920x1080"}})
    model_profile       TEXT DEFAULT '{}',          -- JSON: 多模态模型配置（llm/image/video/music）
    input_schema        TEXT,                       -- JSON Schema: 期望输入结构
    output_schema       TEXT,                       -- JSON Schema: 承诺输出结构

    -- 交互层
    interaction_config  TEXT DEFAULT '{}',          -- JSON: { style, transparency, protocol }
    system_prompt       TEXT DEFAULT '',
    peers               TEXT DEFAULT '[]',          -- JSON: [agent_id, ...] 协作白名单

    -- LLM 调参
    temperature         REAL DEFAULT 0.7,
    max_tokens          INTEGER DEFAULT 4096,

    -- 进化数据
    evolution_history   TEXT DEFAULT '[]',          -- JSON: [{ version, changes, timestamp }]
    performance_metrics TEXT DEFAULT '{}',          -- JSON: { accuracy, creativity, speed, user_rating }
    runtime_info        TEXT DEFAULT '{}',          -- JSON: { deployment, endpoint, ... }
    extra               TEXT DEFAULT '{}',          -- JSON: 扩展字段（策略提示等）

    created_at  TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 存量数据库迁移（仅当列不存在时执行，幂等）
-- SQLite 不支持 IF NOT EXISTS 在 ALTER TABLE 上，需通过 Python 端处理
-- 如有旧库缺少此两列，运行: sqlite3 data/agent_platform.db "ALTER TABLE agents ADD COLUMN temperature REAL DEFAULT 0.7; ALTER TABLE agents ADD COLUMN max_tokens INTEGER DEFAULT 4096;"

-- ============================================================
-- 项目
-- ============================================================
CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    type            TEXT DEFAULT 'custom',           -- custom | novel | video | general
    status          TEXT DEFAULT 'idle',            -- idle | running | paused | completed | failed
    emoji           TEXT DEFAULT '🎬',
    mode            TEXT DEFAULT 'sequential',      -- sequential | parallel | roundtable
    config          TEXT DEFAULT '{}',              -- JSON: { idea, style, episodes, ... }
    workflow_state  TEXT DEFAULT '{}',              -- JSON: { phases:[], current:null }
    template_id     TEXT,                           -- 使用的模板 ID
    checkpoint_state TEXT DEFAULT '',               -- JSON: WorkflowState checkpoint
    checkpoint_version INTEGER DEFAULT 0,           -- 乐观锁版本号
    access_token    TEXT DEFAULT '',                -- 项目级访问令牌
    created_at      TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at      TEXT DEFAULT (datetime('now', 'localtime'))
);

-- ============================================================
-- 项目 ↔ Agent 关联（项目组装的 Agent 团队）
-- ============================================================
CREATE TABLE IF NOT EXISTS project_agents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    join_order      INTEGER DEFAULT 0,             -- 执行顺序
    role            TEXT DEFAULT '',                -- 在项目中扮演的角色
    status          TEXT DEFAULT 'active',
    competition_group TEXT DEFAULT '',              -- 竞争组标识
    joined_at       TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

-- ============================================================
-- Agent 对话记录（核心）
-- ============================================================
CREATE TABLE IF NOT EXISTS conversations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL,
    phase           TEXT DEFAULT '',                -- 所属阶段
    sender_type     TEXT NOT NULL DEFAULT 'agent',  -- agent | user | system
    sender_id       TEXT DEFAULT '',
    receiver_id     TEXT DEFAULT '',
    message_type    TEXT NOT NULL DEFAULT 'chat',   -- task | handoff | review_request | question | answer | escalate | decide | status | thinking | alert | chat
    content         TEXT NOT NULL,
    parent_id       INTEGER,                        -- 回复的消息 ID
    metadata        TEXT DEFAULT NULL,              -- JSON 附加数据
    consensus_reached INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- FTS5 全文搜索索引
CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
    content,
    content_rowid='rowid',
    tokenize='unicode61'
);

-- ============================================================
-- 系统设置
-- ============================================================
CREATE TABLE IF NOT EXISTS system_settings (
    key         TEXT PRIMARY KEY,          -- 配置键
    value       TEXT NOT NULL,             -- 配置值（JSON 或纯文本）
    category    TEXT DEFAULT 'general',    -- 分类：llm | image | cache | system
    description TEXT DEFAULT '',
    updated_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 系统设置初始数据
INSERT OR IGNORE INTO system_settings (key, value, category, description) VALUES
-- LLM 平台基础模型
('llm_model',           '"deepseek-v4-flash"',          'llm',    '默认 LLM 模型 (deepseek-v4-flash | deepseek-v4-pro)'),
('llm_api_key',         '"sk-xxxxxxxx"',                 'llm',    'LLM API Key'),
('llm_base_url',        '"https://api.deepseek.com/v1"', 'llm',    'LLM Base URL'),
('llm_temperature',     '0.7',                           'llm',    'LLM 温度'),
('llm_max_token',       '4096',                          'llm',    'LLM Max Token'),

-- 缓存
('cache_enabled',       'true',                          'cache',  '缓存开关');

-- ============================================================
-- 决策卡（Agent 分歧时升级给用户）
-- ============================================================
CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL,
    conversation_id INTEGER,
    phase           TEXT DEFAULT '',
    question        TEXT NOT NULL,                  -- 问题描述
    options         TEXT NOT NULL DEFAULT '[]',     -- JSON: [{ id, label, agentId, agentView }]
    escalated_by    TEXT DEFAULT '',                -- 哪个 Agent 发起
    chosen_option   TEXT DEFAULT NULL,              -- 用户选择
    decided_by      TEXT DEFAULT NULL,              -- agent | user
    resolved_at     TEXT DEFAULT NULL,
    created_at      TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);

-- ============================================================
-- Agent 记忆（三类记忆）
-- ============================================================
CREATE TABLE IF NOT EXISTS agent_memories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id        TEXT NOT NULL,
    project_id      TEXT NOT NULL DEFAULT '',
    memory_type     TEXT NOT NULL DEFAULT 'episodic', -- episodic | semantic | procedural
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    importance      REAL DEFAULT 0.5,
    access_count    INTEGER DEFAULT 0,
    last_accessed   TEXT,
    created_at      TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (agent_id) REFERENCES agents(id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_memories_agent ON agent_memories(agent_id, memory_type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_unique ON agent_memories(agent_id, project_id, memory_type, key);

-- ============================================================
-- Agent 学习记录（进化）
-- ============================================================
CREATE TABLE IF NOT EXISTS agent_learnings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id        TEXT NOT NULL,
    project_id      TEXT,
    knowledge_gained TEXT DEFAULT '',
    capability_scores_before TEXT DEFAULT '{}',
    capability_scores_after  TEXT DEFAULT '{}',
    created_at      TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (agent_id) REFERENCES agents(id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- ============================================================
-- 产物库
-- ============================================================
CREATE TABLE IF NOT EXISTS artifacts (
    id              TEXT PRIMARY KEY,
    project_id      TEXT,
    agent_id        TEXT,
    type            TEXT NOT NULL,                  -- 文本 | 图片 | 音频 | 视频 | 通用
    name            TEXT NOT NULL,
    file_path       TEXT,
    metadata        TEXT DEFAULT '{}',              -- JSON: { prompt, dimensions, quality_score, ... }
    parent_id       TEXT,                           -- 衍生源
    usage_count     INTEGER DEFAULT 0,
    run_id          TEXT DEFAULT '',                -- P0-3 产物归属校验
    created_at      TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

-- ============================================================
-- 预设模板（Agent 组合方案）
-- ============================================================
CREATE TABLE IF NOT EXISTS preset_templates (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    emoji           TEXT DEFAULT '📋',
    description     TEXT DEFAULT '',
    project_type    TEXT DEFAULT '',               -- [DEPRECATED] 保留兼容，新代码使用 tags
    tags            TEXT DEFAULT '[]',              -- JSON: ["comic", "novel", ...] 自由标签
    agent_ids       TEXT NOT NULL DEFAULT '[]',     -- JSON: [agent_id, ...] 顺序
    workflow_config TEXT DEFAULT '{}',              -- JSON: { pattern, review_mode, ... }
    is_default      INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now', 'localtime'))
);

-- ============================================================
-- 种子数据：通用平台不预置任何业务 Agent 或模板。
-- 用户通过 API / UI 自行注册 Agent、创建项目、组合模板。
-- ============================================================

-- ============================================================
-- Platform-Core v2: Sessions（会话生命周期）
-- ============================================================
CREATE TABLE IF NOT EXISTS sessions (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    domain_id           TEXT NOT NULL DEFAULT 'general',
    status              TEXT DEFAULT 'idle',
    mode                TEXT DEFAULT 'sequential',
    participants        TEXT DEFAULT '[]',
    protocol            TEXT DEFAULT '',
    config_snapshot     TEXT DEFAULT '{}',
    total_rounds        INTEGER DEFAULT 0,
    total_agents_executed INTEGER DEFAULT 0,
    artifacts_produced  INTEGER DEFAULT 0,
    quality_scores      TEXT DEFAULT '{}',
    created_at          TEXT DEFAULT (datetime('now', 'localtime')),
    started_at          TEXT,
    completed_at        TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

-- ============================================================
-- Platform-Core v2: Domain Adapters（已注册的领域适配器）
-- ============================================================
CREATE TABLE IF NOT EXISTS domain_adapters (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    version         TEXT DEFAULT '1.0.0',
    config          TEXT DEFAULT '{}',
    registered_at   TEXT DEFAULT (datetime('now', 'localtime')),
    active          INTEGER DEFAULT 1
);

-- ============================================================
-- Platform-Core v2: Asset Versions（资产版本）
-- ============================================================
CREATE TABLE IF NOT EXISTS asset_versions (
    version_id      TEXT PRIMARY KEY,
    asset_id        TEXT NOT NULL,
    version_num     INTEGER DEFAULT 1,
    content_hash    TEXT DEFAULT '',
    file_path       TEXT DEFAULT '',
    metadata        TEXT DEFAULT '{}',
    created_by      TEXT DEFAULT '',
    created_at      TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_asset_versions ON asset_versions(asset_id, version_num);

-- ============================================================
-- Platform-Core v2: Asset Locks（资产锁）
-- ============================================================
CREATE TABLE IF NOT EXISTS asset_locks (
    asset_id        TEXT PRIMARY KEY,
    locked_by       TEXT DEFAULT '',
    locked_at       TEXT DEFAULT (datetime('now', 'localtime')),
    expires_at      TEXT,
    reason          TEXT DEFAULT ''
);

-- ============================================================
-- Platform-Core v2: Evolution Cases（成功案例）
-- ============================================================
CREATE TABLE IF NOT EXISTS evolution_cases (
    case_id         TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    project_id      TEXT DEFAULT '',
    domain_id       TEXT DEFAULT '',
    result_summary  TEXT DEFAULT '{}',
    quality_score   REAL DEFAULT 0,
    iterations      INTEGER DEFAULT 1,
    duration_seconds REAL DEFAULT 0,
    prompt_used     TEXT DEFAULT '',
    created_at      TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_evolution_cases_agent ON evolution_cases(agent_id);

-- ============================================================
-- Platform-Core v2: Evolution Logs（决策日志）
-- ============================================================
CREATE TABLE IF NOT EXISTS evolution_logs (
    log_id          TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    project_id      TEXT DEFAULT '',
    decision_type   TEXT DEFAULT '',
    context         TEXT DEFAULT '{}',
    outcome         TEXT DEFAULT '',
    was_successful  INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_evolution_logs_agent ON evolution_logs(agent_id);

-- ============================================================
-- Platform-Core v3: Optimization Snapshots（优化快照）
-- ============================================================
CREATE TABLE IF NOT EXISTS optimization_snapshots (
    snapshot_id     TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    original_prompt TEXT DEFAULT '',
    original_temperature REAL DEFAULT 0.7,
    original_max_tokens INTEGER DEFAULT 4096,
    applied_suggestions TEXT DEFAULT '[]',     -- JSON: [suggestion_id, ...]
    pre_optimization_scores TEXT DEFAULT '[]', -- JSON: [float, ...]
    post_optimization_scores TEXT DEFAULT '[]',-- JSON: [float, ...]
    is_reverted     INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_opt_snapshots_agent ON optimization_snapshots(agent_id);

-- ============================================================
-- Platform-Core v3: Workflow Events（轻量事件持久化）
-- ============================================================
CREATE TABLE IF NOT EXISTS workflow_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    trace_id    TEXT DEFAULT '',
    event_type  TEXT NOT NULL,
    payload     TEXT DEFAULT '{}',
    created_at  TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_workflow_events_project ON workflow_events(project_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_events_trace ON workflow_events(trace_id);

-- ============================================================
-- Platform-Core v4: Skills（技能包）
-- ============================================================
CREATE TABLE IF NOT EXISTS skills (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'custom',   -- platform | custom | mcp | imported
    status      TEXT NOT NULL DEFAULT 'active',   -- active | archived
    manifest    TEXT NOT NULL DEFAULT '{}',
    mcp_config  TEXT DEFAULT '{}',
    created_at  TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

-- Agent 挂载 skill_ids（JSON 数组）
-- ALTER TABLE agents ADD COLUMN skill_ids TEXT DEFAULT '[]';  -- 由 Python 迁移处理

-- ============================================================
-- Platform-Core v4: MCP Servers
-- ============================================================
CREATE TABLE IF NOT EXISTS mcp_servers (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    transport       TEXT NOT NULL DEFAULT 'sse',    -- stdio | sse
    url             TEXT DEFAULT '',
    command         TEXT DEFAULT '',
    args            TEXT DEFAULT '[]',
    env             TEXT DEFAULT '{}',
    auth_header     TEXT DEFAULT '',
    enabled         INTEGER DEFAULT 1,
    health_status   TEXT DEFAULT 'unknown',
    created_at      TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at      TEXT DEFAULT (datetime('now', 'localtime'))
);
