-- ============================================================
-- 005: 领域知识库（Knowledge Base）
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_entries (
    id              TEXT PRIMARY KEY,
    domain_id       TEXT NOT NULL,
    phase_id        TEXT DEFAULT '',
    category        TEXT NOT NULL DEFAULT 'norm',
    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    tags            TEXT DEFAULT '[]',
    priority        INTEGER DEFAULT 50,
    inject_mode     TEXT DEFAULT 'always',
    inject_to       TEXT DEFAULT '',
    source          TEXT NOT NULL DEFAULT 'platform',
    owner_id        TEXT DEFAULT '',
    char_count      INTEGER DEFAULT 0,
    sort_order      INTEGER DEFAULT 0,
    is_archived     INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    updated_at      TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS knowledge_embeddings (
    entry_id        TEXT PRIMARY KEY,
    embedding       BLOB,
    model_name      TEXT DEFAULT '',
    dimension       INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (entry_id) REFERENCES knowledge_entries(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ke_domain ON knowledge_entries(domain_id, category);
CREATE INDEX IF NOT EXISTS idx_ke_domain_phase ON knowledge_entries(domain_id, phase_id);
CREATE INDEX IF NOT EXISTS idx_ke_source ON knowledge_entries(source, owner_id);
CREATE INDEX IF NOT EXISTS idx_ke_inject ON knowledge_entries(domain_id, inject_mode, is_archived);
