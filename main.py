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
@register("astrbot_plugin_marianna", "玛丽亚·特蕾莎·冯·哈布斯堡", PLUGIN_VERSION, "guguqi2233")
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

        # 数据盽 - 使用 Path 对象
        self.data_dir = Path(__file__).parent / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.user_states_file = self.data_dir / "user_states.json"
        self.user_profiles_file = self.data_dir / "user_profiles.json"
        self.global_state_file = self.data_dir / "global_state.json"
        self.local_memory_db_file = self.data_dir / "local_memory.db"
        self.conv_history_dir = self.data_dir / "conversation_history"
        self.conv_history_dir.mkdir(exist_ok=True)

        # 文件锁（用于并发控制?
        self._file_locks: Dict[str, asyncio.Lock] = {}

        # 加载数据
        self.user_states: Dict[str, Dict[str, Any]] = self._load_json(self.user_states_file, {})
        self.user_profiles: Dict[str, Dict[str, Any]] = self._load_json(self.user_profiles_file, {})
        self.global_state: Dict[str, Any] = self._load_json(self.global_state_file, {})

        # 读取插件配置（由 AstrBot 框架通过 __init__ 笺参数传入?
        self.config: Dict[str, Any] = config if config else {}
        self._static_prompt_cache: Dict[str, str] = {}
        self._dynamic_prompt_cache: Dict[Any, str] = {}
        self._mnemosyne_query_cache: Dict[str, Dict[str, Any]] = {}
        self._local_memory_query_cache: Dict[str, Dict[str, Any]] = {}
        self._recent_history_cache: Dict[Any, List[Dict[str, str]]] = {}
        self._style_fingerprint_cache: Dict[Any, str] = {}
        self._apply_config()

        # Mnemosyne 插件引用（将在启动后动测）
        self.mnemosyne_plugin = None
        self._mnemosyne_checked = False
        self.mnemosyne_available = False

        # 待理事件缓存（用于 on_llm_request ?on_llm_response 传）
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
        self._profile_versions: Dict[str, int] = {}
        self._profile_dirty_users: Set[str] = set()
        self._state_save_task: Optional[asyncio.Task] = None
        self._profile_save_task: Optional[asyncio.Task] = None
        self._history_append_counts: Dict[str, int] = {}
        self._summary_dirty_users: Set[str] = set()
        self._mnemosyne_entries_cache: Dict[str, Dict[str, Any]] = {}
        self._mnemosyne_write_buffers: Dict[str, List[Dict[str, Any]]] = {}
        self._mnemosyne_write_waiters: Dict[str, List[asyncio.Future]] = {}
        self._mnemosyne_flush_tasks: Dict[str, asyncio.Task] = {}
        self._local_memory_initialized = False
        self._local_memory_fts_available = False
        self._profile_update_running: Set[str] = set()
        self._profile_update_rerun: Dict[str, Dict[str, Any]] = {}
        self._user_locks: Dict[str, asyncio.Lock] = {}

        self.logger.info("玛丽亚·特蕾莎插件初始化完成")

    # ======================== 生命周期方法 ========================

    async def initialize(self):
        """插件激活时调用，用于启动后台任务。"""
        try:
            self.logger.info("玛丽亚·特蕾莎插件正在加载...")

            self._spawn_background_task(self._auto_summary_loop(), "auto_summary_loop")
            self._spawn_background_task(
                self._check_mnemosyne_availability(),
                "mnemosyne_availability",
            )

            self.logger.info("玛丽亚·特蕾莎插件加载完成")
        except Exception as e:
            self.logger.error(f"插件加载失败: {e}", exc_info=True)

    async def terminate(self):
        """插件禁用/重载时调用，用于清理资源并保存数据。"""
        try:
            self.logger.info("玛丽亚·特蕾莎插件正在卸载...")

            # 取消有后台任?
            background_tasks = self._get_runtime_task_list("_background_tasks")
            for task in background_tasks:
                if isinstance(task, asyncio.Task) and not task.done():
                    task.cancel()

            # 等待任务完成（忽略取消异常）
            if background_tasks:
                await asyncio.gather(
                    *[
                        task
                        for task in background_tasks
                        if isinstance(task, asyncio.Task)
                    ],
                    return_exceptions=True,
                )
            background_tasks.clear()

            # 等待有已派发但尚朮成的异写入任务，避免重?卸载时丢数据
            pending_tasks = self._get_runtime_task_set("_pending_tasks")
            if pending_tasks:
                await asyncio.gather(*list(pending_tasks), return_exceptions=True)

            await self._drain_mnemosyne_flush_tasks()

            # 保存有数?
            all_saved = await self._save_all_data()

            if all_saved:
                self.logger.info("玛丽亚·特蕾莎插件已卸载，有数捷保存")
            else:
                self.logger.warning("玛丽亚·特蕾莎插件已卸载，但部分数据保存失败，请检查日志")
        except Exception as e:
            self.logger.error(f"插件卸载时出? {e}", exc_info=True)

    # ======================== LLM Hooks ========================

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        ?AstrBot ?LLM 发求前触发?

        职责?
          1. 调用分析?LLM 计算当前用户的情感状态?
          2. 将色定用户画像相关忆注?req.system_prompt
          3. 按配置将插件本地历史或总结注入 req.contexts；默认不注入，避免与 AstrBot 历史重复
          4. 设置 req.temperature
        """
        session_key = ""
        user_lock: Optional[asyncio.Lock] = None
        lock_acquired = False
        try:
            request_started_at = time.perf_counter()
            user_id = self._get_scoped_user_id(event)
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
            scene_memory_policy = self._build_scene_memory_policy(event)
            state["_scene_memory_policy"] = scene_memory_policy
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
            state["近是否轻量Prompt"] = skip_analysis
            state_snapshot = self._copy_state_for_prompt(state)
            state_snapshot["本轮场景记忆策略"] = scene_memory_policy

            self._get_runtime_dict_cache("_pending_debug_deltas")[session_key] = {
                "user_id": user_id,
                "message_key": message_key,
                "deltas": dict(applied_changes),
                "turn_analysis": dict(turn_analysis),
                "active_event": dict(active_event),
                "state_explanation": self._coerce_runtime_dict_value(analysis_bundle.get("state_explanation", {})),
                "skip_analysis": skip_analysis,
                "debug_mode": bool(state.get("调试模式", self.default_debug_mode)),
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
                event=event,
            )

            self.logger.debug(
                f"[on_llm_request] user={user_id} "
                f"state={state_snapshot['当前状态']} "
                f"deltas={applied_changes} "
                f"contexts={len(self._coerce_runtime_list_value(getattr(req, 'contexts', [])))} "
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
            self._get_runtime_dict_cache("_pending_events").pop(session_key, None)
            self._get_runtime_dict_cache("_pending_debug_deltas").pop(session_key, None)
            self._get_runtime_dict_cache("_analysis_request_cache").pop(session_key, None)
            self._get_runtime_dict_cache("_session_alias_created_at").pop(session_key, None)
            self.logger.error(f"on_llm_request 处理失败: {e}", exc_info=True)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse):
        """
        ?AstrBot 收到 LLM 响应后触发?

        职责?
          1. 处理特殊剧情事件（锁定事件前?
          2. 在调试模式下追加数信?
          3. 将回复存入话历?
          4. 异更新用户画像
        """
        try:
            user_id = self._get_scoped_user_id(event)
            session_key = self._get_session_key(event, user_id)
            message_key = self._normalize_analysis_content(event.message_str)
            self._purge_stale_pending_records()
            reply = self._strip_debug_artifacts(response.completion_text or "")
            self._get_runtime_dict_cache("_analysis_request_cache").pop(session_key, None)
            debug_session_key, pending_debug = self._pop_pending_debug_delta(session_key, message_key)
            self._get_runtime_dict_cache("_session_alias_created_at").pop(debug_session_key, None)
            if isinstance(pending_debug, dict) and pending_debug.get("user_id"):
                user_id = str(pending_debug.get("user_id") or user_id)
            states = self._get_user_states_store() if hasattr(self, "_get_user_states_store") else getattr(self, "user_states", {})
            state = states.get(user_id, {}) if isinstance(states, dict) else {}
            if not isinstance(state, dict):
                state = {}
            if (
                isinstance(pending_debug, dict)
                and pending_debug.get("message_key") == message_key
            ):
                deltas = self._coerce_runtime_dict_value(pending_debug.get("deltas", {}))
                turn_analysis = self._coerce_runtime_dict_value(pending_debug.get("turn_analysis", {}))
                active_event = self._coerce_runtime_dict_value(pending_debug.get("active_event", {}))
                state_explanation = self._coerce_runtime_dict_value(pending_debug.get("state_explanation", {}))
            else:
                deltas = {}
                turn_analysis = {}
                active_event = {}
                state_explanation = {}

            #  1. 特殊事件处理
            pending_event = self._get_runtime_dict_cache("_pending_events").pop(session_key, None)
            if (
                isinstance(pending_event, dict)
                and pending_event.get("type") == "locked"
                and pending_event.get("message_key") == message_key
            ):
                locked_prefix = (
                    "*（她忽然安静下来，琥色的眼眸直直望着你，"
                    "过了很久，她轻声说）*\n\n"
                )
                locked_suffix = (
                    "\n\n> *从这一刻起，你已经是她的"
                    "\u201c命定之人\u201d了。*"
                )
                reply = locked_prefix + reply + locked_suffix

            reply = self._self_check_reply(reply, state, event.message_str)
            response.completion_text = reply

            #  2. 调试模式追加数?
            debug_mode = bool(state.get("调试模式", pending_debug.get("debug_mode", self.default_debug_mode) if isinstance(pending_debug, dict) else self.default_debug_mode))
            if debug_mode:
                response.completion_text = reply + self._build_debug_footer(
                    state,
                    deltas,
                    state_explanation=state_explanation,
                )

            #  3. 存入对话历史
            self._spawn_task(self._add_to_history(user_id, "assistant", reply))

            #  4. 更新用户画像（异步，不阻塞响应）
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
        user_id = self._get_command_scoped_user_id(event)
        state = self._get_state(user_id, count_interaction=False)
        await self._reconcile_destined_one_state(user_id, state)
        debug_key = "\u8c03\u8bd5\u6a21\u5f0f"
        enabled = not bool(state.get(debug_key, self.default_debug_mode))
        self._set_debug_mode_for_related_states(event, user_id, enabled)
        status = "\u5f00\u542f" if enabled else "\u5173\u95ed"
        display_action = "\u663e\u793a" if enabled else "\u4e0d\u518d\u663e\u793a"
        yield event.plain_result(
            f"\U0001f50d \u739b\u4e3d\u4e9a\u8c03\u8bd5\u6a21\u5f0f\u5df2{status}\u3002\n"
            f"> *\u4e4b\u540e\u7684\u666e\u901a\u5bf9\u8bdd\u56de\u590d\u5c06{display_action}\u5f53\u524d\u6570\u503c\u4e0e\u672c\u8f6e\u53d8\u5316\u3002*"
        )

    @marianna_group.command("状态")  # type: ignore
    async def cmd_marianna_status(self, event: AstrMessageEvent):
        """查看当前所有数值与状态描述。"""
        user_id = self._get_command_scoped_user_id(event)
        state = self._get_state(user_id, count_interaction=False)
        if await self._reconcile_destined_one_state(user_id, state):
            self._schedule_state_save(user_id, state)
        yield event.plain_result(self._build_state_report(state))

    @marianna_group.command("诊断")  # type: ignore
    async def cmd_marianna_diagnostic(self, event: AstrMessageEvent):
        """查看最近一轮状态变化的决策链路。"""
        user_id = self._get_command_scoped_user_id(event)
        state = self._get_state(user_id, count_interaction=False)
        if await self._reconcile_destined_one_state(user_id, state):
            self._schedule_state_save(user_id, state)
        yield event.plain_result(self._build_diagnostic_report(state))

    @marianna_group.command("诊断历史")  # type: ignore
    async def cmd_marianna_diagnostic_history(self, event: AstrMessageEvent):
        """查看近几次状态诊断摘要。"""
        user_id = self._get_command_scoped_user_id(event)
        state = self._get_state(user_id, count_interaction=False)
        yield event.plain_result(self._build_diagnostic_history_report(state, limit=5))

    @marianna_group.command("\u7248\u672c")  # type: ignore
    async def cmd_marianna_version(self, event: AstrMessageEvent):
        """Show plugin version and release summary."""
        yield event.plain_result(self._build_release_report())

    @marianna_group.command("\u914d\u7f6e\u4f53\u68c0")  # type: ignore
    async def cmd_marianna_config_audit(self, event: AstrMessageEvent):
        """Show token/cache/memory related config audit."""
        yield event.plain_result(self._build_config_audit_report())

    @marianna_group.command("\u6a21\u578b\u68c0\u6d4b")  # type: ignore
    async def cmd_marianna_model_probe(self, event: AstrMessageEvent):
        """Detect chat/analysis/embedding provider ids without spending tokens."""
        yield event.plain_result(await self._build_model_probe_report(event))

    @marianna_group.command("记忆统计")  # type: ignore
    async def cmd_marianna_memory_stats(self, event: AstrMessageEvent):
        """查看当前用户的内置长期记忆统计。"""
        user_id = self._get_command_scoped_user_id(event)
        stats = await self._get_builtin_memory_stats(user_id)
        yield event.plain_result(self._build_memory_stats_report(stats))

    @marianna_group.command("最近记忆")  # type: ignore
    async def cmd_marianna_recent_memories(self, event: AstrMessageEvent):
        """查看当前用户最近写入的内置长期记忆。"""
        user_id = self._get_command_scoped_user_id(event)
        memories = await self._get_recent_builtin_memories(user_id, limit=5)
        yield event.plain_result(self._build_recent_memory_report(memories))



    def _get_command_tail(self, event: AstrMessageEvent, command_name: str) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        parts = text.split()
        if len(parts) >= 3:
            return " ".join(parts[2:]).strip()
        if command_name in text:
            return text.split(command_name, 1)[-1].strip()
        return ""

    @marianna_group.command("\u8bb0\u5fc6\u641c\u7d22")  # type: ignore
    async def cmd_marianna_memory_search(self, event: AstrMessageEvent):
        """Search visible builtin long-term memories for the current user."""
        user_id = self._get_command_scoped_user_id(event)
        query = self._get_command_tail(event, "\u8bb0\u5fc6\u641c\u7d22")
        if not query:
            yield event.plain_result("\u8bf7\u5728\u547d\u4ee4\u540e\u8f93\u5165\u5173\u952e\u8bcd\uff0c\u4f8b\u5982\uff1a/\u739b\u4e3d\u4e9a \u8bb0\u5fc6\u641c\u7d22 \u751f\u65e5")
            return
        memories = await self._search_builtin_memories(user_id, query, limit=8)
        yield event.plain_result(self._build_memory_search_report(memories, query))

    @marianna_group.command("\u8bb0\u5fc6\u5220\u9664")  # type: ignore
    async def cmd_marianna_memory_delete(self, event: AstrMessageEvent):
        """Delete one visible builtin memory by id prefix."""
        user_id = self._get_command_scoped_user_id(event)
        memory_id = self._get_command_tail(event, "\u8bb0\u5fc6\u5220\u9664")
        if not memory_id:
            yield event.plain_result("\u8bf7\u63d0\u4f9b\u8bb0\u5fc6 id \u524d\u7f00\uff0c\u4f8b\u5982\uff1a/\u739b\u4e3d\u4e9a \u8bb0\u5fc6\u5220\u9664 a1b2c3")
            return
        deleted = await self._delete_builtin_memory(user_id, memory_id)
        yield event.plain_result("\u8bb0\u5fc6\u5df2\u5220\u9664\u3002" if deleted else "\u6ca1\u6709\u627e\u5230\u552f\u4e00\u5339\u914d\u7684\u53ef\u89c1\u8bb0\u5fc6\u3002")

    @marianna_group.command("\u8bb0\u5fc6\u4fdd\u62a4")  # type: ignore
    async def cmd_marianna_memory_protect(self, event: AstrMessageEvent):
        """Protect one visible builtin memory from cleanup/decay."""
        user_id = self._get_command_scoped_user_id(event)
        memory_id = self._get_command_tail(event, "\u8bb0\u5fc6\u4fdd\u62a4")
        if not memory_id:
            yield event.plain_result("\u8bf7\u63d0\u4f9b\u8bb0\u5fc6 id \u524d\u7f00\uff0c\u4f8b\u5982\uff1a/\u739b\u4e3d\u4e9a \u8bb0\u5fc6\u4fdd\u62a4 a1b2c3")
            return
        protected = await self._protect_builtin_memory(user_id, memory_id)
        yield event.plain_result("\u8bb0\u5fc6\u5df2\u4fdd\u62a4\uff0c\u540e\u7eed\u66f4\u4e0d\u5bb9\u6613\u88ab\u6e05\u7406\u6216\u964d\u6743\u3002" if protected else "\u6ca1\u6709\u627e\u5230\u552f\u4e00\u5339\u914d\u7684\u53ef\u89c1\u8bb0\u5fc6\u3002")

    @marianna_group.command("\u8bb0\u5fc6\u53ef\u89c1\u6027")  # type: ignore
    async def cmd_marianna_memory_visibility(self, event: AstrMessageEvent):
        """Set memory visibility: private_only/group_only/public_profile/sensitive."""
        user_id = self._get_command_scoped_user_id(event)
        tail = self._get_command_tail(event, "\u8bb0\u5fc6\u53ef\u89c1\u6027")
        parts = tail.split()
        if len(parts) < 2:
            yield event.plain_result("\u8bf7\u63d0\u4f9b\u8bb0\u5fc6 id \u548c\u53ef\u89c1\u6027\uff0c\u4f8b\u5982\uff1a/\u739b\u4e3d\u4e9a \u8bb0\u5fc6\u53ef\u89c1\u6027 a1b2c3 public_profile")
            return
        changed = await self._set_builtin_memory_visibility(user_id, parts[0], parts[1])
        yield event.plain_result("\u8bb0\u5fc6\u53ef\u89c1\u6027\u5df2\u66f4\u65b0\u3002" if changed else "\u6ca1\u6709\u627e\u5230\u552f\u4e00\u5339\u914d\u7684\u53ef\u89c1\u8bb0\u5fc6\uff0c\u6216\u53ef\u89c1\u6027\u503c\u65e0\u6548\u3002")

    @marianna_group.command("\u8bb0\u5fc6\u56de\u586b")  # type: ignore
    async def cmd_marianna_memory_backfill(self, event: AstrMessageEvent):
        """Backfill privacy/evidence/temperature for old builtin memories."""
        updated = await self._backfill_builtin_memory_privacy(limit=500)
        yield event.plain_result(f"\u65e7\u8bb0\u5fc6\u9690\u79c1\u4e0e\u51b7\u70ed\u5c42\u56de\u586b\u5b8c\u6210\uff0c\u672c\u6b21\u66f4\u65b0 {updated} \u6761\u3002")

    @marianna_group.command("\u8bb0\u5fc6\u5bfc\u51fa")  # type: ignore
    async def cmd_marianna_memory_export(self, event: AstrMessageEvent):
        """Export visible builtin memories for the current user."""
        user_id = self._get_command_scoped_user_id(event)
        file_path = await self._export_builtin_memories(user_id, limit=500)
        if not file_path:
            yield event.plain_result("内置本地记忆未启用或初始化失败，未生成导出文件。")
            return
        yield event.plain_result(f"\u8bb0\u5fc6\u5df2\u5bfc\u51fa\uff1a{file_path}")

    @marianna_group.command("\u8bb0\u5fc6\u5bfc\u5165")  # type: ignore
    async def cmd_marianna_memory_import(self, event: AstrMessageEvent):
        """Import builtin memories from data/memory_exports/*.jsonl."""
        user_id = self._get_command_scoped_user_id(event)
        file_name = self._get_command_tail(event, "\u8bb0\u5fc6\u5bfc\u5165")
        if not file_name:
            yield event.plain_result("\u8bf7\u63d0\u4f9b data/memory_exports \u4e0b\u7684 jsonl \u6587\u4ef6\u540d\u3002")
            return
        imported = await self._import_builtin_memories(user_id, file_name, limit=500)
        yield event.plain_result(f"\u8bb0\u5fc6\u5bfc\u5165\u5b8c\u6210\uff0c\u672c\u6b21\u5199\u5165 {imported} \u6761\u3002")

    @marianna_group.command("\u8bb0\u5fc6\u5065\u5eb7")  # type: ignore
    async def cmd_marianna_memory_health(self, event: AstrMessageEvent):
        """Run a local memory health check."""
        user_id = self._get_command_scoped_user_id(event)
        health = await self._check_builtin_memory_health(user_id)
        yield event.plain_result(self._build_memory_health_report(health))

    @marianna_group.command("\u8bb0\u5fc6\u4fee\u590d")  # type: ignore
    async def cmd_marianna_memory_repair(self, event: AstrMessageEvent):
        """Repair local memory metadata and indexes."""
        user_id = self._get_command_scoped_user_id(event)
        result = await self._repair_builtin_memory(user_id)
        yield event.plain_result(self._build_memory_repair_report(result))

    @marianna_group.command("\u8bb0\u5fc6\u6a21\u5f0f")  # type: ignore
    async def cmd_marianna_memory_mode(self, event: AstrMessageEvent):
        """Show or switch memory mode: lean/balanced/rich."""
        mode = self._get_command_tail(event, "\u8bb0\u5fc6\u6a21\u5f0f").strip().lower()
        if mode:
            if mode not in MEMORY_MODE_PRESETS:
                yield event.plain_result("\u8bb0\u5fc6\u6a21\u5f0f\u53ea\u652f\u6301 lean\u3001balanced\u3001rich\u3002")
                return
            self._get_config_source()["memory_mode_preset"] = mode
            self._apply_config()
        yield event.plain_result(self._build_memory_mode_report())

    @marianna_group.command("记忆清理")  # type: ignore
    async def cmd_marianna_memory_cleanup(self, event: AstrMessageEvent):
        """清理当前用户低显著度、未命中的互动印象。"""
        user_id = self._get_command_scoped_user_id(event)
        deleted = await self._cleanup_low_value_builtin_memories(user_id)
        yield event.plain_result(
            f"🧹 玛丽亚整理了内置记忆。\n> *清理低价值互动印?{deleted} 条?"
        )

    @marianna_group.command("重置")  # type: ignore
    async def cmd_marianna_reset(self, event: AstrMessageEvent):
        """重置该用户的所有状态，但保留已学习的用户画像。"""
        user_id = self._get_command_scoped_user_id(event)
        new_state = copy.deepcopy(DEFAULT_STATE)
        new_state["最后互动时间"] = None
        new_state["调试模式"] = self.default_debug_mode
        self._clear_pending_for_user(user_id)
        reset_notice = "> *她你的印象仍在，但此刻的一切情绕值都已重新开始?"
        if self._is_destined_user(user_id):
            await self._clear_destined_one()
            reset_notice = (
                "> *她你的印象仍在，但此刻的一切情绕值都已重新开始?"
                "\n> *全局“命定之人标记也并解除?"
            )
        self._schedule_state_save(user_id, new_state)
        yield event.plain_result(
            "玛丽亚轻轻整理了下摆，仿佛把纷乱的情绪重新收回了心底\n"
            f"{reset_notice}"
        )

    @marianna_group.command("画像")  # type: ignore
    async def cmd_marianna_profile(self, event: AstrMessageEvent):
        """显示玛丽亚对你的印象，也就是她已经学到的用户画像。"""
        user_id = self._get_command_scoped_user_id(event)
        profile = self._get_profile(user_id)
        yield event.plain_result(self._build_profile_report(profile))

    @marianna_group.command("重置学习")  # type: ignore
    async def cmd_marianna_reset_learning(self, event: AstrMessageEvent):
        """清除玛丽亚已学习的用户画像，但不影响当前状态。"""
        user_id = self._get_command_scoped_user_id(event)
        profiles = self._get_user_profiles_store() if hasattr(self, "_get_user_profiles_store") else getattr(self, "user_profiles", {})
        if isinstance(profiles, dict) and user_id in profiles:
            del profiles[user_id]
            self._schedule_profile_file_save(user_id)
        yield event.plain_result(
            "玛丽亚将关于你的学习笔重新锁进了抽屉\n"
            "> *她会重新认识你，但刻的情绪状不会因此抹去?"
        )

    @marianna_group.command("重载配置")  # type: ignore
    async def cmd_marianna_reload_config(self, event: AstrMessageEvent):
        """热重载插件配置，并立即应用到后续行为。"""
        self._apply_config()
        yield event.plain_result(
            "⚙️ 玛丽亚的插件配置已重新载入\n"
            "> *新的参数会从接下来的对话始生效?"
        )

    @marianna_group.command("perf")  # type: ignore
    async def cmd_marianna_perf(self, event: AstrMessageEvent):
        """查看最近一段时间的内部性能统计。"""
        yield event.plain_result(self._build_perf_report())

    @marianna_group.command("\u8fd0\u884c\u89c2\u6d4b")  # type: ignore
    async def cmd_marianna_live_observation(self, event: AstrMessageEvent):
        """Show live cache/context/privacy observation samples."""
        tail = self._get_command_tail(event, "\u8fd0\u884c\u89c2\u6d4b")
        limit = self._coerce_runtime_int(tail, default=8, minimum=1, maximum=30)
        yield event.plain_result(self._build_live_observation_report(limit=limit))
