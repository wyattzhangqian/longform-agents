-- Agent 扩展字段
ALTER TABLE agents ADD COLUMN input_schema TEXT;
ALTER TABLE agents ADD COLUMN output_schema TEXT;
ALTER TABLE agents ADD COLUMN tool_namespaces TEXT DEFAULT '[]';
ALTER TABLE agents ADD COLUMN runtime_info TEXT DEFAULT '{}';
ALTER TABLE agents ADD COLUMN extra TEXT DEFAULT '{}';
ALTER TABLE agents ADD COLUMN model_profile TEXT DEFAULT '{}';
