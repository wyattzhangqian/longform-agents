-- 统一凭证库 — 媒体 key 从 agent 解耦到统一凭证表
CREATE TABLE IF NOT EXISTS credentials (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    provider    TEXT NOT NULL,   -- VOLCENGINE / OPENAI / SUNO / DEEPSEEK / CUSTOM
    api_key     TEXT NOT NULL,   -- base64 编码
    base_url    TEXT DEFAULT '',
    description TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_credentials_provider ON credentials(provider);
