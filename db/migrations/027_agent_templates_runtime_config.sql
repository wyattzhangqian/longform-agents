-- 027: agent_templates 运行时配置补齐
-- 1) max_tool_iterations：模板可配置工具迭代上限（base.py execute 从 config 读取）
-- 2) skill_ids：补持久化缺失列（AgentTemplate 模型早有该字段，但表/_to_row/INSERT 均未含，
--    导致模板 skill_ids 只活内存缓存、重启后从 DB 读回丢成 []）
ALTER TABLE agent_templates ADD COLUMN max_tool_iterations INTEGER DEFAULT NULL;
ALTER TABLE agent_templates ADD COLUMN skill_ids TEXT NOT NULL DEFAULT '[]';
