# Marianna

Marianna 是一个面向 AstrBot 的人格、关系状态与本地记忆插件。当前版本重点是：降低大模型 token 成本、避免重复上下文注入、区分群聊/私聊记忆策略，并用本地状态机维持人格一致性。

当前版本：`v1.5.4`

## 核心能力

- 本地关系状态机：好感、信任、焦虑、优雅、锁定进度等数值由本地规则控制。
- 人格一致性守卫：存在命定之人后，对其他用户自动收敛暧昧和占有行为。
- 本地记忆系统：SQLite 记忆、槽位去重、保护记忆、负反馈、候选暂存、摘要压缩。
- 可选向量召回：可接入 AstrBot 的 embedding/向量模型能力。
- 群聊/私聊分层：默认私聊高沉浸，群聊省 token 且只使用公开记忆。
- Prompt 成本控制：轻量 prompt、预算保护、自动记忆模式降档、成本画像统计。
- 诊断命令：可查看状态变化、记忆召回、成本预算和配置风险。

## 默认策略

默认推荐保持：

```yaml
enable_token_cost_optimization: true
enable_scene_memory_mode: true
private_chat_memory_mode_preset: rich
group_chat_memory_mode_preset: lean
private_chat_context_injection: true
group_chat_context_injection: false
avoid_duplicate_context_injection: true
enable_prompt_budget_guard: true
enable_prompt_cost_auto_memory_mode: true
```

含义：

- 私聊：允许更丰富的本地记忆体验。
- 群聊：默认省 token，避免注入私密记忆。
- AstrBot 已有对话历史时：插件不会重复塞入历史，尽量保护 DeepSeek 等模型的输入缓存命中。

## 重要成本警告

`enable_context_injection` 是高风险配置。

如果 AstrBot 或模型提供商已经自动保存并注入对话历史，插件再把历史塞进 `req.contexts`，可能导致：

- 输入缓存命中下降；
- 历史内容重复；
- prompt token 翻倍；
- DeepSeek 等模型更难命中缓存。

除非你明确需要插件自行注入历史，否则建议：

```yaml
enable_context_injection: false
inject_summary_as_context: false
avoid_duplicate_context_injection: true
```

如果必须开启上下文注入，请至少保持：

```yaml
avoid_duplicate_context_injection: true
context_history_limit: 6
context_max_tokens_per_msg: 220
```

## 推荐配置

### 省 token 群聊

```yaml
enable_token_cost_optimization: true
enable_scene_memory_mode: true
group_chat_memory_mode_preset: lean
group_chat_context_injection: false
group_chat_inject_summary_as_context: false
memory_prompt_limit: 2
builtin_memory_prompt_char_budget: 160
enable_prompt_budget_guard: true
enable_prompt_cost_auto_memory_mode: true
```

### 平衡模式

```yaml
enable_token_cost_optimization: true
enable_context_injection: false
inject_summary_as_context: false
memory_mode_preset: balanced
memory_prompt_limit: 3
builtin_memory_prompt_char_budget: 260
enable_prompt_budget_guard: true
enable_prompt_budget_auto_throttle: true
```

### 高沉浸私聊

```yaml
enable_token_cost_optimization: true
enable_scene_memory_mode: true
private_chat_memory_mode_preset: rich
private_chat_context_injection: true
private_chat_inject_summary_as_context: true
memory_mode_preset: rich
memory_prompt_limit: 5
builtin_memory_prompt_char_budget: 520
enable_builtin_memory_vector: true
marianna_embedding_provider_id: your_embedding_provider_id
```

## 记忆系统

本地记忆默认保存到：

```text
data/local_memory.db
data/conversation_history/
data/user_profiles.json
data/user_states.json
```

记忆层级：

- `event`：承诺、道歉、秘密、边界、关系转折。
- `profile`：称呼、生日、长期偏好等稳定资料。
- `summary`：低价值互动压缩后的摘要。
- `impression`：普通互动印象。

隐私层级：

