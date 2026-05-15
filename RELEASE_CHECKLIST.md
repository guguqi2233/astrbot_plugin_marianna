# Marianna Release Checklist

发布前按这个清单走一遍，避免把运行数据、临时缓存或高风险配置带进正式版本。

## 1. 工作树整理

- 保留源码：`main.py`、`marianna/`、`_conf_schema.json`、`metadata.yaml`、`requirements.txt`、`README.md`。
- 保留测试脚本：`scripts/test_behavior.py`、`scripts/scenario_regression.py`。
- 保留空目录占位：`data/.gitkeep`。
- 不发布运行数据：`data/user_states.json`、`data/user_profiles.json`、`data/local_memory.db`、`data/conversation_history/`。
- 不发布 Python 缓存：`__pycache__/`、`*.pyc`。

## 2. 必跑验证

```bash
python scripts/test_behavior.py
python scripts/scenario_regression.py
python scripts/release_audit.py
python scripts/release_audit.py --strict
python -m py_compile main.py marianna/memory.py marianna/runtime.py marianna/analysis.py marianna/history.py marianna/prompts.py marianna/turn.py marianna/state_store.py marianna/profile.py scripts/test_behavior.py scripts/scenario_regression.py
python -m json.tool _conf_schema.json
```

`--strict` 会把 warning 也作为失败处理。开发机存在 `data/user_states.json`、`data/user_profiles.json` 或 `__pycache__/` 时，普通审计可以通过，严格审计会失败，这是为了防止发布包混入运行数据。

## 3. 成本风险检查

- `enable_context_injection` 默认应为 `false`。
- `avoid_duplicate_context_injection` 默认应为 `true`。
- 群聊默认应为 `group_chat_memory_mode_preset: lean`。
- 群聊默认应为 `group_chat_context_injection: false`。
- 私聊默认可为 `private_chat_memory_mode_preset: rich`。

## 4. 真实环境观察

- 用 DeepSeek 连续对话 20 轮，观察输入缓存命中率。
- 运行 `/玛丽亚 运行观测`，确认重复上下文风险和群聊隐私风险为 0。
- 群聊测试私密记忆不会被注入。
- 私聊测试重要记忆能召回，但不会重复注入同槽位旧版本。
- 久别重逢、道歉、承诺、边界触发等场景要能解释数值变化。

## 5. 文档检查

- README 可读且无乱码。
- 配置风险说明清楚。
- 版本号与 `metadata.yaml` 一致。
