-- 033: run_feedback.agent_id — 按 agent 聚合历史质量指标（Phase 3，执行方案 4.5/6.3）
-- agent_matcher 需要按 agent 聚合成功率/质量门通过率，故补 agent_id 列（记录时写入）。
ALTER TABLE run_feedback ADD COLUMN agent_id TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_run_feedback_agent ON run_feedback(agent_id);
