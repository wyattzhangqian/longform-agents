-- 028: run_checkpoints — checkpoint 历史保留（P1-10）
-- 背景：checkpoint 是每项目单槽（projects.checkpoint_state），新 run / replay
-- 直接覆盖，历史丢失。新增历史表：save_checkpoint 时额外写入一条 immutable
-- 历史行（保留最近 N 条），供回放基准与审计。
CREATE TABLE IF NOT EXISTS run_checkpoints (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT NOT NULL,
    run_id        TEXT NOT NULL DEFAULT '',
    version       INTEGER NOT NULL DEFAULT 0,
    checkpoint_state TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'running',  -- running | completed | failed
    created_at    TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_run_checkpoints_project
    ON run_checkpoints(project_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_run_checkpoints_run
    ON run_checkpoints(project_id, run_id);
