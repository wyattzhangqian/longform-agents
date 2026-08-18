-- 032: run_feedback — 运行结果反馈统计（Phase 3，执行方案 6.3）
-- Planner 只读取聚合指标（成功率/质量门通过率/重试/耗时），不把任意历史输出塞进 prompt。
CREATE TABLE IF NOT EXISTS run_feedback (
    id                  TEXT PRIMARY KEY,
    plan_id             TEXT NOT NULL DEFAULT '',
    revision            INTEGER NOT NULL DEFAULT 1,
    run_id              TEXT NOT NULL DEFAULT '',
    phase_id            TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT '',        -- success / failed / partial
    quality_gate_passed INTEGER NOT NULL DEFAULT 1,      -- 0/1
    retries             INTEGER NOT NULL DEFAULT 0,
    duration_s          REAL NOT NULL DEFAULT 0,
    failure_reason      TEXT NOT NULL DEFAULT '',
    user_modified       INTEGER NOT NULL DEFAULT 0,      -- 用户是否修改过计划
    final_rating        INTEGER,                          -- 1-5（可空）
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_run_feedback_plan ON run_feedback(plan_id, revision);
