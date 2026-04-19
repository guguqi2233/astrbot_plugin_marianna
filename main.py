import json
import os
import re
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Tuple

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain

# 尝试导入 Mnemosyne 长期记忆插件（可选）
try:
    from astrbot_plugin_mnemosyne import MnemosyneManager
    MNEMOSYNE_AVAILABLE = True
except ImportError:
    MNEMOSYNE_AVAILABLE = False
    logger.warning("Mnemosyne 未安装，长期记忆功能将不可用。如需使用，请在插件市场安装。")

# ======================== 常量定义 ========================

DEFAULT_STATE = {
    "好感度": 20,
    "病娇值": 15,
    "锁定进度": 0,
    "信任度": 10,
    "占有欲": 20,
    "焦虑值": 10,
    "优雅值": 75,
    "当前状态": "冷傲贵族",
    "最后互动时间": None,
    "互动计数": 0,
    "已触发锁定事件": False,
    "已触发崩溃事件": False,
    "礼物记录": [],
    "秘密分享": [],
    "用户喜好": {}
}

STATE_NAMES = {
    "COLD_NOBLE": "冷傲贵族",
    "TSUNDERE_PROBE": "傲娇试探",
    "SWEET_INDUCE": "甜蜜诱导",
    "LATENT_VINE": "潜伏之藤",
    "LOCKED_FATE": "锁定·命定之人",
    "ANXIETY_EDGE": "焦虑·崩溃边缘",
    "ELEGANCE_COLLAPSE": "优雅崩坏"
}

STATE_DESCRIPTIONS = {
    "冷傲贵族": "她对你保持着礼貌的距离，微笑优雅但疏离。",
    "傲娇试探": "她嘴上不饶人，但目光总是不自觉地追随你。",
    "甜蜜诱导": "她的话语中带着若有若无的暗示，像藤蔓轻轻搭上你的肩。",
    "潜伏之藤": "她依旧优雅，但笑容深处藏着让人心跳加速的东西。",
    "锁定·命定之人": "她的眼神告诉你——你已经无处可逃了。",
    "焦虑·崩溃边缘": "她强装镇定，但指尖微微颤抖。",
    "优雅崩坏": "她的优雅面具出现了裂痕。"
}

# ======================== 插件主类 ========================

