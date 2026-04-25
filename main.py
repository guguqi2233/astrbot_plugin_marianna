import asyncio
import copy
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register

try:
    from .marianna.constants import *
    from .marianna.runtime import MariannaRuntimeMixin
    from .marianna.memory import MariannaMemoryMixin
    from .marianna.state_store import MariannaStateStoreMixin
    from .marianna.history import MariannaHistoryMixin
    from .marianna.analysis import MariannaAnalysisMixin
    from .marianna.profile import MariannaProfileMixin
    from .marianna.prompts import MariannaPromptMixin
    from .marianna.turn import MariannaTurnMixin
except ImportError:
    if __package__:
        raise
    from marianna.constants import *
    from marianna.runtime import MariannaRuntimeMixin
    from marianna.memory import MariannaMemoryMixin
    from marianna.state_store import MariannaStateStoreMixin
    from marianna.history import MariannaHistoryMixin
    from marianna.analysis import MariannaAnalysisMixin
    from marianna.profile import MariannaProfileMixin
    from marianna.prompts import MariannaPromptMixin
    from marianna.turn import MariannaTurnMixin

# ======================== Plugin Entry ========================
@register("astrbot_plugin_marianna", "玛丽亚·特蕾莎·冯·哈布斯堡", "1.0.0", "guguqi2233")
class MariannaPersonality(
    MariannaRuntimeMixin,
    MariannaMemoryMixin,
    MariannaStateStoreMixin,
    MariannaHistoryMixin,
    MariannaAnalysisMixin,
    MariannaProfileMixin,
    MariannaPromptMixin,
    MariannaTurnMixin,
    Star,
):
    """哈布斯堡贵族少女人格插件，通过对话语义驱动状态变化。"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context, config)

        # 使用全局 logger 统一日志记录
        self.logger = logger

        # 数据目录 - 使用 Path 对象
        self.data_dir = Path(__file__).parent / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.user_states_file = self.data_dir / "user_states.json"
        self.user_profiles_file = self.data_dir / "user_profiles.json"
        self.global_state_file = self.data_dir / "global_state.json"
        self.conv_history_dir = self.data_dir / "conversation_history"
        self.conv_history_dir.mkdir(exist_ok=True)

        # 文件锁（用于并发控制）
        self._file_locks: Dict[str, asyncio.Lock] = {}

        # 加载数据
        self.user_states: Dict[str, Dict[str, Any]] = self._load_json(self.user_states_file, {})
        self.user_profiles: Dict[str, Dict[str, Any]] = self._load_json(self.user_profiles_file, {})
        self.global_state: Dict[str, Any] = self._load_json(self.global_state_file, {})

        # 读取插件配置（由 AstrBot 框架通过 __init__ 第二参数传入）
        self.config: Dict[str, Any] = config if config else {}
        self._static_prompt_cache: Dict[str, str] = {}
        self._dynamic_prompt_cache: Dict[Any, str] = {}
        self._mnemosyne_query_cache: Dict[str, Dict[str, Any]] = {}
        self._recent_history_cache: Dict[Any, List[Dict[str, str]]] = {}
        self._apply_config()

        # Mnemosyne 插件引用（将在启动后动态检测）
        self.mnemosyne_plugin = None
        self._mnemosyne_checked = False
        self.mnemosyne_available = False

        # 待处理事件缓存（用于 on_llm_request → on_llm_response 传递）
        self._pending_events: Dict[str, Dict[str, Any]] = {}
        self._pending_debug_deltas: Dict[str, Dict[str, Any]] = {}
        self._analysis_request_cache: Dict[str, Dict[str, Any]] = {}
        self._session_alias_queues: Dict[str, List[str]] = {}
        self._session_alias_created_at: Dict[str, float] = {}
        self._session_counter = 0

        # 后台任务引用（用于清理）
        self._background_tasks: List[asyncio.Task] = []
        self._pending_tasks: Set[asyncio.Task] = set()
        self._pending_task_semaphore = asyncio.Semaphore(BACKGROUND_TASK_CONCURRENCY)
        self._perf_stats: Dict[str, Dict[str, Any]] = {}
        self._state_versions: Dict[str, int] = {}
        self._state_dirty_users: Set[str] = set()
        self._profile_dirty_users: Set[str] = set()
        self._state_save_task: Optional[asyncio.Task] = None
        self._profile_save_task: Optional[asyncio.Task] = None
        self._history_append_counts: Dict[str, int] = {}
        self._summary_dirty_users: Set[str] = set()
        self._mnemosyne_entries_cache: Dict[str, Dict[str, Any]] = {}
        self._mnemosyne_write_buffers: Dict[str, List[Dict[str, Any]]] = {}
        self._mnemosyne_write_waiters: Dict[str, List[asyncio.Future]] = {}
        self._mnemosyne_flush_tasks: Dict[str, asyncio.Task] = {}
        self._profile_update_running: Set[str] = set()
        self._profile_update_rerun: Dict[str, Dict[str, Any]] = {}
        self._user_locks: Dict[str, asyncio.Lock] = {}

        self.logger.info("玛丽亚·特蕾莎插件初始化完成")

    # ======================== 生命周期方法 ========================

    async def initialize(self):
        """插件激活时调用，用于启动后台任务"""
        try:
            self.logger.info("玛丽亚·特蕾莎插件正在加载...")

            task1 = asyncio.create_task(self._auto_summary_loop())
            task2 = asyncio.create_task(self._check_mnemosyne_availability())
            self._background_tasks.extend([task1, task2])

            self.logger.info("玛丽亚·特蕾莎插件加载完成")
        except Exception as e:
            self.logger.error(f"插件加载失败: {e}", exc_info=True)

    async def terminate(self):
        """插件禁用/重载时调用，用于清理资源并保存数据"""
        try:
            self.logger.info("玛丽亚·特蕾莎插件正在卸载...")

            # 取消所有后台任务
            for task in self._background_tasks:
                if not task.done():
                    task.cancel()

            # 等待任务完成（忽略取消异常）
            if self._background_tasks:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

            # 等待所有已派发但尚未完成的异步写入任务，避免重载/卸载时丢数据
            if self._pending_tasks:
                await asyncio.gather(*list(self._pending_tasks), return_exceptions=True)

            await self._drain_mnemosyne_flush_tasks()

            # 保存所有数据
            all_saved = await self._save_all_data()

            if all_saved:
                self.logger.info("玛丽亚·特蕾莎插件已卸载，所有数据已保存")
            else:
                self.logger.warning("玛丽亚·特蕾莎插件已卸载，但部分数据保存失败，请检查日志")
        except Exception as e:
            self.logger.error(f"插件卸载时出错: {e}", exc_info=True)

    # ======================== LLM Hooks ========================

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        当 AstrBot 向 LLM 发送请求前触发。

        职责：
          1. 调用分析型 LLM 计算当前用户的情感状态增量
          2. 将角色设定、用户画像、相关记忆注入 req.system_prompt
          3. 将最近对话历史注入 req.contexts
          4. 设置 req.temperature
        """
        session_key = ""
        user_lock: Optional[asyncio.Lock] = None
        lock_acquired = False
        try:
            request_started_at = time.perf_counter()
            user_id = event.get_sender_id()
            user_lock = self._get_user_lock(user_id)
            await user_lock.acquire()
            lock_acquired = True

            user_name = (event.get_sender_name() or "").strip() or str(user_id)
            session_key = self._get_session_key(event, user_id, create=True)
            message_text = event.message_str
            message_key = self._normalize_analysis_content(message_text)
            self._purge_stale_pending_records()

            state, old_state_name, old_lock_progress = await self._prepare_turn_state(
                user_id,
                user_name,
            )
            analysis_bundle = await self._run_turn_analysis(
                event,
                user_id,
                user_name,
                session_key,
                message_text,
                message_key,
                state,
                old_state_name,
                old_lock_progress,
            )
            applied_changes = analysis_bundle["applied_changes"]
            turn_analysis = analysis_bundle["turn_analysis"]
            active_event = analysis_bundle["active_event"]
            skip_analysis = bool(analysis_bundle["skip_analysis"])
            state_snapshot = self._copy_state_for_prompt(state)

            self._pending_debug_deltas[session_key] = {
                "message_key": message_key,
                "deltas": dict(applied_changes),
                "turn_analysis": dict(turn_analysis),
                "active_event": dict(active_event),
                "_created_at": time.monotonic(),
            }

            user_lock.release()
            lock_acquired = False
            user_lock = None

            await self._inject_prompt_and_context(
                req,
                user_id,
                state_snapshot,
                message_text,
                turn_analysis,
                active_event,
                skip_analysis,
            )

            self.logger.debug(
                f"[on_llm_request] user={user_id} "
                f"state={state_snapshot['当前状态']} "
                f"deltas={applied_changes} "
                f"contexts={len(getattr(req, 'contexts', []) or [])} "
                f"system_prompt_len={len(req.system_prompt)}"
            )
            self._log_perf(
                "on_llm_request",
                request_started_at,
                user_id,
                extra=f"skip_analysis={int(skip_analysis)}",
                threshold_ms=5.0,
            )
        except asyncio.CancelledError:
            if lock_acquired and user_lock is not None:
                user_lock.release()
            raise
        except Exception as e:
            if lock_acquired and user_lock is not None:
                user_lock.release()
            self._pending_events.pop(session_key, None)
            self._pending_debug_deltas.pop(session_key, None)
            self._analysis_request_cache.pop(session_key, None)
            self._session_alias_created_at.pop(session_key, None)
            self.logger.error(f"on_llm_request 处理失败: {e}", exc_info=True)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse):
        """
        当 AstrBot 收到 LLM 响应后触发。

        职责：
          1. 处理特殊剧情事件（锁定事件前缀）
          2. 在调试模式下追加数值信息
          3. 将回复存入对话历史
          4. 异步更新用户画像
        """
        try:
            user_id = event.get_sender_id()
            session_key = self._get_session_key(event, user_id)
            message_key = self._normalize_analysis_content(event.message_str)
            self._purge_stale_pending_records()
            reply = self._strip_debug_artifacts(response.completion_text or "")
            state = self.user_states.get(user_id, {})
            self._analysis_request_cache.pop(session_key, None)
            pending_debug = self._pending_debug_deltas.pop(session_key, {})
            self._session_alias_created_at.pop(session_key, None)
            if (
                isinstance(pending_debug, dict)
                and pending_debug.get("message_key") == message_key
            ):
                deltas = dict(pending_debug.get("deltas", {}))
                turn_analysis = dict(pending_debug.get("turn_analysis", {}))
                active_event = dict(pending_debug.get("active_event", {}))
            else:
                deltas = {}
                turn_analysis = {}
                active_event = {}

            # ── 1. 特殊事件处理 ───────────────────────────────────────────
            pending_event = self._pending_events.pop(session_key, None)
            if (
                isinstance(pending_event, dict)
                and pending_event.get("type") == "locked"
                and pending_event.get("message_key") == message_key
            ):
                locked_prefix = (
                    "*（她忽然安静下来，琥珀色的眼眸直直望着你，"
                    "过了很久，她轻声说——）*\n\n"
                )
                locked_suffix = (
                    "\n\n> *从这一刻起，你已经是她的"
                    "\u201c命定之人\u201d了。*"
                )
                reply = locked_prefix + reply + locked_suffix

            response.completion_text = reply

            # ── 2. 调试模式追加数值 ───────────────────────────────────────
            if state.get("调试模式", self.default_debug_mode):
                response.completion_text = reply + self._build_debug_footer(state, deltas)

            # ── 3. 存入对话历史 ───────────────────────────────────────────
            self._spawn_task(self._add_to_history(user_id, "assistant", reply))

            # ── 4. 更新用户画像（异步，不阻塞响应） ──────────────────────
            if self.enable_profile and self._should_update_user_profile(event.message_str, state):
                self._schedule_profile_update(
                    user_id,
                    event.message_str,
                    reply,
                    event=event,
                )

            if self.enable_emotional_memory and self.enable_selective_interaction_memory:
                self._spawn_task(
                    self._store_interaction_memory_if_needed(
                        user_id,
                        event.message_str,
                        deltas,
                        state,
                        turn_analysis=turn_analysis,
                        bot_reply=reply,
                        active_event=active_event,
                    )
                )

            self.logger.debug(
                f"[on_llm_response] user={user_id} reply_len={len(reply or '')}"
            )
        except Exception as e:
            self.logger.error(f"on_llm_response 处理失败: {e}", exc_info=True)

    # ======================== Commands ========================

    @filter.command_group("玛丽亚")
    def marianna_group(self):
        """玛丽亚插件指令组，直接输入 `/玛丽亚` 可查看命令菜单。"""
        pass

    @marianna_group.command("调试")  # type: ignore
    async def cmd_marianna_debug(self, event: AstrMessageEvent):
        """切换调试模式，在对话回复后显示当前数值与本轮变化。"""
        user_id = event.get_sender_id()
        state = self._get_state(user_id, count_interaction=False)
        await self._reconcile_destined_one_state(user_id, state)
        state["调试模式"] = not state.get("调试模式", self.default_debug_mode)
        self._schedule_state_save(user_id, state)
        status = "开启" if state["调试模式"] else "关闭"
        yield event.plain_result(
            f"🔍 玛丽亚调试模式已{status}。\n"
            f"> *之后的普通对话回复将{'显示' if state['调试模式'] else '不再显示'}当前数值与本轮变化。*"
        )

    @marianna_group.command("状态")  # type: ignore
    async def cmd_marianna_status(self, event: AstrMessageEvent):
        """查看当前所有数值与状态描述。"""
        user_id = event.get_sender_id()
        state = self._get_state(user_id, count_interaction=False)
        if await self._reconcile_destined_one_state(user_id, state):
            self._schedule_state_save(user_id, state)
        yield event.plain_result(self._build_state_report(state))

    @marianna_group.command("重置")  # type: ignore
    async def cmd_marianna_reset(self, event: AstrMessageEvent):
        """重置该用户的所有状态，但保留已学习的用户画像。"""
        user_id = event.get_sender_id()
        new_state = copy.deepcopy(DEFAULT_STATE)
        new_state["最后互动时间"] = None
        new_state["调试模式"] = self.default_debug_mode
        self._clear_pending_for_user(user_id)
        reset_notice = "> *她对你的印象仍在，但此刻的一切情绪数值都已重新开始。*"
        if self._is_destined_user(user_id):
            await self._clear_destined_one()
            reset_notice = (
                "> *她对你的印象仍在，但此刻的一切情绪数值都已重新开始。*"
                "\n> *全局“命定之人”标记也一并解除。*"
            )
        self._schedule_state_save(user_id, new_state)
        yield event.plain_result(
            "玛丽亚轻轻整理了一下裙摆，仿佛把纷乱的情绪重新收回了心底。\n"
            f"{reset_notice}"
        )

    @marianna_group.command("画像")  # type: ignore
    async def cmd_marianna_profile(self, event: AstrMessageEvent):
        """显示玛丽亚对你的印象，也就是她已经学到的用户画像。"""
        user_id = event.get_sender_id()
        profile = self._get_profile(user_id)
        yield event.plain_result(self._build_profile_report(profile))

    @marianna_group.command("重置学习")  # type: ignore
    async def cmd_marianna_reset_learning(self, event: AstrMessageEvent):
        """清除玛丽亚已学习的用户画像，但不影响当前状态。"""
        user_id = event.get_sender_id()
        if user_id in self.user_profiles:
            del self.user_profiles[user_id]
            self._schedule_profile_file_save(user_id)
        yield event.plain_result(
            "玛丽亚将关于你的学习笔记重新锁进了抽屉。\n"
            "> *她会重新认识你，但此刻的情绪状态不会因此被抹去。*"
        )

    @marianna_group.command("重载配置")  # type: ignore
    async def cmd_marianna_reload_config(self, event: AstrMessageEvent):
        """热重载插件配置，并立即应用到后续行为。"""
        self._apply_config()
        yield event.plain_result(
            "⚙️ 玛丽亚的插件配置已重新载入。\n"
            "> *新的参数会从接下来的对话开始生效。*"
        )

    @marianna_group.command("perf")  # type: ignore
    async def cmd_marianna_perf(self, event: AstrMessageEvent):
        """查看最近一段时间的内部性能统计。"""
        yield event.plain_result(self._build_perf_report())
