-- 产物发布表 — 生成分享 token，免登录查看
CREATE TABLE IF NOT EXISTS published_artifacts (
    token           TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    artifact_name   TEXT NOT NULL,
    published_by    TEXT DEFAULT '',
    expires_at      TEXT DEFAULT '',
    view_count      INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pub_project ON published_artifacts(project_id);
