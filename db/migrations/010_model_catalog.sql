-- 模型 Catalog 持久化（支持 Web UI 增删，env var 为种子）
CREATE TABLE IF NOT EXISTS model_catalog (
    id              TEXT PRIMARY KEY,       -- 模型 ID（如 deepseek-v4-flash）
    modality        TEXT NOT NULL,          -- llm / image / video / music
    provider        TEXT NOT NULL DEFAULT '',
    label           TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    base_url        TEXT NOT NULL DEFAULT '',
    api_key_env     TEXT NOT NULL DEFAULT '',  -- Key 引用名（如 DEEPSEEK_API_KEY）
    default_temperature REAL NOT NULL DEFAULT 0.7,
    default_max_tokens INTEGER NOT NULL DEFAULT 4096,
    source          TEXT NOT NULL DEFAULT 'user',  -- seed(env导入) / user(UI创建)
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_mc_modality ON model_catalog(modality);
