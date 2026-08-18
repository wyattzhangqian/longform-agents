-- 030: agent 编排配置对齐 — domain 型顶层与 model_profile 同步 + 通用 Agent 补配置
--
-- 背景（2026-08-12 核查发现）：
-- 1. 运行时权威是 model_profile.llm（core/models/router.py resolve_agent），顶层 model/max_tokens
--    仅作 legacy 展示列。此前 88b87f4 提 max_tokens 只改了 fixture 源与顶层列，未同步
--    model_profile.llm.max_tokens → 存量 domain 型 agent 运行时仍是 4096，提额落空。
-- 2. database.py:539 迁移只 replace 了 model_profile 里的 claude-sonnet-4-6，顶层 model 列残留
--    claude-sonnet-4-6（与 profile 的 deepseek-v4-flash 不一致），展示层误导。
-- 3. custom fixture agent（fixture_agent_* 4 个）tool_ids 为空，被编排时无法 file_write 产出。
-- 4. builtin_reviewer 只有 file_read 无 file_write → 评审意见无法落盘。
-- 5. builtin_researcher/reviewer/general 的 model_profile 无 thinking:disabled（旧格式
--    {"model": "deepseek-chat"}），长输出可能被思维链吃预算。
--
-- 修复方向：以 model_profile.llm 为运行时权威，顶层列回填同步；max_tokens 采用"已提额"的顶层值
-- 反写 profile（保留 32000/16384 分档），model 采用 profile 值回写顶层（清 claude-sonnet-4-6 残留）。
-- 通用 Agent（fixture_agent_* / builtin_*）统一 thinking:disabled + file_write。

-- 1) domain 型：顶层 model 列 → 对齐 profile.llm.model_id（清 claude-sonnet-4-6 残留）
UPDATE agents
SET model = json_extract(model_profile, '$.llm.model_id')
WHERE type = 'domain'
  AND status = 'active'
  AND json_extract(model_profile, '$.llm.model_id') IS NOT NULL
  AND model IS NOT NULL
  AND model != json_extract(model_profile, '$.llm.model_id');

-- 2) domain 型：model_profile.llm.max_tokens → 对齐顶层已提额值（4096 → 16384/32000）
UPDATE agents
SET model_profile = json_set(
        model_profile,
        '$.llm.max_tokens',
        max_tokens
    )
WHERE type = 'domain'
  AND status = 'active'
  AND max_tokens IS NOT NULL
  AND json_extract(model_profile, '$.llm.max_tokens') IS NOT NULL
  AND json_extract(model_profile, '$.llm.max_tokens') != max_tokens;

-- 3) custom fixture：补 file_write 工具（否则被编排无法产出）
UPDATE agents
SET tool_ids = json('["file_write"]')
WHERE id IN ('fixture_agent_researcher', 'fixture_agent_writer',
             'fixture_agent_reviewer', 'fixture_agent_general')
  AND (tool_ids IS NULL OR tool_ids = '' OR json_extract(tool_ids, '$') IS NULL
       OR json_array_length(tool_ids) = 0);

-- 4) builtin_reviewer：补 file_write（评审意见落盘）
UPDATE agents
SET tool_ids = json('["file_read", "file_write"]')
WHERE id = 'builtin_reviewer'
  AND (tool_ids IS NULL OR tool_ids = '' OR json_extract(tool_ids, '$') IS NULL
       OR json_array_length(tool_ids) = 0);

-- 5) 通用 Agent（fixture_agent_* / builtin_researcher / builtin_reviewer / builtin_general）
--    与全部 template 型：model_profile.llm.extra.thinking → disabled
--    （防止 reasoning 思维链吃光预算致空产出；尤其 32000 长文档 template）
UPDATE agents
SET model_profile = json_set(
        model_profile,
        '$.llm.extra',
        json_object('thinking', 'disabled')
    )
WHERE (
    id IN ('fixture_agent_researcher', 'fixture_agent_writer',
           'fixture_agent_reviewer', 'fixture_agent_general',
           'builtin_researcher', 'builtin_reviewer', 'builtin_general')
    OR type = 'template'
  )
  AND json_extract(model_profile, '$.llm.extra.thinking') IS NULL;
