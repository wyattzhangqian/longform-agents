-- 项目成员表 — 团队协作
CREATE TABLE IF NOT EXISTS project_members (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    role        TEXT DEFAULT 'viewer',   -- owner / editor / viewer
    joined_at   TEXT DEFAULT (datetime('now')),
    UNIQUE(project_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_pmembers_project ON project_members(project_id);

-- 项目评论表
CREATE TABLE IF NOT EXISTS project_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    user_id     TEXT DEFAULT '',
    user_name   TEXT DEFAULT '',
    content     TEXT NOT NULL,
    phase_id    TEXT DEFAULT '',
    reply_to    INTEGER DEFAULT NULL,
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pcomments_project ON project_comments(project_id);
