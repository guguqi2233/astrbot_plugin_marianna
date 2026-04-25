# 玛丽亚·特蕾莎·冯·哈布斯堡

`astrbot_plugin_marianna` 是一个面向 AstrBot 的角色人格插件。它以“哈布斯堡贵族少女”为核心人设，通过额外的 LLM 语义分析、用户画像、对话历史和长期记忆，动态调整角色对用户的情感状态，并把这些状态注入主对话流程。

当前版本：`v1.0.0`

## 核心能力

- 动态情感数值：好感度、信任度、病娇值、锁定进度、焦虑值、优雅值和占有欲会随普通对话变化。
- 分层情绪引擎：长期关系阶段、主情绪模式、危机覆盖、表现强度和主动事件分开推导，避免单一状态名承载过多行为。
- 双阶段 LLM 流程：先用分析型 LLM 判断本轮语义和数值增量，再让主对话模型生成最终回复。
- 动态系统提示词：根据数值、状态、画像、记忆和当前发言实时构建角色提示词。
- 用户画像学习：自动提取称呼、生日、职业、所在地、兴趣偏好、沟通风格等信息。
- 对话历史与总结：本地保留最近历史，并在空闲后自动生成长期总结。
- Mnemosyne 集成：可选接入 Mnemosyne 长期记忆，支持分层检索、去重、强化、覆盖和遗忘。
- 性能优化：缓存近期历史、记忆检索结果和动态提示词；低价值消息会走 compact prompt，减少不必要分析。
- 调试与观测：支持状态、画像、性能统计和调试尾注。

## 安装

1. 将插件目录放入 AstrBot 的 `data/plugins/` 目录下。
2. 确认依赖已安装：

   ```bash
   pip install -r requirements.txt
   ```

3. 重启 AstrBot，或在 AstrBot 管理面板中重新加载插件。
4. 在插件配置页面按需要调整模型、记忆、历史和数值参数。

## 快速使用

直接和玛丽亚普通对话即可。问候、试探、夸奖、安抚、冷落、承诺、分享秘密、触碰边界等语义都会被分析型 LLM 映射为数值和状态变化。

输入 `/玛丽亚` 查看指令菜单。

| 指令 | 说明 |
| --- | --- |
| `/玛丽亚` | 显示插件命令菜单 |
| `/玛丽亚 调试` | 切换调试模式，在回复后显示当前数值和本轮变化 |
| `/玛丽亚 状态` | 查看当前数值、关系阶段和状态描述 |
| `/玛丽亚 重置` | 重置当前用户状态，但保留用户画像 |
| `/玛丽亚 画像` | 查看玛丽亚已学习到的用户画像 |
| `/玛丽亚 重置学习` | 清除当前用户画像，但不影响情感状态 |
| `/玛丽亚 重载配置` | 热重载插件配置 |
| `/玛丽亚 perf` | 查看近期内部性能统计 |

## 工作流程

```text
用户发送消息
    │
    ▼
on_llm_request
    1. 读取用户状态、画像、近期历史和长期记忆
    2. 判断是否跳过分析，或调用分析型 LLM
    3. 应用数值增量，更新关系阶段和主动事件
    4. 构建 system_prompt、contexts 和 temperature
    │
    ▼
AstrBot 主 LLM 生成回复
    │
    ▼
on_llm_response
    1. 处理锁定等特殊事件
    2. 可选追加调试尾注
    3. 保存净化后的对话历史
    4. 异步更新画像、总结和长期记忆
```

### 分析原则

- 数值变化主要由最新用户发言触发，旧历史只用于理解语境和调整幅度。
- 无关旧事件不会反复推动数值变化，避免“旧账重复结算”。
- 请求侧会隔离同用户并发消息，并在可识别的同一事件重入时复用分析结果，避免重复叠加数值。
- 普通寒暄、低价值确认和极短消息会尽量跳过昂贵分析，改用轻量 prompt。
- 代码侧会对 LLM 返回增量做阶段校验，防止低好感直接跳到高病娇、高锁定或高占有。