- `private_only`：仅私聊使用。
- `group_only`：仅群聊使用。
- `public_profile`：群聊可使用的公开资料。
- `sensitive`：敏感记忆，默认不在群聊暴露。

## 命令

常用命令包括：

```text
/玛丽亚 状态
/玛丽亚 诊断
/玛丽亚 诊断历史
/玛丽亚 配置风险
/玛丽亚 perf
/玛丽亚 运行观测
/玛丽亚 记忆健康
/玛丽亚 记忆修复
/玛丽亚 记忆成本
/玛丽亚 记忆模式 lean
/玛丽亚 记忆模式 balanced
/玛丽亚 记忆模式 rich
```

不同 AstrBot 环境可能会对命令前缀或中文命令解析有差异，实际以插件注册行为为准。

`/玛丽亚 运行观测` 会输出最近真实请求的后台观测结果，包括：

- DeepSeek/cache proxy：用 system prompt 稳定前缀比例估算缓存友好度。
- Context duplication risk：检查 `req.contexts` 是否重复，或是否与 system prompt 重叠。
- Group privacy risk：检查群聊 prompt 记忆 trace 中是否出现 private/sensitive 可见性。

该工具只保存长度、hash、计数和可见性标签，不保存完整用户消息或完整 prompt。

## 发布前检查

建议每次发布前执行：

```bash
python scripts/test_behavior.py
python scripts/scenario_regression.py
python scripts/release_audit.py
python scripts/release_audit.py --strict
python -m py_compile main.py marianna/memory.py marianna/runtime.py marianna/analysis.py marianna/history.py marianna/prompts.py marianna/turn.py marianna/state_store.py marianna/profile.py scripts/test_behavior.py scripts/scenario_regression.py
python -m json.tool _conf_schema.json
```

并确认运行数据不会进入发布包：

```text
data/*
!data/.gitkeep
__pycache__/
*.pyc
```

`release_audit.py --strict` 会把本地运行数据和 Python 缓存警告视为失败，适合真正打包或 CI 发布前使用。

## 当前工程风险

当前功能已经较完整，但发布前仍建议重点观察：

- 真实 AstrBot 环境下的群聊/私聊识别是否稳定；
- DeepSeek 输入缓存命中率是否符合预期；
- 长期运行后 SQLite 记忆规模是否增长过快；
- 群聊是否只召回 `public_profile` 和安全记忆；
- 开启 `enable_context_injection` 后是否出现历史重复注入；
- 诊断面板是否能清楚解释本轮数值变化和记忆注入原因。

## 版本说明

### v1.5.4

- Reuse the most recent scoped group state when AstrBot provides an unstable group `unified_msg_origin` without a stable group id.
- Prevent same-user group conversations from splitting values across multiple `group:hash::user` state records.
- Add regression coverage for unstable group-origin state reuse.

### v1.5.3

- Fix debug mode inheritance when a group-scoped state is created after `/??? ??` is enabled on the raw user state.
- Make response-stage debug footer respect the pending request debug flag and resync scene states when needed.
- Add regression coverage for first-turn group debug inheritance.

### v1.5.2

- Add `/Marianna model probe` command alias `/??? ????` for zero-token provider checks of chat, analysis, and embedding models.
- Add subtle opening-signal deltas so self-introductions, polite help requests, serious listening, and restrained praise can move favor/trust by small amounts when the analysis LLM returns no explicit delta.
- Fix debug mode persistence across private/group scene keys for the same user.
- Add regression coverage for frozen opening values and debug-state propagation.

### v1.5.1

- 默认群聊使用省 token 策略，私聊使用高沉浸记忆策略。
- 增加群聊/私聊独立记忆模式与上下文注入开关。
- 强化 DeepSeek 等模型的缓存友好策略。
- 增加本地记忆槽位、保护层、负反馈、候选暂存和摘要维护。
- 强化异常配置、异常数值、异常缓存时间戳的自愈能力。
- 增加行为测试和场景回归覆盖。
