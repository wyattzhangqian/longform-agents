-- 平台资产库（跨项目复用）
CREATE TABLE IF NOT EXISTS asset_library (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    type            TEXT NOT NULL DEFAULT 'document',
    category        TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '[]',
    description     TEXT NOT NULL DEFAULT '',
    content_path    TEXT NOT NULL DEFAULT '',
    content_text    TEXT,
    thumbnail_url   TEXT NOT NULL DEFAULT '',
    mime_type       TEXT NOT NULL DEFAULT 'text/markdown',
    file_size       INTEGER NOT NULL DEFAULT 0,
    source_project_id TEXT NOT NULL DEFAULT '',
    source_phase_id   TEXT NOT NULL DEFAULT '',
    source_artifact   TEXT NOT NULL DEFAULT '',
    owner_id        TEXT NOT NULL DEFAULT '',
    visibility      TEXT NOT NULL DEFAULT 'private',
    ref_count       INTEGER NOT NULL DEFAULT 0,
    fork_count      INTEGER NOT NULL DEFAULT 0,
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_asset_type ON asset_library(type);
CREATE INDEX IF NOT EXISTS idx_asset_owner ON asset_library(owner_id);
CREATE INDEX IF NOT EXISTS idx_asset_visibility ON asset_library(visibility);

CREATE TABLE IF NOT EXISTS project_asset_refs (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    asset_id        TEXT NOT NULL,
    ref_type        TEXT NOT NULL DEFAULT 'reference',
    alias           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_par_project ON project_asset_refs(project_id);

CREATE TABLE IF NOT EXISTS asset_versions (
    id              TEXT PRIMARY KEY,
    asset_id        TEXT NOT NULL,
    version         INTEGER NOT NULL,
    content_text    TEXT,
    content_path    TEXT NOT NULL DEFAULT '',
    change_summary  TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(asset_id, version)
);
