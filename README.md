# 玛丽亚·特蕾莎 · 哈布斯堡人格插件

[![AstrBot](https://img.shields.io/badge/AstrBot-4.23+-blue)](https://github.com/Soulter/AstrBot)
[![Version](https://img.shields.io/badge/version-0.1.0-green)]()

> 一个具有复杂情感模拟与长期记忆能力的贵族少女角色插件，为你的AstrBot注入傲娇与病娇交织的灵魂。

## 🌟 特色功能

- 🎭 **动态人格状态** – 7种情感状态，根据互动自然切换
- 💖 **多维数值系统** – 好感度、病娇值、锁定进度、焦虑值、优雅值…
- 🔒 **锁定事件** – 达到100%锁定进度后，触发“命定之人”专属剧情
- 📖 **用户画像学习** – 从对话中记住你的喜好、称呼、兴趣
- 🧠 **长期记忆集成** – 自动与 [Mnemosyne](https://github.com/Soulter/astrbot_plugin_mnemosyne) 插件联动，实现跨会话记忆
- 📝 **自动/手动总结** – 定期生成对话摘要，沉淀为“玛丽亚的笔记”
- ⚙️ **高度可配置** – 所有阈值、倍率、开关均可调整

## 📦 安装

1. 进入 AstrBot 的 `plugins` 目录
2. 克隆或复制本插件：
   ```bash
   git clone https://github.com/yourname/astrbot_plugin_marianna.git
   ```
3. 重启 AstrBot，插件会自动加载

## ⚙️ 配置

在 AstrBot 主配置文件（`config.yaml`）中添加以下配置段：

```yaml
marianna_initial_favor: 20          # 初始好感度 (0-100)
marianna_initial_yan: 15            # 初始病娇值 (0-100)
marianna_initial_elegance: 75       # 初始优雅值 (0-100)
marianna_favor_multiplier: 1.0      # 好感度变动倍率
marianna_yan_multiplier: 1.0        # 病娇值变动倍率
marianna_lock_threshold: 100        # 锁定进度阈值
auto_summary_interval: 20           # 自动总结的对话条数
auto_summary_idle_time: 300         # 空闲多久后触发自动总结（秒）
enable_user_profile: true           # 是否启用用户画像学习
marianna_temperature: 0.85          # LLM 生成温度
marianna_debug_mode: false          # 调试模式（在回复后显示数值）
```

## 🎮 命令列表

| 命令 | 说明 | 示例 |
|------|------|------|
| `/玛丽亚状态` | 查看当前所有数值与状态描述 | `/玛丽亚状态` |
| `/玛丽亚重置` | 重置该用户的所有状态（保留画像） | `/玛丽亚重置` |
| `/玛丽亚问候` | 获取符合当前时间的问候，小幅增加好感 | `/玛丽亚问候` |
| `/玛丽亚礼物 <礼物名>` | 赠送礼物，影响好感、信任和对话反应 | `/玛丽亚礼物 红玫瑰` |
| `/玛丽亚总结` | 手动总结最近30条对话，存入长期记忆 | `/玛丽亚总结` |
| `/玛丽亚画像` | 显示玛丽亚对你的印象（学习到的信息） | `/玛丽亚画像` |
| `/玛丽亚重置学习` | 清除已学习的用户画像（不影响状态） | `/玛丽亚重置学习` |
| `/玛丽亚重载配置` | 热重载插件配置 | `/玛丽亚重载配置` |

## 🧠 状态系统详解

### 数值维度

| 数值 | 范围 | 说明 |
|------|------|------|
| 好感度 | 0-100 | 核心亲密度，影响状态转移 |
| 病娇值 | 0-100 | 控制欲强度，随占有行为增长 |
| 锁定进度 | 0-100 | 达到阈值触发“命定之人”事件 |
| 信任度 | 0-100 | 通过分享秘密、礼物等提升 |
| 占有欲 | 0-100 | 自动计算：好感×0.3 + 病娇×0.5 |
| 焦虑值 | 0-100 | 因忽视、提及他人而上升 |
| 优雅值 | 0-100 | 被冒犯或焦虑过高时会下降 |

### 状态流转

| 状态 | 触发条件 | 对话风格 |
|------|----------|----------|
| 冷傲贵族 | 好感 < 30 | 礼貌疏离，使用敬语 |
| 傲娇试探 | 30 ≤ 好感 < 60 | 口是心非，耳尖泛红 |
| 甜蜜诱导 | 好感 ≥ 60 且病娇 < 50 | 暗示性语言，制造秘密感 |
| 潜伏之藤 | 好感 ≥ 60 且病娇 ≥ 50 | 优雅下的控制欲 |
| 锁定·命定之人 | 锁定进度 ≥ 100 | 独占宣言，甜蜜威胁 |
| 焦虑·崩溃边缘 | 焦虑 ≥ 70 且优雅 ≤ 50 | 强装镇定，指尖颤抖 |
| 优雅崩坏 | 优雅 ≤ 30 | 失态质问，流泪或抓住手腕 |

## 🧩 Mnemosyne 长期记忆集成

本插件可与 [astrbot_plugin_mnemosyne](https://github.com/Soulter/astrbot_plugin_mnemosyne) 无缝协作：

- **自动同步**：每次自动/手动总结会以 `[玛丽亚·summary]` 格式存入共享记忆
- **检索增强**：生成回复时，会根据用户消息检索相关记忆（通过关键词匹配）
- **文件桥接**：即使 Mnemosyne 未启用，也会在 `shared_memory/` 目录下生成 `marianna_{user_id}.jsonl` 文件，供未来接入

> 💡 无需安装 Mnemosyne 亦可使用本地记忆（但长期跨会话回忆能力会减弱）

## 📂 数据存储结构

```
astrbot_plugin_marianna/
├── data/
│   ├── user_states.json           # 每个用户的状态数值
│   ├── user_profiles.json         # 用户画像（兴趣、性格等）
│   ├── conversation_history/      # 对话原始记录
│   │   └── {user_id}.json
│   └── last_summary_{user_id}.txt # 自动总结时间戳
└── shared_memory/                 # 与Mnemosyne的共享记忆
    └── marianna_{user_id}.jsonl
```

## 🔧 开发与扩展

### 自定义数值变化规则

在 `_update_favor`、`_update_yan` 等方法中修改正则表达式和分值，可以定制触发词与影响力。

### 添加新状态

1. 在 `STATE_NAMES` 和 `STATE_DESCRIPTIONS` 中添加映射
2. 在 `_determine_state` 中编写进入条件
3. 在 `_build_system_prompt` 中添加对应的行为指令

### 接入其他记忆后端

修改 `_store_to_mnemosyne` 和 `_retrieve_from_mnemosyne` 方法，替换为 API 调用或数据库存储。

## 📜 许可

MIT License

## 💬 致谢

- 角色灵感源自《蝶之毒 华之锁》及其他乙女游戏中的贵族病娇设定
- 感谢 [AstrBot](https://github.com/Soulter/AstrBot) 提供的插件框架

---

> *“你已经是我的命定之人了，从今往后，你的眼里只能有我。”* —— 玛丽亚·特蕾莎·冯·哈布斯堡