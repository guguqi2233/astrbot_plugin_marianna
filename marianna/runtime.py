import asyncio
import copy
import hashlib
import json
import os
import re
import time
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
    async def _save_all_data(self) -> bool:
        """保存所有数据，返回是否全部成功。"""
        results = await asyncio.gather(
            self._save_json_async(self.user_states_file, self.user_states),
            self._save_json_async(self.user_profiles_file, self.user_profiles),
            self._save_json_async(self.global_state_file, self.global_state),
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
        self._pending_tasks.add(task)

        def _on_done(done_task: asyncio.Task):
            self._pending_tasks.discard(done_task)
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
            async with self._pending_task_semaphore:
                started = True
                return await coro
        finally:
            if not started and hasattr(coro, "close"):
                coro.close()

    # ======================== 辅助函数 ========================

    async def _get_lock(self, file_path: Path) -> asyncio.Lock:
        """获取文件锁（用于并发控制）"""
        path_str = str(file_path)
        if path_str not in self._file_locks:
            self._file_locks[path_str] = asyncio.Lock()
        return self._file_locks[path_str]

    def _get_user_lock(self, user_id: str) -> asyncio.Lock:
        """获取同一用户请求锁，降低连续消息导致的状态交错。"""
        key = str(user_id or "unknown")
        if key not in self._user_locks:
            self._user_locks[key] = asyncio.Lock()
        return self._user_locks[key]

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
                    return json.load(f)
        except Exception as e:
            self.logger.error(f"加载 {path} 失败: {e}", exc_info=True)
        return default

    async def _write_text_atomic(self, path: Path, content: str):
        """以临时文件 + 替换方式写入，避免读到半截文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.tmp")
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
        lock = await self._get_lock(path)
        async with lock:
            try:
                payload = json.dumps(data, ensure_ascii=False, indent=2)
                await self._write_text_atomic(path, payload)
                self.logger.debug(f"已保存文件: {path}")
            except Exception as e:
                self.logger.error(f"保存 {path} 失败: {e}", exc_info=True)
                raise

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
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default

        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _apply_config(self):
        """应用配置到默认值和运行时参数"""
        try:
            DEFAULT_STATE["好感度"] = self.config.get("marianna_initial_favor", 0)
            DEFAULT_STATE["病娇值"] = self.config.get("marianna_initial_yan", 0)
            DEFAULT_STATE["信任度"] = self.config.get("marianna_initial_trust", 15)
            DEFAULT_STATE["焦虑值"] = self.config.get("marianna_initial_anxiety", 5)
            DEFAULT_STATE["占有欲"] = 0
            DEFAULT_STATE["优雅值"] = self.config.get("marianna_initial_elegance", 85)
            self.favor_multiplier = self.config.get("marianna_favor_multiplier", 1.0)
            self.yan_multiplier = self.config.get("marianna_yan_multiplier", 1.0)
            raw_lock_threshold = self.config.get("marianna_lock_threshold", 100)
            self.lock_threshold = max(50, min(100, raw_lock_threshold))
            self.auto_summary_interval = self.config.get("auto_summary_interval", 20)
            self.auto_summary_idle = self.config.get("auto_summary_idle_time", 300)
            self.enable_profile = self.config.get("enable_user_profile", True)
            self.enable_emotional_memory = self.config.get("enable_emotional_memory", True)
            self.enable_selective_interaction_memory = self.config.get(
                "enable_selective_interaction_memory",
                True,
            )
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
            self.enable_memory_update_layer = self.config.get(
                "enable_memory_update_layer",
                True,
            )
            self.enable_memory_forgetting_layer = self.config.get(
                "enable_memory_forgetting_layer",
                True,
            )
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
            self.temperature = self.config.get("marianna_temperature", 0.85)
            self.analysis_provider_id = (
                self.config.get("marianna_analysis_provider_id", "") or ""
            ).strip()
            self.default_debug_mode = self.config.get("marianna_debug_mode", False)
            DEFAULT_STATE["调试模式"] = self.default_debug_mode
            # LLM 上下文注入配置
            self.context_injection_enabled = self.config.get("enable_context_injection", True)
            self.max_context_messages = self._get_config_int(
                "context_history_limit", 10, minimum=0, maximum=1000
            )
            self.inject_history = self.config.get("enable_context_injection", True)
            self.inject_summary_in_context = self.config.get("inject_summary_as_context", True)
            self.inject_state_details = self.config.get("inject_state_details", True)
            self.enable_value_dialogue_modulation = self.config.get(
                "enable_value_dialogue_modulation",
                True,
            )
            self.enable_emotion_recognition_layer = self.config.get(
                "enable_emotion_recognition_layer",
                True,
            )
            self.enable_active_event_layer = self.config.get(
                "enable_active_event_layer",
                True,
            )
            self.active_event_cooldown_turns = self._get_config_int(
                "active_event_cooldown_turns",
                ACTIVE_EVENT_COOLDOWN_TURNS,
                minimum=1,
                maximum=50,
            )
            self.enable_reflection_update_layer = self.config.get(
                "enable_reflection_update_layer",
                True,
            )
            self.max_tokens_per_message = self._get_config_int(
                "context_max_tokens_per_msg", 300, minimum=50, maximum=20000
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
                minimum=10_000,
                maximum=1_000_000,
            )
            self.history_retention_limit = self._get_config_int(
                "conversation_history_retention_limit",
                CONVERSATION_HISTORY_RETENTION_LIMIT,
                minimum=200,
                maximum=5000,
            )
            self.enable_performance_logging = self.config.get(
                "enable_performance_logging",
                True,
            )
            if hasattr(self, "_static_prompt_cache"):
                self._static_prompt_cache.clear()
            if hasattr(self, "_dynamic_prompt_cache"):
                self._dynamic_prompt_cache.clear()
            if hasattr(self, "_mnemosyne_query_cache"):
                self._mnemosyne_query_cache.clear()
            if hasattr(self, "_recent_history_cache"):
                self._recent_history_cache.clear()
            self.logger.debug("配置应用成功")
        except Exception as e:
            self.logger.error(f"应用配置失败: {e}", exc_info=True)

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
        if not create:
            queue = self._session_alias_queues.get(alias_key)
            if queue:
                session_key = queue.pop(0)
                if not queue:
                    self._session_alias_queues.pop(alias_key, None)
                return session_key
            return alias_key

        self._session_counter += 1
        session_key = f"{alias_key}::seq:{self._session_counter}"
        self._session_alias_queues.setdefault(alias_key, []).append(session_key)
        self._session_alias_created_at[session_key] = time.monotonic()
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
        for cache in (
            self._pending_events,
            self._pending_debug_deltas,
            self._analysis_request_cache,
        ):
            for key, value in list(cache.items()):
                if not isinstance(value, dict):
                    del cache[key]
                    continue

                created_at = value.get("_created_at")
                if created_at is None:
                    continue

                try:
                    if float(created_at) < cutoff:
                        del cache[key]
                except (TypeError, ValueError):
                    del cache[key]

        active_keys = set(self._pending_events)
        active_keys.update(self._pending_debug_deltas)
        active_keys.update(self._analysis_request_cache)
        for key, created_at in list(self._session_alias_created_at.items()):
            try:
                expired = float(created_at) < cutoff
            except (TypeError, ValueError):
                expired = True
            if expired and key not in active_keys:
                self._session_alias_created_at.pop(key, None)
        for alias_key, queue in list(self._session_alias_queues.items()):
            filtered = [
                key for key in queue
                if key in active_keys or key in self._session_alias_created_at
            ]
            if filtered:
                self._session_alias_queues[alias_key] = filtered
            else:
                self._session_alias_queues.pop(alias_key, None)

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
        stats = self._perf_stats.setdefault(
            label,
            {
                "samples": deque(maxlen=PERF_STATS_MAX_SAMPLES),
                "count": 0,
                "max": 0.0,
                "last": 0.0,
            },
        )
        samples = stats["samples"]
        samples.append(float(elapsed_ms))
        stats["count"] = int(stats.get("count", 0) or 0) + 1
        stats["last"] = float(elapsed_ms)
        stats["max"] = max(float(stats.get("max", 0.0) or 0.0), float(elapsed_ms))

    def _build_perf_report(self) -> str:
        if not self._perf_stats:
            return "暂无性能统计。"

        rows = []
        for label, stats in self._perf_stats.items():
            samples = list(stats.get("samples", []) or [])
            if not samples:
                continue
            avg_ms = sum(samples) / len(samples)
            rows.append((
                avg_ms,
                label,
                int(stats.get("count", 0) or 0),
                len(samples),
                float(stats.get("last", 0.0) or 0.0),
                float(stats.get("max", 0.0) or 0.0),
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
        lines.append(f"后台任务排队：{len(self._pending_tasks)} 个")
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
        for cache in (self._pending_events, self._pending_debug_deltas, self._analysis_request_cache):
            for key in list(cache.keys()):
                if self._pending_key_belongs_to_user(key, user_id):
                    del cache[key]

    def _get_effective_temperature(self, state: Optional[Dict[str, Any]] = None) -> float:
        temperature = float(self.temperature)
        if state and getattr(self, "enable_value_dialogue_modulation", True):
            anxiety = int(state.get("焦虑值", 0) or 0)
            elegance_value = state.get("优雅值", 85)
            elegance = 85 if elegance_value is None else int(elegance_value)
            yan = int(state.get("病娇值", 0) or 0)

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


