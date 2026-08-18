-- 022: 约束协商台账 — shared_thread 问答中提取的已确认约束持久化
CREATE TABLE IF NOT EXISTS negotiation_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL DEFAULT '',
    project_id  TEXT NOT NULL DEFAULT '',
    phase_id    TEXT NOT NULL DEFAULT '',
    data        TEXT NOT NULL DEFAULT '{}',  -- JSON: NegotiationEntry
    created_at  TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_negotiation_run ON negotiation_ledger(run_id);
CREATE INDEX IF NOT EXISTS idx_negotiation_project ON negotiation_ledger(project_id);
