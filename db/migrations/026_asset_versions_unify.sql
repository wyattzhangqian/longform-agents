-- P0-2 修复：统一 asset_versions 表结构
-- 问题：009 迁移先跑建旧表(version 列)，schema.sql 新表(version_num 列)因 IF NOT EXISTS 跳过，
--       而 core/vault 按新结构读写 version_num → 全新部署写产物版本必炸 no such column: version_num
-- 方案：重建为新结构（version_id/version_num/content_hash/file_path/metadata/created_by/created_at），迁移旧数据

CREATE TABLE IF NOT EXISTS asset_versions_new (
    version_id      TEXT PRIMARY KEY,
    asset_id        TEXT NOT NULL,
    version_num     INTEGER DEFAULT 1,
    content_hash    TEXT DEFAULT '',
    file_path       TEXT DEFAULT '',
    metadata        TEXT DEFAULT '{}',
    created_by      TEXT DEFAULT '',
    created_at      TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 迁移旧数据（旧列 id/version/content_text/content_path/change_summary/created_at → 新列）
INSERT OR IGNORE INTO asset_versions_new
    (version_id, asset_id, version_num, content_hash, file_path, metadata, created_by, created_at)
SELECT id, asset_id, version, content_text, content_path, change_summary, '', created_at
FROM asset_versions;

DROP TABLE asset_versions;
ALTER TABLE asset_versions_new RENAME TO asset_versions;

CREATE INDEX IF NOT EXISTS idx_asset_versions ON asset_versions(asset_id, version_num);
