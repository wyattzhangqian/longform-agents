-- 长内容配置：plans 表存储长内容字段（content_type/total_units/ingest_mode/existing_units 等 JSON）
-- execute_saved_plan 读取此列，塞入 RunContext 并触发骨架规划/记忆初始化/Ingestor
ALTER TABLE plans ADD COLUMN long_content_config TEXT DEFAULT '{}';
