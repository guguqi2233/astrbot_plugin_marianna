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

class MariannaTurnMixin:
    async def _prepare_turn_state(
        self,
        user_id: str,
        user_name: str,
    ) -> Tuple[Dict[str, Any], str, int]:
        state = self._get_state(user_id, count_interaction=False)
        if await self._reconcile_destined_one_state(user_id, state):
            self._schedule_state_save(user_id, state)

        old_state_name = state.get("当前状态", STATE_NAMES["COLD_NOBLE"])
        try:
            old_lock_progress = int(state.get("锁定进度", 0) or 0)
        except (TypeError, ValueError):
            old_lock_progress = 0

        destined_info = self._get_destined_one_info()
        if not destined_info and old_lock_progress >= self.lock_threshold:
            await self._set_destined_one(user_id, user_name)

        return state, old_state_name, old_lock_progress

    def _copy_state_for_prompt(self, state: Dict[str, Any]) -> Dict[str, Any]:
        prompt_fields = (
            "好感度",
            "病娇值",
            "锁定进度",
            "信任度",
            "占有欲",
            "焦虑值",
            "优雅值",
            "当前状态",
            "互动计数",
            "最近主动事件互动",
            "已触发锁定事件",
            "已触发崩溃事件",
            "调试模式",
        )
        return {
            field: state.get(field, DEFAULT_STATE.get(field))
            for field in prompt_fields
        }

    async def _run_turn_analysis(
        self,
        event: AstrMessageEvent,
        user_id: str,
        user_name: str,
        session_key: str,
        message_text: str,
        message_key: str,
        state: Dict[str, Any],
        old_state_name: str,
        old_lock_progress: int,
    ) -> Dict[str, Any]:
        skip_analysis = self._should_skip_analysis_llm(message_text)

        if skip_analysis:
            self._touch_state_interaction(state)
            self._schedule_state_save(user_id, state)
            self._spawn_task(self._add_to_history(user_id, "user", message_text))
            self.logger.debug(f"[on_llm_request] user={user_id} analysis_skipped=1")
            return {
                "applied_changes": {},
                "turn_analysis": self._build_fallback_turn_analysis(message_text, deltas={}),
                "active_event": {},
                "skip_analysis": True,
                "is_duplicate_analysis": False,
            }

        analysis_history_entries = await self._get_analysis_memory_entries(
            user_id,
            message_text,
        )
        analysis_fingerprint = self._build_analysis_request_fingerprint(
            session_key,
            message_text,
            analysis_history_entries,
        )
        cached_analysis = self._analysis_request_cache.get(session_key, {})
        is_duplicate_analysis = (
            isinstance(cached_analysis, dict)
            and cached_analysis.get("fingerprint") == analysis_fingerprint
        )

        if is_duplicate_analysis:
            self.logger.debug(f"[on_llm_request] user={user_id} analysis_cache_hit=1")
            return {
                "applied_changes": dict(cached_analysis.get("applied_changes", {})),
                "turn_analysis": dict(cached_analysis.get("turn_analysis", {})),
                "active_event": dict(cached_analysis.get("active_event", {})),
                "skip_analysis": False,
                "is_duplicate_analysis": True,
            }

        self._touch_state_interaction(state)
        analysis_result = await self._analyze_state_changes(
            event,
            user_id,
            state,
            message_text,
            history_entries=analysis_history_entries,
        )
        turn_analysis = self._extract_turn_analysis(analysis_result)
        deltas = self._extract_analysis_deltas(analysis_result)
        if not turn_analysis:
            turn_analysis = self._build_fallback_turn_analysis(
                message_text,
                deltas=deltas,
            )

        applied_changes = self._apply_llm_state_changes(user_id, state, deltas)
        milestone_memory = self._build_mnemosyne_state_milestone(
            old_state_name,
            state.get("当前状态", ""),
        )
        destined_info = self._get_destined_one_info()

        if self._is_destined_user(user_id) and destined_info.get("user_name") != user_name:
            await self._set_destined_one(user_id, user_name)
            destined_info = self._get_destined_one_info()

        if (
            old_lock_progress < self.lock_threshold
            and state["锁定进度"] >= self.lock_threshold
            and not state.get("已触发锁定事件", False)
        ):
            if not destined_info or self._is_destined_user(user_id):
                await self._set_destined_one(user_id, user_name)
                state["已触发锁定事件"] = True
                self._pending_events[session_key] = {
                    "type": "locked",
                    "message_key": message_key,
                    "_created_at": time.monotonic(),
                }
            else:
                state["锁定进度"] = max(0, self.lock_threshold - 1)
                state["当前状态"] = self._determine_state(state)

        if self._pending_events.get(session_key, {}).get("type") == "locked":
            active_event = {}
        else:
            active_event = self._select_active_event(
                state,
                message_text,
                turn_analysis,
            )
        if active_event:
            state["最近主动事件互动"] = int(state.get("互动计数", 0) or 0)

        if milestone_memory and self.mnemosyne_available and self.enable_emotional_memory:
            self._spawn_task(
                self._store_to_mnemosyne(
                    user_id,
                    milestone_memory,
                    "milestone",
                    salience=7,
                    memory_layer="event",
                )
            )

        self._analysis_request_cache[session_key] = {
            "fingerprint": analysis_fingerprint,
            "applied_changes": dict(applied_changes),
            "turn_analysis": dict(turn_analysis),
            "active_event": dict(active_event),
            "_created_at": time.monotonic(),
        }
        self._schedule_state_save(user_id, state)
        self._spawn_task(self._add_to_history(user_id, "user", message_text))

        return {
            "applied_changes": applied_changes,
            "turn_analysis": turn_analysis,
            "active_event": active_event,
            "skip_analysis": False,
            "is_duplicate_analysis": False,
        }

    async def _inject_prompt_and_context(
        self,
        req: ProviderRequest,
        user_id: str,
        state: Dict[str, Any],
        message_text: str,
        turn_analysis: Dict[str, str],
        active_event: Dict[str, str],
        skip_analysis: bool,
    ):
        prompt_started_at = time.perf_counter()
        compact_prompt = skip_analysis
        plugin_system_prompt = await self._build_system_prompt(
            user_id,
            state,
            message_text,
            turn_analysis=turn_analysis,
            active_event=active_event,
            skip_memory_retrieval=skip_analysis,
            compact_prompt=compact_prompt,
        )
        self._log_perf("build_system_prompt", prompt_started_at, user_id, threshold_ms=5.0)

        existing_system_prompt = getattr(req, "system_prompt", "") or ""
        if existing_system_prompt.strip():
            req.system_prompt = f"{existing_system_prompt}\n\n{plugin_system_prompt}"
        else:
            req.system_prompt = plugin_system_prompt

        contexts_started_at = time.perf_counter()
        existing_contexts = list(getattr(req, "contexts", []) or [])
        if self.context_injection_enabled:
            contexts = []

            if self.inject_history:
                history = await self._get_recent_history_async(
                    user_id,
                    limit=self.max_context_messages,
                )
                for entry in history:
                    role = entry.get("role", "user")
                    content = self._limit_text_for_prompt(
                        entry.get("content", ""),
                        self.max_tokens_per_message,
                    )
                    contexts.append({"role": role, "content": content})

            if self.inject_summary_in_context and not contexts:
                prof = self._get_profile(user_id)
                summaries = prof.get("玛丽亚学习笔记", {}).get("自动总结", [])
                if summaries:
                    latest = self._strip_debug_artifacts(summaries[-1].get("summary", ""))
                    if latest:
                        hint = f"*（玛丽亚回忆起之前的对话：{latest[:200]}）*"
                        contexts.append({"role": "assistant", "content": hint})

            if contexts:
                req.contexts = contexts + existing_contexts

        self._log_perf(
            "inject_contexts",
            contexts_started_at,
            user_id,
            extra=f"contexts={len(getattr(req, 'contexts', []) or [])}",
            threshold_ms=5.0,
        )
        self._apply_request_temperature(req, state=state)


