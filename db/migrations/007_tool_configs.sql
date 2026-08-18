-- Add tool_configs column for per-agent tool presets (Phase 3)
ALTER TABLE agents ADD COLUMN tool_configs TEXT DEFAULT '{}';
