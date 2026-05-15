import asyncio
import copy
import hashlib
import json
import math
import os
import re
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import ProviderRequest, LLMResponse

from .compat import AIOFILES_AVAILABLE, aiofiles
from .constants import *

class MariannaRuntimeMixin:
    def _coerce_runtime_int(
        self,
        value: Any,
        default: int = 0,
        *,
        minimum: Optional[int] = None,
        maximum: Optional[int] = None,
    ) -> int:
        try:
            coerced = int(value if value is not None else default)
        except (TypeError, ValueError, OverflowError):
            coerced = int(default or 0)
        if minimum is not None:
            coerced = max(minimum, coerced)
        if maximum is not None:
            coerced = min(maximum, coerced)
        return coerced

    def _coerce_runtime_float(
        self,
        value: Any,
        default: float = 0.0,
        *,
        minimum: Optional[float] = None,
        maximum: Optional[float] = None,
    ) -> float:
        try:
            coerced = float(value if value is not None else default)
        except (TypeError, ValueError):
            coerced = float(default or 0.0)
        if coerced != coerced or coerced in (float("inf"), float("-inf")):
            coerced = float(default or 0.0)
        if minimum is not None:
            coerced = max(minimum, coerced)
        if maximum is not None:
            coerced = min(maximum, coerced)
        return coerced

    async def _save_all_data(self) -> bool:
        """保存所有数据，返回是否全部成功。"""
        user_states = (
            self._build_persistable_user_states()
            if hasattr(self, "_build_persistable_user_states")
            else (
                self._get_user_states_store()
                if hasattr(self, "_get_user_states_store")
                else getattr(self, "user_states", {})
            )
        )
        user_profiles = (
            self._build_persistable_user_profiles()
            if hasattr(self, "_build_persistable_user_profiles")
            else getattr(self, "user_profiles", {})
        )
        global_state = (
            self._build_persistable_global_state()
            if hasattr(self, "_build_persistable_global_state")
            else self._get_global_state()
            if hasattr(self, "_get_global_state")
            else getattr(self, "global_state", {})
        )
        user_states_file = (
            self._get_store_file_path("user_states_file", "user_states.json")
            if hasattr(self, "_get_store_file_path")
            else self.user_states_file
        )
        user_profiles_file = (
            self._get_store_file_path("user_profiles_file", "user_profiles.json")
            if hasattr(self, "_get_store_file_path")
            else self.user_profiles_file
        )
        global_state_file = (
            self._get_store_file_path("global_state_file", "global_state.json")
            if hasattr(self, "_get_store_file_path")
            else self.global_state_file
        )
        results = await asyncio.gather(
            self._save_json_async(user_states_file, user_states),
            self._save_json_async(user_profiles_file, user_profiles),
            self._save_json_async(global_state_file, global_state),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            for error in errors:
                self.logger.error(f"保存数据失败: {error}", exc_info=(type(error), error, error.__traceback__))
            return False
        self.logger.info("所有数据已保存")
        return True

    def _spawn_task(self, coro: Any) -> asyncio.Task:
        """统一追踪派发出去的异步任务，避免重载时遗漏。"""
        task = asyncio.create_task(self._run_pending_task(coro))
        self._get_runtime_task_set("_pending_tasks").add(task)

        def _on_done(done_task: asyncio.Task):
            self._get_runtime_task_set("_pending_tasks").discard(done_task)
            try:
                exc = done_task.exception()
            except asyncio.CancelledError:
                return
            if exc:
                self.logger.error(
                    f"后台异步任务失败: {exc}",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(_on_done)
        return task

    async def _run_pending_task(self, coro: Any):
        started = False
        try:
            async with self._get_pending_task_semaphore():
                started = True
                return await coro
        finally:
            if not started and hasattr(coro, "close"):
                coro.close()

    def _get_runtime_task_set(self, attr_name: str) -> Set[asyncio.Task]:
        tasks = getattr(self, attr_name, None)
        if not isinstance(tasks, set):
            tasks = set()
            setattr(self, attr_name, tasks)
        else:
            valid_tasks = {task for task in tasks if isinstance(task, asyncio.Task)}
            if len(valid_tasks) != len(tasks):
                tasks = valid_tasks
                setattr(self, attr_name, tasks)
        return tasks

    def _get_runtime_task_list(self, attr_name: str) -> List[asyncio.Task]:
        tasks = getattr(self, attr_name, None)
        if not isinstance(tasks, list):
            tasks = []
            setattr(self, attr_name, tasks)
        else:
            valid_tasks = [task for task in tasks if isinstance(task, asyncio.Task)]
            if len(valid_tasks) != len(tasks):
                tasks = valid_tasks
                setattr(self, attr_name, tasks)
        return tasks

    def _spawn_background_task(self, coro: Any, label: str = "background") -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._get_runtime_task_list("_background_tasks").append(task)

        def _on_done(done_task: asyncio.Task):
            background_tasks = self._get_runtime_task_list("_background_tasks")
            if done_task in background_tasks:
                background_tasks.remove(done_task)
            try:
                exc = done_task.exception()
            except asyncio.CancelledError:
                return
            if exc:
                self.logger.error(
                    f"background task failed [{label}]: {exc}",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(_on_done)
        return task

    def _get_pending_task_semaphore(self) -> asyncio.Semaphore:
        semaphore = getattr(self, "_pending_task_semaphore", None)
        if not isinstance(semaphore, asyncio.Semaphore):
            limit = self._coerce_runtime_int(
                getattr(self, "background_task_concurrency", BACKGROUND_TASK_CONCURRENCY),
                default=BACKGROUND_TASK_CONCURRENCY,
                minimum=1,
                maximum=100,
            )
            semaphore = asyncio.Semaphore(limit)
            self._pending_task_semaphore = semaphore
        return semaphore

    # ======================== 辅助函数 ========================

    async def _get_lock(self, file_path: Path) -> asyncio.Lock:
        """获取文件锁（用于并发控制）"""
        path_str = str(file_path)
        file_locks = getattr(self, "_file_locks", None)
        if not isinstance(file_locks, dict):
            file_locks = {}
            self._file_locks = file_locks
        if path_str not in file_locks:
            file_locks[path_str] = asyncio.Lock()
        return file_locks[path_str]

    def _get_user_lock(self, user_id: str) -> asyncio.Lock:
        """获取同一用户请求锁，降低连续消息导致的状态交错。"""
        key = str(user_id or "unknown")
        user_locks = getattr(self, "_user_locks", None)
        if not isinstance(user_locks, dict):
            user_locks = {}
            self._user_locks = user_locks
        if key not in user_locks:
            user_locks[key] = asyncio.Lock()
        return user_locks[key]

    def _safe_user_file_stem(self, user_id: Any) -> str:
        raw = str(user_id or "unknown")
        reserved_windows_names = {
            "CON", "PRN", "AUX", "NUL",
            "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
            "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
        }
        base_name = raw.split(".", 1)[0].upper()
        if (
            raw
            and raw not in {".", ".."}
            and not raw.endswith(".")
            and base_name not in reserved_windows_names
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", raw)
        ):
            return raw
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._-")[:48]
        return f"{safe}_{digest}" if safe else digest

    def _load_json(self, path: Path, default: Any) -> Any:
        """同步加载 JSON 文件"""
        try:
            if path.exists():
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if default is not None and not isinstance(data, type(default)):
                    self.logger.warning(f"JSON {path} 的数据类型不符，已使用默认值")
                    return default
                return data
        except Exception as e:
            self.logger.error(f"加载 {path} 失败: {e}", exc_info=True)
        return default

    async def _write_text_atomic(self, path: Path, content: str):
        """以临时文件 + 替换方式写入，避免读到半截文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            if AIOFILES_AVAILABLE:
                async with aiofiles.open(temp_path, 'w', encoding='utf-8') as f:
                    await f.write(content)
            else:
                temp_path.write_text(content, encoding='utf-8')
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    async def _save_json_async(self, path: Path, data: Any):
        """异步保存 JSON 文件（带文件锁）"""
        path = Path(path)
        lock = await self._get_lock(path)
        async with lock:
            try:
                payload_data = (
                    self._make_json_safe(data)
                    if hasattr(self, "_make_json_safe")
                    else data
                )
                payload = json.dumps(
                    payload_data,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                )
                await self._write_text_atomic(path, payload)
                self.logger.debug(f"已保存文件: {path}")
            except Exception as e:
                self.logger.error(f"保存 {path} 失败: {e}", exc_info=True)
                raise

    def _get_config_source(self) -> Dict[str, Any]:
        config = getattr(self, "config", None)
        if not isinstance(config, dict):
            config = {}
            self.config = config
        return config

    def _get_config_int(
        self,
        key: str,
        default: int,
        *,
        minimum: Optional[int] = None,
        maximum: Optional[int] = None,
    ) -> int:
        """读取整数配置，并在配置异常时回退默认值。"""
        try:
            value = int(self._get_config_source().get(key, default))
        except (TypeError, ValueError, OverflowError):
            value = default

        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _get_config_float(
        self,
        key: str,
        default: float,
        *,
        minimum: Optional[float] = None,
        maximum: Optional[float] = None,
    ) -> float:
        """读取浮点配置，并在配置异常时回退默认值。"""
        try:
            value = float(self._get_config_source().get(key, default))
        except (TypeError, ValueError):
            value = default
        if not math.isfinite(value):
            value = default

        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _get_config_bool(self, key: str, default: bool) -> bool:
        """读取布尔配置，兼容配置面板传入的字符串值。"""
        value = self._get_config_source().get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
                return True
            if normalized in {"0", "false", "no", "n", "off", "disable", "disabled"}:
                return False
        return bool(default)

    def _apply_config(self):
        """应用配置到默认值和运行时参数"""
        try:
            DEFAULT_STATE["好感度"] = self._get_config_int(
                "marianna_initial_favor", 0, minimum=0, maximum=100
            )
            DEFAULT_STATE["病娇值"] = self._get_config_int(
                "marianna_initial_yan", 0, minimum=0, maximum=100
            )
            DEFAULT_STATE["信任度"] = self._get_config_int(
                "marianna_initial_trust", 15, minimum=0, maximum=100
            )
            DEFAULT_STATE["焦虑值"] = self._get_config_int(
                "marianna_initial_anxiety", 5, minimum=0, maximum=100
            )
            DEFAULT_STATE["占有欲"] = 0
            DEFAULT_STATE["优雅值"] = self._get_config_int(
                "marianna_initial_elegance", 85, minimum=0, maximum=100
            )
            self.favor_multiplier = self._get_config_float(
                "marianna_favor_multiplier", 1.0, minimum=0.5, maximum=2.0
            )
            self.yan_multiplier = self._get_config_float(
                "marianna_yan_multiplier", 1.0, minimum=0.5, maximum=2.0
            )
            self.lock_threshold = self._get_config_int(
                "marianna_lock_threshold", 100, minimum=50, maximum=100
            )
            self.auto_summary_interval = self._get_config_int(
                "auto_summary_interval", 20, minimum=5, maximum=100
            )
            self.auto_summary_idle = self._get_config_int(
                "auto_summary_idle_time", 300, minimum=60, maximum=3600
            )
            self.enable_profile = self._get_config_bool("enable_user_profile", True)
            self.enable_emotional_memory = self._get_config_bool("enable_emotional_memory", True)
            self.enable_builtin_memory = self._get_config_bool("enable_builtin_memory", ENABLE_BUILTIN_MEMORY)
            self.enable_token_cost_optimization = self._get_config_bool("enable_token_cost_optimization", ENABLE_TOKEN_COST_OPTIMIZATION)
            self.avoid_duplicate_context_injection = self._get_config_bool("avoid_duplicate_context_injection", AVOID_DUPLICATE_CONTEXT_INJECTION)
            self.enable_adaptive_lightweight_prompt = self._get_config_bool("enable_adaptive_lightweight_prompt", ENABLE_ADAPTIVE_LIGHTWEIGHT_PROMPT)
            self.adaptive_lightweight_prompt_max_chars = self._get_config_int(
                "adaptive_lightweight_prompt_max_chars",
                ADAPTIVE_LIGHTWEIGHT_PROMPT_MAX_CHARS,
                minimum=4,
                maximum=200,
            )
            self.enable_prompt_budget_guard = self._get_config_bool("enable_prompt_budget_guard", ENABLE_PROMPT_BUDGET_GUARD)
            self.prompt_token_budget = self._get_config_int(
                "prompt_token_budget",
                PROMPT_TOKEN_BUDGET,
                minimum=300,
                maximum=50000,
            )
            self.enable_prompt_budget_memory_anchor = self._get_config_bool("enable_prompt_budget_memory_anchor", ENABLE_PROMPT_BUDGET_MEMORY_ANCHOR)
            self.prompt_budget_memory_anchor_chars = self._get_config_int(
                "prompt_budget_memory_anchor_chars",
                PROMPT_BUDGET_MEMORY_ANCHOR_CHARS,
                minimum=0,
                maximum=1000,
            )
            self.prompt_budget_history_limit = self._get_config_int(
                "prompt_budget_history_limit",
                PROMPT_BUDGET_HISTORY_LIMIT,
                minimum=1,
                maximum=100,
            )
            self.enable_prompt_cost_profile_stats = self._get_config_bool("enable_prompt_cost_profile_stats", ENABLE_PROMPT_COST_PROFILE_STATS)
            self.prompt_cost_profile_window = self._get_config_int(
                "prompt_cost_profile_window",
                PROMPT_COST_PROFILE_WINDOW,
                minimum=1,
                maximum=100,
            )
            self.enable_prompt_cost_auto_memory_mode = self._get_config_bool("enable_prompt_cost_auto_memory_mode", ENABLE_PROMPT_COST_AUTO_MEMORY_MODE)
            self.prompt_cost_auto_lean_hit_rate = self._get_config_int(
                "prompt_cost_auto_lean_hit_rate",
                PROMPT_COST_AUTO_LEAN_HIT_RATE,
                minimum=1,
                maximum=100,
            )
            self.prompt_cost_auto_balanced_hit_rate = self._get_config_int(
                "prompt_cost_auto_balanced_hit_rate",
                PROMPT_COST_AUTO_BALANCED_HIT_RATE,
                minimum=0,
                maximum=100,
            )
            self.prompt_cost_auto_mode_sticky_turns = self._get_config_int(
                "prompt_cost_auto_mode_sticky_turns",
                PROMPT_COST_AUTO_MODE_STICKY_TURNS,
                minimum=0,
                maximum=10,
            )
            self.enable_prompt_budget_auto_throttle = self._get_config_bool("enable_prompt_budget_auto_throttle", ENABLE_PROMPT_BUDGET_AUTO_THROTTLE)
            self.prompt_budget_auto_throttle_min_streak = self._get_config_int(
                "prompt_budget_auto_throttle_min_streak",
                PROMPT_BUDGET_AUTO_THROTTLE_MIN_STREAK,
                minimum=1,
                maximum=10,
            )
            self.prompt_budget_auto_throttle_recovery_turns = self._get_config_int(
                "prompt_budget_auto_throttle_recovery_turns",
                PROMPT_BUDGET_AUTO_THROTTLE_RECOVERY_TURNS,
                minimum=1,
                maximum=10,
            )
            self.enable_prompt_budget_compression_tiers = self._get_config_bool("enable_prompt_budget_compression_tiers", ENABLE_PROMPT_BUDGET_COMPRESSION_TIERS)
            self.prompt_budget_heavy_compact_streak = self._get_config_int(
                "prompt_budget_heavy_compact_streak",
                PROMPT_BUDGET_HEAVY_COMPACT_STREAK,
                minimum=1,
                maximum=20,
            )
            self.prompt_budget_throttle_log_limit = self._get_config_int(
                "prompt_budget_throttle_log_limit",
                PROMPT_BUDGET_THROTTLE_LOG_LIMIT,
                minimum=1,
                maximum=50,
            )
            self.prompt_budget_throttle_escalation_hits = self._get_config_int(
                "prompt_budget_throttle_escalation_hits",
                PROMPT_BUDGET_THROTTLE_ESCALATION_HITS,
                minimum=1,
                maximum=10,
            )
            self.prompt_budget_throttle_escalation_recovery_clear = self._get_config_int(
                "prompt_budget_throttle_escalation_recovery_clear",
                PROMPT_BUDGET_THROTTLE_ESCALATION_RECOVERY_CLEAR,
                minimum=1,
                maximum=10,
            )
            self.enable_prompt_budget_memory_mode_adaptation = self._get_config_bool("enable_prompt_budget_memory_mode_adaptation", ENABLE_PROMPT_BUDGET_MEMORY_MODE_ADAPTATION)
            self.prompt_budget_memory_mode_pressure_hit_rate = self._get_config_int(
                "prompt_budget_memory_mode_pressure_hit_rate",
                PROMPT_BUDGET_MEMORY_MODE_PRESSURE_HIT_RATE,
                minimum=1,
                maximum=100,
            )
            self.prompt_budget_memory_mode_lean_limit = self._get_config_int(
                "prompt_budget_memory_mode_lean_limit",
                PROMPT_BUDGET_MEMORY_MODE_LEAN_LIMIT,
                minimum=0,
                maximum=20,
            )
            self.prompt_budget_memory_mode_balanced_limit = self._get_config_int(
                "prompt_budget_memory_mode_balanced_limit",
                PROMPT_BUDGET_MEMORY_MODE_BALANCED_LIMIT,
                minimum=0,
                maximum=20,
            )
            self.prompt_budget_memory_mode_rich_limit = self._get_config_int(
                "prompt_budget_memory_mode_rich_limit",
                PROMPT_BUDGET_MEMORY_MODE_RICH_LIMIT,
                minimum=0,
                maximum=20,
            )
            self.prompt_budget_memory_mode_lean_chars = self._get_config_int(
                "prompt_budget_memory_mode_lean_chars",
                PROMPT_BUDGET_MEMORY_MODE_LEAN_CHARS,
                minimum=0,
                maximum=5000,
            )
            self.prompt_budget_memory_mode_balanced_chars = self._get_config_int(
                "prompt_budget_memory_mode_balanced_chars",
                PROMPT_BUDGET_MEMORY_MODE_BALANCED_CHARS,
                minimum=0,
                maximum=5000,
            )
            self.prompt_budget_memory_mode_rich_chars = self._get_config_int(
                "prompt_budget_memory_mode_rich_chars",
                PROMPT_BUDGET_MEMORY_MODE_RICH_CHARS,
                minimum=0,
                maximum=5000,
            )
            self.enable_prompt_budget_memory_value_priority = self._get_config_bool("enable_prompt_budget_memory_value_priority", ENABLE_PROMPT_BUDGET_MEMORY_VALUE_PRIORITY)
            self.prompt_budget_memory_priority_char_trigger = self._get_config_int(
                "prompt_budget_memory_priority_char_trigger",
                PROMPT_BUDGET_MEMORY_PRIORITY_CHAR_TRIGGER,
                minimum=0,
                maximum=5000,
            )
            self.enable_prompt_budget_memory_candidate_expansion = self._get_config_bool("enable_prompt_budget_memory_candidate_expansion", ENABLE_PROMPT_BUDGET_MEMORY_CANDIDATE_EXPANSION)
            self.prompt_budget_memory_candidate_multiplier = self._get_config_int(
                "prompt_budget_memory_candidate_multiplier",
                PROMPT_BUDGET_MEMORY_CANDIDATE_MULTIPLIER,
                minimum=1,
                maximum=10,
            )
            self.prompt_budget_memory_candidate_max = self._get_config_int(
                "prompt_budget_memory_candidate_max",
                PROMPT_BUDGET_MEMORY_CANDIDATE_MAX,
                minimum=1,
                maximum=100,
            )
            self.enable_prompt_memory_slot_dedup = self._get_config_bool("enable_prompt_memory_slot_dedup", ENABLE_PROMPT_MEMORY_SLOT_DEDUP)
            self.enable_prompt_memory_selection_trace = self._get_config_bool("enable_prompt_memory_selection_trace", ENABLE_PROMPT_MEMORY_SELECTION_TRACE)
            self.prompt_memory_selection_trace_limit = self._get_config_int(
                "prompt_memory_selection_trace_limit",
                PROMPT_MEMORY_SELECTION_TRACE_LIMIT,
                minimum=0,
                maximum=10,
            )
            self.enable_selective_interaction_memory = self._get_config_bool("enable_selective_interaction_memory", True)
            self.memory_prompt_limit = self._get_config_int(
                "memory_prompt_limit",
                MEMORY_PROMPT_LIMIT,
                minimum=0,
                maximum=20,
            )
            self.memory_prompt_event_limit = self._get_config_int(
                "memory_prompt_event_limit",
                MEMORY_PROMPT_EVENT_LIMIT,
                minimum=0,
                maximum=10,
            )
            self.memory_prompt_impression_limit = self._get_config_int(
                "memory_prompt_impression_limit",
                MEMORY_PROMPT_IMPRESSION_LIMIT,
                minimum=0,
                maximum=10,
            )
            self.memory_prompt_summary_limit = self._get_config_int(
                "memory_prompt_summary_limit",
                MEMORY_PROMPT_SUMMARY_LIMIT,
                minimum=0,
                maximum=10,
            )
            self.memory_prompt_profile_limit = self._get_config_int(
                "memory_prompt_profile_limit",
                MEMORY_PROMPT_PROFILE_LIMIT,
                minimum=0,
                maximum=10,
            )
            self.interaction_memory_min_delta = self._get_config_int(
                "interaction_memory_min_delta",
                INTERACTION_MEMORY_MIN_DELTA,
                minimum=1,
                maximum=10,
            )
            self.enable_memory_write_candidates = self._get_config_bool("enable_memory_write_candidates", ENABLE_MEMORY_WRITE_CANDIDATES)
            self.memory_write_candidate_promote_hits = self._get_config_int(
                "memory_write_candidate_promote_hits",
                MEMORY_WRITE_CANDIDATE_PROMOTE_HITS,
                minimum=2,
                maximum=10,
            )
            self.memory_write_candidate_limit = self._get_config_int(
                "memory_write_candidate_limit",
                MEMORY_WRITE_CANDIDATE_LIMIT,
                minimum=1,
                maximum=50,
            )
            self.enable_memory_update_layer = self._get_config_bool("enable_memory_update_layer", True)
            self.enable_memory_forgetting_layer = self._get_config_bool("enable_memory_forgetting_layer", True)
            self.memory_decay_days = self._get_config_int(
                "memory_decay_days",
                MEMORY_DECAY_DAYS,
                minimum=7,
                maximum=365,
            )
            self.memory_hard_cleanup_days = self._get_config_int(
                "memory_hard_cleanup_days",
                MEMORY_HARD_CLEANUP_DAYS,
                minimum=30,
                maximum=3650,
            )
            self.builtin_memory_retention_limit = self._get_config_int(
                "builtin_memory_retention_limit",
                BUILTIN_MEMORY_RETENTION_LIMIT,
                minimum=100,
                maximum=10000,
            )
            self.builtin_memory_prompt_char_budget = self._get_config_int(
                "builtin_memory_prompt_char_budget",
                BUILTIN_MEMORY_PROMPT_CHAR_BUDGET,
                minimum=80,
                maximum=2000,
            )
            self.builtin_memory_summary_max_chars = self._get_config_int(
                "builtin_memory_summary_max_chars",
                BUILTIN_MEMORY_SUMMARY_MAX_CHARS,
                minimum=40,
                maximum=500,
            )
            self.builtin_memory_import_max_content_chars = self._get_config_int(
                "builtin_memory_import_max_content_chars",
                BUILTIN_MEMORY_IMPORT_MAX_CONTENT_CHARS,
                minimum=120,
                maximum=20000,
            )
            self.builtin_memory_import_max_line_chars = self._get_config_int(
                "builtin_memory_import_max_line_chars",
                BUILTIN_MEMORY_IMPORT_MAX_LINE_CHARS,
                minimum=1000,
                maximum=1_000_000,
            )
            self.enable_builtin_memory_vector = self._get_config_bool("enable_builtin_memory_vector", ENABLE_BUILTIN_MEMORY_VECTOR)
            self.embedding_provider_id = str(
                self._get_config_source().get("marianna_embedding_provider_id", "") or ""
            ).strip()
            self.builtin_memory_vector_min_similarity = self._get_config_float(
                "builtin_memory_vector_min_similarity",
                BUILTIN_MEMORY_VECTOR_MIN_SIMILARITY,
                minimum=0.0,
                maximum=1.0,
            )
            self.builtin_memory_vector_weight = self._get_config_int(
                "builtin_memory_vector_weight",
                BUILTIN_MEMORY_VECTOR_WEIGHT,
                minimum=0,
                maximum=10,
            )
            self.builtin_memory_vector_candidate_limit = self._get_config_int(
                "builtin_memory_vector_candidate_limit",
                BUILTIN_MEMORY_VECTOR_CANDIDATE_LIMIT,
                minimum=16,
                maximum=512,
            )
            self.builtin_memory_vector_max_dimensions = self._get_config_int(
                "builtin_memory_vector_max_dimensions",
                BUILTIN_MEMORY_VECTOR_MAX_DIMENSIONS,
                minimum=1,
                maximum=32768,
            )
            self.temperature = self._get_config_float(
                "marianna_temperature", 0.85, minimum=0.0, maximum=2.0
            )
            self.analysis_provider_id = str(
                self._get_config_source().get("marianna_analysis_provider_id", "") or ""
            ).strip()
            self.default_debug_mode = self._get_config_bool("marianna_debug_mode", False)
            DEFAULT_STATE["调试模式"] = self.default_debug_mode
            self.state_scope_mode = str(
                self._get_config_source().get("state_scope_mode", STATE_SCOPE_MODE) or STATE_SCOPE_MODE
            ).strip()
            if self.state_scope_mode not in STATE_SCOPE_MODES:
                self.state_scope_mode = STATE_SCOPE_MODE
            # LLM 上下文注入配置
            self.context_injection_enabled = self._get_config_bool("enable_context_injection", False)
            self.max_context_messages = self._get_config_int(
                "context_history_limit", 6, minimum=0, maximum=1000
            )
            self.inject_history = bool(self.context_injection_enabled)
            self.inject_summary_in_context = bool(
                self.context_injection_enabled
                and self._get_config_bool("inject_summary_as_context", False)
            )
            self.inject_state_details = self._get_config_bool("inject_state_details", True)
            self.enable_value_dialogue_modulation = self._get_config_bool("enable_value_dialogue_modulation", True)
            self.enable_emotion_recognition_layer = self._get_config_bool("enable_emotion_recognition_layer", True)
            self.enable_active_event_layer = self._get_config_bool("enable_active_event_layer", True)
            self.enable_code_state_decision = self._get_config_bool("enable_code_state_decision", ENABLE_CODE_STATE_DECISION)
            self.enable_memory_evidence_stage_gate = self._get_config_bool("enable_memory_evidence_stage_gate", ENABLE_MEMORY_EVIDENCE_STAGE_GATE)
            self.enable_persona_consistency_guard = self._get_config_bool("enable_persona_consistency_guard", ENABLE_PERSONA_CONSISTENCY_GUARD)
            self.enable_reply_length_strategy = self._get_config_bool("enable_reply_length_strategy", ENABLE_REPLY_LENGTH_STRATEGY)
            self.enable_prompt_template_mode = self._get_config_bool("enable_prompt_template_mode", ENABLE_PROMPT_TEMPLATE_MODE)
            self.enable_behavior_style_layer = self._get_config_bool("enable_behavior_style_layer", ENABLE_BEHAVIOR_STYLE_LAYER)
            self.short_term_emotion_decay = self._get_config_float(
                "short_term_emotion_decay",
                SHORT_TERM_EMOTION_DECAY,
                minimum=0.0,
                maximum=0.95,
            )
            self.enable_behavior_band_smoothing = self._get_config_bool("enable_behavior_band_smoothing", ENABLE_BEHAVIOR_BAND_SMOOTHING)
            self.enable_behavior_continuity_bridge = self._get_config_bool("enable_behavior_continuity_bridge", ENABLE_BEHAVIOR_CONTINUITY_BRIDGE)
            self.enable_behavior_action_budget = self._get_config_bool("enable_behavior_action_budget", ENABLE_BEHAVIOR_ACTION_BUDGET)
            self.enable_reply_variety_guard = self._get_config_bool("enable_reply_variety_guard", ENABLE_REPLY_VARIETY_GUARD)
            self.enable_recent_style_fingerprint = self._get_config_bool("enable_recent_style_fingerprint", ENABLE_RECENT_STYLE_FINGERPRINT)
            self.recent_style_fingerprint_limit = self._get_config_int(
                "recent_style_fingerprint_limit",
                RECENT_STYLE_FINGERPRINT_LIMIT,
                minimum=0,
                maximum=12,
            )
            self.enable_reply_template_trim = self._get_config_bool("enable_reply_template_trim", ENABLE_REPLY_TEMPLATE_TRIM)
            self.reply_template_trim_max_actions = self._get_config_int(
                "reply_template_trim_max_actions",
                REPLY_TEMPLATE_TRIM_MAX_ACTIONS,
                minimum=0,
                maximum=4,
            )
            self.enable_time_aware_short_term_decay = self._get_config_bool("enable_time_aware_short_term_decay", ENABLE_TIME_AWARE_SHORT_TERM_DECAY)
            self.short_term_decay_half_life_hours = self._get_config_float(
                "short_term_decay_half_life_hours",
                SHORT_TERM_DECAY_HALF_LIFE_HOURS,
                minimum=0.25,
                maximum=168.0,
            )
            self.enable_relationship_event_log = self._get_config_bool("enable_relationship_event_log", ENABLE_RELATIONSHIP_EVENT_LOG)
            self.relationship_event_log_limit = self._get_config_int(
                "relationship_event_log_limit",
                RELATIONSHIP_EVENT_LOG_LIMIT,
                minimum=1,
                maximum=100,
            )
            self.enable_behavior_style_variant = self._get_config_bool("enable_behavior_style_variant", ENABLE_BEHAVIOR_STYLE_VARIANT)
            self.enable_memory_recall_negative_feedback = self._get_config_bool("enable_memory_recall_negative_feedback", ENABLE_MEMORY_RECALL_NEGATIVE_FEEDBACK)
            self.enable_memory_privacy_layer = self._get_config_bool("enable_memory_privacy_layer", ENABLE_MEMORY_PRIVACY_LAYER)
            self.enable_memory_scene_bridge = self._get_config_bool("enable_memory_scene_bridge", ENABLE_MEMORY_SCENE_BRIDGE)
            self.enable_memory_evidence_trace = self._get_config_bool("enable_memory_evidence_trace", ENABLE_MEMORY_EVIDENCE_TRACE)
            self.enable_memory_conflict_resolution = self._get_config_bool("enable_memory_conflict_resolution", ENABLE_MEMORY_CONFLICT_RESOLUTION)
            self.enable_memory_temperature_layer = self._get_config_bool("enable_memory_temperature_layer", ENABLE_MEMORY_TEMPERATURE_LAYER)
            self.memory_recall_negative_feedback_max = self._get_config_int(
                "memory_recall_negative_feedback_max",
                MEMORY_RECALL_NEGATIVE_FEEDBACK_MAX,
                minimum=0,
                maximum=20,
            )
            self.memory_hot_days = self._get_config_int(
                "memory_hot_days",
                MEMORY_HOT_DAYS,
                minimum=1,
                maximum=30,
            )
            self.memory_warm_days = self._get_config_int(
                "memory_warm_days",
                MEMORY_WARM_DAYS,
                minimum=7,
                maximum=365,
            )
            self.memory_recall_cooldown_seconds = self._get_config_int(
                "memory_recall_cooldown_seconds",
                MEMORY_RECALL_COOLDOWN_SECONDS,
                minimum=0,
                maximum=86400,
            )
            self.memory_mode_preset = str(
                self._get_config_source().get("memory_mode_preset", MEMORY_MODE_PRESET) or MEMORY_MODE_PRESET
            ).strip().lower()
            if self.memory_mode_preset not in MEMORY_MODE_PRESETS:
                self.memory_mode_preset = MEMORY_MODE_PRESET
            self.enable_scene_memory_mode = self._get_config_bool("enable_scene_memory_mode", ENABLE_SCENE_MEMORY_MODE)
            self.private_chat_memory_mode_preset = self._normalize_memory_mode(
                self._get_config_source().get("private_chat_memory_mode_preset", PRIVATE_CHAT_MEMORY_MODE_PRESET),
                PRIVATE_CHAT_MEMORY_MODE_PRESET,
            )
            self.group_chat_memory_mode_preset = self._normalize_memory_mode(
                self._get_config_source().get("group_chat_memory_mode_preset", GROUP_CHAT_MEMORY_MODE_PRESET),
                GROUP_CHAT_MEMORY_MODE_PRESET,
            )
            self.private_chat_context_injection = self._get_config_bool("private_chat_context_injection", PRIVATE_CHAT_CONTEXT_INJECTION)
            self.group_chat_context_injection = self._get_config_bool("group_chat_context_injection", GROUP_CHAT_CONTEXT_INJECTION)
            self.private_chat_inject_summary_as_context = self._get_config_bool("private_chat_inject_summary_as_context", PRIVATE_CHAT_INJECT_SUMMARY_AS_CONTEXT)
            self.group_chat_inject_summary_as_context = self._get_config_bool("group_chat_inject_summary_as_context", GROUP_CHAT_INJECT_SUMMARY_AS_CONTEXT)
            self.behavior_band_sticky_turns = self._get_config_int(
                "behavior_band_sticky_turns",
                BEHAVIOR_BAND_STICKY_TURNS,
                minimum=0,
                maximum=10,
            )
            self.enable_active_event_queue = self._get_config_bool("enable_active_event_queue", ENABLE_ACTIVE_EVENT_QUEUE)
            self.enable_state_explanation_log = self._get_config_bool("enable_state_explanation_log", ENABLE_STATE_EXPLANATION_LOG)
            self.enable_memory_evidence_grading = self._get_config_bool("enable_memory_evidence_grading", ENABLE_MEMORY_EVIDENCE_GRADING)
            self.enable_reply_self_check = self._get_config_bool("enable_reply_self_check", ENABLE_REPLY_SELF_CHECK)
            self.enable_state_delta_smoothing = self._get_config_bool("enable_state_delta_smoothing", ENABLE_STATE_DELTA_SMOOTHING)
            self.state_smooth_repeat_decay_start = self._get_config_int(
                "state_smooth_repeat_decay_start",
                STATE_SMOOTH_REPEAT_DECAY_START,
                minimum=2,
                maximum=20,
            )
            self.state_smooth_high_value_start = self._get_config_int(
                "state_smooth_high_value_start",
                STATE_SMOOTH_HIGH_VALUE_START,
                minimum=60,
                maximum=99,
            )
            self.state_smooth_near_max_start = self._get_config_int(
                "state_smooth_near_max_start",
                STATE_SMOOTH_NEAR_MAX_START,
                minimum=70,
                maximum=100,
            )
            self.state_smooth_high_anxiety_start = self._get_config_int(
                "state_smooth_high_anxiety_start",
                STATE_SMOOTH_HIGH_ANXIETY_START,
                minimum=40,
                maximum=100,
            )
            self.memory_quality_min_salience = self._get_config_int(
                "memory_quality_min_salience",
                MEMORY_QUALITY_MIN_SALIENCE,
                minimum=0,
                maximum=10,
            )
            self.memory_quality_min_text_chars = self._get_config_int(
                "memory_quality_min_text_chars",
                MEMORY_QUALITY_MIN_TEXT_CHARS,
                minimum=1,
                maximum=80,
            )
            self.enable_memory_quality_filter = self._get_config_bool("enable_memory_quality_filter", ENABLE_MEMORY_QUALITY_FILTER)
            self.diagnostic_history_limit = self._get_config_int(
                "diagnostic_history_limit",
                DIAGNOSTIC_HISTORY_LIMIT,
                minimum=1,
                maximum=100,
            )
            self.enable_relationship_cooldown = self._get_config_bool("enable_relationship_cooldown", ENABLE_RELATIONSHIP_COOLDOWN)
            self.relationship_cooldown_idle_days = self._get_config_int(
                "relationship_cooldown_idle_days",
                RELATIONSHIP_COOLDOWN_IDLE_DAYS,
                minimum=1,
                maximum=365,
            )
            self.relationship_cooldown_max_delta = self._get_config_int(
                "relationship_cooldown_max_delta",
                RELATIONSHIP_COOLDOWN_MAX_DELTA,
                minimum=0,
                maximum=30,
            )
            self.memory_cleanup_max_delete = self._get_config_int(
                "memory_cleanup_max_delete",
                MEMORY_CLEANUP_MAX_DELETE,
                minimum=1,
                maximum=5000,
            )
            self.active_event_queue_max_size = self._get_config_int(
                "active_event_queue_max_size",
                ACTIVE_EVENT_QUEUE_MAX_SIZE,
                minimum=0,
                maximum=10,
            )
            self.reply_self_check_max_chars = self._get_config_int(
                "reply_self_check_max_chars",
                REPLY_SELF_CHECK_MAX_CHARS,
                minimum=120,
                maximum=3000,
            )
            self.reply_self_check_max_lines = self._get_config_int(
                "reply_self_check_max_lines",
                REPLY_SELF_CHECK_MAX_LINES,
                minimum=3,
                maximum=50,
            )
            self.active_event_cooldown_turns = self._get_config_int(
                "active_event_cooldown_turns",
                ACTIVE_EVENT_COOLDOWN_TURNS,
                minimum=1,
                maximum=50,
            )
            self.active_event_idle_hours = self._get_config_int(
                "active_event_idle_hours",
                ACTIVE_EVENT_IDLE_HOURS,
                minimum=1,
                maximum=168,
            )
            self._apply_memory_mode_preset()
            self.enable_reflection_update_layer = self._get_config_bool("enable_reflection_update_layer", True)
            self.max_tokens_per_message = self._get_config_int(
                "context_max_tokens_per_msg", 220, minimum=50, maximum=20000
            )
            self.analysis_history_limit = self._get_config_int(
                "analysis_history_limit",
                ANALYSIS_HISTORY_LIMIT,
                minimum=0,
                maximum=1000,
            )
            self.analysis_relevant_memory_limit = self._get_config_int(
                "analysis_relevant_memory_limit",
                ANALYSIS_RELEVANT_MEMORY_LIMIT,
                minimum=0,
                maximum=200,
            )
            self.analysis_recent_context_limit = self._get_config_int(
                "analysis_recent_context_limit",
                ANALYSIS_RECENT_CONTEXT_LIMIT,
                minimum=0,
                maximum=50,
            )
            self.analysis_mnemosyne_memory_limit = self._get_config_int(
                "analysis_mnemosyne_memory_limit",
                ANALYSIS_MNEMOSYNE_MEMORY_LIMIT,
                minimum=0,
                maximum=50,
            )
            self.analysis_max_chars_per_message = self._get_config_int(
                "analysis_max_chars_per_msg",
                ANALYSIS_MAX_CHARS_PER_MSG,
                minimum=100,
                maximum=20000,
            )
            self.analysis_context_char_budget = self._get_config_int(
                "analysis_context_char_budget",
                ANALYSIS_CONTEXT_CHAR_BUDGET,
                minimum=1_000,
                maximum=1_000_000,
            )
            self.history_retention_limit = self._get_config_int(
                "conversation_history_retention_limit",
                CONVERSATION_HISTORY_RETENTION_LIMIT,
                minimum=200,
                maximum=5000,
            )
            self.history_max_entry_chars = self._get_config_int(
                "history_max_entry_chars",
                HISTORY_MAX_ENTRY_CHARS,
                minimum=200,
                maximum=20000,
            )
            self.history_duplicate_window_seconds = self._get_config_int(
                "history_duplicate_window_seconds",
                HISTORY_DUPLICATE_WINDOW_SECONDS,
                minimum=0,
                maximum=300,
            )
            self.enable_performance_logging = self._get_config_bool("enable_performance_logging", True)
            if self.enable_token_cost_optimization:
                self.memory_prompt_limit = min(
                    self.memory_prompt_limit,
                    TOKEN_OPT_MEMORY_PROMPT_LIMIT,
                )
                self.max_context_messages = min(
                    self.max_context_messages,
                    TOKEN_OPT_CONTEXT_HISTORY_LIMIT,
                )
                self.max_tokens_per_message = min(
                    self.max_tokens_per_message,
                    TOKEN_OPT_CONTEXT_MAX_CHARS_PER_MSG,
                )
                self.analysis_history_limit = min(
                    self.analysis_history_limit,
                    TOKEN_OPT_ANALYSIS_HISTORY_LIMIT,
                )
                self.analysis_relevant_memory_limit = min(
                    self.analysis_relevant_memory_limit,
                    TOKEN_OPT_ANALYSIS_RELEVANT_MEMORY_LIMIT,
                )
                self.analysis_recent_context_limit = min(
                    self.analysis_recent_context_limit,
                    TOKEN_OPT_ANALYSIS_RECENT_CONTEXT_LIMIT,
                )
                self.analysis_mnemosyne_memory_limit = min(
                    self.analysis_mnemosyne_memory_limit,
                    TOKEN_OPT_ANALYSIS_MNEMOSYNE_MEMORY_LIMIT,
                )
                self.analysis_max_chars_per_message = min(
                    self.analysis_max_chars_per_message,
                    TOKEN_OPT_ANALYSIS_MAX_CHARS_PER_MSG,
                )
                self.analysis_context_char_budget = min(
                    self.analysis_context_char_budget,
                    TOKEN_OPT_ANALYSIS_CONTEXT_CHAR_BUDGET,
                )
            for attr_name in (
                "_static_prompt_cache",
                "_dynamic_prompt_cache",
                "_mnemosyne_query_cache",
                "_local_memory_query_cache",
                "_recent_history_cache",
                "_style_fingerprint_cache",
            ):
                self._clear_runtime_cache_attr(attr_name)
            self.logger.debug("配置应用成功")
        except Exception as e:
            self.logger.error(f"应用配置失败: {e}", exc_info=True)

    def _normalize_memory_mode(self, value: Any, fallback: str = MEMORY_MODE_PRESET) -> str:
        mode = str(value or fallback or MEMORY_MODE_PRESET).strip().lower()
        return mode if mode in MEMORY_MODE_PRESETS else fallback

    def _build_memory_mode_profile(self, mode: str) -> Dict[str, int]:
        normalized = self._normalize_memory_mode(mode)
        if normalized == "lean":
            return {
                "memory_limit": 2,
                "event_limit": 1,
                "impression_limit": 1,
                "summary_limit": 1,
                "profile_limit": 1,
                "char_budget": 180,
                "recall_cooldown_seconds": 600,
                "active_event_cooldown_turns": 10,
                "active_event_idle_hours": 36,
            }
        if normalized == "rich":
            return {
                "memory_limit": 5,
                "event_limit": 2,
                "impression_limit": 2,
                "summary_limit": 1,
                "profile_limit": 1,
                "char_budget": 520,
                "recall_cooldown_seconds": 240,
                "active_event_cooldown_turns": 5,
                "active_event_idle_hours": 12,
            }
        return {
            "memory_limit": 3,
            "event_limit": 2,
            "impression_limit": 1,
            "summary_limit": 1,
            "profile_limit": 1,
            "char_budget": 260,
            "recall_cooldown_seconds": 300,
            "active_event_cooldown_turns": 7,
            "active_event_idle_hours": 24,
        }

    def _apply_explicit_memory_policy_overrides(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        explicit_config = self._get_config_source()
        overrides = {
            "memory_prompt_limit": ("memory_limit", "memory_prompt_limit"),
            "memory_prompt_event_limit": ("event_limit", "memory_prompt_event_limit"),
            "memory_prompt_impression_limit": ("impression_limit", "memory_prompt_impression_limit"),
            "memory_prompt_summary_limit": ("summary_limit", "memory_prompt_summary_limit"),
            "memory_prompt_profile_limit": ("profile_limit", "memory_prompt_profile_limit"),
            "builtin_memory_prompt_char_budget": ("char_budget", "builtin_memory_prompt_char_budget"),
            "memory_recall_cooldown_seconds": ("recall_cooldown_seconds", "memory_recall_cooldown_seconds"),
            "active_event_cooldown_turns": ("active_event_cooldown_turns", "active_event_cooldown_turns"),
            "active_event_idle_hours": ("active_event_idle_hours", "active_event_idle_hours"),
        }
        result = dict(profile)
        for config_key, (policy_key, attr_name) in overrides.items():
            if config_key not in explicit_config:
                continue
            result[policy_key] = self._coerce_runtime_int(
                getattr(self, attr_name, result.get(policy_key, 0)),
                default=result.get(policy_key, 0),
                minimum=0,
            )
        return result

    def _build_scene_memory_policy(self, event: Optional[AstrMessageEvent] = None) -> Dict[str, Any]:
        is_group = self._is_group_event(event)
        if getattr(self, "enable_scene_memory_mode", ENABLE_SCENE_MEMORY_MODE):
            mode = (
                getattr(self, "group_chat_memory_mode_preset", GROUP_CHAT_MEMORY_MODE_PRESET)
                if is_group
                else getattr(self, "private_chat_memory_mode_preset", PRIVATE_CHAT_MEMORY_MODE_PRESET)
            )
            context_enabled = (
                getattr(self, "group_chat_context_injection", GROUP_CHAT_CONTEXT_INJECTION)
                if is_group
                else getattr(self, "private_chat_context_injection", PRIVATE_CHAT_CONTEXT_INJECTION)
            )
            summary_enabled = (
                getattr(self, "group_chat_inject_summary_as_context", GROUP_CHAT_INJECT_SUMMARY_AS_CONTEXT)
                if is_group
                else getattr(self, "private_chat_inject_summary_as_context", PRIVATE_CHAT_INJECT_SUMMARY_AS_CONTEXT)
            )
        else:
            mode = getattr(self, "memory_mode_preset", MEMORY_MODE_PRESET)
            context_enabled = getattr(self, "context_injection_enabled", False)
            summary_enabled = getattr(self, "inject_summary_in_context", False)

        normalized_mode = self._normalize_memory_mode(mode)
        if not getattr(self, "enable_scene_memory_mode", ENABLE_SCENE_MEMORY_MODE):
            return {
                "enabled": False,
                "scene": "group" if is_group else "private",
                "mode": normalized_mode,
                "context_injection_enabled": bool(context_enabled),
                "inject_history": bool(context_enabled),
                "inject_summary_in_context": bool(context_enabled and summary_enabled),
                "memory_limit": self._coerce_runtime_int(getattr(self, "memory_prompt_limit", MEMORY_PROMPT_LIMIT), default=MEMORY_PROMPT_LIMIT, minimum=0),
                "event_limit": self._coerce_runtime_int(getattr(self, "memory_prompt_event_limit", MEMORY_PROMPT_EVENT_LIMIT), default=MEMORY_PROMPT_EVENT_LIMIT, minimum=0),
                "impression_limit": self._coerce_runtime_int(getattr(self, "memory_prompt_impression_limit", MEMORY_PROMPT_IMPRESSION_LIMIT), default=MEMORY_PROMPT_IMPRESSION_LIMIT, minimum=0),
                "summary_limit": self._coerce_runtime_int(getattr(self, "memory_prompt_summary_limit", MEMORY_PROMPT_SUMMARY_LIMIT), default=MEMORY_PROMPT_SUMMARY_LIMIT, minimum=0),
                "profile_limit": self._coerce_runtime_int(getattr(self, "memory_prompt_profile_limit", MEMORY_PROMPT_PROFILE_LIMIT), default=MEMORY_PROMPT_PROFILE_LIMIT, minimum=0),
                "char_budget": self._coerce_runtime_int(getattr(self, "builtin_memory_prompt_char_budget", BUILTIN_MEMORY_PROMPT_CHAR_BUDGET), default=BUILTIN_MEMORY_PROMPT_CHAR_BUDGET, minimum=0),
                "recall_cooldown_seconds": self._coerce_runtime_int(getattr(self, "memory_recall_cooldown_seconds", MEMORY_RECALL_COOLDOWN_SECONDS), default=MEMORY_RECALL_COOLDOWN_SECONDS, minimum=0),
                "active_event_cooldown_turns": self._coerce_runtime_int(getattr(self, "active_event_cooldown_turns", ACTIVE_EVENT_COOLDOWN_TURNS), default=ACTIVE_EVENT_COOLDOWN_TURNS, minimum=0),
                "active_event_idle_hours": self._coerce_runtime_int(getattr(self, "active_event_idle_hours", ACTIVE_EVENT_IDLE_HOURS), default=ACTIVE_EVENT_IDLE_HOURS, minimum=0),
            }
        profile = self._apply_explicit_memory_policy_overrides(
            self._build_memory_mode_profile(normalized_mode)
        )
        return {
            "enabled": bool(getattr(self, "enable_scene_memory_mode", ENABLE_SCENE_MEMORY_MODE)),
            "scene": "group" if is_group else "private",
            "mode": normalized_mode,
            "context_injection_enabled": bool(context_enabled),
            "inject_history": bool(context_enabled),
            "inject_summary_in_context": bool(context_enabled and summary_enabled),
            **profile,
        }

    def _apply_memory_mode_preset(self):
        mode = str(getattr(self, "memory_mode_preset", MEMORY_MODE_PRESET) or MEMORY_MODE_PRESET).lower()
        explicit_config = self._get_config_source()

        def preset_can_apply(key: str) -> bool:
            return key not in explicit_config

        def current_int(attr: str, default: int) -> int:
            return self._coerce_runtime_int(getattr(self, attr, default), default=default, minimum=0)

        if mode == "lean":
            if preset_can_apply("memory_prompt_limit"):
                self.memory_prompt_limit = min(current_int("memory_prompt_limit", MEMORY_PROMPT_LIMIT), 2)
            if preset_can_apply("memory_prompt_event_limit"):
                self.memory_prompt_event_limit = min(current_int("memory_prompt_event_limit", MEMORY_PROMPT_EVENT_LIMIT), 1)
            if preset_can_apply("memory_prompt_impression_limit"):
                self.memory_prompt_impression_limit = min(current_int("memory_prompt_impression_limit", MEMORY_PROMPT_IMPRESSION_LIMIT), 1)
            if preset_can_apply("memory_prompt_summary_limit"):
                self.memory_prompt_summary_limit = min(current_int("memory_prompt_summary_limit", MEMORY_PROMPT_SUMMARY_LIMIT), 1)
            if preset_can_apply("memory_prompt_profile_limit"):
                self.memory_prompt_profile_limit = min(current_int("memory_prompt_profile_limit", MEMORY_PROMPT_PROFILE_LIMIT), 1)
            if preset_can_apply("builtin_memory_prompt_char_budget"):
                self.builtin_memory_prompt_char_budget = min(
                    current_int("builtin_memory_prompt_char_budget", BUILTIN_MEMORY_PROMPT_CHAR_BUDGET),
                    180,
                )
            if preset_can_apply("memory_recall_cooldown_seconds"):
                self.memory_recall_cooldown_seconds = max(
                    current_int("memory_recall_cooldown_seconds", MEMORY_RECALL_COOLDOWN_SECONDS),
                    600,
                )
            if preset_can_apply("active_event_cooldown_turns"):
                self.active_event_cooldown_turns = max(
                    current_int("active_event_cooldown_turns", ACTIVE_EVENT_COOLDOWN_TURNS),
                    10,
                )
            if preset_can_apply("active_event_idle_hours"):
                self.active_event_idle_hours = max(
                    current_int("active_event_idle_hours", ACTIVE_EVENT_IDLE_HOURS),
                    36,
                )
        elif mode == "rich":
            if preset_can_apply("memory_prompt_limit"):
                self.memory_prompt_limit = max(current_int("memory_prompt_limit", MEMORY_PROMPT_LIMIT), 5)
            if preset_can_apply("memory_prompt_event_limit"):
                self.memory_prompt_event_limit = max(current_int("memory_prompt_event_limit", MEMORY_PROMPT_EVENT_LIMIT), 2)
            if preset_can_apply("memory_prompt_impression_limit"):
                self.memory_prompt_impression_limit = max(current_int("memory_prompt_impression_limit", MEMORY_PROMPT_IMPRESSION_LIMIT), 2)
            if preset_can_apply("memory_prompt_summary_limit"):
                self.memory_prompt_summary_limit = max(current_int("memory_prompt_summary_limit", MEMORY_PROMPT_SUMMARY_LIMIT), 1)
            if preset_can_apply("memory_prompt_profile_limit"):
                self.memory_prompt_profile_limit = max(current_int("memory_prompt_profile_limit", MEMORY_PROMPT_PROFILE_LIMIT), 1)
            if preset_can_apply("builtin_memory_prompt_char_budget"):
                self.builtin_memory_prompt_char_budget = max(
                    current_int("builtin_memory_prompt_char_budget", BUILTIN_MEMORY_PROMPT_CHAR_BUDGET),
                    360,
                )
            if preset_can_apply("memory_recall_cooldown_seconds"):
                self.memory_recall_cooldown_seconds = min(
                    current_int("memory_recall_cooldown_seconds", MEMORY_RECALL_COOLDOWN_SECONDS),
                    240,
                )

    def _get_default_chat_provider_id(self) -> Optional[str]:
        """获取默认聊天模型 provider ID。"""
        try:
            provider = self.context.get_using_provider()
            if not provider:
                return None
            meta = provider.meta() if hasattr(provider, "meta") else None
            return getattr(meta, "id", None) or getattr(provider, "id", None)
        except Exception as e:
            self.logger.warning(f"获取默认聊天模型 provider ID 失败: {e}")
            return None

    async def _get_current_chat_provider_id(
        self, event: Optional[AstrMessageEvent] = None
    ) -> Optional[str]:
        """按 AstrBot v4.23.2 推荐方式获取当前会话使用的聊天模型 ID。"""
        if event is not None:
            umo = getattr(event, "unified_msg_origin", None)
            if umo:
                try:
                    return await self.context.get_current_chat_provider_id(umo=umo)
                except Exception as e:
                    self.logger.warning(
                        f"获取当前会话聊天模型 ID 失败，将回退默认 provider: {e}"
                    )
        return self._get_default_chat_provider_id()

    async def _get_analysis_provider_id(
        self, event: Optional[AstrMessageEvent] = None
    ) -> Optional[str]:
        """获取分析型 LLM 的 provider ID，优先使用插件配置。"""
        if self.analysis_provider_id:
            return self.analysis_provider_id
        return await self._get_current_chat_provider_id(event)

    async def _call_analysis_llm(
        self,
        *,
        purpose: str,
        prompt: str,
        system_prompt: str,
        event: Optional[AstrMessageEvent] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Optional[LLMResponse]:
        """通过 AstrBot v4.23.2 推荐的 llm_generate 接口调用分析型 LLM。"""
        provider_id = await self._get_analysis_provider_id(event)
        if not provider_id:
            self.logger.warning(f"{purpose}失败：未找到可用的分析型 LLM provider")
            return None

        kwargs: Dict[str, Any] = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        call_started_at = time.perf_counter()
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
                **kwargs,
            )
            self._log_perf(
                f"{purpose}.llm_generate",
                call_started_at,
                extra=f"provider={provider_id}",
                threshold_ms=10.0,
            )
            self.logger.debug(f"{purpose}使用 provider={provider_id}")
            return resp
        except Exception as e:
            self._log_perf(
                f"{purpose}.llm_generate_failed",
                call_started_at,
                extra=f"provider={provider_id}",
                threshold_ms=10.0,
            )
            fallback_provider_id = await self._get_current_chat_provider_id(event)
            if fallback_provider_id and fallback_provider_id != provider_id:
                self.logger.warning(
                    f"{purpose}使用分析型 provider={provider_id} 失败，将回退当前会话模型 "
                    f"provider={fallback_provider_id}: {e}"
                )
                fallback_started_at = time.perf_counter()
                try:
                    resp = await self.context.llm_generate(
                        chat_provider_id=fallback_provider_id,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        **kwargs,
                    )
                    self._log_perf(
                        f"{purpose}.llm_generate_fallback",
                        fallback_started_at,
                        extra=f"provider={fallback_provider_id}",
                        threshold_ms=10.0,
                    )
                    return resp
                except Exception as inner_e:
                    self._log_perf(
                        f"{purpose}.llm_generate_fallback_failed",
                        fallback_started_at,
                        extra=f"provider={fallback_provider_id}",
                        threshold_ms=10.0,
                    )
                    self.logger.error(
                        f"{purpose}回退到当前会话模型后仍失败: {inner_e}",
                        exc_info=True,
                    )
                    return None

            self.logger.error(f"{purpose}失败: {e}", exc_info=True)
            return None

    def _get_event_unique_id(self, event: Optional[AstrMessageEvent]) -> str:
        if event is None:
            return ""
        for getter_name in ("get_message_id", "get_msg_id", "get_event_id"):
            getter = getattr(event, getter_name, None)
            if callable(getter):
                try:
                    value = getter()
                except Exception:
                    value = None
                if value:
                    return str(value)
        for source in (
            event,
            getattr(event, "message_obj", None),
            getattr(event, "message", None),
            getattr(event, "raw_message", None),
        ):
            if source is None:
                continue
            for attr in ("message_id", "msg_id", "event_id", "id", "seq"):
                value = getattr(source, attr, None)
                if callable(value):
                    try:
                        value = value()
                    except Exception:
                        value = None
                if value:
                    return str(value)
        return ""

    def _get_session_alias_key(
        self,
        event: Optional[AstrMessageEvent] = None,
        user_id: Optional[str] = None,
    ) -> str:
        current_user_id = user_id or (event.get_sender_id() if event else "unknown")
        umo = getattr(event, "unified_msg_origin", None) if event is not None else None
        base_key = f"{umo}::{current_user_id}" if umo else str(current_user_id)
        message_text = getattr(event, "message_str", "") if event is not None else ""
        if not message_text:
            return base_key

        normalized = self._normalize_analysis_content(message_text)
        message_hash = hashlib.sha1(normalized.encode("utf-8")).hexdigest()
        return f"{base_key}::{message_hash[:MESSAGE_CACHE_KEY_HASH_CHARS]}"

    def _get_event_group_id(self, event: Optional[AstrMessageEvent]) -> str:
        if event is None:
            return ""
        for getter_name in ("get_group_id", "get_group", "get_room_id", "get_channel_id"):
            getter = getattr(event, getter_name, None)
            if callable(getter):
                try:
                    value = getter()
                except Exception:
                    value = None
                if value:
                    return str(value)
        for source in (
            event,
            getattr(event, "message_obj", None),
            getattr(event, "message", None),
            getattr(event, "raw_message", None),
        ):
            if source is None:
                continue
            for attr in ("group_id", "group", "room_id", "channel_id", "guild_id"):
                value = getattr(source, attr, None)
                if callable(value):
                    try:
                        value = value()
                    except Exception:
                        value = None
                if value:
                    return str(value)
        return ""

    def _is_group_event(self, event: Optional[AstrMessageEvent]) -> bool:
        if event is None:
            return False
        if self._get_event_group_id(event):
            return True
        for getter_name in ("is_group", "is_group_message"):
            getter = getattr(event, getter_name, None)
            if callable(getter):
                try:
                    if getter():
                        return True
                except Exception:
                    pass
        umo = str(getattr(event, "unified_msg_origin", "") or "").lower()
        return any(marker in umo for marker in ("group", "guild", "channel", "群"))

    def _get_event_scene_key(self, event: Optional[AstrMessageEvent]) -> str:
        if event is None:
            return "private"
        group_id = self._get_event_group_id(event)
        if group_id:
            return f"group:{group_id}"
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if self._is_group_event(event) and umo:
            scene_hash = hashlib.sha1(umo.encode("utf-8")).hexdigest()[:MESSAGE_CACHE_KEY_HASH_CHARS]
            return f"group:{scene_hash}"
        if umo:
            scene_hash = hashlib.sha1(umo.encode("utf-8")).hexdigest()[:MESSAGE_CACHE_KEY_HASH_CHARS]
            return f"scene:{scene_hash}"
        return "private"

    def _get_scoped_user_id(
        self,
        event: Optional[AstrMessageEvent] = None,
        user_id: Optional[str] = None,
    ) -> str:
        raw_user_id = str(user_id or (event.get_sender_id() if event else "unknown") or "unknown")
        mode = str(getattr(self, "state_scope_mode", STATE_SCOPE_MODE) or STATE_SCOPE_MODE)
        if mode == "user_global":
            return raw_user_id
        scene_key = self._get_event_scene_key(event)
        if mode == "scene_user":
            return f"{scene_key}::{raw_user_id}"
        if mode == "private_global_group_scene" and self._is_group_event(event):
            return f"{scene_key}::{raw_user_id}"
        return raw_user_id

    def _get_session_key(
        self,
        event: Optional[AstrMessageEvent] = None,
        user_id: Optional[str] = None,
        *,
        create: bool = False,
    ) -> str:
        """为当前消息构造键，用于暂存响应前后的临时状态。"""
        alias_key = self._get_session_alias_key(event, user_id)
        event_uid = self._get_event_unique_id(event)
        if event_uid:
            return f"{alias_key}::event:{event_uid}"
        session_alias_queues = self._get_runtime_dict_cache("_session_alias_queues")
        if not create:
            queue = session_alias_queues.get(alias_key)
            if isinstance(queue, list) and queue:
                session_key = queue.pop(0)
                if not queue:
                    session_alias_queues.pop(alias_key, None)
                return session_key
            if queue is not None:
                session_alias_queues.pop(alias_key, None)
            return alias_key

        self._session_counter = self._coerce_runtime_int(
            getattr(self, "_session_counter", 0),
            default=0,
            minimum=0,
        ) + 1
        session_key = f"{alias_key}::seq:{self._session_counter}"
        queue = session_alias_queues.setdefault(alias_key, [])
        if not isinstance(queue, list):
            queue = []
            session_alias_queues[alias_key] = queue
        queue.append(session_key)
        if len(queue) > 64:
            del queue[:-64]
        self._get_runtime_dict_cache("_session_alias_created_at")[session_key] = time.monotonic()
        return session_key

    def _pending_key_belongs_to_user(self, key: str, user_id: str) -> bool:
        key_text = str(key)
        return (
            key_text == user_id
            or key_text.startswith(f"{user_id}::")
            or key_text.endswith(f"::{user_id}")
            or f"::{user_id}::" in key_text
        )

    def _purge_stale_pending_records(self):
        """清理过期的请求/响应临时缓存，避免长期运行时积累。"""
        cutoff = time.monotonic() - PENDING_CACHE_TTL_SECONDS
        pending_events = self._get_runtime_dict_cache("_pending_events")
        pending_debug_deltas = self._get_runtime_dict_cache("_pending_debug_deltas")
        analysis_request_cache = self._get_runtime_dict_cache("_analysis_request_cache")
        for cache in (pending_events, pending_debug_deltas, analysis_request_cache):
            for key, value in list(cache.items()):
                if not isinstance(value, dict):
                    del cache[key]
                    continue

                created_at = value.get("_created_at")
                if created_at is None:
                    continue

                try:
                    timestamp = float(created_at)
                    if not math.isfinite(timestamp) or timestamp < cutoff:
                        del cache[key]
                except (TypeError, ValueError):
                    del cache[key]

        active_keys = set(pending_events)
        active_keys.update(pending_debug_deltas)
        active_keys.update(analysis_request_cache)
        session_alias_created_at = self._get_runtime_dict_cache("_session_alias_created_at")
        for key, created_at in list(session_alias_created_at.items()):
            try:
                timestamp = float(created_at)
                expired = not math.isfinite(timestamp) or timestamp < cutoff
            except (TypeError, ValueError):
                expired = True
            if expired and key not in active_keys:
                session_alias_created_at.pop(key, None)
        session_alias_queues = self._get_runtime_dict_cache("_session_alias_queues")
        for alias_key, queue in list(session_alias_queues.items()):
            if not isinstance(queue, list):
                session_alias_queues.pop(alias_key, None)
                continue
            filtered = [
                key for key in queue
                if key in active_keys or key in session_alias_created_at
            ]
            if filtered:
                session_alias_queues[alias_key] = filtered
            else:
                session_alias_queues.pop(alias_key, None)

    def _get_runtime_dict_cache(self, attr_name: str) -> Dict[Any, Any]:
        cache = getattr(self, attr_name, None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, attr_name, cache)
        return cache

    def _get_runtime_deque_cache(self, attr_name: str, maxlen: int = 50) -> deque:
        cache = getattr(self, attr_name, None)
        if not isinstance(cache, deque):
            cache = deque(maxlen=max(1, self._coerce_runtime_int(maxlen, default=50, minimum=1)))
            setattr(self, attr_name, cache)
        return cache

    def _coerce_runtime_dict_value(self, value: Any) -> Dict[Any, Any]:
        if isinstance(value, dict):
            return dict(value)
        return {}

    def _coerce_runtime_list_value(self, value: Any) -> List[Any]:
        if isinstance(value, (list, deque)):
            return list(value)
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, set):
            return list(value)
        return []

    def _clear_runtime_cache_attr(self, attr_name: str):
        cache = getattr(self, attr_name, None)
        if isinstance(cache, (dict, list, set)):
            cache.clear()
        else:
            setattr(self, attr_name, {})

    def _log_perf(
        self,
        label: str,
        started_at: float,
        user_id: Optional[str] = None,
        *,
        extra: str = "",
        threshold_ms: float = 0.0,
    ):
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        self._record_perf_sample(label, elapsed_ms)
        if not getattr(self, "enable_performance_logging", True):
            return
        if elapsed_ms < threshold_ms:
            return
        user_part = f" user={user_id}" if user_id else ""
        extra_part = f" {extra}" if extra else ""
        self.logger.debug(f"[perf] {label}{user_part} {elapsed_ms:.1f}ms{extra_part}")

    def _record_perf_sample(self, label: str, elapsed_ms: float):
        perf_stats = self._get_runtime_dict_cache("_perf_stats")
        stats = perf_stats.setdefault(
            label,
            {
                "samples": deque(maxlen=PERF_STATS_MAX_SAMPLES),
                "count": 0,
                "max": 0.0,
                "last": 0.0,
            },
        )
        if not isinstance(stats, dict):
            stats = {
                "samples": deque(maxlen=PERF_STATS_MAX_SAMPLES),
                "count": 0,
                "max": 0.0,
                "last": 0.0,
            }
            perf_stats[label] = stats
        samples = stats.get("samples")
        if not isinstance(samples, deque):
            samples = deque(maxlen=PERF_STATS_MAX_SAMPLES)
            stats["samples"] = samples
        elapsed = self._coerce_runtime_float(elapsed_ms, default=0.0, minimum=0.0)
        samples.append(elapsed)
        stats["count"] = self._coerce_runtime_int(stats.get("count", 0), default=0, minimum=0) + 1
        stats["last"] = elapsed
        stats["max"] = max(
            self._coerce_runtime_float(stats.get("max", 0.0), default=0.0, minimum=0.0),
            elapsed,
        )

    def _build_release_report(self) -> str:
        audit = self._collect_config_audit_items()
        issue_count = sum(1 for item in audit if item.get("level") in {"warn", "risk"})
        status = "OK" if issue_count == 0 else f"{issue_count} item(s) need attention"
        return "\n".join([
            f"Marianna {PLUGIN_VERSION} - {PLUGIN_RELEASE_NAME}",
            "Release focus:",
            "- Default-off local context injection to avoid duplicating AstrBot history.",
            "- Cost-aware memory recall, slot dedup, and prompt budget throttling.",
            "- Private/group boundary separation with protected memory layers.",
            "- Local config audit command for token/cache/memory risks.",
            f"Config audit: {status}",
            "Commands: /玛丽亚 配置体检, /玛丽亚 记忆健康, /玛丽亚 perf",
        ])

    def _collect_config_audit_items(self) -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []

        def add(level: str, title: str, detail: str):
            items.append({"level": level, "title": title, "detail": detail})

        if getattr(self, "enable_token_cost_optimization", True):
            add("ok", "token_cost_optimization", "enabled")
        else:
            add("risk", "token_cost_optimization", "disabled; prompt/history/memory limits will be looser")

        if getattr(self, "enable_scene_memory_mode", ENABLE_SCENE_MEMORY_MODE):
            private_policy = self._build_scene_memory_policy(None)
            group_probe = type("_GroupProbe", (), {"group_id": "diagnostic"})()
            group_policy = self._build_scene_memory_policy(group_probe)
            add(
                "ok",
                "scene_memory_mode",
                f"private={private_policy.get('mode')}/context={'on' if private_policy.get('context_injection_enabled') else 'off'}, "
                f"group={group_policy.get('mode')}/context={'on' if group_policy.get('context_injection_enabled') else 'off'}",
            )
            if private_policy.get("context_injection_enabled"):
                add(
                    "info",
                    "private_context_injection",
                    "enabled for rich private immersion; existing AstrBot contexts are still protected",
                )
            if group_policy.get("context_injection_enabled"):
                add(
                    "warn",
                    "group_context_injection",
                    "enabled; group chats are high-frequency and may reduce cache stability",
                )
            else:
                add("ok", "group_context_injection", "disabled for token-saving group chats")
        else:
            add("warn", "scene_memory_mode", "disabled; private/group chats share memory_mode_preset")

        if getattr(self, "context_injection_enabled", False):
            if getattr(self, "avoid_duplicate_context_injection", True):
                add("warn", "context_injection", "enabled; AstrBot contexts are protected from duplicate plugin history")
            else:
                add("risk", "context_injection", "enabled without duplicate guard; may double inject history")
        else:
            add("ok", "context_injection", "disabled by default for better provider cache hits")

        if getattr(self, "inject_summary_in_context", False):
            add("warn", "summary_context", "enabled; useful for immersion but costs extra input tokens")
        else:
            add("ok", "summary_context", "not injected into req.contexts")

        memory_limit = self._coerce_runtime_int(
            getattr(self, "memory_prompt_limit", MEMORY_PROMPT_LIMIT),
            default=MEMORY_PROMPT_LIMIT,
            minimum=0,
        )
        memory_chars = self._coerce_runtime_int(
            getattr(self, "builtin_memory_prompt_char_budget", BUILTIN_MEMORY_PROMPT_CHAR_BUDGET),
            default=BUILTIN_MEMORY_PROMPT_CHAR_BUDGET,
            minimum=0,
        )
        if memory_limit > 3 or memory_chars > 360:
            add("warn", "memory_prompt_budget", f"limit={memory_limit}, chars={memory_chars}; consider lean/balanced for cache-sensitive models")
        else:
            add("ok", "memory_prompt_budget", f"limit={memory_limit}, chars={memory_chars}")

        context_messages = self._coerce_runtime_int(getattr(self, "max_context_messages", 0), default=0, minimum=0)
        if getattr(self, "context_injection_enabled", False) and context_messages > 8:
            add("warn", "context_history_limit", f"{context_messages} messages; high values reduce cache hit stability")
        else:
            add("ok", "context_history_limit", str(context_messages))

        budget = self._coerce_runtime_int(
            getattr(self, "prompt_token_budget", PROMPT_TOKEN_BUDGET),
            default=PROMPT_TOKEN_BUDGET,
            minimum=0,
        )
        if budget > 6000:
            add("warn", "prompt_token_budget", f"{budget}; high budget allows large dynamic prompts")
        else:
            add("ok", "prompt_token_budget", str(budget))

        if getattr(self, "enable_builtin_memory", ENABLE_BUILTIN_MEMORY):
            vector_state = "on" if getattr(self, "enable_builtin_memory_vector", False) else "off"
            if vector_state == "on" and not str(getattr(self, "embedding_provider_id", "") or "").strip():
                add("warn", "memory_vector", "enabled but embedding provider id is empty; vector recall will fall back")
            else:
                add("ok", "builtin_memory", f"enabled, vector={vector_state}")
        else:
            add("warn", "builtin_memory", "disabled; relationship evidence and recall will be thinner")

        scope = str(getattr(self, "state_scope_mode", STATE_SCOPE_MODE) or STATE_SCOPE_MODE)
        if scope == "user_global":
            add("warn", "state_scope_mode", "user_global shares private/group state; private_global_group_scene is safer")
        else:
            add("ok", "state_scope_mode", scope)

        if getattr(self, "enable_memory_privacy_layer", True):
            add("ok", "group_privacy", "memory visibility guard enabled")
        else:
            add("risk", "group_privacy", "memory privacy layer disabled")

        return items

    def _build_config_audit_report(self) -> str:
        items = self._collect_config_audit_items()
        risk_count = sum(1 for item in items if item.get("level") == "risk")
        warn_count = sum(1 for item in items if item.get("level") == "warn")
        lines = [
            f"Marianna config audit ({PLUGIN_VERSION})",
            f"Summary: {risk_count} risk, {warn_count} warning",
        ]
        for item in items:
            level = str(item.get("level", "ok")).upper()
            lines.append(f"- [{level}] {item.get('title')}: {item.get('detail')}")
        if risk_count or warn_count:
            lines.append("Recommended baseline: enable token optimization, keep context injection off, use balanced/lean memory mode.")
        else:
            lines.append("Recommended baseline matched.")
        return "\n".join(lines)

    def _build_perf_report(self) -> str:
        perf_stats = self._get_runtime_dict_cache("_perf_stats")
        if not perf_stats:
            return "暂无性能统计。"

        rows = []
        for label, stats in perf_stats.items():
            if not isinstance(stats, dict):
                continue
            samples = self._coerce_runtime_list_value(stats.get("samples", []))
            if not samples:
                continue
            samples = [
                self._coerce_runtime_float(sample, default=0.0, minimum=0.0)
                for sample in samples
            ]
            avg_ms = sum(samples) / len(samples)
            rows.append((
                avg_ms,
                label,
                self._coerce_runtime_int(stats.get("count", 0), default=0, minimum=0),
                len(samples),
                self._coerce_runtime_float(stats.get("last", 0.0), default=0.0, minimum=0.0),
                self._coerce_runtime_float(stats.get("max", 0.0), default=0.0, minimum=0.0),
            ))
        if not rows:
            return "暂无性能统计。"

        rows.sort(reverse=True)
        lines = ["性能统计（最近样本均值）："]
        for avg_ms, label, count, sample_count, last_ms, max_ms in rows[:12]:
            lines.append(
                f"- {label}: avg={avg_ms:.1f}ms last={last_ms:.1f}ms "
                f"max={max_ms:.1f}ms samples={sample_count}/{count}"
            )
        pending_task_count = len(self._get_runtime_task_set("_pending_tasks"))
        lines.append(f"后台任务排队：{pending_task_count} 个")
        return "\n".join(lines)

    def _common_prefix_chars(self, left: str, right: str) -> int:
        limit = min(len(left), len(right))
        index = 0
        while index < limit and left[index] == right[index]:
            index += 1
        return index

    def _get_observation_scene(self, event: Optional[Any], scene_policy: Optional[Dict[str, Any]] = None) -> str:
        policy = scene_policy if isinstance(scene_policy, dict) else {}
        scene = str(policy.get("scene", "") or "").strip().lower()
        if scene in {"group", "private"}:
            return scene
        if event is not None and getattr(event, "group_id", None):
            return "group"
        return "private"

    def _record_live_observation(
        self,
        *,
        user_id: str,
        event: Optional[Any],
        req: Any,
        state: Dict[str, Any],
        scene_policy: Optional[Dict[str, Any]],
        existing_context_count: int,
        skip_plugin_history: bool,
    ) -> None:
        system_prompt = str(getattr(req, "system_prompt", "") or "")
        contexts = self._coerce_runtime_list_value(getattr(req, "contexts", []))
        context_texts = []
        for item in contexts:
            if isinstance(item, dict):
                text = str(item.get("content", "") or "").strip()
                if text:
                    context_texts.append(text)
        normalized_contexts = [self._normalize_analysis_content(text) for text in context_texts]
        duplicate_context_count = len(normalized_contexts) - len(set(normalized_contexts))
        system_overlap_count = sum(1 for text in normalized_contexts if text and text in system_prompt)
        before_count = self._coerce_runtime_int(existing_context_count, default=0, minimum=0)
        plugin_context_count = max(0, len(contexts) - before_count)
        prompt_hash = hashlib.sha1(system_prompt.encode("utf-8", errors="ignore")).hexdigest() if system_prompt else ""
        scene = self._get_observation_scene(event, scene_policy)
        scope = f"{scene}:{user_id}"
        last_by_scope = self._get_runtime_dict_cache("_live_observation_last_by_scope")
        previous = last_by_scope.get(scope)
        if isinstance(previous, dict):
            previous_prompt = str(previous.get("system_prompt", "") or "")
            stable_prefix_chars = self._common_prefix_chars(previous_prompt, system_prompt)
        else:
            stable_prefix_chars = 0
        stable_prefix_ratio = stable_prefix_chars / max(1, len(system_prompt)) if previous else 0.0
        memory_trace = self._coerce_runtime_list_value(getattr(self, "_last_prompt_memory_selection_trace", []))
        visibility_counts: Dict[str, int] = {}
        group_privacy_risk = 0
        for item in memory_trace:
            if not isinstance(item, dict):
                continue
            visibility = str(item.get("visibility", "") or "default")
            visibility_counts[visibility] = visibility_counts.get(visibility, 0) + 1
            if scene == "group" and visibility not in {"public_profile", "default", ""}:
                group_privacy_risk += 1
        prompt_estimate = self._coerce_runtime_dict_value(state.get("??Prompt??", {}))
        sample = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "user_id": str(user_id),
            "scene": scene,
            "mode": str((scene_policy or {}).get("mode", "") or ""),
            "system_prompt_chars": len(system_prompt),
            "estimated_tokens": self._coerce_runtime_int(prompt_estimate.get("tokens", 0), default=0, minimum=0),
            "prompt_hash": prompt_hash[:12],
            "stable_prefix_ratio": round(stable_prefix_ratio, 3),
            "contexts_before": before_count,
            "contexts_after": len(contexts),
            "plugin_context_count": plugin_context_count,
            "skip_plugin_history": bool(skip_plugin_history),
            "duplicate_context_count": max(0, duplicate_context_count),
            "system_overlap_count": max(0, system_overlap_count),
            "memory_selected": len(memory_trace),
            "memory_visibility_counts": visibility_counts,
            "group_privacy_risk": group_privacy_risk,
            "budget_guard_applied": bool(prompt_estimate.get("budget_guard_applied", False)),
            "compact": bool(prompt_estimate.get("compact", False)),
        }
        self._get_runtime_deque_cache("_live_observation_samples", maxlen=50).append(sample)
        last_by_scope[scope] = {
            "system_prompt": system_prompt[:8192],
            "prompt_hash": prompt_hash,
            "time": sample["time"],
        }

    def _build_live_observation_report(self, limit: int = 8) -> str:
        samples = self._coerce_runtime_list_value(getattr(self, "_live_observation_samples", []))
        samples = [item for item in samples if isinstance(item, dict)]
        if not samples:
            return "\n".join([
                "Marianna live observation",
                "No samples yet. Send a few real private/group messages first, then run this command again.",
            ])
        effective_limit = self._coerce_runtime_int(limit, default=8, minimum=1, maximum=30)
        recent = samples[-effective_limit:]
        duplicate_hits = sum(
            1 for item in samples
            if self._coerce_runtime_int(item.get("duplicate_context_count", 0), default=0, minimum=0) > 0
            or self._coerce_runtime_int(item.get("system_overlap_count", 0), default=0, minimum=0) > 0
        )
        privacy_hits = sum(
            self._coerce_runtime_int(item.get("group_privacy_risk", 0), default=0, minimum=0)
            for item in samples
        )
        avg_stable = sum(
            self._coerce_runtime_float(item.get("stable_prefix_ratio", 0.0), default=0.0, minimum=0.0, maximum=1.0)
            for item in samples
        ) / max(1, len(samples))
        lines = [
            "Marianna live observation",
            f"Samples: {len(samples)}",
            f"DeepSeek/cache proxy: avg stable prefix {avg_stable:.0%} (higher is better after repeated similar turns)",
            f"Context duplication risk: {duplicate_hits} sample(s)",
            f"Group privacy risk: {privacy_hits} suspicious injected memory item(s)",
            "",
            "Recent samples:",
        ]
        for item in recent:
            risk_parts = []
            if self._coerce_runtime_int(item.get("duplicate_context_count", 0), default=0, minimum=0) > 0:
                risk_parts.append("duplicate_context")
            if self._coerce_runtime_int(item.get("system_overlap_count", 0), default=0, minimum=0) > 0:
                risk_parts.append("context_prompt_overlap")
            if self._coerce_runtime_int(item.get("group_privacy_risk", 0), default=0, minimum=0) > 0:
                risk_parts.append("group_privacy")
            risk_text = ",".join(risk_parts) if risk_parts else "ok"
            stable = self._coerce_runtime_float(item.get("stable_prefix_ratio", 0.0), default=0.0, minimum=0.0, maximum=1.0)
            lines.append(
                "- "
                f"{item.get('time')} {item.get('scene')}/{item.get('mode')} "
                f"tokens={item.get('estimated_tokens')} chars={item.get('system_prompt_chars')} "
                f"contexts={item.get('contexts_before')}->{item.get('contexts_after')} "
                f"plugin_ctx={item.get('plugin_context_count')} stable={stable:.0%} "
                f"mem={item.get('memory_selected')} vis={item.get('memory_visibility_counts', {})} "
                f"risk={risk_text}"
            )
        lines.append("")
        if duplicate_hits or privacy_hits:
            lines.append("Action: inspect context injection settings and group memory visibility before publishing.")
        else:
            lines.append("Action: no duplicate context or group privacy risk detected in collected samples.")
        return "\n".join(lines)

    def _trim_dict_cache(self, cache: Dict[Any, Any], max_entries: int):
        while len(cache) > max_entries:
            try:
                oldest_key = next(iter(cache))
            except StopIteration:
                return
            del cache[oldest_key]

    def _clear_pending_for_user(self, user_id: str):
        """清理某个用户相关的临时缓存。"""
        for cache in (
            self._get_runtime_dict_cache("_pending_events"),
            self._get_runtime_dict_cache("_pending_debug_deltas"),
            self._get_runtime_dict_cache("_analysis_request_cache"),
        ):
            for key in list(cache.keys()):
                if self._pending_key_belongs_to_user(key, user_id):
                    del cache[key]
        session_alias_created_at = self._get_runtime_dict_cache("_session_alias_created_at")
        for key in list(session_alias_created_at.keys()):
            if self._pending_key_belongs_to_user(key, user_id):
                del session_alias_created_at[key]
        session_alias_queues = self._get_runtime_dict_cache("_session_alias_queues")
        for alias_key, queue in list(session_alias_queues.items()):
            if self._pending_key_belongs_to_user(alias_key, user_id):
                del session_alias_queues[alias_key]
                continue
            if not isinstance(queue, list):
                del session_alias_queues[alias_key]
                continue
            filtered = [
                key for key in queue
                if not self._pending_key_belongs_to_user(key, user_id)
            ]
            if filtered:
                session_alias_queues[alias_key] = filtered
            else:
                del session_alias_queues[alias_key]

    def _get_effective_temperature(self, state: Optional[Dict[str, Any]] = None) -> float:
        temperature = self._coerce_runtime_float(getattr(self, "temperature", 0.7), default=0.7)
        if state and getattr(self, "enable_value_dialogue_modulation", True):
            anxiety = self._coerce_runtime_int(state.get("焦虑值", 0), default=0, minimum=0, maximum=100)
            elegance_value = state.get("优雅值", 85)
            elegance = self._coerce_runtime_int(elegance_value, default=85, minimum=0, maximum=100)
            yan = self._coerce_runtime_int(state.get("病娇值", 0), default=0, minimum=0, maximum=100)

            if anxiety >= 70:
                temperature += 0.08
            elif anxiety >= 45:
                temperature += 0.04

            if elegance <= 30:
                temperature += 0.08
            elif elegance <= 55:
                temperature += 0.04
            elif elegance >= 85:
                temperature -= 0.04

            if yan >= 70:
                temperature += 0.03

        return round(max(0.5, min(1.2, temperature)), 2)

    def _apply_request_temperature(self, req: ProviderRequest, state: Optional[Dict[str, Any]] = None):
        """尽量把插件温度配置写入请求对象，同时兼容不同版本字段。"""
        effective_temperature = self._get_effective_temperature(state)
        if hasattr(req, "temperature"):
            try:
                req.temperature = effective_temperature
            except Exception:
                pass

        kwargs = getattr(req, "kwargs", None)
        if isinstance(kwargs, dict):
            kwargs["temperature"] = effective_temperature


