-- 031: plans.strategy + revision — 计划契约无损传递（Phase 0）
-- 背景：Planner 会算出 strategy（pipeline/dag/roundtable/adaptive），但 SavePlanRequest
-- 与 plans 表都没有承载 strategy，导致首页选择的 roundtable 不会进入 compile_from_plan。
-- revision 用于计划版本（每次用户编辑产生新 revision）。
ALTER TABLE plans ADD COLUMN strategy TEXT NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