## 情感系统

### 数值字段

| 字段 | 初始值 | 范围 | 说明 |
| --- | ---: | --- | --- |
| 好感度 | `0` | `0-100` | 决定亲近许可，是多数高强度关系变化的前置条件 |
| 信任度 | `15` | `0-100` | 可独立于好感变化，受诚实、守诺、尊重边界影响 |
| 病娇值 | `0` | `0-100` | 仅在好感度 `>=60` 后进入有效变化链路 |
| 锁定进度 | `0` | `0-100` | 达到阈值后触发“命定之人”相关状态 |
| 焦虑值 | `5` | `0-100` | 受失约、冷落、关系不确定性影响 |
| 优雅值 | `85` | `0-100` | 表示贵族克制和体面程度，过低会出现失态 |
| 占有欲 | `0` | `0-100` | 由系统推导，不直接由 LLM 输出 |

### 阶段约束

| 当前好感区间 | 允许变化 | 限制 |
| --- | --- | --- |
| `<30` | 好感度、信任度、优雅值 | 不表现病态控制，不增长病娇值、锁定进度、占有欲和焦虑值 |
| `30-59` | 好感度、信任度、优雅值、少量焦虑值 | 病娇值、锁定进度、占有欲仍保持关闭 |
| `>=60` | 全部数值可进入有效变化 | 仍受单轮变化范围和状态规则限制 |

### 主要状态

| 状态 | 典型条件 | 表现 |
| --- | --- | --- |
| 冷傲贵族 | 好感度 `<30` | 礼貌疏离，强调身份和边界 |
| 傲娇试探 | 好感度 `30-59` | 口是心非，出现轻微关心 |
| 甜蜜诱导 | 好感度 `>=60` 且病娇值较低 | 温柔暧昧，开始埋下轻微依恋暗示 |
| 潜伏之藤 | 好感度 `>=60` 且病娇值较高 | 更强的专属感和隐性操控，但仍保持优雅 |
| 锁定·命定之人 | 锁定进度达到阈值 | 强烈宿命叙事和专属关系表达 |
| 焦虑·崩溃边缘 | 高焦虑且低优雅 | 克制感减弱，哀求、质问、自嘲增多 |
| 优雅崩坏 | 优雅值过低 | 礼仪外壳明显失稳 |

## 记忆与画像

### 本地画像

用户画像保存在 `data/user_profiles.json`，会记录：

- 基本信息：称呼、生日、职业、所在地等。
- 兴趣偏好：音乐、书籍、食物、颜色、喜欢/讨厌的内容。
- 沟通风格：用户习惯的表达方式、互动偏好和敏感点。
- 玛丽亚学习笔记：自动总结和长期观察。

### 本地历史

对话历史以 JSONL 方式保存在：

```text
data/conversation_history/<safe_user_id>.jsonl
```

特殊 `user_id` 会被安全化为文件名，避免路径穿越、非法 Windows 文件名或写入失败。

### Mnemosyne 长期记忆

若检测到相邻插件目录中的 `astrbot_plugin_mnemosyne`，本插件会启用长期情感记忆联动，并将共享记忆写入：

```text
shared_memory/marianna_<safe_user_id>.jsonl
```

长期记忆按层使用：

- `event`：承诺、秘密、边界、阶段转折、强情绪事件。
- `impression`：互动模式、情绪印象、反复出现的关系信号。
- `summary`：自动总结出的长期上下文。
- `profile`：画像类长期信息。

记忆会根据命中次数、更新时间、重叠程度和重要性进行强化、覆盖、降权或清理。

## 配置

### 基础数值

