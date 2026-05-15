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
        state["???????ID"] = user_id
        state["?????????"] = str(user_id).startswith("group:")
        state["_本轮前最后互动时间"] = state.get("最后互动时间")
        if self._inherit_debug_mode_from_related_state(user_id, state):
            self._schedule_state_save(user_id, state)
        if await self._reconcile_destined_one_state(user_id, state):
            self._schedule_state_save(user_id, state)
        cooldown_changes = self._apply_relationship_cooldown_if_needed(state, user_id=user_id)
        if cooldown_changes:
            self._schedule_state_save(user_id, state)

        old_state_name = state.get("当前状态", STATE_NAMES["COLD_NOBLE"])
        old_lock_progress = self._coerce_analysis_int(
            state.get("锁定进度", 0),
            default=0,
            minimum=0,
            maximum=100,
        )
        lock_threshold = self._coerce_analysis_int(
            getattr(self, "lock_threshold", 100),
            default=100,
            minimum=0,
            maximum=100,
        )

        destined_info = self._get_destined_one_info()
        if not destined_info and old_lock_progress >= lock_threshold:
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
            "关系状态机",
            "互动计数",
            "最近主动事件互动",
            "主动事件队列",
            "短期心情",
            "情绪余温",
            "防备值",
            "被触动值",
            "表达克制度",
            "上轮短期心情",
            "当前行为档位",
            "上轮行为档位",
            "目标行为档位",
            "行为档位稳定轮数",
            "行为档位理由",
            "行为连续性提示",
            "行为风格变体",
            "关系事件日志",
            "最近召回记忆",
            "最近召回记忆已反馈",
            "最近记忆负反馈",
            "最近风格指纹提示",
            "最近是否轻量Prompt",
            "阶段证据确认",
            "已触发锁定事件",
            "已触发崩溃事件",
            "调试模式",
        )
        return {
            field: state.get(field, DEFAULT_STATE.get(field))
            for field in prompt_fields
        }

    async def _commit_turn_analysis_result(
        self,
        *,
        user_id: str,
        user_name: str,
        session_key: str,
        message_key: str,
        message_text: str,
        state: Dict[str, Any],
        old_state_name: str,
        old_lock_progress: int,
        analysis_result: Dict[str, Any],
        analysis_fingerprint: Optional[str] = None,
    ) -> Dict[str, Any]:
        turn_analysis = self._extract_turn_analysis(analysis_result)
        deltas = self._extract_analysis_deltas(analysis_result)
        if not turn_analysis:
            turn_analysis = self._build_fallback_turn_analysis(
                message_text,
                deltas=deltas,
            )

        if analysis_result.get("__memory_evidence"):
            state["阶段证据确认"] = True
        evidence = analysis_result.get("__evidence")
        if isinstance(evidence, dict):
            state["阶段证据等级"] = str(evidence.get("level", "无") or "无")
        explanation = analysis_result.get("__state_explanation")
        if (
            getattr(self, "enable_state_explanation_log", ENABLE_STATE_EXPLANATION_LOG)
            and isinstance(explanation, dict)
        ):
            state["最近状态解释"] = dict(explanation)
        applied_changes = self._apply_llm_state_changes(user_id, state, deltas)
        behavior_state = self._update_short_term_behavior_state(
            state,
            turn_analysis,
            applied_changes,
            user_id=user_id,
            user_msg=message_text,
        )
        if (
            getattr(self, "enable_state_explanation_log", ENABLE_STATE_EXPLANATION_LOG)
            and isinstance(explanation, dict)
        ):
            explanation["实际变化"] = {
                field: value
                for field, value in applied_changes.items()
                if isinstance(value, int) and value != 0
            }
            explanation["短期心理"] = dict(behavior_state)
            state["最近最终变化"] = dict(applied_changes)
            explanation["time"] = datetime.now().isoformat()
            state["最近状态解释"] = dict(explanation)
            history = state.get("诊断历史", [])
            if not isinstance(history, list):
                history = []
            history.append(dict(explanation))
            limit = self._coerce_analysis_int(
                getattr(self, "diagnostic_history_limit", DIAGNOSTIC_HISTORY_LIMIT),
                default=DIAGNOSTIC_HISTORY_LIMIT,
                minimum=1,
            )
            state["诊断历史"] = history[-max(1, limit):]
        milestone_memory = self._build_mnemosyne_state_milestone(
            old_state_name,
            state.get("当前状态", ""),
        )
        destined_info = self._get_destined_one_info()

        if self._is_destined_user(user_id) and destined_info.get("user_name") != user_name:
            await self._set_destined_one(user_id, user_name)
            destined_info = self._get_destined_one_info()

        lock_threshold = self._coerce_analysis_int(
            getattr(self, "lock_threshold", 100),
            default=100,
            minimum=0,
            maximum=100,
        )
        if (
            old_lock_progress < lock_threshold
            and state["锁定进度"] >= lock_threshold
            and not state.get("已触发锁定事件", False)
        ):
            if not destined_info or self._is_destined_user(user_id):
                await self._set_destined_one(user_id, user_name)
                state["已触发锁定事件"] = True
                self._get_runtime_dict_cache("_pending_events")[session_key] = {
                    "type": "locked",
                    "message_key": message_key,
                    "_created_at": time.monotonic(),
                }
            else:
                state["锁定进度"] = max(0, lock_threshold - 1)
                state["当前状态"] = self._determine_state(state)

        if self._get_runtime_dict_cache("_pending_events").get(session_key, {}).get("type") == "locked":
            active_event = {}
        else:
            active_event = self._select_active_event(
                state,
                message_text,
                turn_analysis,
            )
            if not active_event:
                active_event = self._pop_active_event_from_queue(state, message_text)
        if active_event:
            state["最近主动事件互动"] = self._coerce_analysis_int(
                state.get("互动计数", 0),
                default=0,
                minimum=0,
            )
        self._refresh_active_event_queue(state, message_text, turn_analysis, active_event)

        if milestone_memory and self.enable_emotional_memory:
            if getattr(self, "enable_builtin_memory", ENABLE_BUILTIN_MEMORY):
                self._spawn_task(
                    self._store_to_builtin_memory(
                        user_id,
                        milestone_memory,
                        "milestone",
                        salience=7,
                        memory_layer="event",
                    )
                )
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

        if analysis_fingerprint:
            self._get_runtime_dict_cache("_analysis_request_cache")[session_key] = {
                "fingerprint": analysis_fingerprint,
                "applied_changes": dict(applied_changes),
                "turn_analysis": dict(turn_analysis),
                "active_event": dict(active_event),
                "state_explanation": self._coerce_runtime_dict_value(state.get("最近状态解释", {})),
                "_created_at": time.monotonic(),
            }
        self._schedule_state_save(user_id, state)
        self._spawn_task(self._add_to_history(user_id, "user", message_text))

        return {
            "applied_changes": applied_changes,
            "turn_analysis": turn_analysis,
                "active_event": active_event,
                "state_explanation": self._coerce_runtime_dict_value(state.get("最近状态解释", {})),
                "skip_analysis": False,
                "is_duplicate_analysis": False,
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
                "state_explanation": {},
                "skip_analysis": True,
                "is_duplicate_analysis": False,
            }

        local_analysis_result = self._build_local_state_analysis(
            state,
            message_text,
            user_id=user_id,
        )
        if local_analysis_result is not None:
            self._touch_state_interaction(state)
            self.logger.debug(f"[on_llm_request] user={user_id} analysis_local=1")
            return await self._commit_turn_analysis_result(
                user_id=user_id,
                user_name=user_name,
                session_key=session_key,
                message_key=message_key,
                message_text=message_text,
                state=state,
                old_state_name=old_state_name,
                old_lock_progress=old_lock_progress,
                analysis_result=local_analysis_result,
            )

        analysis_history_entries = await self._get_analysis_memory_entries(
            user_id,
            message_text,
            scene_policy=state.get("_scene_memory_policy"),
        )
        analysis_fingerprint = self._build_analysis_request_fingerprint(
            session_key,
            message_text,
            analysis_history_entries,
            scene_policy=state.get("_scene_memory_policy"),
        )
        cached_analysis = self._get_runtime_dict_cache("_analysis_request_cache").get(session_key, {})
        is_duplicate_analysis = (
            isinstance(cached_analysis, dict)
            and cached_analysis.get("fingerprint") == analysis_fingerprint
        )

        if is_duplicate_analysis:
            self.logger.debug(f"[on_llm_request] user={user_id} analysis_cache_hit=1")
            return {
                "applied_changes": self._coerce_runtime_dict_value(cached_analysis.get("applied_changes", {})),
                "turn_analysis": self._coerce_runtime_dict_value(cached_analysis.get("turn_analysis", {})),
                "active_event": self._coerce_runtime_dict_value(cached_analysis.get("active_event", {})),
                "state_explanation": self._coerce_runtime_dict_value(cached_analysis.get("state_explanation", {})),
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
        return await self._commit_turn_analysis_result(
            user_id=user_id,
            user_name=user_name,
            session_key=session_key,
            message_key=message_key,
            message_text=message_text,
            state=state,
            old_state_name=old_state_name,
            old_lock_progress=old_lock_progress,
            analysis_result=analysis_result,
            analysis_fingerprint=analysis_fingerprint,
        )

    def _should_skip_turn_memory_retrieval(
        self,
        message_text: str,
        turn_analysis: Optional[Dict[str, str]] = None,
    ) -> bool:
        if not getattr(self, "enable_token_cost_optimization", ENABLE_TOKEN_COST_OPTIMIZATION):
            return False
        normalized = self._normalize_analysis_content(message_text)
        if not normalized:
            return True
        if self._should_use_local_state_analysis(normalized):
            return True
        if self._should_expand_analysis_history(normalized):
            return False
        signal = str((turn_analysis or {}).get("关系信号", "") or "")
        if signal and signal not in {"无明显关系推进", "暂无明显关系推进", "释放善意"}:
            return False
        return True

    def _should_use_adaptive_lightweight_prompt(
        self,
        message_text: str,
        turn_analysis: Optional[Dict[str, str]],
        active_event: Optional[Dict[str, str]],
        *,
        skip_memory_retrieval: bool,
    ) -> bool:
        if not getattr(self, "enable_token_cost_optimization", ENABLE_TOKEN_COST_OPTIMIZATION):
            return False
        if not getattr(self, "enable_adaptive_lightweight_prompt", ENABLE_ADAPTIVE_LIGHTWEIGHT_PROMPT):
            return False
        if active_event:
            return False
        if not skip_memory_retrieval:
            return False
        normalized = self._normalize_analysis_content(message_text)
        if not normalized or normalized.startswith("/"):
            return False
        if self._should_expand_analysis_history(normalized):
            return False
        max_chars = self._coerce_analysis_int(
            getattr(
                self,
                "adaptive_lightweight_prompt_max_chars",
                ADAPTIVE_LIGHTWEIGHT_PROMPT_MAX_CHARS,
            ),
            default=ADAPTIVE_LIGHTWEIGHT_PROMPT_MAX_CHARS,
            minimum=1,
        )
        if len(normalized) > max_chars:
            return False
        analysis = turn_analysis or {}
        signal = str(analysis.get("关系信号", "") or "")
        if signal and signal not in {"无明显关系推进", "暂无明显关系推进", "释放善意"}:
            return False
        intent = str(analysis.get("用户意图", "") or "")
        strong_intents = (
            "道歉",
            "承诺",
            "分享秘密",
            "触发边界",
            "占有试探",
            "离开暗示",
            "冒犯",
            "调情",
        )
        return not any(item in intent for item in strong_intents)

    async def _inject_prompt_and_context(
        self,
        req: ProviderRequest,
        user_id: str,
        state: Dict[str, Any],
        message_text: str,
        turn_analysis: Dict[str, str],
        active_event: Dict[str, str],
        skip_analysis: bool,
        event: Optional[Any] = None,
    ):
        prompt_started_at = time.perf_counter()
        scene_policy = state.get("_scene_memory_policy")
        if not isinstance(scene_policy, dict):
            scene_policy = state.get("本轮场景记忆策略")
        if not isinstance(scene_policy, dict):
            scene_policy = self._build_scene_memory_policy(event)
            state["本轮场景记忆策略"] = scene_policy
        skip_memory_retrieval = skip_analysis or self._should_skip_turn_memory_retrieval(
            message_text,
            turn_analysis,
        )
        adaptive_compact = self._should_use_adaptive_lightweight_prompt(
            message_text,
            turn_analysis,
            active_event,
            skip_memory_retrieval=skip_memory_retrieval,
        )
        compact_prompt = skip_analysis or adaptive_compact
        if skip_analysis:
            prompt_reason = "分析已跳过，使用轻量 prompt"
        elif adaptive_compact:
            prompt_reason = "短句且无强关系信号，使用自适应轻量 prompt"
        else:
            prompt_reason = "常规 prompt"
        state["最近记忆召回策略"] = {
            "skipped": bool(skip_memory_retrieval),
            "compact": bool(compact_prompt),
            "reason": prompt_reason if skip_memory_retrieval or compact_prompt else "常规召回",
        }
        plugin_system_prompt = await self._build_system_prompt(
            user_id,
            state,
            message_text,
            turn_analysis=turn_analysis,
            active_event=active_event,
            skip_memory_retrieval=skip_memory_retrieval,
            compact_prompt=compact_prompt,
        )
        self._log_perf("build_system_prompt", prompt_started_at, user_id, threshold_ms=5.0)

        existing_system_prompt = getattr(req, "system_prompt", "") or ""
        if existing_system_prompt.strip():
            req.system_prompt = f"{existing_system_prompt}\n\n{plugin_system_prompt}"
        else:
            req.system_prompt = plugin_system_prompt

        contexts_started_at = time.perf_counter()
        existing_contexts = self._coerce_runtime_list_value(getattr(req, "contexts", []))
        context_injection_enabled = bool(
            scene_policy.get(
                "context_injection_enabled",
                getattr(self, "context_injection_enabled", False),
            )
        )
        inject_history = bool(scene_policy.get("inject_history", context_injection_enabled))
        inject_summary_in_context = bool(scene_policy.get("inject_summary_in_context", False))
        skip_plugin_history = False
        if context_injection_enabled:
            contexts = []
            skip_plugin_history = (
                bool(existing_contexts)
                and getattr(self, "avoid_duplicate_context_injection", True)
            ) or (
                skip_analysis
                and getattr(self, "enable_token_cost_optimization", ENABLE_TOKEN_COST_OPTIMIZATION)
            )

            if inject_history and not skip_plugin_history:
                history_limit = self._coerce_history_limit(
                    getattr(self, "max_context_messages", TOKEN_OPT_CONTEXT_HISTORY_LIMIT),
                    default=TOKEN_OPT_CONTEXT_HISTORY_LIMIT,
                    minimum=0,
                )
                message_char_limit = self._coerce_history_limit(
                    getattr(self, "max_tokens_per_message", TOKEN_OPT_CONTEXT_MAX_CHARS_PER_MSG),
                    default=TOKEN_OPT_CONTEXT_MAX_CHARS_PER_MSG,
                    minimum=0,
                )
                history = await self._get_recent_history_async(
                    user_id,
                    limit=history_limit,
                )
                for entry in history:
                    role = entry.get("role", "user")
                    content = self._limit_text_for_prompt(
                        entry.get("content", ""),
                        message_char_limit,
                    )
                    contexts.append({"role": role, "content": content})

            if (
                inject_summary_in_context
                and not contexts
                and not existing_contexts
                and not skip_plugin_history
            ):
                prof = self._get_profile(user_id)
                summaries = prof.get("玛丽亚学习笔记", {}).get("自动总结", [])
                if summaries:
                    latest_summary = next(
                        (
                            item for item in reversed(summaries)
                            if isinstance(item, dict)
                        ),
                        {},
                    )
                    latest = self._strip_debug_artifacts(latest_summary.get("summary", ""))
                    if latest:
                        hint = f"*（玛丽亚回忆起之前的对话：{latest[:200]}）*"
                        contexts.append({"role": "assistant", "content": hint})

            if contexts:
                req.contexts = contexts + existing_contexts
            elif skip_plugin_history:
                req.contexts = existing_contexts

        if hasattr(self, "_record_live_observation"):
            self._record_live_observation(
                user_id=user_id,
                event=event,
                req=req,
                state=state,
                scene_policy=scene_policy,
                existing_context_count=len(existing_contexts),
                skip_plugin_history=skip_plugin_history,
            )

        self._log_perf(
            "inject_contexts",
            contexts_started_at,
            user_id,
            extra=f"contexts={len(self._coerce_runtime_list_value(getattr(req, 'contexts', [])))}",
            threshold_ms=5.0,
        )
        self._apply_request_temperature(req, state=state)


