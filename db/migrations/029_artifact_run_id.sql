-- 029: artifacts.run_id — 产物归属校验（P0-3）
-- 背景：产物按 project_id+agent_id+phase_id 登记，无 run 归属。多 run 复用同一
-- 项目/agent 时，旧 Run 残留产物会被新 Run 的产物检查误判为"已有产物"。
-- 新增 run_id 列（可空，兼容历史数据），登记时写入，检查时按 run 归属校验。
ALTER TABLE artifacts ADD COLUMN run_id TEXT DEFAULT '';
-- 注意：artifacts 无 phase_id 列（phase 在 metadata JSON），索引只按实际列建
CREATE INDEX IF NOT EXISTS idx_artifacts_run_id
    ON artifacts(project_id, agent_id, run_id);