| 配置项 | 默认值 | 说明 |
| --- | ---: | --- |
| `marianna_initial_favor` | `0` | 新用户初始好感度 |
| `marianna_initial_yan` | `0` | 新用户初始病娇值 |
| `marianna_initial_trust` | `15` | 新用户初始信任度 |
| `marianna_initial_anxiety` | `5` | 新用户初始焦虑值 |
| `marianna_initial_elegance` | `85` | 新用户初始优雅值 |
| `marianna_favor_multiplier` | `1.0` | 好感度变化倍率 |
| `marianna_yan_multiplier` | `1.0` | 病娇值变化倍率 |
| `marianna_lock_threshold` | `100` | 锁定状态触发阈值 |
| `marianna_temperature` | `0.85` | 主对话请求温度 |
| `marianna_debug_mode` | `false` | 新用户默认是否显示调试尾注 |

`marianna_debug_mode` 只影响新用户默认值，不会覆盖已有用户；已有用户可通过 `/玛丽亚 调试` 单独切换。

### 历史、总结和上下文

| 配置项 | 默认值 | 说明 |
| --- | ---: | --- |
| `conversation_history_retention_limit` | `1000` | 每个用户本地保留的最近历史消息条数 |
| `enable_context_injection` | `true` | 是否注入历史到 `req.contexts` |
| `context_history_limit` | `10` | 注入主回复的历史条数 |
| `context_max_tokens_per_msg` | `300` | 注入历史时单条消息最大长度 |
| `inject_state_details` | `true` | 是否将当前数值细节注入系统提示词 |
| `inject_summary_as_context` | `true` | 当近期历史不足时是否注入最近总结 |
| `auto_summary_interval` | `20` | 自动总结触发间隔 |
| `auto_summary_idle_time` | `300` | 触发总结前需要空闲的秒数 |

### 分析型 LLM

| 配置项 | 默认值 | 说明 |
| --- | ---: | --- |
| `marianna_analysis_provider_id` | 空 | 分析、画像、总结使用的 provider；留空时跟随当前会话模型 |
| `analysis_history_limit` | `120` | 状态分析前扫描的最近历史条数 |
| `analysis_relevant_memory_limit` | `24` | 状态分析最多注入的相关聊天记忆条数 |
| `analysis_recent_context_limit` | `6` | 固定保留的最近上下文条数 |
| `analysis_mnemosyne_memory_limit` | `8` | 额外检索的 Mnemosyne 长期记忆条数 |
| `analysis_max_chars_per_msg` | `4000` | 分析与总结时单条历史最大字符数 |
| `analysis_context_char_budget` | `800000` | 分析历史部分总字符预算 |

### 画像与长期记忆

| 配置项 | 默认值 | 说明 |
| --- | ---: | --- |
| `enable_user_profile` | `true` | 是否启用用户画像学习 |
| `enable_emotional_memory` | `true` | 是否启用 Mnemosyne 情感记忆 |
| `enable_selective_interaction_memory` | `true` | 是否只写入有分量的互动印象 |
| `interaction_memory_min_delta` | `2` | 互动印象写入的核心情绪变化阈值 |
| `memory_prompt_limit` | `5` | 主回复最多注入的长期记忆条数 |
| `memory_prompt_event_limit` | `2` | 事件节点记忆名额 |
| `memory_prompt_impression_limit` | `2` | 情绪印象记忆名额 |
| `memory_prompt_summary_limit` | `1` | 长期总结记忆名额 |
| `memory_prompt_profile_limit` | `1` | 画像类长期记忆名额 |
| `enable_memory_update_layer` | `true` | 是否启用重复强化和高重叠覆盖 |
| `enable_memory_forgetting_layer` | `true` | 是否启用衰减、降权和清理 |
| `memory_decay_days` | `45` | 情绪印象开始明显降权的大致天数 |
| `memory_hard_cleanup_days` | `180` | 被覆盖且长期未命中的旧印象清理天数 |

### 回复行为

| 配置项 | 默认值 | 说明 |
| --- | ---: | --- |
| `enable_value_dialogue_modulation` | `true` | 是否让数值连续调制回复语气和亲疏 |
| `enable_emotion_recognition_layer` | `true` | 是否注入用户意图、情绪和关系信号 |
| `enable_active_event_layer` | `true` | 是否允许低频主动事件推进关系 |
| `active_event_cooldown_turns` | `7` | 同一用户两次主动事件之间的最小互动轮数 |
| `enable_reflection_update_layer` | `true` | 是否在回复后将本轮反思整理为互动印象 |