@register("astrbot_plugin_marianna", "玛丽亚·特蕾莎·冯·哈布斯堡", "2.0.0", "YourName")
class MariannaPersonality(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 数据目录
        self.data_dir = os.path.join(os.path.dirname(__file__), "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.user_states_file = os.path.join(self.data_dir, "user_states.json")
        self.user_profiles_file = os.path.join(self.data_dir, "user_profiles.json")
        self.conv_history_dir = os.path.join(self.data_dir, "conversation_history")
        os.makedirs(self.conv_history_dir, exist_ok=True)
        
        # 加载数据
        self.user_states = self._load_json(self.user_states_file, {})
        self.user_profiles = self._load_json(self.user_profiles_file, {})
        
        # 读取插件配置
        self.config = self._load_config()
        self._apply_config()
        
        # 初始化 Mnemosyne（如果可用且配置启用）
        self.memory_manager = None
        if MNEMOSYNE_AVAILABLE and self.config.get("enable_emotional_memory", True):
            try:
                self.memory_manager = MnemosyneManager(context)
                logger.info("✅ Mnemosyne 长期记忆已启用")
            except Exception as e:
                logger.error(f"Mnemosyne 初始化失败: {e}")
        
        # 启动后台自动总结任务
        asyncio.create_task(self._auto_summary_loop())
        
        # 注册消息处理器（拦截所有普通消息）
        @self.handler.on_message
        async def handle_message(event: AstrMessageEvent):
            return await self._on_message(event)
    
    # ======================== 辅助函数 ========================
    
    def _load_json(self, path: str, default: Any) -> Any:
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载 {path} 失败: {e}")
        return default
    
    def _save_json(self, path: str, data: Any):
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存 {path} 失败: {e}")
    
    def _load_config(self) -> Dict[str, Any]:
        try:
            from astrbot.core.config.config import Config
            config = Config.get_instance()
            plugin_cfg = config.get("plugins", {}).get("astrbot_plugin_marianna", {})
            return plugin_cfg
        except Exception:
            return {}
    
    def _apply_config(self):
        """应用配置到默认值和运行时参数"""
        DEFAULT_STATE["好感度"] = self.config.get("marianna_initial_favor", 20)
        DEFAULT_STATE["病娇值"] = self.config.get("marianna_initial_yan", 15)
        DEFAULT_STATE["优雅值"] = self.config.get("marianna_initial_elegance", 75)
        self.favor_multiplier = self.config.get("marianna_favor_multiplier", 1.0)
        self.yan_multiplier = self.config.get("marianna_yan_multiplier", 1.0)
        self.lock_threshold = self.config.get("marianna_lock_threshold", 100)
        self.auto_summary_interval = self.config.get("auto_summary_interval", 20)
        self.auto_summary_idle = self.config.get("auto_summary_idle_time", 300)
        self.enable_profile = self.config.get("enable_user_profile", True)
        self.temperature = self.config.get("marianna_temperature", 0.85)
        self.debug_mode = self.config.get("marianna_debug_mode", False)
    
    # ======================== 用户状态管理 ========================
    
    def _get_state(self, user_id: str) -> Dict[str, Any]:
        if user_id not in self.user_states:
            state = DEFAULT_STATE.copy()
            state["最后互动时间"] = datetime.now().isoformat()
            self.user_states[user_id] = state
            self._save_json(self.user_states_file, self.user_states)
        self.user_states[user_id]["最后互动时间"] = datetime.now().isoformat()
        self.user_states[user_id]["互动计数"] += 1
        return self.user_states[user_id]
    
    def _save_state(self, user_id: str, state: Dict):
        self.user_states[user_id] = state
        self._save_json(self.user_states_file, self.user_states)
    
    def _get_profile(self, user_id: str) -> Dict[str, Any]:
        if user_id not in self.user_profiles:
            self.user_profiles[user_id] = {
                "基本信息": {},
                "兴趣爱好": {"音乐": [], "书籍": [], "食物": [], "颜色": []},
                "性格特征": {"主要情绪": [], "沟通风格": ""},
                "互动记录": {"首次互动": datetime.now().isoformat(), "总互动次数": 0},
                "玛丽亚学习笔记": {"喜欢的话题": [], "反感的话题": [], "自动总结": []}
            }
        return self.user_profiles[user_id]
    
    def _save_profile(self, user_id: str, profile: Dict):
        self.user_profiles[user_id] = profile
        self._save_json(self.user_profiles_file, self.user_profiles)
    
    # ======================== 对话历史存储 ========================
    
    def _add_to_history(self, user_id: str, role: str, content: str):
        history_file = os.path.join(self.conv_history_dir, f"{user_id}.json")
        history = self._load_json(history_file, [])
        history.append({
            "role": role,
            "content": content,
            "time": datetime.now().isoformat()
        })
        # 保留最近 200 条
        if len(history) > 200:
            history = history[-200:]
        self._save_json(history_file, history)
    
    def _get_recent_history(self, user_id: str, limit: int = 20) -> List[Dict]:
        history_file = os.path.join(self.conv_history_dir, f"{user_id}.json")
        history = self._load_json(history_file, [])
        return history[-limit:]
    
    # ======================== 数值更新逻辑 ========================
    
    def _update_favor(self, state: Dict, message: str) -> int:
        msg = message.lower()
        change = 0
        # 正向事件
        if re.search(r"早安|晚安|早[呀啊]|晚[呀啊]", msg):
            change += 1
        if re.search(r"记得你(喜欢|最爱|讨厌)", msg):
            change += 4
        if re.search(r"其实(你是在|你就是|你明明)", msg):
            change += 7
        if re.search(r"保护|维护|站你这边|别怕", msg):
            change += 10
        if re.search(r"只有你|只在意你|你是唯一|只和你", msg):
            change += 12
        if re.search(r"命定之人|你就是我的|你是我的命", msg):
            change += 15
        # 负向事件
        if re.search(r"烦不烦|离我远点|受不了你|闭嘴", msg):
            change -= 15
        if re.search(r"不像某人|不如那个|没[他她]好看|比不上", msg):
            change -= 8
        if re.search(r"忙着?呢|没空|改天|下次吧", msg):
            change -= 4
        if re.search(r"别装了|假惺惺|虚伪", msg):
            change -= 10
        # 礼物
        if "玛丽亚礼物" in message:
            change += 6
        change = max(-25, min(25, change))
        new_val = state["好感度"] + int(change * self.favor_multiplier)
        state["好感度"] = max(0, min(100, new_val))
        return change
    
    def _update_yan(self, state: Dict, fav_change: int, lock_change: int, message: str) -> int:
        msg = message.lower()
        change = 0
        # 好感度跨阈值（每10点）
        old_fav = state["好感度"] - fav_change
        new_fav = state["好感度"]
        if new_fav // 10 > old_fav // 10:
            change += 5
        # 锁定进度增加
        if lock_change > 0:
            change += lock_change // 2
        # 触发词
        if re.search(r"和别人|其他人|那个人|她是谁", msg):
            change += 8
        if re.search(r"再考虑|不知道|不确定|也许吧", msg):
            change += 5
        if re.search(r"只爱你|只喜欢你|非你不可", msg):
            change -= 8
        change = max(-15, min(20, change))
        new_val = state["病娇值"] + int(change * self.yan_multiplier)
        state["病娇值"] = max(0, min(100, new_val))
        return change
    
    def _update_lock_progress(self, state: Dict, message: str) -> int:
        msg = message.lower()
        change = 0
        if re.search(r"其实(你是在|你就是|你明明)", msg):
            change += 7
        if re.search(r"只告诉你|我们的秘密|不告诉别人|我保证不说", msg):
            change += 10
        if re.search(r"保护|维护|站在你这边", msg):
            change += 10
        if re.search(r"只有你|只在意你|你是唯一", msg):
            change += 12
        if re.search(r"命定之人|你就是我的命", msg):
            change += 20
        if re.search(r"赴约|花园见|老地方|长椅", msg):
            change += 7
        if re.search(r"不会离开|有我在|别怕", msg) and state["焦虑值"] > 50:
            change += 10
        change = min(change, 20)
        new_val = state["锁定进度"] + change
        state["锁定进度"] = min(100, new_val)
        return change
    
    def _update_anxiety(self, state: Dict, message: str) -> int:
        msg = message.lower()
        change = 0
        if re.search(r"和别人|其他人|那个人|她|他", msg):
            change += 12
        if re.search(r"算了|随便|无所谓|你看着办", msg):
            change += 8
        if re.search(r"拥抱|别担心|有我在|安心", msg):
            change -= 15
        if re.search(r"你最重要|只爱你|非你不可", msg):
            change -= 12
        if re.search(r"太黏人了|控制欲|受不了", msg):
            change += 20
        # 检查上次互动时间（超过2天）
        last_time = datetime.fromisoformat(state["最后互动时间"])
        if datetime.now() - last_time > timedelta(days=2):
            change += 10
        change = max(-20, min(25, change))
        new_val = state["焦虑值"] + change
        state["焦虑值"] = max(0, min(100, new_val))
        return change
    
    def _update_elegance(self, state: Dict, anxiety_change: int, message: str) -> int:
        msg = message.lower()
        change = 0
        if anxiety_change > 0:
            change -= min(15, anxiety_change // 2)
        if re.search(r"优雅|得体|美丽|有气质|高贵", msg):
            change += 5
        if re.search(r"讨厌|恶心|做作|矫情", msg):
            change -= 15
        if re.search(r"别装了|假惺惺|虚伪", msg):
            change -= 20
        if state["优雅值"] < 30 and state["焦虑值"] > 60:
            change -= 5
        change = max(-25, min(15, change))
        new_val = state["优雅值"] + change
        state["优雅值"] = max(0, min(100, new_val))
        return change
    
    def _update_possessiveness(self, state: Dict):
        fav = state["好感度"]
        yan = state["病娇值"]
        possess = int(fav * 0.3 + yan * 0.5)
        if state["锁定进度"] >= self.lock_threshold:
            possess += 20
        state["占有欲"] = min(100, possess)
    
    def _determine_state(self, state: Dict) -> str:
        fav = state["好感度"]
        yan = state["病娇值"]
        lock = state["锁定进度"]
        anx = state["焦虑值"]
        ele = state["优雅值"]
        if lock >= self.lock_threshold:
            return STATE_NAMES["LOCKED_FATE"]
        if anx >= 70 and ele <= 50:
            return STATE_NAMES["ANXIETY_EDGE"]
        if ele <= 30:
            return STATE_NAMES["ELEGANCE_COLLAPSE"]
        if fav < 30:
            return STATE_NAMES["COLD_NOBLE"]
        if fav >= 60 and yan >= 50:
            return STATE_NAMES["LATENT_VINE"]
        if fav >= 60:
            return STATE_NAMES["SWEET_INDUCE"]
        if fav >= 30:
            return STATE_NAMES["TSUNDERE_PROBE"]
        return STATE_NAMES["COLD_NOBLE"]
    
    # ======================== 总结功能 ========================
    
    async def _generate_summary(self, history: List[Dict]) -> str:
        if not history:
            return "无足够对话内容可总结。"
        # 构建文本
        text = "\n".join([f"{h['role']}: {h['content'][:200]}" for h in history])
        prompt = f"""请总结以下对话，提取关键信息：用户兴趣、玛丽亚的情感变化、重要事件。输出简洁要点。

对话：
{text}"""
        try:
            llm = self.context.get_llm_client()
            resp = await llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5,
                max_tokens=300
            )
            return resp.choices[0].message.content
        except Exception as e:
            logger.error(f"总结生成失败: {e}")
            return "（总结生成失败）"
    
    async def _trigger_auto_summary(self, user_id: str):
        history = self._get_recent_history(user_id, limit=self.auto_summary_interval)
        if len(history) < 5:
            return
        summary = await self._generate_summary(history)
        # 存储到记忆
        if self.memory_manager:
            await self.memory_manager.add_memory(
                user_id,
                f"自动总结：{summary}",
                metadata={"type": "auto_summary", "timestamp": datetime.now().isoformat()}
            )
        else:
            profile = self._get_profile(user_id)
            profile["玛丽亚学习笔记"]["自动总结"].append({
                "time": datetime.now().isoformat(),
                "summary": summary
            })
            # 只保留最近5条
            if len(profile["玛丽亚学习笔记"]["自动总结"]) > 5:
                profile["玛丽亚学习笔记"]["自动总结"] = profile["玛丽亚学习笔记"]["自动总结"][-5:]
            self._save_profile(user_id, profile)
        logger.info(f"已为用户 {user_id} 生成自动总结")
    
    async def _auto_summary_loop(self):
        while True:
            await asyncio.sleep(60)  # 每分钟检查一次
            now = datetime.now()
            for user_id, state in list(self.user_states.items()):
                last = datetime.fromisoformat(state["最后互动时间"])
                if (now - last).total_seconds() > self.auto_summary_idle:
                    # 避免重复总结：检查最近一次总结时间
                    last_summary_file = os.path.join(self.data_dir, f"last_summary_{user_id}.txt")
                    if os.path.exists(last_summary_file):
                        with open(last_summary_file, 'r') as f:
                            last_time_str = f.read().strip()
                        if last_time_str:
                            last_time = datetime.fromisoformat(last_time_str)
                            if (now - last_time).total_seconds() < self.auto_summary_idle * 2:
                                continue
                    await self._trigger_auto_summary(user_id)
                    with open(last_summary_file, 'w') as f:
                        f.write(now.isoformat())
    
    # ======================== 用户画像学习 ========================
    
    async def _update_user_profile_from_message(self, user_id: str, user_msg: str, bot_reply: str):
        if not self.enable_profile:
            return
        profile = self._get_profile(user_id)
        prompt = f"""分析对话，提取用户画像信息。只返回 JSON，格式如下：
{{
  "基本信息": {{"称呼": "", "生日": "", "职业": ""}},
  "兴趣爱好": {{"音乐": [], "书籍": [], "食物": [], "颜色": []}},
  "性格特征": {{"主要情绪": [], "沟通风格": ""}},
  "喜欢的话题": [],
  "反感的话题": []
}}
对话：
用户：{user_msg}
玛丽亚：{bot_reply}
"""
        try:
            llm = self.context.get_llm_client()
            resp = await llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=400
            )
            data = json.loads(resp.choices[0].message.content)
            # 合并到 profile
            if "基本信息" in data:
                for k, v in data["基本信息"].items():
                    if v:
                        profile["基本信息"][k] = v
            if "兴趣爱好" in data:
                for cat, items in data["兴趣爱好"].items():
                    if cat in profile["兴趣爱好"] and items:
                        profile["兴趣爱好"][cat] = list(set(profile["兴趣爱好"][cat] + items))
            if "性格特征" in data:
                if "主要情绪" in data["性格特征"]:
                    profile["性格特征"]["主要情绪"] = list(set(profile["性格特征"]["主要情绪"] + data["性格特征"]["主要情绪"]))
                if data["性格特征"].get("沟通风格"):
                    profile["性格特征"]["沟通风格"] = data["性格特征"]["沟通风格"]
            if "喜欢的话题" in data:
                profile["玛丽亚学习笔记"]["喜欢的话题"] = list(set(profile["玛丽亚学习笔记"]["喜欢的话题"] + data["喜欢的话题"]))
            if "反感的话题" in data:
                profile["玛丽亚学习笔记"]["反感的话题"] = list(set(profile["玛丽亚学习笔记"]["反感的话题"] + data["反感的话题"]))
            profile["互动记录"]["总互动次数"] += 1
            self._save_profile(user_id, profile)
        except Exception as e:
            logger.error(f"用户画像更新失败: {e}")
    
    # ======================== 动态系统提示词 ========================
    
    async def _build_system_prompt(self, user_id: str, state: Dict, user_msg: str) -> str:
        base = "你是玛丽亚·特蕾莎，哈布斯堡贵族少女，傲娇+诱导性病娇。"
        state_instruction = {
            "冷傲贵族": "保持礼貌疏离，使用敬语。回答简短，不主动展开话题。",
            "傲娇试探": "口是心非，语速略快，耳尖泛红。可以说“哼”“才不是”。",
            "甜蜜诱导": "主动制造两人间的秘密，使用暗示性语言，尾音上扬。",
            "潜伏之藤": "优雅下隐含控制欲，出现“如果你...我会...”的假设性威胁。",
            "锁定·命定之人": "独占宣言，甜蜜威胁与脆弱交替。可以说“你是我的”。",
            "焦虑·崩溃边缘": "强装镇定但指尖颤抖，语气平静空洞。",
            "优雅崩坏": "失态，可能直接质问、流泪或抓住手腕。"
        }.get(state["当前状态"], "")
        
        # 用户画像摘要
        profile_text = ""
        if self.enable_profile:
            prof = self._get_profile(user_id)
            if prof["基本信息"].get("称呼"):
                profile_text += f"用户希望被称呼为：{prof['基本信息']['称呼']}\n"
            if prof["兴趣爱好"]["音乐"]:
                profile_text += f"用户喜欢音乐：{', '.join(prof['兴趣爱好']['音乐'])}\n"
            if prof["兴趣爱好"]["食物"]:
                profile_text += f"用户喜欢食物：{', '.join(prof['兴趣爱好']['食物'])}\n"
            if prof["玛丽亚学习笔记"]["喜欢的话题"]:
                profile_text += f"用户喜欢聊：{', '.join(prof['玛丽亚学习笔记']['喜欢的话题'][:3])}\n"
        
        # 相关记忆检索
        memory_text = ""
        if self.memory_manager:
            try:
                memories = await self.memory_manager.search_memories(user_id, user_msg, limit=3)
                if memories:
                    memory_text = "相关记忆：\n" + "\n".join([m.get("content", "")[:100] for m in memories]) + "\n"
            except Exception as e:
                logger.error(f"记忆检索失败: {e}")
        
        return f"""{base}
当前状态：{state['当前状态']}。{state_instruction}
{profile_text}
{memory_text}
注意：不要直接复述记忆内容，而是自然融入对话。保持贵族优雅，除非优雅崩坏状态。"""
    
    # ======================== 指令处理 ========================
    
    @filter.command("玛丽亚状态")
    async def cmd_status(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        state = self._get_state(user_id)
        desc = STATE_DESCRIPTIONS.get(state["当前状态"], "")
        result = f"""📜 **玛丽亚·特蕾莎的心之日记**

{desc}

💝 好感度：{state['好感度']}/100
🔒 锁定进度：{state['锁定进度']}/{self.lock_threshold}
🌿 病娇值：{state['病娇值']}/100
✨ 优雅值：{state['优雅值']}/100
🎭 当前状态：{state['当前状态']}

> *这是她不会说出口的内心独白，请你务必保密。*"""
        yield event.plain_result(result)
    
    @filter.command("玛丽亚重置")
    async def cmd_reset(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        self.user_states[user_id] = DEFAULT_STATE.copy()
        self.user_states[user_id]["最后互动时间"] = datetime.now().isoformat()
        self._save_json(self.user_states_file, self.user_states)
        # 可选：保留画像，不重置
        yield event.plain_result("玛丽亚·特蕾莎轻轻整理了一下裙摆，像是什么都没有发生过。\n> *但她看向你的目光中，有一丝说不清道不明的情绪一闪而过。*")
    
    @filter.command("玛丽亚问候")
    async def cmd_greeting(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        state = self._get_state(user_id)
        hour = datetime.now().hour
        if 5 <= hour < 12:
            reply = "早、早安！\n*（她微微别过脸，手指轻轻缠绕着一缕金发）* 我才不是特意等你来问好的。"
        elif 12 <= hour < 18:
            reply = "午安。\n*（她提起裙摆行了一个标准的屈膝礼，眼中闪过一丝不易察觉的笑意）*"
        elif 18 <= hour < 22:
            reply = "晚……晚安。\n*（耳尖微红）* 记得早点休息，不要……让我担心。"
        else:
            reply = "这么晚了还不休息？\n*（她皱了皱眉，但语气里藏着关心）* 贵族的礼仪不包括熬夜的。"
        state["好感度"] = min(100, state["好感度"] + 1)
        self._update_possessiveness(state)
        state["当前状态"] = self._determine_state(state)
        self._save_state(user_id, state)
        yield event.plain_result(reply)
    
    @filter.command("玛丽亚礼物")
    async def cmd_gift(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        # 提取礼物描述
        gift_desc = event.message_str.replace("/玛丽亚礼物", "").strip()
        if not gift_desc:
            yield event.plain_result("> *（玛丽亚微微歪头，眼神中带着期待和一丝不悦）* 哼，说要送礼物，却不说送什么？这是在戏弄一位贵族小姐吗？")
            return
        state = self._get_state(user_id)
        state["好感度"] = min(100, state["好感度"] + 8)
        state["信任度"] = min(100, state["信任度"] + 5)
        state["礼物记录"].append(gift_desc)
        
        gift_lower = gift_desc.lower()
        if re.search(r"花|玫瑰|百合|郁金香", gift_lower):
            reply = f"*（她接过{gift_desc}，琥珀色的眼眸微微睁大）* 哼……算你有品味。不过，就、就算你送我花，我也不会……（低头嗅了嗅花香，嘴角不自觉上扬）"
        elif re.search(r"书|小说|诗集|手稿", gift_lower):
            reply = f"*（她眼睛一亮，但立刻恢复平静）* 你……你怎么知道我喜欢看这个？……不，我、我才不喜欢骑士小说！不过……既然你送了，我就勉强收下好了。"
        elif re.search(r"胸针|发饰|首饰|宝石|项链", gift_lower):
            reply = f"*（她的手指轻轻抚过{gift_desc}）* 这个……很漂亮。（突然抬头盯着你）你送过别人同样的东西吗？……没有？哼，那就好。"
        else:
            reply = f"*（她优雅地接过{gift_desc}，但耳尖悄悄泛红）* 哼，我才没有很高兴……不过，谢谢。"
        
        self._update_possessiveness(state)
        state["当前状态"] = self._determine_state(state)
        self._save_state(user_id, state)
        yield event.plain_result(reply)
    
    @filter.command("玛丽亚总结")
    async def cmd_summary(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        history = self._get_recent_history(user_id, limit=30)
        if len(history) < 3:
            yield event.plain_result("> *（玛丽亚轻轻摇头）* 我们聊得还不够多，再聊一会儿吧。")
            return
        yield event.plain_result("📝 正在总结最近的对话，请稍候...")
        summary = await self._generate_summary(history)
        # 存储到记忆
        if self.memory_manager:
            await self.memory_manager.add_memory(
                user_id,
                f"手动总结：{summary}",
                metadata={"type": "manual_summary", "timestamp": datetime.now().isoformat()}
            )
        else:
            profile = self._get_profile(user_id)
            profile["玛丽亚学习笔记"]["自动总结"].append({
                "time": datetime.now().isoformat(),
                "summary": summary
            })
            self._save_profile(user_id, profile)
        yield event.plain_result(f"📝 **玛丽亚的对话笔记**\n\n{summary}\n\n> *她将这份总结默默收入了记忆中。*")
    
    @filter.command("玛丽亚画像")
    async def cmd_profile(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        prof = self._get_profile(user_id)
        if not any(prof["兴趣爱好"].values()) and not prof["基本信息"]:
            yield event.plain_result("> *（玛丽亚轻声说）* 我还不够了解你。多聊聊，让我记住你的样子。")
            return
        text = "📖 **玛丽亚眼中的你**\n\n"
        if prof["基本信息"].get("称呼"):
            text += f"我喜欢称呼你为：{prof['基本信息']['称呼']}\n"
        if prof["兴趣爱好"]["音乐"]:
            text += f"你喜欢听：{', '.join(prof['兴趣爱好']['音乐'])}\n"
        if prof["兴趣爱好"]["书籍"]:
            text += f"你喜欢读：{', '.join(prof['兴趣爱好']['书籍'])}\n"
        if prof["兴趣爱好"]["食物"]:
            text += f"你喜欢吃：{', '.join(prof['兴趣爱好']['食物'])}\n"
        if prof["兴趣爱好"]["颜色"]:
            text += f"你喜欢的颜色：{', '.join(prof['兴趣爱好']['颜色'])}\n"
        if prof["玛丽亚学习笔记"].get("喜欢的话题"):
            text += f"你喜欢聊：{', '.join(prof['玛丽亚学习笔记']['喜欢的话题'][:3])}\n"
        if prof["性格特征"].get("沟通风格"):
            text += f"你的沟通风格：{prof['性格特征']['沟通风格']}\n"
        text += f"\n> *我们已互动 {prof['互动记录']['总互动次数']} 次。*"
        yield event.plain_result(text)
    
    @filter.command("玛丽亚重置学习")
    async def cmd_reset_learning(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        if user_id in self.user_profiles:
            del self.user_profiles[user_id]
            self._save_json(self.user_profiles_file, self.user_profiles)
        yield event.plain_result("玛丽亚轻轻整理了一下裙摆，关于你的记忆被悄悄抹去了一部分……")
    
    @filter.command("玛丽亚重载配置")
    async def cmd_reload_config(self, event: AstrMessageEvent):
        self.config = self._load_config()
        self._apply_config()
        yield event.plain_result("⚙️ 玛丽亚·特蕾莎的配置已重新加载。\n> *她似乎感受到了什么变化，轻轻整理了一下裙摆。*")
    
    # ======================== 主消息处理 ========================
    
async def _on_message(self, event: AstrMessageEvent):
    """处理用户消息（异步生成器）"""
    # 提取纯文本
    plain_text = ""
    for comp in event.get_messages():
        if isinstance(comp, Plain):
            plain_text += comp.text
    if not plain_text:
        return  

    # 如果是指令，让框架处理（指令已注册，这里直接返回避免重复处理）
    if plain_text.startswith("/"):
        return

    user_id = event.get_sender_id()
    state = self._get_state(user_id)

    # 更新数值
    fav_change = self._update_favor(state, plain_text)
    lock_change = self._update_lock_progress(state, plain_text)
    yan_change = self._update_yan(state, fav_change, lock_change, plain_text)
    anx_change = self._update_anxiety(state, plain_text)
    ele_change = self._update_elegance(state, anx_change, plain_text)
    self._update_possessiveness(state)

    new_state_name = self._determine_state(state)
    event_trigger = None
    if new_state_name != state["当前状态"]:
        state["当前状态"] = new_state_name
        if state["锁定进度"] >= self.lock_threshold and not state.get("已触发锁定事件", False):
            state["已触发锁定事件"] = True
            event_trigger = "locked"

    self._save_state(user_id, state)

    # 存储用户消息到历史
    self._add_to_history(user_id, "user", plain_text)

    # 更新用户画像（异步，不等待）
    if self.enable_profile:
        asyncio.create_task(self._update_user_profile_from_message(user_id, plain_text, ""))

    # 构建系统提示词
    system_prompt = await self._build_system_prompt(user_id, state, plain_text)
    user_prompt = plain_text

    # 调用 LLM
    llm = self.context.get_llm_client()
    try:
        resp = await llm.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=self.temperature,
            max_tokens=500
        )
        reply = resp.choices[0].message.content

        if event_trigger == "locked":
            reply = "*（她忽然安静下来，琥珀色的眼眸直直望着你，过了很久，她轻声说——）*\n\n" + reply + "\n\n> *从这一刻起，你已经是她的“命定之人”了。*"

        self._add_to_history(user_id, "assistant", reply)

        if self.enable_profile:
            asyncio.create_task(self._update_user_profile_from_message(user_id, plain_text, reply))

        if self.debug_mode:
            reply += f"\n\n---\n*[好感:{state['好感度']} 病娇:{state['病娇值']} 锁定:{state['锁定进度']}%]*"

        yield event.plain_result(reply)
    except Exception as e:
        logger.error(f"LLM 调用失败: {e}")
        yield event.plain_result("*（玛丽亚沉默了片刻，似乎陷入了某种思绪...）*")

    # 函数结束，隐式返回 None（不带值，允许）
    return
    
    async def terminate(self):
        """插件卸载时保存所有数据"""
        self._save_json(self.user_states_file, self.user_states)
        self._save_json(self.user_profiles_file, self.user_profiles)
        logger.info("玛丽亚·特蕾莎插件已卸载，所有数据已保存。")