## 项目结构

```text
astrbot_plugin_marianna/
├── main.py                 # AstrBot 插件入口、生命周期、事件钩子和命令组
├── metadata.yaml           # 插件元数据
├── _conf_schema.json       # WebUI 配置 schema
├── requirements.txt        # 依赖
├── marianna/
│   ├── constants.py        # 常量、正则、默认状态表
│   ├── compat.py           # aiofiles 兼容层
│   ├── runtime.py          # 配置、IO、任务、性能统计、provider 调用
│   ├── memory.py           # Mnemosyne、记忆检索、去重、写入和画像更新调度
│   ├── state_store.py      # 用户状态、画像、全局状态和保存队列
│   ├── history.py          # 本地对话历史 JSONL、缓存和压缩
│   ├── analysis.py         # 状态分析、数值约束、总结和状态报告
│   ├── profile.py          # 用户画像抽取与合并
│   ├── prompts.py          # 动态系统提示词构建
│   └── turn.py             # 单轮请求分析和 prompt/context 注入
└── data/                   # 运行时生成的数据目录
```

## 数据文件

| 路径 | 说明 |
| --- | --- |
| `data/user_states.json` | 用户情感数值、兼容状态摘要和调试状态 |
| `data/user_profiles.json` | 用户画像数据 |
| `data/global_state.json` | 全局唯一“命定之人”的 `user_id / user_name` |
| `data/conversation_history/<safe_user_id>.jsonl` | 本地对话历史 |
| `shared_memory/marianna_<safe_user_id>.jsonl` | Mnemosyne 共享长期记忆 |

## 维护与检查

常用检查命令：

```bash
python -m compileall -q main.py marianna
python -c "import ast, pathlib; [ast.parse(p.read_text(encoding='utf-8')) for p in [pathlib.Path('main.py'), *pathlib.Path('marianna').glob('*.py')]]; print('ast ok')"
git diff --check -- main.py marianna README.md metadata.yaml
```

说明：

- `main.py` 应保持轻量，只放 AstrBot 入口、生命周期、钩子和命令。
- 新功能优先放入 `marianna/` 下对应 mixin 模块。
- 写入状态、画像、历史和长期记忆时优先复用现有保存队列、文件锁和缓存失效逻辑。
- 对外行为变更需要同步更新 `_conf_schema.json`、`metadata.yaml` 和 README。

## 版本历史

### v1.0.0

- 正式版发布。
- 完成核心代码模块化，保留 `main.py` 作为 AstrBot 插件入口。
- 修复卸载/重载时 Mnemosyne 批量写入可能未完全 drain 的风险。
- 修复同一用户并发发送相同内容时 session 临时状态可能互相覆盖的问题。
- 增加用户文件名安全化处理，避免特殊 `user_id` 导致非法路径或路径穿越。
- 强化保存失败反馈，避免卸载时将部分保存失败误报为全部成功。

### v0.9.x

- 引入分层情绪引擎、记忆更新层、长期记忆衰减和 selective interaction memory。
- 优化状态分析窗口、相关历史检索、提示词缓存和低价值消息跳过策略。
- 增加用户画像本地抽取、自动总结、命定之人全局状态和调试/性能命令。

### v0.8.x

- 新增 `/玛丽亚` 指令组。
- 完善状态报告、调试尾注、分阶段提示词规则和代码侧数值硬约束。
- 引入细分状态提示词、关系阶段、主情绪模式、危机覆盖和表现强度。

### v0.7.x 及更早

- 新增分析型 LLM provider 配置。
- 将数值变化逻辑从固定触发词升级为 LLM 语义分析。
- 接入 `on_llm_request` / `on_llm_response` 钩子、上下文注入、自动总结和用户画像。

## 许可证

AGPL-3.0。详见 [LICENSE](LICENSE)。
