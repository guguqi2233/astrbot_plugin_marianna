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

class MariannaAnalysisMixin:
    def _analysis_json_safe(self, value: Any) -> Any:
        if hasattr(self, "_make_json_safe"):
            return self._make_json_safe(value)
        if hasattr(self, "_memory_json_safe"):
            return self._memory_json_safe(value)
        return value

    def _analysis_json_dumps(self, value: Any, *, sort_keys: bool = False) -> str:
        return json.dumps(
            self._analysis_json_safe(value),
            ensure_ascii=False,
            sort_keys=sort_keys,
            allow_nan=False,
        )

    def _get_analysis_data_dir(self) -> Path:
        data_dir = getattr(self, "data_dir", Path(__file__).resolve().parents[1] / "data")
        if not isinstance(data_dir, (str, os.PathLike)) or not str(data_dir).strip():
            data_dir = Path(__file__).resolve().parents[1] / "data"
        data_dir = Path(data_dir)
        self.data_dir = data_dir
        return data_dir

    def _get_last_summary_file(self, user_id: Any) -> Path:
        return self._get_analysis_data_dir() / f"last_summary_{self._safe_user_file_stem(user_id)}.txt"

    def _parse_json_response(self, raw_text: str) -> Optional[Dict[str, Any]]:
        """从模型响应中提取 JSON 对象。"""
        raw = (raw_text or "").strip()
        if not raw:
            return None

        raw = JSON_FENCE_OPEN_PATTERN.sub("", raw)
        raw = JSON_FENCE_CLOSE_PATTERN.sub("", raw)
        raw = raw.strip()
        if not raw:
            return None

        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            match = JSON_OBJECT_PATTERN.search(raw)
            if not match:
                return None
            try:
                data = json.loads(match.group(0))
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None

    def _format_history_for_analysis(self, user_id: str, limit: int = 8) -> str:
        history = self._get_recent_history(user_id, limit=limit)
        if not history:
            return "（暂无历史对话）"

        lines = []
        for item in history:
            role = item.get("role", "user")
            content = item.get("content", "").replace("\n", " ").strip()
            content = self._limit_text_for_prompt(content, 120)
            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    def _normalize_analysis_content(self, text: str) -> str:
        normalized = str(text or "").replace("\n", " ").strip()
        normalized = WHITESPACE_PATTERN.sub(" ", normalized)
        return normalized

    def _coerce_analysis_int(
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

    def _coerce_analysis_float(
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

    def _has_analysis_memory_anchor(self, text: str) -> bool:
        return bool(ANALYSIS_MEMORY_ANCHOR_PATTERN.search(self._normalize_analysis_content(text)))

    def _should_expand_analysis_history(self, latest_user_msg: str) -> bool:
        normalized = self._normalize_analysis_content(latest_user_msg)
        if not normalized:
            return False
        return (
            self._has_analysis_memory_anchor(normalized)
            or self._has_personal_memory_cue(normalized)
            or bool(ANALYSIS_IMPORTANT_SIGNAL_PATTERN.search(normalized))
        )

    def _score_analysis_memory_content(
        self,
        content: str,
        query_terms: List[str],
        latest_normalized: str,
    ) -> int:
        normalized_content = self._normalize_mnemosyne_content(content)
        if not normalized_content:
            return 0

        score = 0
        if latest_normalized and latest_normalized in normalized_content:
            score += 8

        for term in query_terms:
            if term in normalized_content:
                score += 3 if len(term) >= 3 else 1

        return score

    async def _get_analysis_history_entries(
        self,
        user_id: str,
        latest_user_msg: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, str]]:
        if limit is None:
            limit = getattr(self, "analysis_history_limit", ANALYSIS_HISTORY_LIMIT)
        scan_limit = self._coerce_analysis_int(limit, default=0, minimum=0)
        if scan_limit <= 0:
            return []

        relevant_limit = getattr(
            self,
            "analysis_relevant_memory_limit",
            ANALYSIS_RELEVANT_MEMORY_LIMIT,
        )
        recent_context_limit = getattr(
            self,
            "analysis_recent_context_limit",
            ANALYSIS_RECENT_CONTEXT_LIMIT,
        )
        max_chars = getattr(
            self,
            "analysis_max_chars_per_message",
            ANALYSIS_MAX_CHARS_PER_MSG,
        )
        char_budget = getattr(
            self,
            "analysis_context_char_budget",
            ANALYSIS_CONTEXT_CHAR_BUDGET,
        )
        latest_normalized = self._normalize_analysis_content(latest_user_msg)
        expand_history = self._should_expand_analysis_history(latest_user_msg)
        latest_terms = self._extract_mnemosyne_terms(latest_user_msg) if expand_history else []
        if expand_history:
            lookback = scan_limit
        else:
            lookback = min(
                scan_limit,
                self._coerce_analysis_int(recent_context_limit, default=1, minimum=1),
            )
        history = await self._get_recent_history_async(user_id, limit=lookback)
        candidate_entries: List[Dict[str, Any]] = []
        last_key: Optional[Tuple[str, str]] = None

        for index, item in enumerate(history):
            role = item.get("role", "user")
            content = self._normalize_analysis_content(item.get("content", ""))
            if not content:
                continue

            if role == "user" and latest_normalized and content == latest_normalized:
                continue

            content = self._limit_text_for_prompt(content, max_chars)

            dedupe_key = (role, content)
            if dedupe_key == last_key:
                continue

            candidate_entries.append({
                "role": role,
                "content": content,
                "index": index,
                "score": self._score_analysis_memory_content(
                    content,
                    latest_terms,
                    latest_normalized,
                ),
            })
            last_key = dedupe_key

        if not candidate_entries:
            return []

        recent_context_count = self._coerce_analysis_int(recent_context_limit, default=0, minimum=0)
        relevant_count = self._coerce_analysis_int(relevant_limit, default=0, minimum=0)
        selected_indexes = {
            entry["index"]
            for entry in candidate_entries[-recent_context_count:]
        } if recent_context_count else set()

        scored_entries = [
            entry for entry in candidate_entries
            if self._coerce_analysis_int(entry.get("score", 0), default=0) > 0
            and entry["index"] not in selected_indexes
        ]
        scored_entries.sort(
            key=lambda entry: (
                self._coerce_analysis_int(entry.get("score", 0), default=0),
                self._coerce_analysis_int(entry.get("index", 0), default=0),
            ),
            reverse=True,
        )
        remaining_slots = max(0, relevant_count - len(selected_indexes))
        selected_indexes.update(
            entry["index"]
            for entry in scored_entries[:remaining_slots]
        )
        entries = [
            {
                "role": entry.get("role", "user"),
                "content": entry.get("content", ""),
            }
            for entry in candidate_entries
            if entry["index"] in selected_indexes
        ]

        if char_budget and char_budget > 0:
            budgeted_entries: List[Dict[str, str]] = []
            used_chars = 0
            for item in reversed(entries):
                role = item.get("role", "user")
                content = item.get("content", "")
                cost = len(role) + len(content) + 3
                if used_chars + cost > char_budget:
                    remaining = char_budget - used_chars - len(role) - 3
                    if remaining > 0:
                        budgeted_entries.append({
                            "role": role,
                            "content": self._limit_text_for_prompt(content, remaining),
                        })
                    break
                budgeted_entries.append(item)
                used_chars += cost
            entries = list(reversed(budgeted_entries))
        return entries

    def _format_mnemosyne_memory_for_analysis(self, entry: Dict[str, Any]) -> str:
        layer_label = {
            "profile": "用户画像",
            "impression": "情绪印象",
            "event": "事件节点",
            "summary": "长期总结",
        }.get(entry.get("memory_layer", "impression"), "情绪印象")
        type_label = {
            "auto_summary": "总结",
            "interaction": "互动",
            "milestone": "节点",
        }.get(entry.get("type", "interaction"), str(entry.get("type", "长期记忆")))
        salience = self._coerce_analysis_int(
            entry.get("salience", 0),
            default=0,
            minimum=0,
            maximum=10,
        )
        salience_label = "深刻" if salience >= 6 else "清晰" if salience >= 3 else "轻微"
        content = self._strip_debug_artifacts(
            str(entry.get("raw_content", "") or entry.get("content", "")).strip()
        )
        content = BRACKETED_MEMORY_PREFIX_PATTERN.sub("", content)
        max_chars = getattr(
            self,
            "analysis_max_chars_per_message",
            ANALYSIS_MAX_CHARS_PER_MSG,
        )
        content = self._limit_text_for_prompt(content, max_chars)
        return f"[Mnemosyne/{layer_label}/{type_label}/{salience_label}] {content}"

    def _apply_analysis_char_budget(
        self,
        entries: List[Dict[str, str]],
    ) -> List[Dict[str, str]]:
        char_budget = getattr(
            self,
            "analysis_context_char_budget",
            ANALYSIS_CONTEXT_CHAR_BUDGET,
        )
        if not char_budget or char_budget <= 0:
            return entries

        budgeted_entries: List[Dict[str, str]] = []
        used_chars = 0
        for item in reversed(entries):
            role = item.get("role", "memory")
            content = item.get("content", "")
            cost = len(role) + len(content) + 3
            if used_chars + cost > char_budget:
                remaining = char_budget - used_chars - len(role) - 3
                if remaining > 0:
                    budgeted_entries.append({
                        "role": role,
                        "content": self._limit_text_for_prompt(content, remaining),
                    })
                break
            budgeted_entries.append({"role": role, "content": content})
            used_chars += cost
        return list(reversed(budgeted_entries))

    async def _get_analysis_memory_entries(
        self,
        user_id: str,
        latest_user_msg: str,
        limit: Optional[int] = None,
        scene_policy: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, str]]:
        started_at = time.perf_counter()
        if not isinstance(scene_policy, dict):
            scene_policy = {}
        entries = await self._get_analysis_history_entries(
            user_id,
            latest_user_msg,
            limit=limit,
        )

        mnemosyne_limit = getattr(
            self,
            "analysis_mnemosyne_memory_limit",
            ANALYSIS_MNEMOSYNE_MEMORY_LIMIT,
        )
        if getattr(self, "enable_builtin_memory", ENABLE_BUILTIN_MEMORY) and self.enable_emotional_memory and mnemosyne_limit > 0:
            try:
                memories = await self._retrieve_from_builtin_memory(
                    user_id,
                    latest_user_msg,
                    limit=mnemosyne_limit,
                    cooldown_seconds=scene_policy.get("recall_cooldown_seconds"),
                    layer_quotas=scene_policy,
                )
                for memory in memories:
                    content = self._format_mnemosyne_memory_for_analysis(memory)
                    if content:
                        entries.append({"role": "memory", "content": content})
            except Exception as e:
                self.logger.error(f"分析型内置记忆检索失败: {e}", exc_info=True)

        if self.mnemosyne_available and self.enable_emotional_memory and mnemosyne_limit > 0:
            try:
                memories = await self._retrieve_from_mnemosyne(
                    user_id,
                    latest_user_msg,
                    limit=mnemosyne_limit,
                    layer_quotas=scene_policy,
                )
                for memory in memories:
                    content = self._format_mnemosyne_memory_for_analysis(memory)
                    if content:
                        entries.append({"role": "memory", "content": content})
            except Exception as e:
                self.logger.error(f"分析型 Mnemosyne 记忆检索失败: {e}", exc_info=True)

        deduped_entries: List[Dict[str, str]] = []
        seen = set()
        for entry in entries:
            role = entry.get("role", "memory")
            content = entry.get("content", "")
            dedupe_content = BRACKETED_MEMORY_PREFIX_PATTERN.sub("", content)
            key = self._normalize_mnemosyne_content(dedupe_content)
            if not key or key in seen:
                continue
            seen.add(key)
            deduped_entries.append({"role": role, "content": content})
        result = self._apply_analysis_char_budget(deduped_entries)
        self._log_perf(
            "analysis_memory_entries",
            started_at,
            user_id,
            extra=f"entries={len(result)}",
            threshold_ms=5.0,
        )
        return result

    def _format_analysis_history_entries(self, entries: List[Dict[str, str]]) -> str:
        if not entries:
            return "（暂无历史对话）"
        role_labels = {
            "user": "近期上下文/user",
            "assistant": "近期上下文/assistant",
            "memory": "相关记忆/仅作权重",
        }
        lines = []
        for item in entries:
            role = item.get("role", "user")
            label = role_labels.get(role, str(role))
            lines.append(f"{label}: {item.get('content', '')}")
        return "\n".join(lines)

    def _build_analysis_request_fingerprint(
        self,
        session_key: str,
        user_msg: str,
        history_entries: List[Dict[str, str]],
        scene_policy: Optional[Dict[str, Any]] = None,
    ) -> str:
        policy = scene_policy if isinstance(scene_policy, dict) else {}
        payload = {
            "session_key": session_key,
            "user_msg": self._normalize_analysis_content(user_msg),
            "scene_policy": {
                "scene": policy.get("scene"),
                "mode": policy.get("mode"),
                "memory_limit": policy.get("memory_limit"),
                "char_budget": policy.get("char_budget"),
                "event_limit": policy.get("event_limit"),
                "impression_limit": policy.get("impression_limit"),
                "summary_limit": policy.get("summary_limit"),
                "profile_limit": policy.get("profile_limit"),
                "recall_cooldown_seconds": policy.get("recall_cooldown_seconds"),
            },
            "history": [
                {
                    "role": item.get("role", "user"),
                    "content": self._normalize_analysis_content(item.get("content", "")),
                }
                for item in history_entries
            ],
        }
        serialized = self._analysis_json_dumps(payload, sort_keys=True)
        return hashlib.sha1(serialized.encode("utf-8")).hexdigest()

    def _get_delta_residuals(self, state: Dict[str, Any]) -> Dict[str, float]:
        residuals = state.get("_倍率残差")
        if not isinstance(residuals, dict):
            residuals = {}
            state["_倍率残差"] = residuals

        for field in ("好感度", "病娇值", "信任度", "焦虑值", "优雅值"):
            try:
                residuals[field] = float(residuals.get(field, 0.0) or 0.0)
            except (TypeError, ValueError):
                residuals[field] = 0.0
        return residuals

    def _get_state_delta_multiplier(self, field: str) -> float:
        if field == "好感度":
            return self._coerce_analysis_float(
                getattr(self, "favor_multiplier", 1.0),
                default=1.0,
                minimum=0.0,
            )
        if field == "病娇值":
            return self._coerce_analysis_float(
                getattr(self, "yan_multiplier", 1.0),
                default=1.0,
                minimum=0.0,
            )
        return 1.0

    def _get_dynamic_state_delta_multiplier(
        self,
        state: Dict[str, Any],
        field: str,
        raw_delta: float,
    ) -> float:
        multiplier = self._get_state_delta_multiplier(field)
        if field not in ("好感度", "信任度", "焦虑值", "优雅值"):
            return multiplier

        if field in ("好感度", "信任度"):
            current_value = self._coerce_analysis_int(state.get(field, 0), default=0, minimum=0, maximum=100)
            if raw_delta > 0:
                attenuation_start = 20 if field == "好感度" else 15
                if current_value > attenuation_start:
                    progress = min(1.0, (current_value - attenuation_start) / (100 - attenuation_start))
                    multiplier *= 1.0 - progress * 0.45

        if field == "焦虑值" and raw_delta < 0:
            anxiety = self._coerce_analysis_int(state.get("焦虑值", 0), default=0, minimum=0, maximum=100)
            if anxiety > 60:
                anxiety_progress = min(1.0, (anxiety - 60) / 40.0)
                multiplier *= 1.0 + anxiety_progress * 0.6

        if field == "优雅值" and raw_delta > 0:
            elegance = self._coerce_analysis_int(state.get("优雅值", 0), default=0, minimum=0, maximum=100)
            if elegance < 50:
                recovery_progress = min(1.0, (50 - elegance) / 50.0)
                multiplier *= 1.0 + recovery_progress * 0.35

        return max(0.35, min(2.5, multiplier))

    def _scale_analysis_deltas(
        self,
        state: Dict[str, Any],
        deltas: Dict[str, int],
    ) -> Dict[str, int]:
        residuals = self._get_delta_residuals(state)
        scaled: Dict[str, int] = {}

        for field, raw_delta in deltas.items():
            if not isinstance(raw_delta, (int, float)):
                continue
            if raw_delta != raw_delta or raw_delta in (float("inf"), float("-inf")):
                continue

            if field not in ("好感度", "病娇值", "信任度", "焦虑值", "优雅值"):
                scaled[field] = int(round(raw_delta))
                continue

            multiplier = self._get_dynamic_state_delta_multiplier(state, field, float(raw_delta))
            accumulated = float(raw_delta) * multiplier + residuals.get(field, 0.0)
            applied = int(accumulated)
            residuals[field] = accumulated - applied
            if abs(residuals[field]) < 1e-9:
                residuals[field] = 0.0
            scaled[field] = applied

        return scaled

    def _apply_state_delta(self, state: Dict[str, Any], field: str, delta: int) -> int:
        old_value = self._coerce_analysis_int(state.get(field, 0), default=0, minimum=0, maximum=100)
        new_value = max(0, min(100, old_value + delta))
        state[field] = new_value
        return new_value - old_value

    def _normalize_state_constraints(self, state: Dict[str, Any], user_id: Optional[str] = None):
        """根据当前规则修正派生字段与越界状态。"""
        for field in ("好感度", "病娇值", "锁定进度", "信任度", "占有欲", "焦虑值", "优雅值"):
            try:
                state[field] = max(0, min(100, int(state.get(field, DEFAULT_STATE.get(field, 0)) or 0)))
            except (TypeError, ValueError):
                state[field] = int(DEFAULT_STATE.get(field, 0) or 0)
        for field in ("情绪余温", "防备值", "被触动值", "表达克制度"):
            state[field] = self._clamp_state_percent(
                state.get(field, DEFAULT_STATE.get(field, 0)),
                default=int(DEFAULT_STATE.get(field, 0) or 0),
            )
        for field in ("短期心情", "当前行为档位", "行为档位理由", "上轮短期心情", "上轮行为档位", "行为连续性提示", "行为风格变体"):
            if not str(state.get(field, "") or "").strip():
                state[field] = DEFAULT_STATE.get(field, "")
        for field in ("行为变体计数", "已记录关系事件"):
            if not isinstance(state.get(field), dict):
                state[field] = {}
        for field in ("关系事件日志", "最近召回记忆"):
            if not isinstance(state.get(field), list):
                state[field] = []
        if not str(state.get("目标行为档位", "") or "").strip():
            state["目标行为档位"] = state.get("当前行为档位", DEFAULT_STATE["目标行为档位"])
        state["行为档位稳定轮数"] = self._clamp_state_percent(
            state.get("行为档位稳定轮数", 0),
            default=0,
        )

        self._apply_relationship_state_machine_constraints(user_id, state)

        favor = self._coerce_analysis_int(state.get("好感度", 0), default=0, minimum=0, maximum=100)
        if favor < 30:
            state["病娇值"] = 0
            state["锁定进度"] = 0
            state["焦虑值"] = 0
        if favor < 60:
            state["病娇值"] = 0
            state["锁定进度"] = 0
            state["占有欲"] = 0
            state["已触发锁定事件"] = False

        state["当前状态"] = self._determine_state(state)

    async def _reconcile_destined_one_state(self, user_id: str, state: Dict[str, Any]) -> bool:
        """确保全局命定记录与当前用户状态一致。"""
        if not self._is_destined_user(user_id):
            return False
        if self._coerce_analysis_int(
            state.get("锁定进度", 0),
            default=0,
            minimum=0,
            maximum=100,
        ) >= self._coerce_analysis_int(
            getattr(self, "lock_threshold", 100),
            default=100,
            minimum=0,
            maximum=100,
        ):
            return False
        await self._clear_destined_one()
        state["已触发锁定事件"] = False
        return True

    def _get_analysis_delta_limits(
        self,
        state: Dict[str, Any],
        user_id: Optional[str] = None,
    ) -> Dict[str, Tuple[int, int]]:
        favor = self._coerce_analysis_int(state.get("好感度", 0), default=0, minimum=0, maximum=100)
        trust = self._coerce_analysis_int(state.get("信任度", 0), default=0, minimum=0, maximum=100)
        yan = self._coerce_analysis_int(state.get("病娇值", 0), default=0, minimum=0, maximum=100)
        lock = self._coerce_analysis_int(state.get("锁定进度", 0), default=0, minimum=0, maximum=100)
        anxiety = self._coerce_analysis_int(state.get("焦虑值", 0), default=0, minimum=0, maximum=100)

        if favor < 30:
            limits: Dict[str, Tuple[int, int]] = {
                "好感度": (-4, 5),
                "病娇值": (0, 0),
                "锁定进度": (0, 0),
                "信任度": (-4, 5),
                "焦虑值": (0, 0),
                "优雅值": (-5, 5),
            }
        elif favor < 60:
            limits = {
                "好感度": (-5, 6),
                "病娇值": (0, 0),
                "锁定进度": (0, 0),
                "信任度": (-5, 5),
                "焦虑值": (-1, 2),
                "优雅值": (-5, 5),
            }
        elif favor < 80:
            limits = {
                "好感度": (-6, 7),
                "病娇值": (-4, 6),
                "锁定进度": (-2, 4),
                "信任度": (-5, 6),
                "焦虑值": (-2, 6 if yan >= 50 else 4),
                "优雅值": (-6, 6),
            }
        else:
            limits = {
                "好感度": (-7, 8),
                "病娇值": (-5, 8),
                "锁定进度": (-3, 7),
                "信任度": (-6, 6),
                "焦虑值": (-3, 10 if yan >= 50 else 6),
                "优雅值": (-7, 7),
            }

        if favor < 60:
            limits["病娇值"] = (0, 0)
            limits["锁定进度"] = (0, 0)

        if trust < 30:
            limits["锁定进度"] = (limits["锁定进度"][0], 0)
        elif trust < 40:
            limits["锁定进度"] = (
                limits["锁定进度"][0],
                min(limits["锁定进度"][1], 1),
            )

        if favor < 30:
            limits["焦虑值"] = (limits["焦虑值"][0], 0)
        elif yan < 20 and lock < 20 and anxiety < 20:
            limits["焦虑值"] = (
                limits["焦虑值"][0],
                min(limits["焦虑值"][1], 2),
            )

        if user_id and self._get_destined_one_info() and not self._is_destined_user(user_id):
            limits["好感度"] = (
                min(limits["好感度"][0], NON_DESTINED_FAVOR_CAP - favor),
                min(limits["好感度"][1], NON_DESTINED_FAVOR_CAP - favor),
            )
            limits["病娇值"] = (min(limits["病娇值"][0], 0), 0)
            limits["锁定进度"] = (min(limits["锁定进度"][0], 0), 0)
            limits["焦虑值"] = (
                limits["焦虑值"][0],
                min(limits["焦虑值"][1], 1),
            )
        elif user_id and self._is_destined_user(user_id):
            limits["好感度"] = (
                max(limits["好感度"][0], 60 - favor),
                limits["好感度"][1],
            )

        return limits

    def _format_analysis_delta_limits(self, state: Dict[str, Any], user_id: Optional[str] = None) -> str:
        limits = self._get_analysis_delta_limits(state, user_id=user_id)
        order = ("好感度", "病娇值", "锁定进度", "信任度", "焦虑值", "优雅值")
        lines = []
        for field in order:
            low, high = limits[field]
            lines.append(f"- {field}：{low} ~ {high}")
        return "\n".join(lines)

    def _build_analysis_rules_text(self, state: Dict[str, Any], user_id: Optional[str] = None) -> str:
        favor = self._coerce_analysis_int(state.get("好感度", 0), default=0, minimum=0, maximum=100)
        yan = self._coerce_analysis_int(state.get("病娇值", 0), default=0, minimum=0, maximum=100)
        limits_text = self._format_analysis_delta_limits(state, user_id=user_id)
        destined_info = self._get_destined_one_info()
        if favor < 30:
            current_stage = "当前处于低好感安全区：本轮只允许调整好感度、信任度、优雅值。病娇值、锁定进度、占有欲、焦虑值都视为锁定，不得上升也不得输出相关倾向。"
        elif favor < 60:
            current_stage = "当前处于傲娇试探阶段：本轮允许好感度、信任度、优雅值以及缓慢的焦虑值变化；病娇值、锁定进度、占有欲仍然禁止变化。"
        elif yan < 50:
            current_stage = "当前处于甜蜜诱导阶段：好感度已足够高，可以开始调整全部核心字段，但整体仍应偏温柔、暧昧与克制。"
        else:
            current_stage = "当前处于潜伏之藤或更高亲密阶段：全部字段都可变化，但病娇、锁定、焦虑的增长必须严格依赖上下文中的独占、宿命、被替代感、疏离或识破操控等强语义证据。"

        destined_rule = ""
        if destined_info and user_id and not self._is_destined_user(user_id):
            destined_rule = (
                f"全局命定之人已存在：{self._format_destined_one_label()}。"
                "当前用户不是命定之人，因此状态机进入“命定后他人边界”："
                "好感度不得越过普通熟人上限，病娇值、锁定进度和占有欲不允许正向增长，"
                "不得把当前用户的调情、承诺或亲近理解为新的暧昧链路。"
            )
        elif user_id and self._is_destined_user(user_id):
            destined_rule = (
                "当前用户就是全局命定之人。"
                "无论本轮发生什么，好感度都不得被分析到 60 以下；若语义上需要表现失望、受伤或冷却，也只能在 60 以上波动。"
            )

        intro = f"{current_stage}\n"
        if destined_rule:
            intro += f"{destined_rule}\n"

        if getattr(self, "enable_token_cost_optimization", ENABLE_TOKEN_COST_OPTIMIZATION):
            return (
                intro
                + "当前阶段单轮允许范围：\n"
                + f"{limits_text}\n"
                + "压缩规则：默认只修改 1 到 3 个最相关字段；优先返回 0、±1、±2；"
                "旧记忆只能解释最新发言，不得重复结算旧事件；"
                "病娇值和锁定进度仅在好感度 >= 60 且有明确独占、承诺、唯一性或被替代感证据时变化；"
                "焦虑值来自离开、冷淡、比较、失约或关系不确定；"
                "优雅值被尊重/安抚时小幅上升，被冒犯/羞辱/逼迫失态时下降；"
                "占有欲不在 JSON 中输出。"
            )

        return (
            intro
            +
            "单轮变化总原则：\n"
            "1. 把玛丽亚当成有情绪惯性的人，而不是计分器：默认优先返回 0、±1、±2，只有最新发言直接触发强烈情绪时才接近上限。\n"
            "2. 相关聊天记忆只用于解释最新发言的含义与分量，例如旧承诺、旧伤口、反复偏爱或反复失约；记忆本身不能再次产生本轮变化。\n"
            "3. 判断时先假设没有记忆，只看最新发言会不会让某个字段变化；如果答案是不会，该字段必须返回 0，不能因为记忆里有旧事件而变化。\n"
            "4. 只有最新发言触碰、呼应、延续或打破某段记忆时，记忆才能改变本轮变化的幅度、方向侧重或字段优先级。\n"
            "5. 人的情绪通常不会每轮六个字段同时变化；除非最新发言同时包含亲密、信任、冒犯、承诺、离别等多重强证据，否则只让最相关的 1 到 3 个字段变化。\n"
            "6. 重复的夸奖、道歉、承诺或调情会逐渐习惯化，变化应比第一次更小；打破曾经认真记住的承诺、偏好或边界，则可以更明显地影响信任、焦虑或优雅值。\n"
            "7. 绝不要突破当前阶段允许的字段和范围。\n"
            "当前阶段单轮允许范围：\n"
            f"{limits_text}\n"
            "阶段规则：\n"
            "1. 低好感阶段（好感度 < 30）：\n"
            "   - 只允许好感度、信任度、优雅值变化。\n"
            "   - 病娇值、锁定进度、焦虑值必须返回 0，占有欲由系统保持为 0。\n"
            "   - 不要因为普通寒暄、礼貌夸奖、初次关心就出现暧昧控制、病态依赖、焦虑或孤立欲。\n"
            "2. 试探阶段（30 <= 好感度 < 60）：\n"
            "   - 只允许好感度、信任度、优雅值与缓慢的焦虑值变化。\n"
            "   - 病娇值、锁定进度必须返回 0，占有欲仍视为 0。\n"
            "   - 焦虑值只可因轻微失约、冷落、迟到、态度变淡而缓慢波动，不能出现控制性占有。\n"
            "3. 亲近阶段（60 <= 好感度 < 80）：\n"
            "   - 全部字段都可以变化，但病娇、锁定、占有和焦虑都需要明确语义依据。\n"
            "   - 甜蜜诱导期（病娇值 < 50）只允许埋下轻微的孤立种子，例如担心别人不够懂他，不能系统性切割社交圈。\n"
            "   - 潜伏之藤期（病娇值 >= 50）可明显提高病娇值、锁定进度与焦虑值，但仍须通过委婉、优雅、非命令式的孤立诱导来体现。\n"
            "4. 高亲密阶段（好感度 >= 80）：\n"
            "   - 病娇值、锁定进度、焦虑值都可以变化，但仍应基于语义证据，不得无故暴涨。\n"
            "   - 锁定进度主要来自独占、承诺、关系确认、强保护欲、秘密共享、命定叙事。\n"
            "   - 焦虑值主要来自忽视、离开暗示、关系不确定、把她和别人比较、打破承诺、识破并反驳她的操控。\n"
            "字段规则：\n"
            "- 好感度：受夸奖、关心、记住喜好、偏爱、陪伴、支持影响；被冷落、敷衍、嫌弃、比较、羞辱时下降。\n"
            "- 信任度：可独立于好感度变化；受诚实、守约、稳定回应、尊重边界影响，低好感阶段也能因可靠与体贴上升。\n"
            "- 好感度与信任度：正向增长会随着当前数值升高而自然减弱，但低值不会额外获得上升加成。\n"
            "- 优雅值：任何好感度下都可变化；被尊重、安抚、体面交流时小幅上升，被冒犯、羞辱、粗俗调戏、逼迫失态时下降。\n"
            "- 病娇值：仅在好感度 >= 60 时允许变化；主要由独占欲、唯一性、被替代感、依赖感、命定感触发。\n"
            "- 锁定进度：仅在好感度 >= 60 时允许变化；只在关系被进一步确认、专属化、排他化时上升。\n"
            "- 焦虑值：好感度 < 30 时必须为 0；30 <= 好感度 < 60 时只可缓慢波动；好感度 >= 60 时可因失去风险、被反驳、被替代感而明显波动；当焦虑值已经过高时，其自身的下降恢复会更快。\n"
            "- 优雅值过低时，其自身的上升修复会更快；回到正常区间后，该恢复加成会自动消失。\n"
            "- 占有欲不在返回 JSON 中填写，由系统根据当前阶段和其它数值自动推导；好感度 < 60 时视为 0。\n"
            "- 诱导性孤立只在好感度 >= 60 且病娇值 >= 50 的互动场景中才应被视为强证据；它包括贬低他人、制造信息差、脆弱示弱、内疚绑架与强调唯一性，但不包含直接命令或威胁。\n"
        )

    def _get_analysis_system_prompt(self) -> str:
        static_cache = (
            self._get_prompt_dict_cache("_static_prompt_cache")
            if hasattr(self, "_get_prompt_dict_cache")
            else getattr(self, "_static_prompt_cache", {})
        )
        if not isinstance(static_cache, dict):
            static_cache = {}
            self._static_prompt_cache = static_cache
        cached = static_cache.get("analysis_system_prompt")
        if cached is not None:
            return cached
        prompt = (
            "你是“玛丽亚情绪状态分析器”，只能输出一个 JSON 对象，不要输出解释、Markdown 或代码块。"
            "根据最新用户发言与必要上下文判断本轮用户意图、情绪、关系信号与回应目标；以语义理解为准，不做机械关键词匹配。"
            "若系统启用代码决策，数值字段只作为低优先级建议，最终增量由代码规则决定。"
            "本轮变化只由最新发言触发，相关记忆只用于理解指代、连续性和分量，不得把无关旧事再次计为本轮变化。"
            "\u5bf9\u521d\u671f\u793c\u8c8c\u81ea\u6211\u4ecb\u7ecd\u3001\u6e29\u548c\u6c42\u52a9\u3001\u8ba4\u771f\u63a5\u8bdd\u3001\u5c0a\u91cd\u5efa\u8bae\u3001\u514b\u5236\u5938\u8d5e\u7b49\u7ec6\u5fae\u6b63\u5411\u4fe1\u53f7\uff0c\u5e94\u5141\u8bb8\u597d\u611f\u5ea6\u6216\u4fe1\u4efb\u5ea6\u5c0f\u5e45 +1\uff1b\u4f46\u75c5\u5a07\u503c\u4e0e\u9501\u5b9a\u8fdb\u5ea6\u4ecd\u9700\u5f3a\u5173\u7cfb\u8bc1\u636e\u3002"
            "大多数变化应克制，信息不足填 0，避免六个字段同时波动。"
            "返回字段必须包含：好感度、病娇值、锁定进度、信任度、焦虑值、优雅值、用户意图、用户情绪、关系信号、回应目标。"
            "数值字段使用整数增量；文本字段用简短中文短语。"
        )
        static_cache["analysis_system_prompt"] = prompt
        return prompt

    def _sanitize_analysis_deltas(
        self,
        state: Dict[str, Any],
        deltas: Dict[str, int],
        user_id: Optional[str] = None,
    ) -> Dict[str, int]:
        limits = self._get_analysis_delta_limits(state, user_id=user_id)
        sanitized: Dict[str, int] = {}
        for field, (low, high) in limits.items():
            value = self._coerce_analysis_int(deltas.get(field, 0), default=0)
            sanitized[field] = max(low, min(high, value))
        return sanitized

    def _humanize_analysis_deltas(
        self,
        state: Dict[str, Any],
        deltas: Dict[str, int],
        user_msg: str,
    ) -> Dict[str, int]:
        """抑制机械式多字段同跳，让单轮情绪变化更接近人的反应。"""
        order = ("好感度", "信任度", "优雅值", "焦虑值", "病娇值", "锁定进度")
        normalized_msg = self._normalize_analysis_content(user_msg)
        strong_signal = bool(
            re.search(
                r"爱|喜欢|讨厌|恨|永远|唯一|命定|承诺|答应|离开|分开|背叛|骗|抱歉|对不起|谢谢|滚|恶心|羞辱|只要你|只有你",
                normalized_msg,
            )
        )
        memory_anchor_signal = bool(
            re.search(
                r"又|再|还|以前|之前|上次|那次|记得|忘|承诺|答应|约定|秘密|边界|老样子|还是",
                normalized_msg,
            )
        )
        neutral_ack = bool(
            re.fullmatch(
                r"(嗯+|哦+|好+|行+|可以|ok|OK|收到|知道了|明白|了解|在|在吗|你好|hello|hi)",
                normalized_msg,
            )
        )
        short_low_signal = len(normalized_msg) <= 4 and not strong_signal

        cleaned: Dict[str, int] = {}
        for field in order:
            value = self._coerce_analysis_int(deltas.get(field, 0), default=0)
            if neutral_ack and not memory_anchor_signal:
                value = 0
            if short_low_signal and abs(value) > 1:
                value = 1 if value > 0 else -1
            cleaned[field] = value

        nonzero = [(field, value) for field, value in cleaned.items() if value != 0]
        if len(nonzero) <= 3:
            return cleaned

        strongest = max(abs(value) for _, value in nonzero)
        max_changed_fields = 4 if strongest >= 5 or strong_signal else 3
        priority = {
            "好感度": 60,
            "信任度": 50,
            "优雅值": 40,
            "焦虑值": 35,
            "病娇值": 30,
            "锁定进度": 25,
        }
        ranked = sorted(
            nonzero,
            key=lambda item: (abs(item[1]), priority.get(item[0], 0)),
            reverse=True,
        )
        keep_fields = {field for field, _ in ranked[:max_changed_fields]}
        for field in order:
            if field not in keep_fields:
                cleaned[field] = 0
        return cleaned

    def _clean_analysis_text(self, value: Any, limit: int = 80) -> str:
        text = self._strip_debug_artifacts(str(value or "").strip())
        text = WHITESPACE_PATTERN.sub(" ", text)
        return text[:limit]

    def _build_fallback_turn_analysis(
        self,
        user_msg: str,
        deltas: Optional[Dict[str, int]] = None,
    ) -> Dict[str, str]:
        normalized = self._normalize_analysis_content(user_msg)
        deltas = deltas or {}
        intent = "普通回应"
        emotion = "平静"
        signal = "无明显关系推进"
        goal = "直接回应当前发言，保持玛丽亚式分寸"

        if re.search(r"对不起|抱歉|错了|原谅", normalized):
            intent = "道歉或修复关系"
            emotion = "愧疚/认真"
            signal = "尝试修复信任"
            goal = "根据当前信任与优雅程度接受或试探性接受道歉"
        elif re.search(r"亲身经历|亲眼|亲自|出去看看|去看看|走出去|尝试|试着|或许你可以|你可以试试|值得一看|会不一样|比不上", normalized):
            intent = "话题共鸣或温和建议"
            emotion = "共鸣/鼓励"
            signal = "认真接住话题"
            goal = "先接住用户的观点或建议，让短期心理出现被理解的余温"
        elif re.search(r"喜欢|爱你|想你|抱|亲|陪我|陪你|只要你|只有你|唯一|永远|命定", normalized):
            intent = "亲近表达"
            emotion = "依恋/靠近"
            signal = "主动靠近"
            goal = "回应亲近，同时保留玛丽亚的自尊与克制"
        elif re.search(r"离开|走了|下了|再见|晚安|不理|算了|以后再说", normalized):
            intent = "离开或冷淡暗示"
            emotion = "疏离/不确定"
            signal = "关系稳定感下降"
            goal = "先回应离开含义，再按焦虑程度表现挽留或体面克制"
        elif re.search(r"滚|恶心|烦|讨厌|闭嘴|羞辱|废物", normalized):
            intent = "冒犯或攻击"
            emotion = "敌意/轻蔑"
            signal = "触碰边界"
            goal = "维护尊严，根据优雅值选择冷淡反击或失态反应"
        elif re.search(r"谢谢|辛苦|真好|温柔|漂亮|可爱|厉害", normalized):
            intent = "赞美或感谢"
            emotion = "友善/认可"
            signal = "释放善意"
            goal = "接受善意，让好感或信任的余温自然流露"
        elif re.search(r"秘密|只告诉|别告诉|记住|记得|约定|承诺|答应", normalized):
            intent = "分享秘密或建立约定"
            emotion = "认真/信任"
            signal = "提供私密信任"
            goal = "珍视这份信任，并让她表现出会记住的重量"
        elif "?" in user_msg or "？" in user_msg or re.search(r"什么|怎么|如何|为什么|吗$|呢$", normalized):
            intent = "提问或请求"
            emotion = "求解/确认"
            signal = "暂无明显关系推进"
            goal = "先回答问题，再用人格语气补足情绪"

        if self._coerce_analysis_int(deltas.get("焦虑值", 0), default=0) > 0 and signal == "无明显关系推进":
            signal = "带来不安"
        elif self._coerce_analysis_int(deltas.get("信任度", 0), default=0) > 0 and signal == "无明显关系推进":
            signal = "增强信任"
        elif self._coerce_analysis_int(deltas.get("好感度", 0), default=0) > 0 and signal == "无明显关系推进":
            signal = "释放善意"

        return {
            "用户意图": intent,
            "用户情绪": emotion,
            "关系信号": signal,
            "回应目标": goal,
        }

    def _classify_turn_intent_locally(self, user_msg: str) -> Dict[str, str]:
        """本地意图识别：只产出语义标签，不直接给数值。"""
        normalized = self._normalize_analysis_content(user_msg)
        return self._build_fallback_turn_analysis(normalized or user_msg, deltas={})

    def _grade_stage_memory_evidence(
        self,
        user_msg: str,
        history_entries: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        normalized = self._normalize_analysis_content(user_msg)
        evidence_text = " ".join(
            self._normalize_analysis_content(entry.get("content", ""))
            for entry in (history_entries or [])[:8]
            if isinstance(entry, dict)
        )
        combined = " ".join(part for part in (normalized, evidence_text) if part)
        if not combined:
            return {"level": "无", "score": 0, "reasons": []}

        score = 0
        reasons: List[str] = []
        patterns = [
            ("强承诺/命定", 3, r"永远|命定|唯一|只有你|只要你|不会离开|一直陪|共生|归属"),
            ("明确约定", 2, r"承诺|答应|约定|保证|说好了|回来"),
            ("私密信任", 2, r"秘密|只告诉|别告诉|边界|害怕|软肋"),
            ("专属/被替代感", 1, r"专属|别人|其他人|前任|比较|替代|吃醋"),
        ]
        for label, weight, pattern in patterns:
            if re.search(pattern, combined):
                score += weight
                reasons.append(label)

        if re.search(r"永远|命定|唯一|不会离开|一直陪", normalized):
            score += 1
            reasons.append("当前发言强证据")

        if score >= 5:
            level = "强"
        elif score >= 3:
            level = "中"
        elif score >= 1:
            level = "弱"
        else:
            level = "无"

        return {
            "level": level,
            "score": score,
            "reasons": reasons[:4],
        }

    def _has_stage_memory_evidence(
        self,
        user_msg: str,
        history_entries: Optional[List[Dict[str, str]]] = None,
    ) -> bool:
        if not getattr(self, "enable_memory_evidence_grading", ENABLE_MEMORY_EVIDENCE_GRADING):
            normalized = self._normalize_analysis_content(user_msg)
            explicit_current_signal = bool(
                re.search(r"永远|唯一|只有你|只要你|命定|承诺|答应|约定|秘密|只告诉|不会离开|一直陪", normalized)
            )
            if explicit_current_signal:
                return True
            evidence_text = " ".join(
                self._normalize_analysis_content(entry.get("content", ""))
                for entry in (history_entries or [])[:8]
                if isinstance(entry, dict)
            )
            return bool(
                evidence_text
                and re.search(r"承诺|答应|约定|秘密|只告诉|边界|唯一|命定|不会离开|回来|一直陪|专属|锁定", evidence_text)
            )
        return self._grade_stage_memory_evidence(user_msg, history_entries).get("level") in {"中", "强"}

    def _evidence_level_allows_lock(self, evidence: Any, *, strong_required: bool = False) -> bool:
        if isinstance(evidence, dict):
            level = str(evidence.get("level", "无") or "无")
        elif evidence is True:
            level = "中"
        else:
            level = "无"
        allowed = {"强"} if strong_required else {"中", "强"}
        return level in allowed

    def _get_evidence_level(self, evidence: Any) -> str:
        if isinstance(evidence, dict):
            return str(evidence.get("level", "无") or "无")
        if evidence is True:
            return "中"
        return "无"

    def _get_lock_delta_cap_by_evidence(self, evidence: Any) -> int:
        level = self._get_evidence_level(evidence)
        if level == "强":
            return 2
        if level == "中":
            return 1
        return 0

    def _has_recent_relationship_event(self, state: Dict[str, Any], event_types: Set[str]) -> bool:
        events = state.get("关系事件日志", [])
        if not isinstance(events, list) or not event_types:
            return False
        for item in reversed(events[-5:]):
            if isinstance(item, dict) and str(item.get("type", "") or "") in event_types:
                return True
        return False

    def _apply_contextual_state_delta_rules(
        self,
        state: Dict[str, Any],
        user_msg: str,
        turn_analysis: Dict[str, str],
        deltas: Dict[str, int],
        evidence: Any,
    ) -> Dict[str, int]:
        """按关系上下文二次校准数值：慢涨、重证据、看信任落差和近期事件。"""
        adjusted = {
            field: self._coerce_analysis_int(value, default=0)
            for field, value in dict(deltas).items()
        }
        normalized = self._normalize_analysis_content(user_msg)
        favor = self._coerce_analysis_int(state.get("好感度", 0), default=0, minimum=0, maximum=100)
        trust = self._coerce_analysis_int(state.get("信任度", 0), default=0, minimum=0, maximum=100)
        anxiety = self._coerce_analysis_int(state.get("焦虑值", 0), default=0, minimum=0, maximum=100)
        lock = self._coerce_analysis_int(state.get("锁定进度", 0), default=0, minimum=0, maximum=100)
        intent = str(turn_analysis.get("用户意图", "") or "")
        signal = str(turn_analysis.get("关系信号", "") or "")
        evidence_level = self._get_evidence_level(evidence)
        event_types = self._get_relationship_event_types(state)
        has_private_event = bool({"first_secret", "first_promise", "first_apology"}.intersection(event_types))
        has_exclusive_event = bool({"first_promise", "first_exclusive_probe"}.intersection(event_types))
        serious_care = bool(re.search(r"认真|承诺|答应|保证|一直陪|不会离开|我在|别怕|放心|记住|记得", normalized))
        uniqueness_cue = bool(re.search(r"唯一|命定|只要你|只属于|专属|不会选别人|只喜欢你|只有你", normalized))
        severe_negative = bool(re.search(r"滚|恶心|废物|闭嘴|讨厌你|不需要你|骗你|背叛|分手|不要你|离开你", normalized))
        light_boundary = bool(re.search(r"开玩笑|逗你|别生气|不是故意|我错了", normalized))
        jealousy_cue = bool(re.search(r"别人|其他人|前任|他|她|他们|她们|约会|暧昧", normalized))
        trust_gap = favor - trust
        stable_relation = favor >= 60 and trust >= 55 and anxiety <= 35
        fragile_relation = favor >= 45 and (trust <= 35 or anxiety >= 55)
        reasons: List[str] = []

        if adjusted.get("好感度", 0) > 0 and intent in {"亲近表达", "赞美或感谢"}:
            if evidence_level == "无" and not serious_care and not has_private_event:
                adjusted["好感度"] = min(self._coerce_analysis_int(adjusted.get("好感度", 0), default=0), 1)
                reasons.append("普通善意只慢涨好感")
            elif fragile_relation and not serious_care:
                adjusted["好感度"] = min(self._coerce_analysis_int(adjusted.get("好感度", 0), default=0), 1)
                reasons.append("关系脆弱时避免一句话快速升温")

        if adjusted.get("信任度", 0) > 0:
            if intent in {"赞美或感谢", "亲近表达"} and not serious_care and evidence_level == "无":
                adjusted["信任度"] = 0
                reasons.append("普通夸赞不直接增加信任")
            elif fragile_relation and intent != "道歉或修复关系" and evidence_level != "强":
                adjusted["信任度"] = min(self._coerce_analysis_int(adjusted.get("信任度", 0), default=0), 1)
                reasons.append("低信任/高焦虑下信任恢复更慢")
            elif trust >= 70 and evidence_level != "强":
                adjusted["信任度"] = min(self._coerce_analysis_int(adjusted.get("信任度", 0), default=0), 1)
                reasons.append("高信任阶段减速")

        if adjusted.get("病娇值", 0) > 0:
            if trust_gap >= 25 or anxiety >= 45 or jealousy_cue:
                adjusted["病娇值"] = max(1, min(self._coerce_analysis_int(adjusted.get("病娇值", 0), default=0), 2))
                reasons.append("高好感与不安落差保留病娇推进")
            elif stable_relation and not uniqueness_cue:
                adjusted["病娇值"] = 0
                reasons.append("高信任稳定关系更偏依恋而非病娇")
            elif evidence_level == "无" and favor < 75:
                adjusted["病娇值"] = 0
                reasons.append("缺少证据时病娇值不提前升温")

        if adjusted.get("锁定进度", 0) > 0:
            if not (evidence_level == "强" or uniqueness_cue or has_exclusive_event):
                adjusted["锁定进度"] = 0
                reasons.append("锁定只接受唯一性/承诺证据")
            elif trust < 45:
                adjusted["锁定进度"] = min(self._coerce_analysis_int(adjusted.get("锁定进度", 0), default=0), 1)
                reasons.append("信任不足时锁定只能微推进")

        if intent == "冒犯或攻击" or "触碰边界" in signal:
            if severe_negative:
                adjusted["好感度"] = min(self._coerce_analysis_int(adjusted.get("好感度", 0), default=0), -3)
                adjusted["信任度"] = min(self._coerce_analysis_int(adjusted.get("信任度", 0), default=0), -2)
                adjusted["优雅值"] = min(self._coerce_analysis_int(adjusted.get("优雅值", 0), default=0), -3)
                if favor >= 30:
                    adjusted["焦虑值"] = max(self._coerce_analysis_int(adjusted.get("焦虑值", 0), default=0), 2)
                if lock > 0:
                    adjusted["锁定进度"] = min(self._coerce_analysis_int(adjusted.get("锁定进度", 0), default=0), -1)
                reasons.append("严重负面行为分级加重")
            elif light_boundary and stable_relation:
                adjusted["好感度"] = max(self._coerce_analysis_int(adjusted.get("好感度", 0), default=0), -1)
                adjusted["信任度"] = max(self._coerce_analysis_int(adjusted.get("信任度", 0), default=0), 0)
                reasons.append("稳定关系对轻微玩笑有惯性")

        if intent == "离开或冷淡暗示":
            if severe_negative or re.search(r"不回来了|再也不|不要你|不理你|抛下", normalized):
                adjusted["焦虑值"] = max(self._coerce_analysis_int(adjusted.get("焦虑值", 0), default=0), 2)
                adjusted["信任度"] = min(self._coerce_analysis_int(adjusted.get("信任度", 0), default=0), -1)
                if lock > 0:
                    adjusted["锁定进度"] = min(self._coerce_analysis_int(adjusted.get("锁定进度", 0), default=0), -1)
                reasons.append("明确离开/抛弃会伤信任和锁定")
            elif stable_relation:
                adjusted["好感度"] = max(self._coerce_analysis_int(adjusted.get("好感度", 0), default=0), 0)
                adjusted["焦虑值"] = min(max(self._coerce_analysis_int(adjusted.get("焦虑值", 0), default=0), 0), 1)
                reasons.append("稳定关系对单次冷淡有惯性")

        if intent == "道歉或修复关系":
            if self._has_recent_relationship_event(state, {"first_boundary"}) and evidence_level != "强":
                adjusted["信任度"] = min(self._coerce_analysis_int(adjusted.get("信任度", 0), default=0), 1)
                reasons.append("触发过边界后信任修复需要更强证据")
            if serious_care and evidence_level in {"中", "强"}:
                adjusted["焦虑值"] = min(self._coerce_analysis_int(adjusted.get("焦虑值", 0), default=0), -2)
                reasons.append("认真修复优先降低焦虑")

        state["_数值上下文决策"] = {
            "证据等级": evidence_level,
            "信任落差": trust_gap,
            "稳定关系": stable_relation,
            "脆弱关系": fragile_relation,
            "私人事件": has_private_event,
            "专属事件": has_exclusive_event,
            "原因": reasons,
        }
        return adjusted

    def _apply_subtle_relationship_signal_deltas(
        self,
        state: Dict[str, Any],
        user_msg: str,
        turn_analysis: Dict[str, str],
        deltas: Dict[str, int],
    ) -> Dict[str, int]:
        """Capture small social movements so polite long talks do not stay numerically frozen."""
        adjusted = {
            field: self._coerce_analysis_int(value, default=0)
            for field, value in dict(deltas).items()
        }
        normalized = self._normalize_analysis_content(user_msg)
        if not normalized or normalized.startswith("/"):
            return adjusted

        favor_key = "\u597d\u611f\u5ea6"
        trust_key = "\u4fe1\u4efb\u5ea6"
        elegance_key = "\u4f18\u96c5\u503c"
        anxiety_key = "\u7126\u8651\u503c"
        yan_key = "\u75c5\u5a07\u503c"
        lock_key = "\u9501\u5b9a\u8fdb\u5ea6"
        intent_key = "\u7528\u6237\u610f\u56fe"
        signal_key = "\u5173\u7cfb\u4fe1\u53f7"
        subtle_key = "_\u7ec6\u5fae\u5173\u7cfb\u4fe1\u53f7"

        intent = str(turn_analysis.get(intent_key, "") or "")
        signal = str(turn_analysis.get(signal_key, "") or "")
        favor = self._coerce_analysis_int(state.get(favor_key, 0), default=0, minimum=0, maximum=100)
        trust = self._coerce_analysis_int(state.get(trust_key, 0), default=0, minimum=0, maximum=100)
        subtle_reasons: List[str] = []

        positive_fields = (favor_key, trust_key, elegance_key)
        if any(self._coerce_analysis_int(adjusted.get(field, 0), default=0) > 0 for field in positive_fields):
            state[subtle_key] = {"\u539f\u56e0": subtle_reasons, "\u5df2\u6709\u660e\u786e\u589e\u91cf": True}
            return adjusted
        if any(self._coerce_analysis_int(adjusted.get(field, 0), default=0) < 0 for field in (favor_key, trust_key, elegance_key, anxiety_key)):
            state[subtle_key] = {"\u539f\u56e0": subtle_reasons, "\u8d1f\u9762\u4f18\u5148": True}
            return adjusted

        self_intro = bool(re.search(r"\u6211\u662f|\u6211\u53eb|\u53ef\u4ee5\u53eb\u6211|\u6765\u81ea|\u65c5\u8005|\u65c5\u4eba|\u65c5\u884c\u8005|\u81ea\u6211\u4ecb\u7ecd", normalized))
        polite_acquaintance = bool(re.search(r"\u8ba4\u8bc6\u4f60|\u8ba4\u8bc6\u59b3|\u5f88\u9ad8\u5174|\u5f88\u8363\u5e78|\u7f8e\u4e3d\u7684\u5c0f\u59d0|\u739b\u4e3d\u4e9a\u5c0f\u59d0", normalized))
        curiosity_about_her = bool(
            re.search(
                r"\u4f60\u73b0\u5728\u5728\u505a\u4ec0\u4e48|\u5728\u505a\u4ec0\u4e48|\u4f60\u5728\u770b\u4ec0\u4e48|\u770b\u7684.*\u662f\u4ec0\u4e48|\u80fd\u5426\u544a\u8bc9\u6211|\u6211\u6709\u70b9\u597d\u5947|"
                r"\u60f3\u4e86\u89e3\u4f60|\u8ba4\u8bc6\u4f60\u4e00\u756a|\u4f60\u559c\u6b22|\u4f60\u89c9\u5f97|\u4f60\u4f1a\u4e0d\u4f1a",
                normalized,
            )
        )
        vulnerable_request = bool(re.search(r"\u4eba\u751f\u5730\u4e0d\u719f|\u8ff7\u8def|\u8fd9\u91cc\u662f\u54ea\u91cc|\u8fd9\u662f\u54ea\u91cc|\u6211\u60f3\u95ee\u95ee|\u80fd\u5e2e\u6211|\u53ef\u4ee5\u5e2e\u6211", normalized))
        respectful_suggestion = bool(
            re.search(
                r"\u6216\u8bb8\u4f60\u53ef\u4ee5|\u4f60\u53ef\u4ee5\u8bd5\u8bd5|\u4e0d\u59a8|\u4e5f\u8bb8|\u4f1a\u4e0d\u4e00\u6837|\u4eb2\u8eab\u7ecf\u5386|\u51fa\u53bb\u770b\u770b|\u53bb\u770b\u770b|"
                r"\u611f\u89c9\u4f1a|\u503c\u5f97\u4e00\u770b",
                normalized,
            )
        )
        serious_listening = bool(re.search(r"\u662f\u7684|\u786e\u5b9e|\u4f60\u8bf4\u5f97|\u6211\u7406\u89e3|\u6211\u660e\u767d|\u539f\u6765\u5982\u6b64|\u7684\u786e", normalized))
        boundary_respect = bool(
            re.search(
                r"\u4e0d\u4f1a\u7ed9\u4f60\u6dfb\u9ebb\u70e6|\u4e0d\u7ed9\u4f60\u6dfb\u9ebb\u70e6|\u4e0d\u8d8a\u8fc7|\u4e0d\u4f1a\u8d8a\u8fc7|\u4e0d\u8d8a\u754c|\u4e0d\u4f1a\u8d8a\u754c|"
                r"\u4f60\u7684\u89c4\u77e9|\u6211\u660e\u767d|\u6211\u4f1a\u9075\u5b88|\u9075\u5b88\u89c4\u77e9|\u82e5\u8ba9\u4f60\u4e3a\u96be|\u4e0d\u60f3\u8ba9\u4f60\u4e3a\u96be|"
                r"\u5df2\u7ecf\u8db3\u591f|\u613f\u610f\u8ba9\u6211|\u8c22\u8c22\u4f60\u7684\u5b89\u6392|\u8c22\u8c22\u4f60\u613f\u610f",
                normalized,
            )
        )
        question_or_request = intent == "\u63d0\u95ee\u6216\u8bf7\u6c42" or "\u6682\u65e0\u660e\u663e\u5173\u7cfb\u63a8\u8fdb" in signal

        if self_intro:
            adjusted[trust_key] = max(adjusted.get(trust_key, 0), 1)
            subtle_reasons.append("\u7528\u6237\u4e3b\u52a8\u81ea\u6211\u4ecb\u7ecd")
        if polite_acquaintance:
            adjusted[favor_key] = max(adjusted.get(favor_key, 0), 1)
            subtle_reasons.append("\u793c\u8c8c\u5efa\u7acb\u76f8\u8bc6")
        if curiosity_about_her and favor < 60:
            adjusted[favor_key] = max(adjusted.get(favor_key, 0), 1)
            subtle_reasons.append("\u5bf9\u5979\u672c\u4eba\u4fdd\u6301\u597d\u5947")
        if vulnerable_request and trust < 50:
            adjusted[trust_key] = max(adjusted.get(trust_key, 0), 1)
            subtle_reasons.append("\u6e29\u548c\u6c42\u52a9\u5e26\u6765\u88ab\u4fe1\u4efb\u611f")
        if respectful_suggestion:
            adjusted[trust_key] = max(adjusted.get(trust_key, 0), 1)
            if favor < 50:
                adjusted[favor_key] = max(adjusted.get(favor_key, 0), 1)
            subtle_reasons.append("\u5c0a\u91cd\u5730\u63a8\u8fdb\u8bdd\u9898")
        if serious_listening and question_or_request and trust < 45:
            adjusted[trust_key] = max(adjusted.get(trust_key, 0), 1)
            subtle_reasons.append("\u8ba4\u771f\u63a5\u4f4f\u5979\u7684\u8bdd")
        if boundary_respect and trust < 30:
            adjusted[trust_key] = max(adjusted.get(trust_key, 0), 1)
            subtle_reasons.append("\u7528\u6237\u5c0a\u91cd\u8fb9\u754c\u4e0e\u89c4\u77e9")

        if subtle_reasons and not any(self._coerce_analysis_int(adjusted.get(field, 0), default=0) for field in positive_fields):
            adjusted[trust_key] = 1
        state[subtle_key] = {"\u539f\u56e0": subtle_reasons, "\u5e94\u7528": bool(subtle_reasons)}
        return adjusted

    def _update_repeated_intent_counter(
        self,
        state: Dict[str, Any],
        turn_analysis: Dict[str, str],
    ) -> int:
        intent_key = "|".join(
            [
                str(turn_analysis.get("用户意图", "") or ""),
                str(turn_analysis.get("关系信号", "") or ""),
            ]
        )
        previous = str(state.get("最近数值意图", "") or "")
        if intent_key and intent_key == previous:
            count = self._coerce_analysis_int(state.get("重复意图次数", 0), default=0, minimum=0) + 1
        else:
            count = 1
        state["最近数值意图"] = intent_key
        state["重复意图次数"] = count
        return count

    def _smooth_state_deltas(
        self,
        state: Dict[str, Any],
        user_msg: str,
        turn_analysis: Dict[str, str],
        deltas: Dict[str, int],
        evidence: Any,
    ) -> Dict[str, int]:
        """让数值变化更像情绪惯性：重复衰减、高值减速、强证据才推进锁定。"""
        if not getattr(self, "enable_state_delta_smoothing", ENABLE_STATE_DELTA_SMOOTHING):
            return deltas

        smoothed = dict(deltas)
        normalized = self._normalize_analysis_content(user_msg)
        repeat_count = self._update_repeated_intent_counter(state, turn_analysis)
        evidence_level = self._get_evidence_level(evidence)
        repeat_decay_start = self._coerce_analysis_int(
            getattr(self, "state_smooth_repeat_decay_start", STATE_SMOOTH_REPEAT_DECAY_START),
            default=STATE_SMOOTH_REPEAT_DECAY_START,
            minimum=1,
        )
        high_value_start = self._coerce_analysis_int(
            getattr(self, "state_smooth_high_value_start", STATE_SMOOTH_HIGH_VALUE_START),
            default=STATE_SMOOTH_HIGH_VALUE_START,
            minimum=0,
            maximum=100,
        )
        near_max_start = self._coerce_analysis_int(
            getattr(self, "state_smooth_near_max_start", STATE_SMOOTH_NEAR_MAX_START),
            default=STATE_SMOOTH_NEAR_MAX_START,
            minimum=0,
            maximum=100,
        )
        high_anxiety_start = self._coerce_analysis_int(
            getattr(self, "state_smooth_high_anxiety_start", STATE_SMOOTH_HIGH_ANXIETY_START),
            default=STATE_SMOOTH_HIGH_ANXIETY_START,
            minimum=0,
            maximum=100,
        )

        # 同类赞美、调情、道歉连续出现时逐渐习惯化，避免刷数值。
        if repeat_count >= repeat_decay_start:
            for field in ("好感度", "信任度", "病娇值"):
                field_delta = self._coerce_analysis_int(smoothed.get(field, 0), default=0)
                if field_delta > 0:
                    smoothed[field] = max(0, field_delta - 1)
            if evidence_level != "强" and self._coerce_analysis_int(smoothed.get("锁定进度", 0), default=0) > 0:
                smoothed["锁定进度"] = 0

        # 越接近满值，正向变化越慢；强证据仍可保留一点推进。
        for field in ("好感度", "信任度", "优雅值"):
            value = self._coerce_analysis_int(state.get(field, 0), default=0, minimum=0, maximum=100)
            delta = self._coerce_analysis_int(smoothed.get(field, 0), default=0)
            if delta <= 0:
                continue
            if value >= near_max_start:
                smoothed[field] = 0 if evidence_level != "强" else min(delta, 1)
            elif value >= high_value_start and delta > 1:
                smoothed[field] = delta - 1

        # 病娇和锁定更慢，避免普通亲近把高强度关系推得过快。
        if (
            self._coerce_analysis_int(state.get("病娇值", 0), default=0, minimum=0, maximum=100) >= 80
            and self._coerce_analysis_int(smoothed.get("病娇值", 0), default=0) > 0
        ):
            smoothed["病娇值"] = 1 if evidence_level == "强" else 0
        lock_delta = self._coerce_analysis_int(smoothed.get("锁定进度", 0), default=0)
        if lock_delta > 0:
            smoothed["锁定进度"] = min(
                lock_delta,
                self._get_lock_delta_cap_by_evidence(evidence),
            )

        # 认真安抚、道歉和稳定承诺可以更明确地修复焦虑，但不刷好感。
        if re.search(r"对不起|抱歉|不会离开|我在|一直陪|别怕|放心|认真|保证", normalized):
            if self._coerce_analysis_int(state.get("焦虑值", 0), default=0, minimum=0, maximum=100) >= 35:
                smoothed["焦虑值"] = min(self._coerce_analysis_int(smoothed.get("焦虑值", 0), default=0), -2)
            if repeat_count >= repeat_decay_start and self._coerce_analysis_int(smoothed.get("好感度", 0), default=0) > 0:
                smoothed["好感度"] = 0

        # 连续冒犯会更伤信任和优雅，但焦虑不无限堆高。
        if turn_analysis.get("用户意图") == "冒犯或攻击" and repeat_count >= max(2, repeat_decay_start - 1):
            smoothed["信任度"] = min(self._coerce_analysis_int(smoothed.get("信任度", 0), default=0), -2)
            smoothed["优雅值"] = min(self._coerce_analysis_int(smoothed.get("优雅值", 0), default=0), -3)
            if (
                self._coerce_analysis_int(state.get("焦虑值", 0), default=0, minimum=0, maximum=100) >= 75
                and self._coerce_analysis_int(smoothed.get("焦虑值", 0), default=0) > 0
            ):
                smoothed["焦虑值"] = 0

        # 焦虑高位时，普通离开暗示不再每次上涨；需要冷淡/抛弃等更强语义。
        if (
            self._coerce_analysis_int(state.get("焦虑值", 0), default=0, minimum=0, maximum=100) >= high_anxiety_start
            and self._coerce_analysis_int(smoothed.get("焦虑值", 0), default=0) > 0
            and not re.search(r"不要你|不理你|离开你|分开|抛下|讨厌你", normalized)
        ):
            smoothed["焦虑值"] = 0

        state["_数值调整说明"] = {
            "重复意图次数": repeat_count,
            "证据等级": evidence_level,
            "重复衰减阈值": repeat_decay_start,
            "\u7ec6\u5fae\u5173\u7cfb\u4fe1\u53f7": self._coerce_runtime_dict_value(state.get("_\u7ec6\u5fae\u5173\u7cfb\u4fe1\u53f7", {})),
        }
        return smoothed

    def _build_state_transition_explanation(
        self,
        *,
        turn_analysis: Dict[str, str],
        evidence: Any,
        raw_deltas: Dict[str, int],
        final_deltas: Dict[str, int],
        code_decision: bool,
        smoothing_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if isinstance(evidence, dict):
            evidence_level = str(evidence.get("level", "无") or "无")
            evidence_reasons = self._coerce_runtime_list_value(evidence.get("reasons", []))[:4]
        else:
            evidence_level = "中" if evidence else "无"
            evidence_reasons = []
        blocked = [
            field
            for field, raw_value in raw_deltas.items()
            if self._coerce_analysis_int(raw_value, default=0) != 0
            and self._coerce_analysis_int(final_deltas.get(field, 0), default=0) == 0
        ]
        changed = {
            field: self._coerce_analysis_int(value, default=0)
            for field, value in final_deltas.items()
            if self._coerce_analysis_int(value, default=0) != 0
        }
        return {
            "意图": turn_analysis.get("用户意图", "普通回应"),
            "关系信号": turn_analysis.get("关系信号", "无明显关系推进"),
            "证据等级": evidence_level,
            "证据理由": evidence_reasons,
            "代码决策": bool(code_decision),
            "实际变化": changed,
            "被拦截字段": blocked,
            "数值平滑": self._coerce_runtime_dict_value(smoothing_info or {}),
        }

    def _decide_state_deltas_from_intent(
        self,
        state: Dict[str, Any],
        user_msg: str,
        turn_analysis: Dict[str, str],
        *,
        user_id: Optional[str] = None,
        memory_evidence: Any = False,
    ) -> Dict[str, int]:
        """用代码把意图标签决策为数值增量，替代让 LLM 直接打分。"""
        normalized = self._normalize_analysis_content(user_msg)
        favor = self._coerce_analysis_int(state.get("好感度", 0), default=0, minimum=0, maximum=100)
        trust = self._coerce_analysis_int(state.get("信任度", 0), default=0, minimum=0, maximum=100)
        yan = self._coerce_analysis_int(state.get("病娇值", 0), default=0, minimum=0, maximum=100)
        intent = turn_analysis.get("用户意图", "")
        signal = turn_analysis.get("关系信号", "")
        deltas = {
            "好感度": 0,
            "病娇值": 0,
            "锁定进度": 0,
            "信任度": 0,
            "焦虑值": 0,
            "优雅值": 0,
        }

        if intent == "冒犯或攻击" or "触碰边界" in signal:
            deltas.update({"好感度": -2, "信任度": -1, "优雅值": -2})
            if favor >= 30:
                deltas["焦虑值"] = 1
        elif intent == "道歉或修复关系":
            deltas.update({"信任度": 1, "优雅值": 1})
            if favor >= 25:
                deltas["焦虑值"] = -1
            if re.search(r"认真|以后|不会|保证|答应", normalized):
                deltas["信任度"] += 1
        elif intent == "亲近表达" or "主动靠近" in signal:
            deltas.update({"好感度": 2, "信任度": 1})
            if favor >= 60 and trust >= 30:
                deltas["病娇值"] = 1
            if self._evidence_level_allows_lock(memory_evidence) and favor >= 65 and trust >= 40:
                deltas["锁定进度"] = 1
        elif intent == "离开或冷淡暗示" or "稳定感下降" in signal:
            if favor >= 30:
                deltas["焦虑值"] = 1
            if favor >= 60:
                deltas["好感度"] = -1
        elif intent == "赞美或感谢" or "释放善意" in signal:
            deltas.update({"好感度": 1, "信任度": 1})
        elif intent == "话题共鸣或温和建议" or "认真接住话题" in signal:
            deltas["信任度"] = 1
            if re.search(r"亲身经历|亲眼|亲自|出去看看|去看看|走出去|尝试|试着|会不一样", normalized):
                deltas["好感度"] = 1
        elif intent == "分享秘密或建立约定":
            deltas.update({"信任度": 2, "好感度": 1})
            if favor >= 60:
                deltas["病娇值"] = 1
            if self._evidence_level_allows_lock(memory_evidence) and favor >= 60 and trust >= 35:
                deltas["锁定进度"] = 2 if self._get_evidence_level(memory_evidence) == "强" else 1

        if re.search(r"别人|其他人|朋友|同事|前任|他|她|他们|她们", normalized) and favor >= 60 and yan >= 35:
            deltas["焦虑值"] = max(deltas["焦虑值"], 1)
            if self._evidence_level_allows_lock(memory_evidence):
                deltas["病娇值"] = max(deltas["病娇值"], 1)

        if (
            getattr(self, "enable_memory_evidence_stage_gate", ENABLE_MEMORY_EVIDENCE_STAGE_GATE)
            and not self._evidence_level_allows_lock(memory_evidence)
        ):
            deltas["锁定进度"] = min(0, deltas["锁定进度"])
            if favor < 75:
                deltas["病娇值"] = min(deltas["病娇值"], 1)

        deltas = self._apply_subtle_relationship_signal_deltas(
            state,
            user_msg,
            turn_analysis,
            deltas,
        )
        deltas = self._apply_contextual_state_delta_rules(
            state,
            user_msg,
            turn_analysis,
            deltas,
            memory_evidence,
        )
        deltas = self._sanitize_analysis_deltas(state, deltas, user_id=user_id)
        state["最近原始数值建议"] = dict(deltas)
        deltas = self._smooth_state_deltas(
            state,
            user_msg,
            turn_analysis,
            deltas,
            memory_evidence,
        )
        state["最近平滑后数值"] = dict(deltas)
        return self._humanize_analysis_deltas(state, deltas, user_msg)

    def _should_use_local_state_analysis(self, user_msg: str) -> bool:
        if not getattr(self, "enable_token_cost_optimization", ENABLE_TOKEN_COST_OPTIMIZATION):
            return False

        normalized = self._normalize_analysis_content(user_msg)
        if not normalized or normalized.startswith("/"):
            return False
        if self._has_analysis_memory_anchor(normalized):
            return False
        if re.search(r"亲身经历|亲眼|亲自|出去看看|去看看|走出去|尝试|试着|或许你可以|你可以试试|值得一看|会不一样|比不上", normalized):
            return len(normalized) <= max(LOCAL_ANALYSIS_MAX_CHARS, 48)
        if len(normalized) <= LOCAL_ANALYSIS_MAX_CHARS and LOCAL_ANALYSIS_SIMPLE_PATTERN.search(normalized):
            return True
        return False

    def _build_local_state_analysis(
        self,
        state: Dict[str, Any],
        user_msg: str,
        user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self._should_use_local_state_analysis(user_msg):
            return None

        normalized = self._normalize_analysis_content(user_msg)
        favor = self._coerce_analysis_int(state.get("好感度", 0), default=0, minimum=0, maximum=100)
        trust = self._coerce_analysis_int(state.get("信任度", 0), default=0, minimum=0, maximum=100)
        deltas = {
            "好感度": 0,
            "病娇值": 0,
            "锁定进度": 0,
            "信任度": 0,
            "焦虑值": 0,
            "优雅值": 0,
        }

        if re.search(r"滚|恶心|烦|讨厌|闭嘴|羞辱", normalized):
            deltas.update({"好感度": -2, "信任度": -1, "优雅值": -2})
            if favor >= 30:
                deltas["焦虑值"] = 1
        elif re.search(r"对不起|抱歉|错了|原谅", normalized):
            deltas.update({"信任度": 1, "优雅值": 1})
            if favor >= 30:
                deltas["焦虑值"] = -1
        elif re.search(r"喜欢你|喜欢妳|爱你|想你|抱抱|亲亲", normalized):
            deltas.update({"好感度": 2, "信任度": 1})
            if favor >= 60 and trust >= 30:
                deltas["病娇值"] = 1
                if re.search(r"永远|唯一|只要你|只有你|命定", normalized):
                    deltas["锁定进度"] = 1
        elif re.search(r"谢谢|辛苦|真好|温柔|漂亮|可爱|厉害", normalized):
            deltas.update({"好感度": 1, "信任度": 1})
        elif re.search(r"亲身经历|亲眼|亲自|出去看看|去看看|走出去|尝试|试着|或许你可以|你可以试试|值得一看|会不一样|比不上", normalized):
            deltas["信任度"] = 1
            if re.search(r"亲身经历|亲眼|亲自|出去看看|去看看|走出去|尝试|试着|会不一样", normalized):
                deltas["好感度"] = 1
        elif re.search(r"晚安|再见|离开|走了|下了", normalized):
            if favor >= 30:
                deltas["焦虑值"] = 1

        deltas = self._sanitize_analysis_deltas(state, deltas, user_id=user_id)
        turn_analysis = self._build_fallback_turn_analysis(user_msg, deltas=deltas)
        evidence = self._grade_stage_memory_evidence(user_msg)
        if getattr(self, "enable_code_state_decision", ENABLE_CODE_STATE_DECISION):
            deltas = self._decide_state_deltas_from_intent(
                state,
                user_msg,
                turn_analysis,
                user_id=user_id,
                memory_evidence=evidence,
            )
        else:
            deltas = self._humanize_analysis_deltas(state, deltas, user_msg)
        explanation = self._build_state_transition_explanation(
            turn_analysis=turn_analysis,
            evidence=evidence,
            raw_deltas=deltas,
            final_deltas=deltas,
            code_decision=bool(getattr(self, "enable_code_state_decision", ENABLE_CODE_STATE_DECISION)),
            smoothing_info=state.get("_数值调整说明", {}),
        )
        return {
            **deltas,
            "__turn_analysis": turn_analysis,
            "__local_analysis": True,
            "__memory_evidence": self._evidence_level_allows_lock(evidence),
            "__evidence": evidence,
            "__state_explanation": explanation,
        }

    def _normalize_turn_analysis(
        self,
        data: Dict[str, Any],
        user_msg: str,
        deltas: Optional[Dict[str, int]] = None,
    ) -> Dict[str, str]:
        aliases = {
            "用户意图": ("用户意图", "user_intent", "intent"),
            "用户情绪": ("用户情绪", "user_emotion", "emotion"),
            "关系信号": ("关系信号", "relationship_signal", "signal"),
            "回应目标": ("回应目标", "response_goal", "goal"),
        }
        fallback = self._build_fallback_turn_analysis(user_msg, deltas=deltas)
        normalized: Dict[str, str] = {}
        for target_key, source_keys in aliases.items():
            value = ""
            for key in source_keys:
                if key in data:
                    value = self._clean_analysis_text(data.get(key), 80)
                    break
            normalized[target_key] = value or fallback[target_key]
        return normalized

    def _extract_turn_analysis(self, analysis_result: Dict[str, Any]) -> Dict[str, str]:
        analysis = analysis_result.get("__turn_analysis", {})
        return dict(analysis) if isinstance(analysis, dict) else {}

    def _extract_analysis_deltas(self, analysis_result: Dict[str, Any]) -> Dict[str, int]:
        allowed_fields = ("好感度", "病娇值", "锁定进度", "信任度", "焦虑值", "优雅值")
        deltas: Dict[str, int] = {}
        for field in allowed_fields:
            value = analysis_result.get(field, 0)
            if isinstance(value, (int, float)) and value == value and value not in (float("inf"), float("-inf")):
                deltas[field] = int(value)
        return deltas

    async def _analyze_state_changes(
        self,
        event: AstrMessageEvent,
        user_id: str,
        state: Dict[str, Any],
        user_msg: str,
        history_entries: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """调用 LLM 分析当前对话应带来的状态变化。"""
        started_at = time.perf_counter()
        snapshot = self._derive_state_snapshot(state)
        analysis_state = {
            "好感度": state.get("好感度", 0),
            "病娇值": state.get("病娇值", 0),
            "锁定进度": state.get("锁定进度", 0),
            "信任度": state.get("信任度", 0),
            "焦虑值": state.get("焦虑值", 0),
            "优雅值": state.get("优雅值", 0),
            "当前状态": snapshot.get("兼容状态", state.get("当前状态", "冷傲贵族")),
            "关系阶段": snapshot.get("关系阶段", RELATION_STAGE_NAMES["OBSERVATION"]),
            "主情绪模式": snapshot.get("主情绪模式", STATE_NAMES["COLD_NOBLE"]),
            "危机覆盖": snapshot.get("危机覆盖", CRISIS_OVERLAY_NAMES["NONE"]),
            "表现强度": snapshot.get("表现强度标签", "标准姿态"),
        }
        entries = history_entries
        if entries is None:
            entries = await self._get_analysis_memory_entries(
                user_id,
                user_msg,
                scene_policy=state.get("_scene_memory_policy"),
            )
        history_text = self._format_analysis_history_entries(entries)
        rules_text = self._build_analysis_rules_text(state, user_id=user_id)
        prompt = f"""请根据以下动态资料输出本轮 JSON。

当前阶段规则：
{rules_text}

当前状态：
{self._analysis_json_dumps(analysis_state)}

相关聊天记忆：
{history_text}

最新用户发言：
{user_msg}"""

        try:
            resp = await self._call_analysis_llm(
                purpose="状态分析",
                prompt=prompt,
                system_prompt=self._get_analysis_system_prompt(),
                temperature=0.2,
                max_tokens=360,
                event=event,
            )
            if not resp:
                self._log_perf("analyze_state_changes", started_at, user_id, threshold_ms=5.0)
                return {}
            data = self._parse_json_response(resp.completion_text or "")
            if not data:
                self.logger.warning(f"状态分析未返回有效 JSON: {resp.completion_text!r}")
                self._log_perf("analyze_state_changes", started_at, user_id, threshold_ms=5.0)
                return {}

            allowed_fields = ("好感度", "病娇值", "锁定进度", "信任度", "焦虑值", "优雅值")
            deltas: Dict[str, int] = {}
            for field in allowed_fields:
                value = data.get(field, 0)
                if isinstance(value, (int, float)) and value == value and value not in (float("inf"), float("-inf")):
                    deltas[field] = max(-10, min(10, int(round(value))))
            stage_limited_deltas = self._sanitize_analysis_deltas(
                state,
                deltas,
                user_id=user_id,
            )
            humanized_deltas = self._humanize_analysis_deltas(
                state,
                stage_limited_deltas,
                user_msg,
            )
            turn_analysis = self._normalize_turn_analysis(
                data,
                user_msg,
                deltas=humanized_deltas,
            )
            evidence = self._grade_stage_memory_evidence(user_msg, entries)
            memory_evidence = self._evidence_level_allows_lock(evidence)
            if getattr(self, "enable_code_state_decision", ENABLE_CODE_STATE_DECISION):
                humanized_deltas = self._decide_state_deltas_from_intent(
                    state,
                    user_msg,
                    turn_analysis,
                    user_id=user_id,
                    memory_evidence=evidence,
                )
            explanation = self._build_state_transition_explanation(
                turn_analysis=turn_analysis,
                evidence=evidence,
                raw_deltas=stage_limited_deltas,
                final_deltas=humanized_deltas,
                code_decision=bool(getattr(self, "enable_code_state_decision", ENABLE_CODE_STATE_DECISION)),
                smoothing_info=state.get("_数值调整说明", {}),
            )
            self._log_perf("analyze_state_changes", started_at, user_id, threshold_ms=5.0)
            return {
                **humanized_deltas,
                "__turn_analysis": turn_analysis,
                "__memory_evidence": memory_evidence,
                "__evidence": evidence,
                "__state_explanation": explanation,
                "__code_decision": bool(getattr(self, "enable_code_state_decision", ENABLE_CODE_STATE_DECISION)),
            }
        except Exception as e:
            self.logger.error(f"状态分析失败: {e}", exc_info=True)
            self._log_perf("analyze_state_changes_failed", started_at, user_id, threshold_ms=5.0)
            return {}

    def _apply_llm_state_changes(
        self,
        user_id: str,
        state: Dict[str, Any],
        deltas: Dict[str, int]
    ) -> Dict[str, int]:
        """将 LLM 分析出的增量应用到用户状态。"""
        tracked_fields = ("好感度", "病娇值", "锁定进度", "信任度", "焦虑值", "优雅值", "占有欲")
        old_values = {
            field: self._coerce_analysis_int(
                state.get(field, DEFAULT_STATE.get(field, 0)),
                default=self._coerce_analysis_int(DEFAULT_STATE.get(field, 0), default=0),
                minimum=0,
                maximum=100,
            )
            for field in tracked_fields
        }
        applied_changes: Dict[str, int] = {}
        scaled_deltas = self._scale_analysis_deltas(state, deltas)
        sanitized_deltas = self._sanitize_analysis_deltas(state, scaled_deltas, user_id=user_id)
        for field in ("好感度", "病娇值", "锁定进度", "信任度", "焦虑值", "优雅值"):
            raw_delta = sanitized_deltas.get(field, 0)
            if not isinstance(raw_delta, (int, float)):
                continue
            self._apply_state_delta(
                state,
                field,
                self._coerce_analysis_int(raw_delta, default=0, minimum=-100, maximum=100),
            )

        self._normalize_state_constraints(state, user_id=user_id)
        self._update_possessiveness(state)
        if self._get_destined_one_info() and not self._is_destined_user(user_id):
            state["占有欲"] = min(
                old_values["占有欲"],
                self._coerce_analysis_int(state.get("占有欲", 0), default=0, minimum=0, maximum=100),
            )
        for field in tracked_fields:
            current_value = self._coerce_analysis_int(
                state.get(field, DEFAULT_STATE.get(field, 0)),
                default=self._coerce_analysis_int(DEFAULT_STATE.get(field, 0), default=0),
                minimum=0,
                maximum=100,
            )
            applied_changes[field] = current_value - old_values[field]
        state["当前状态"] = self._determine_state(state)
        return applied_changes

    def _format_state_value_with_delta(
        self,
        state: Dict[str, Any],
        deltas: Dict[str, int],
        field: str,
    ) -> str:
        value = state.get(field, "?")
        delta = deltas.get(field, 0)
        if isinstance(delta, (int, float)):
            delta_int = self._coerce_analysis_int(delta, default=0, minimum=-100, maximum=100)
            if delta_int != 0:
                return f"{value}({delta_int:+d})"
        return str(value)

    def _build_debug_footer(
        self,
        state: Dict[str, Any],
        deltas: Dict[str, int],
        state_explanation: Optional[Dict[str, Any]] = None,
    ) -> str:
        snapshot = self._derive_state_snapshot(state)
        if getattr(self, "enable_state_explanation_log", ENABLE_STATE_EXPLANATION_LOG):
            explanation = self._coerce_runtime_dict_value(
                state_explanation or state.get("最近状态解释", {}) or {}
            )
        else:
            explanation = {}
        return self._build_debug_footer_with_explanation(
            state,
            deltas,
            snapshot,
            explanation,
        )

    def _build_debug_footer_with_explanation(
        self,
        state: Dict[str, Any],
        deltas: Dict[str, int],
        snapshot: Dict[str, Any],
        explanation: Dict[str, Any],
    ) -> str:
        reason_text = ""
        if explanation:
            blocked = explanation.get("被拦截字段", []) or []
            reasons = explanation.get("证据理由", []) or []
            reason_text = (
                f" 意图:{explanation.get('意图', '?')} "
                f"信号:{explanation.get('关系信号', '?')} "
                f"证据:{explanation.get('证据等级', state.get('阶段证据等级', '无'))}"
            )
            if reasons:
                reason_text += f"({','.join(str(item) for item in reasons[:2])})"
            if blocked:
                reason_text += f" 拦截:{','.join(str(item) for item in blocked[:3])}"
            smoothing = explanation.get("数值平滑", {}) or {}
            if isinstance(smoothing, dict) and smoothing.get("重复意图次数", 0):
                reason_text += f" 重复:{smoothing.get('重复意图次数')}"
        return (
            "\n\n---\n*["
            f"好感:{self._format_state_value_with_delta(state, deltas, '好感度')} "
            f"病娇:{self._format_state_value_with_delta(state, deltas, '病娇值')} "
            f"锁定:{self._format_lock_progress_display(state, deltas)} "
            f"信任:{self._format_state_value_with_delta(state, deltas, '信任度')} "
            f"焦虑:{self._format_state_value_with_delta(state, deltas, '焦虑值')} "
            f"优雅:{self._format_state_value_with_delta(state, deltas, '优雅值')} "
            f"占有:{self._format_state_value_with_delta(state, deltas, '占有欲')} "
            f"状态:{snapshot.get('兼容状态', state.get('当前状态', '?'))} "
            f"关系:{snapshot.get('关系阶段', '?')} "
            f"模式:{snapshot.get('主情绪模式', '?')} "
            f"危机:{snapshot.get('危机覆盖', CRISIS_OVERLAY_NAMES['NONE'])} "
            f"强度:{snapshot.get('表现强度标签', '标准姿态')}"
            f"{reason_text}"
            "]*"
        )

    def _build_state_report(self, state: Dict[str, Any]) -> str:
        snapshot = self._derive_state_snapshot(state)
        desc = self._describe_state_snapshot(snapshot)
        destined_info = self._get_destined_one_info()
        destined_line = ""
        if destined_info:
            destined_line = f"\n命定之人：{self._format_destined_one_label()}"
        return (
            "📜 **玛丽亚当前状态**\n\n"
            f"{desc}\n\n"
            f"好感度：{state.get('好感度', 0)}/100\n"
            f"病娇值：{state.get('病娇值', 0)}/100\n"
            f"锁定进度：{self._format_lock_progress_display(state)}\n"
            f"信任度：{state.get('信任度', 0)}/100\n"
            f"焦虑值：{state.get('焦虑值', 0)}/100\n"
            f"优雅值：{state.get('优雅值', 0)}/100\n"
            f"占有欲：{state.get('占有欲', 0)}/100\n"
            f"互动计数：{state.get('互动计数', 0)}\n"
            f"调试模式：{'开启' if state.get('调试模式', self.default_debug_mode) else '关闭'}\n"
            f"关系状态机：{state.get('关系状态机', RELATIONSHIP_STATE_NAMES['OPEN'])}\n"
            f"兼容状态：{snapshot.get('兼容状态', state.get('当前状态', '未知'))}\n"
            f"关系阶段：{snapshot.get('关系阶段', '未知')}\n"
            f"主情绪模式：{snapshot.get('主情绪模式', '未知')}\n"
            f"危机覆盖：{snapshot.get('危机覆盖', CRISIS_OVERLAY_NAMES['NONE'])}\n"
            f"表现强度：{snapshot.get('表现强度标签', '标准姿态')}\n"
            f"短期心情：{state.get('短期心情', '平静')}\n"
            f"行为档位：{state.get('当前行为档位', '礼貌回应')}\n"
            f"表达克制度：{state.get('表达克制度', 80)}/100\n"
            f"阶段证据确认：{'是' if state.get('阶段证据确认', False) else '否'}\n"
            f"阶段证据等级：{state.get('阶段证据等级', '无')}\n"
            f"状态摘要：{self._format_state_snapshot_compact(snapshot)}"
            f"{destined_line}"
        )

    def _format_delta_map_for_report(self, values: Any) -> str:
        if not isinstance(values, dict) or not values:
            return "无"
        parts = []
        for field in ("好感度", "信任度", "优雅值", "焦虑值", "病娇值", "锁定进度", "占有欲"):
            value = self._coerce_analysis_int(values.get(field, 0), default=0)
            if value:
                parts.append(f"{field}{value:+d}")
        return "，".join(parts) if parts else "无"

    def _build_diagnostic_report(self, state: Dict[str, Any]) -> str:
        explanation = self._coerce_runtime_dict_value(state.get("最近状态解释", {}) or {})
        history = state.get("诊断历史", [])
        if isinstance(history, list) and history:
            explanation = self._coerce_runtime_dict_value(history[-1]) or explanation
        smoothing = self._coerce_runtime_dict_value(explanation.get("数值平滑", {}) or {})
        short_term = self._coerce_runtime_dict_value(explanation.get("短期心理", {}) or {})
        blocked = explanation.get("被拦截字段", []) or []
        reasons = explanation.get("证据理由", []) or []
        action_budget = "未启用"
        try:
            action_budget = self._build_behavior_action_budget_prompt(state, None, compact=bool(state.get("最近是否轻量Prompt", False)))
        except Exception:
            action_budget = "不可用"
        style_hint = str(state.get("最近风格指纹提示", "") or "无")
        if style_hint != "无":
            style_hint = self._limit_text_for_prompt(style_hint.replace("\n", " "), 120)
        latest_event = {}
        event_log = state.get("关系事件日志", [])
        if isinstance(event_log, list) and event_log:
            latest_event = event_log[-1] if isinstance(event_log[-1], dict) else {}
        memory_feedback = state.get("最近记忆负反馈", {})
        if not isinstance(memory_feedback, dict):
            memory_feedback = {}
        memory_cooldown = state.get("最近记忆冷却", state.get("æœ€è¿‘è®°å¿†å†·å´", {}))
        if not isinstance(memory_cooldown, dict):
            memory_cooldown = {}
        prompt_estimate = state.get("最近Prompt估算", state.get("??Prompt??", {}))
        if not isinstance(prompt_estimate, dict):
            prompt_estimate = {}
        prompt_policy = state.get("最近记忆召回策略", {})
        if not isinstance(prompt_policy, dict):
            prompt_policy = {}
        prompt_budget_stats = state.get("Prompt预算统计", {})
        if not isinstance(prompt_budget_stats, dict):
            prompt_budget_stats = {}
        prompt_cost_profile = state.get("Prompt成本画像", prompt_estimate.get("cost_profile", {}))
        if not isinstance(prompt_cost_profile, dict):
            prompt_cost_profile = {}
        cost_profile_text = "无"
        if prompt_cost_profile:
            budget_hit_pct = int(
                self._coerce_analysis_float(
                    prompt_cost_profile.get("budget_hit_rate", 0),
                    default=0.0,
                    minimum=0.0,
                    maximum=1.0,
                )
                * 100
            )
            compact_pct = int(
                self._coerce_analysis_float(
                    prompt_cost_profile.get("compact_rate", 0),
                    default=0.0,
                    minimum=0.0,
                    maximum=1.0,
                )
                * 100
            )
            slot_dedup_pct = int(
                self._coerce_analysis_float(
                    prompt_cost_profile.get("memory_slot_dedup_rate", 0),
                    default=0.0,
                    minimum=0.0,
                    maximum=1.0,
                )
                * 100
            )
            cost_profile_text = (
                f"{prompt_cost_profile.get('samples', 0)}/{prompt_cost_profile.get('window', 0)}轮，"
                f"均值{prompt_cost_profile.get('avg_original_tokens', 0)} token，"
                f"命中率{budget_hit_pct}%，"
                f"轻量率{compact_pct}%，"
                f"记忆{prompt_cost_profile.get('avg_memory_selected', 0)}/"
                f"{prompt_cost_profile.get('avg_memory_injection_limit', 0)}条，"
                f"槽位省{prompt_cost_profile.get('avg_memory_slot_dedup_saved_chars', 0)}字/"
                f"{slot_dedup_pct}%"
            )
        prompt_budget_advice = self._build_prompt_budget_advice(state)
        hot_layer = prompt_estimate.get("hot_layer", {})
        if not isinstance(hot_layer, dict):
            hot_layer = {}
        hot_layer_text = "无"
        if hot_layer.get("name"):
            hot_layer_share_pct = int(
                self._coerce_analysis_float(
                    hot_layer.get("share", 0),
                    default=0.0,
                    minimum=0.0,
                    maximum=1.0,
                )
                * 100
            )
            hot_layer_text = f"{hot_layer.get('name')} {hot_layer.get('tokens', 0)} token / {hot_layer_share_pct}%"
        auto_throttle = state.get("Prompt预算自动降档", {})
        if not isinstance(auto_throttle, dict):
            auto_throttle = {}
        auto_throttle_text = "未触发"
        if auto_throttle.get("enabled"):
            throttle_suffix = "，强退避" if auto_throttle.get("escalated") else ""
            if auto_throttle.get("escalation_recovering"):
                throttle_suffix = "，退避恢复"
            auto_throttle_text = (
                f"{auto_throttle.get('action', '已降档')}"
                f"（{auto_throttle.get('hot_layer', '未知')}，"
                f"等级{auto_throttle.get('compression_tier', 'targeted')}，"
                f"恢复{auto_throttle.get('clear_streak', 0)}/{auto_throttle.get('recovery_turns', 0)}{throttle_suffix}）"
            )
        elif auto_throttle.get("recovered"):
            auto_throttle_text = str(auto_throttle.get("reason", "已恢复"))
        auto_throttle_log_text = self._summarize_prompt_budget_throttle_log(state)
        memory_mode_policy = state.get("Prompt预算记忆模式策略", {})
        if not isinstance(memory_mode_policy, dict):
            memory_mode_policy = {}
        memory_mode_policy_text = "未触发"
        if memory_mode_policy.get("enabled"):
            auto_mode_reason = str(memory_mode_policy.get("auto_mode_reason", "") or "")
            auto_mode_text = f"，自动:{auto_mode_reason}" if auto_mode_reason and auto_mode_reason != "manual" else ""
            sticky_state = state.get("Prompt成本自动记忆档位", {})
            sticky_text = ""
            if isinstance(sticky_state, dict) and sticky_state.get("pending_mode"):
                sticky_text = f"，待升:{sticky_state.get('pending_mode')}({sticky_state.get('pending_turns', 0)})"
            memory_mode_policy_text = (
                f"{memory_mode_policy.get('mode', 'balanced')} "
                f"≤{memory_mode_policy.get('memory_limit', 0)}条/"
                f"{memory_mode_policy.get('char_budget', 0)}字{auto_mode_text}{sticky_text}"
            )
        elif memory_mode_policy.get("recovered"):
            memory_mode_policy_text = str(memory_mode_policy.get("reason", "已恢复"))
        memory_candidate_text = "未触发"
        if prompt_estimate.get("memory_candidate_limit") is not None:
            memory_candidate_text = (
                f"注入≤{prompt_estimate.get('memory_injection_limit', 0)}条，"
                f"候选≤{prompt_estimate.get('memory_candidate_limit', 0)}条，"
                f"价值优先={'是' if prompt_estimate.get('memory_value_priority') else '否'}，"
                f"扩展={'是' if prompt_estimate.get('memory_candidate_expanded') else '否'}"
            )
            if prompt_estimate.get("memory_char_budget") is not None:
                memory_candidate_text += f"，字符≤{prompt_estimate.get('memory_char_budget', 0)}"
        memory_selection_trace = prompt_estimate.get("memory_selection_trace", [])
        if not isinstance(memory_selection_trace, list):
            memory_selection_trace = []
        memory_selection_lines = []
        for item in memory_selection_trace[:3]:
            if not isinstance(item, dict):
                continue
            memory_selection_lines.append(
                f"{item.get('slot', 'none')}/{item.get('layer', '?')}/深{item.get('salience', 0)}:"
                f"{item.get('reason', 'retrieval_order')}:{item.get('preview', '')}"
            )
        memory_selection_text = "；".join(memory_selection_lines) if memory_selection_lines else "无"
        memory_slot_dedup_trace = prompt_estimate.get("memory_slot_dedup_trace", [])
        if not isinstance(memory_slot_dedup_trace, list):
            memory_slot_dedup_trace = []
        memory_slot_dedup_lines = []
        memory_slot_dedup_saved = self._coerce_analysis_int(
            prompt_estimate.get("memory_slot_dedup_saved_chars", 0),
            default=0,
            minimum=0,
        )
        if prompt_estimate.get("memory_slot_dedup_saved_chars") is None:
            memory_slot_dedup_saved = sum(
                self._coerce_analysis_int(item.get("saved_chars", 0), default=0, minimum=0)
                for item in memory_slot_dedup_trace
                if isinstance(item, dict)
            )
        for item in memory_slot_dedup_trace[:3]:
            if not isinstance(item, dict):
                continue
            memory_slot_dedup_lines.append(
                f"{item.get('slot', 'unknown')}:{item.get('dropped_id', '?')}→{item.get('kept_id', '?')}"
            )
        memory_slot_dedup_text = (
            f"{len(memory_slot_dedup_trace)}条"
            + (f"，约省{memory_slot_dedup_saved}字" if memory_slot_dedup_saved > 0 else "")
            + (f"（{'；'.join(memory_slot_dedup_lines)}）" if memory_slot_dedup_lines else "")
            if memory_slot_dedup_trace
            else "无"
        )
        recent_recalled = state.get("最近召回记忆", state.get("æœ€è¿‘å¬å›žè®°å¿†", []))
        if not isinstance(recent_recalled, list):
            recent_recalled = []
        recall_lines = []
        for item in recent_recalled[:3]:
            if not isinstance(item, dict):
                continue
            evidence = item.get("evidence", {}) if isinstance(item.get("evidence", {}), dict) else {}
            reasons = evidence.get("reasons", []) if isinstance(evidence, dict) else []
            reason_text = "/".join(str(reason) for reason in reasons[:2]) if reasons else "?"
            recall_lines.append(
                f"{str(item.get('id', ''))[:8]}({item.get('visibility', 'default')}/{item.get('temperature', 'warm')}/{reason_text})"
            )
        recall_text = "?".join(recall_lines) if recall_lines else "?"
        return (
            "🧭 **玛丽亚状态诊断**\n\n"
            f"意图：{explanation.get('意图', '暂无')}\n"
            f"关系信号：{explanation.get('关系信号', '暂无')}\n"
            f"证据等级：{explanation.get('证据等级', state.get('阶段证据等级', '无'))}\n"
            f"证据理由：{', '.join(str(item) for item in reasons) if reasons else '无'}\n"
            f"数值变化原因：{';'.join(str(item) for item in (explanation.get('证据理由', []) or [])[:3]) if (explanation.get('证据理由', []) or []) else '无'}\n"
            f"长期/短期拆分：长期数值 {self._format_delta_map_for_report(state.get('最近最终变化', explanation.get('实际变化', {})))}；短期反应 {short_term.get('短期心情', state.get('短期心情', '平静'))}/{short_term.get('当前行为档位', state.get('当前行为档位', '礼貌回应'))}\n"
            f"边界/降档：{('，'.join([item for item in [('数值字段被拦截' if blocked else ''), ('群聊公开边界' if state.get('当前是否群聊作用域') else ''), ('命定后他人边界' if state.get('关系状态机') == RELATIONSHIP_STATE_NAMES['BOUNDARY_AFTER_FATE'] else ''), ('阶段证据门槛' if not state.get('阶段证据确认', False) else '')] if item]) or '无')}\n"
            f"重复意图次数：{smoothing.get('重复意图次数', state.get('重复意图次数', 0))}\n"
            f"原始建议：{self._format_delta_map_for_report(state.get('最近原始数值建议', {}))}\n"
            f"平滑后：{self._format_delta_map_for_report(state.get('最近平滑后数值', {}))}\n"
            f"最终变化：{self._format_delta_map_for_report(state.get('最近最终变化', explanation.get('实际变化', {})))}\n"
            f"短期心理：{short_term.get('短期心情', state.get('短期心情', '平静'))} / "
            f"{short_term.get('当前行为档位', state.get('当前行为档位', '礼貌回应'))}，"
            f"目标{short_term.get('目标行为档位', state.get('目标行为档位', state.get('当前行为档位', '礼貌回应')))}，"
            f"克制{short_term.get('表达克制度', state.get('表达克制度', 80))}/100\n"
            f"连续性：{short_term.get('行为连续性提示', state.get('行为连续性提示', '无')) or '无'}\n"
            f"行为平滑：{state.get('行为档位理由', '无')}\n"
            f"行为变体：{state.get('行为风格变体', '无') or '无'}\n"
            f"时间衰减：{short_term.get('时间衰减系数', '无')}\n"
            f"动作预算：{self._limit_text_for_prompt(action_budget, 120)}\n"
            f"风格指纹：{style_hint}\n"
            f"轻量Prompt：{'是' if state.get('最近是否轻量Prompt', False) else '否'}（{prompt_policy.get('reason', '无')}）\n"
            f"关系事件：{latest_event.get('title', '无')}\n"
            f"记忆负反馈：{memory_feedback.get('count', 0)} 条（{memory_feedback.get('reason', '无')}）\n"
            f"记忆召回：{'已跳过' if prompt_policy.get('skipped') else '常规'}；冷却跳过 {memory_cooldown.get('skipped', 0)} 条 / {memory_cooldown.get('seconds', 0)} 秒\n"
            f"预算锚点：{'保留' if prompt_policy.get('anchor_preserved') else '未启用'} / {prompt_policy.get('anchor_count', 0)} 条\n"
            f"最近召回：{recall_text}\n"
            f"Prompt估算：{prompt_estimate.get('tokens', 0)} token / {prompt_estimate.get('chars', 0)} 字符，"
            f"轻量={'是' if prompt_estimate.get('compact', False) else '否'}，"
            f"预算保护={'是' if prompt_estimate.get('budget_guard_applied', False) else '否'}"
            f"({prompt_estimate.get('original_tokens', prompt_estimate.get('tokens', 0))}/{prompt_estimate.get('budget', 0)})\n"
            f"Prompt热层：{hot_layer_text}\n"
            f"预算自动降档：{auto_throttle_text}\n"
            f"记忆模式软上限：{memory_mode_policy_text}\n"
            f"记忆候选池：{memory_candidate_text}\n"
            f"记忆入选原因：{memory_selection_text}\n"
            f"记忆槽位去重：{memory_slot_dedup_text}\n"
            f"降档日志：{auto_throttle_log_text}\n"
            f"成本画像：{cost_profile_text}\n"
            f"预算趋势：{prompt_budget_stats.get('hits', 0)}/{prompt_budget_stats.get('samples', 0)}，"
            f"连续{prompt_budget_stats.get('streak', 0)}，恢复{prompt_budget_stats.get('clear_streak', 0)}，均值{prompt_budget_stats.get('avg_original_tokens', 0)} token；"
            f"建议：{prompt_budget_advice}\n"
            f"被拦截字段：{', '.join(str(item) for item in blocked) if blocked else '无'}\n"
            f"关系状态机：{state.get('关系状态机', RELATIONSHIP_STATE_NAMES['OPEN'])}\n"
            f"阶段证据确认：{'是' if state.get('阶段证据确认', False) else '否'}"
        )

    def _build_diagnostic_history_report(self, state: Dict[str, Any], limit: int = 5) -> str:
        history = state.get("诊断历史", [])
        if not isinstance(history, list) or not history:
            return "🧭 **玛丽亚状态诊断历史**\n\n暂无可回看的诊断记录。"
        effective_limit = self._coerce_analysis_int(limit, default=5, minimum=1, maximum=20)
        lines = ["🧭 **玛丽亚状态诊断历史**", ""]
        for item in history[-effective_limit:]:
            if not isinstance(item, dict):
                continue
            time_text = str(item.get("time", "") or "")[:19] or "未知时间"
            lines.append(
                f"- {time_text} | 意图:{item.get('意图', '暂无')} | "
                f"证据:{item.get('证据等级', '无')} | "
                f"变化:{self._format_delta_map_for_report(item.get('实际变化', {}))}"
            )
        return "\n".join(lines)

    def _estimate_profile_confidence(self, profile: Dict[str, Any]) -> Dict[str, int]:
        def count_values(value: Any) -> int:
            if isinstance(value, dict):
                return sum(count_values(item) for item in value.values())
            if isinstance(value, list):
                return sum(1 for item in value if str(item or "").strip())
            return 1 if str(value or "").strip() else 0

        stats = profile.get("互动记录", {}) if isinstance(profile, dict) else {}
        if not stats and isinstance(profile, dict):
            stats = profile.get("æµœæŽ‘å§©ç’æ¿ç¶", {})
        try:
            update_count = int(
                stats.get("资料更新次数", stats.get("éŽ¬è®³ç°°é”ã„¦î‚¼é?", 0)) or 0
            )
        except (TypeError, ValueError):
            update_count = 0
        evidence_count = count_values(profile)
        score = max(0, min(100, evidence_count * 6 + update_count * 4))
        return {"score": score, "evidence_count": evidence_count, "update_count": update_count}

    def _build_profile_report(self, profile: Dict[str, Any]) -> str:
        if (
            not profile.get("基本信息")
            and not any(profile.get("兴趣爱好", {}).values())
            and not profile.get("玛丽亚学习笔记", {}).get("喜欢的话题")
        ):
            return (
                "> *（玛丽亚轻声说）* 我还不够了解你。"
                " 多聊聊，让我记住你的样子。"
            )

        lines = ["📖 **玛丽亚眼中的你**", ""]
        basic_info = profile.get("基本信息", {})
        hobbies = profile.get("兴趣爱好", {})
        traits = profile.get("性格特征", {})
        notes = profile.get("玛丽亚学习笔记", {})
        stats = profile.get("互动记录", {})

        if basic_info.get("称呼"):
            lines.append(f"偏好称呼：{basic_info['称呼']}")
        if basic_info.get("生日"):
            lines.append(f"生日：{basic_info['生日']}")
        if basic_info.get("职业"):
            lines.append(f"职业：{basic_info['职业']}")
        if basic_info.get("所在地"):
            lines.append(f"所在地：{basic_info['所在地']}")
        if hobbies.get("音乐"):
            lines.append(f"喜欢的音乐：{', '.join(hobbies['音乐'])}")
        if hobbies.get("书籍"):
            lines.append(f"喜欢的书籍：{', '.join(hobbies['书籍'])}")
        if hobbies.get("食物"):
            lines.append(f"喜欢的食物：{', '.join(hobbies['食物'])}")
        if hobbies.get("颜色"):
            lines.append(f"喜欢的颜色：{', '.join(hobbies['颜色'])}")
        if traits.get("沟通风格"):
            lines.append(f"沟通风格：{traits['沟通风格']}")
        if notes.get("喜欢的话题"):
            lines.append(f"常聊话题：{', '.join(notes['喜欢的话题'][:5])}")
        if notes.get("反感的话题"):
            lines.append(f"回避话题：{', '.join(notes['反感的话题'][:5])}")
        confidence = self._estimate_profile_confidence(profile)
        lines.append(
            f"画像置信度：{confidence['score']}/100 "
            f"(证据 {confidence['evidence_count']}，更新 {confidence['update_count']})"
        )
        lines.append(f"累计互动：{stats.get('总互动次数', 0)} 次")
        return "\n".join(lines)

    def _update_possessiveness(self, state: Dict):
        fav = self._coerce_analysis_int(state.get("好感度", 0), default=0, minimum=0, maximum=100)
        yan = self._coerce_analysis_int(state.get("病娇值", 0), default=0, minimum=0, maximum=100)
        lock = self._coerce_analysis_int(state.get("锁定进度", 0), default=0, minimum=0, maximum=100)
        anxiety = self._coerce_analysis_int(state.get("焦虑值", 0), default=0, minimum=0, maximum=100)
        if fav < 60:
            state["占有欲"] = 0
            return

        possess = int(max(0, fav - 60) * 0.20 + yan * 0.45 + lock * 0.35)
        if anxiety > 20:
            possess += int((anxiety - 20) * 0.18)
        if lock >= self._coerce_analysis_int(getattr(self, "lock_threshold", 100), default=100, minimum=0, maximum=100):
            possess += 20
        state["占有欲"] = max(0, min(100, possess))

    def _determine_relationship_stage(self, state: Dict[str, Any]) -> str:
        favor = self._coerce_analysis_int(state.get("好感度", 0), default=0, minimum=0, maximum=100)
        trust = self._coerce_analysis_int(state.get("信任度", 0), default=0, minimum=0, maximum=100)
        yan = self._coerce_analysis_int(state.get("病娇值", 0), default=0, minimum=0, maximum=100)
        lock = self._coerce_analysis_int(state.get("锁定进度", 0), default=0, minimum=0, maximum=100)
        interactions = self._coerce_analysis_int(state.get("互动计数", 0), default=0, minimum=0)
        event_types = self._get_relationship_event_types(state)
        has_private_event = bool({"first_secret", "first_promise", "first_apology"}.intersection(event_types))
        has_exclusive_event = bool({"first_promise", "first_exclusive_probe"}.intersection(event_types))
        evidence_confirmed = (
            not getattr(self, "enable_memory_evidence_stage_gate", ENABLE_MEMORY_EVIDENCE_STAGE_GATE)
            or bool(state.get("阶段证据确认", False))
            or has_exclusive_event
        )

        lock_threshold = self._coerce_analysis_int(getattr(self, "lock_threshold", 100), default=100, minimum=0, maximum=100)
        if lock >= lock_threshold:
            return RELATION_STAGE_NAMES["FATED_LOCK"]
        if (
            evidence_confirmed
            and (
                (favor >= 78 and trust >= 65 and interactions >= (16 if has_exclusive_event else 20))
                or (favor >= 72 and trust >= 58 and yan >= 45 and interactions >= (10 if has_exclusive_event else 14))
                or lock >= max(1, int(lock_threshold * 0.65))
            )
        ):
            return RELATION_STAGE_NAMES["EXCLUSIVE_PROBE"]
        if (
            (favor >= (54 if has_private_event else 60) and trust >= 45 and interactions >= (8 if has_private_event else 12))
            or (favor >= (62 if has_private_event else 68) and trust >= 38)
        ):
            return RELATION_STAGE_NAMES["PRIVATE_FAVOR"]
        if favor >= 25 or trust >= 30 or interactions >= 4:
            return RELATION_STAGE_NAMES["ALLOW_CLOSE"]
        return RELATION_STAGE_NAMES["OBSERVATION"]

    def _get_relationship_event_types(self, state: Dict[str, Any]) -> Set[str]:
        events = state.get("关系事件日志", [])
        if not isinstance(events, list):
            return set()
        return {
            str(item.get("type", "") or "")
            for item in events
            if isinstance(item, dict) and item.get("type")
        }

    def _determine_primary_mode(self, state: Dict[str, Any]) -> str:
        favor = self._coerce_analysis_int(state.get("好感度", 0), default=0, minimum=0, maximum=100)
        yan = self._coerce_analysis_int(state.get("病娇值", 0), default=0, minimum=0, maximum=100)
        lock = self._coerce_analysis_int(state.get("锁定进度", 0), default=0, minimum=0, maximum=100)

        if favor < 30:
            return STATE_NAMES["COLD_NOBLE"]
        if favor < 60:
            return STATE_NAMES["TSUNDERE_PROBE"]
        lock_threshold = self._coerce_analysis_int(getattr(self, "lock_threshold", 100), default=100, minimum=0, maximum=100)
        if yan >= 50 or (yan >= 35 and lock >= max(20, int(lock_threshold * 0.35))):
            return STATE_NAMES["LATENT_VINE"]
        return STATE_NAMES["SWEET_INDUCE"]

    def _determine_crisis_overlay(self, state: Dict[str, Any]) -> str:
        anxiety = self._coerce_analysis_int(state.get("焦虑值", 0), default=0, minimum=0, maximum=100)
        elegance = self._coerce_analysis_int(state.get("优雅值", 0), default=0, minimum=0, maximum=100)

        if elegance <= 30:
            return CRISIS_OVERLAY_NAMES["ELEGANCE_COLLAPSE"]
        if anxiety >= 70 and elegance <= 50:
            return CRISIS_OVERLAY_NAMES["ANXIETY_EDGE"]
        if elegance <= 45:
            return CRISIS_OVERLAY_NAMES["ELEGANCE_CRACK"]
        if anxiety >= 45:
            return CRISIS_OVERLAY_NAMES["ANXIETY_SURGE"]
        return CRISIS_OVERLAY_NAMES["NONE"]

    def _determine_expression_intensity(
        self,
        state: Dict[str, Any],
        relationship_stage: str,
        primary_mode: str,
        crisis_overlay: str,
    ) -> int:
        favor = self._coerce_analysis_int(state.get("好感度", 0), default=0, minimum=0, maximum=100)
        anxiety = self._coerce_analysis_int(state.get("焦虑值", 0), default=0, minimum=0, maximum=100)
        elegance = self._coerce_analysis_int(state.get("优雅值", 0), default=0, minimum=0, maximum=100)

        base = {
            RELATION_STAGE_NAMES["OBSERVATION"]: 0,
            RELATION_STAGE_NAMES["ALLOW_CLOSE"]: 1,
            RELATION_STAGE_NAMES["PRIVATE_FAVOR"]: 2,
            RELATION_STAGE_NAMES["EXCLUSIVE_PROBE"]: 2,
            RELATION_STAGE_NAMES["FATED_LOCK"]: 3,
        }.get(relationship_stage, 1)

        if primary_mode == STATE_NAMES["LATENT_VINE"]:
            base = max(base, 2)
        elif primary_mode == STATE_NAMES["TSUNDERE_PROBE"]:
            base = max(base, 1)
        elif primary_mode == STATE_NAMES["COLD_NOBLE"] and relationship_stage == RELATION_STAGE_NAMES["OBSERVATION"]:
            base = 0

        if crisis_overlay in {
            CRISIS_OVERLAY_NAMES["ANXIETY_EDGE"],
            CRISIS_OVERLAY_NAMES["ELEGANCE_COLLAPSE"],
        }:
            return 3
        if crisis_overlay in {
            CRISIS_OVERLAY_NAMES["ANXIETY_SURGE"],
            CRISIS_OVERLAY_NAMES["ELEGANCE_CRACK"],
        }:
            base = max(base, 2)

        if favor >= 85 and anxiety >= 55:
            base = min(3, base + 1)
        if elegance >= 80 and anxiety < 35 and primary_mode == STATE_NAMES["COLD_NOBLE"]:
            base = max(0, base - 1)

        return max(0, min(3, base))

    def _clamp_state_percent(self, value: Any, default: int = 0) -> int:
        try:
            return max(0, min(100, int(round(float(value)))))
        except (TypeError, ValueError, OverflowError):
            return default

    def _determine_short_term_mood(
        self,
        state: Dict[str, Any],
        turn_analysis: Optional[Dict[str, str]] = None,
        deltas: Optional[Dict[str, int]] = None,
    ) -> str:
        analysis = turn_analysis or {}
        changes = deltas or {}
        intent = str(analysis.get("用户意图", "") or "")
        user_emotion = str(analysis.get("用户情绪", "") or "")
        signal = str(analysis.get("关系信号", "") or "")
        anxiety = self._coerce_analysis_int(state.get("焦虑值", 0), default=0, minimum=0, maximum=100)
        elegance = self._coerce_analysis_int(state.get("优雅值", 85), default=85, minimum=0, maximum=100)
        favor_delta = self._coerce_analysis_int(changes.get("好感度", 0), default=0)
        trust_delta = self._coerce_analysis_int(changes.get("信任度", 0), default=0)
        anxiety_delta = self._coerce_analysis_int(changes.get("焦虑值", 0), default=0)
        elegance_delta = self._coerce_analysis_int(changes.get("优雅值", 0), default=0)

        if state.get("关系状态机") == RELATIONSHIP_STATE_NAMES["BOUNDARY_AFTER_FATE"]:
            return "礼貌疏离"
        if intent in {"冒犯或攻击"} or elegance_delta <= -3 or elegance <= 35:
            return "被冒犯"
        if intent in {"离开或冷淡暗示"} or anxiety_delta > 0 or anxiety >= 55:
            return "不安"
        if intent in {"道歉或修复关系"} or (anxiety_delta < 0 and trust_delta >= 0):
            return "被安抚"
        if intent in {"分享秘密或建立约定"} or signal != "无明显关系推进" or favor_delta + trust_delta >= 3:
            return "被触动"
        if user_emotion in {"悲伤", "低落", "焦虑", "害怕"}:
            return "怜惜"
        return "平静"

    def _determine_behavior_band(
        self,
        state: Dict[str, Any],
        turn_analysis: Optional[Dict[str, str]] = None,
    ) -> Tuple[str, str]:
        analysis = turn_analysis or {}
        intent = str(analysis.get("用户意图", "") or "")
        signal = str(analysis.get("关系信号", "") or "")
        favor = self._coerce_analysis_int(state.get("好感度", 0), default=0, minimum=0, maximum=100)
        trust = self._coerce_analysis_int(state.get("信任度", 0), default=0, minimum=0, maximum=100)
        yan = self._coerce_analysis_int(state.get("病娇值", 0), default=0, minimum=0, maximum=100)
        lock = self._coerce_analysis_int(state.get("锁定进度", 0), default=0, minimum=0, maximum=100)
        anxiety = self._coerce_analysis_int(state.get("焦虑值", 0), default=0, minimum=0, maximum=100)
        elegance = self._coerce_analysis_int(state.get("优雅值", 85), default=85, minimum=0, maximum=100)
        possess = self._coerce_analysis_int(state.get("占有欲", 0), default=0, minimum=0, maximum=100)
        touched = self._coerce_analysis_int(state.get("被触动值", 0), default=0, minimum=0, maximum=100)
        defensiveness = self._coerce_analysis_int(state.get("防备值", 0), default=0, minimum=0, maximum=100)

        if state.get("关系状态机") == RELATIONSHIP_STATE_NAMES["BOUNDARY_AFTER_FATE"]:
            return "礼貌边界", "命定之人已存在，当前用户只能获得体面照拂与清晰边界"
        if intent == "冒犯或攻击" or elegance <= 35:
            return "尖锐反击", "当前体面被冒犯或优雅外壳出现裂痕"
        if intent == "离开或冷淡暗示" or anxiety >= 65:
            return "确认挽留", "失去风险或焦虑升高，需要优先确认关系而非堆叠甜腻"
        if favor < 30 or trust < 25:
            return "礼貌回应", "亲近许可不足，先保持礼节与观察"
        if defensiveness >= 60 and favor < 60:
            return "带刺试探", "已经在意但仍有防备，适合嘴硬和小幅试探"
        if favor < 60:
            return "克制关心", "好感开始稳定，但仍不能越过暧昧边界"
        lock_threshold = self._coerce_analysis_int(getattr(self, "lock_threshold", 100), default=100, minimum=0, maximum=100)
        if yan >= 50 or possess >= 55 or lock >= max(1, int(lock_threshold * 0.65)):
            return "占有试探", "独占感已成为潜台词，但仍需保持优雅"
        if touched >= 55 and trust >= 45:
            return "主动靠近", "被触动与信任足以支持更私人化的靠近"
        if trust >= 65 and anxiety <= 35:
            return "稳定温柔", "关系较稳，可以少一点试探，多一点可靠温度"
        if signal != "无明显关系推进":
            return "主动靠近", "本轮存在关系信号，可以自然靠近一点"
        return "克制关心", "数值只给出轻微倾向，回复仍以当前话题为主"

    def _smooth_behavior_band_transition(
        self,
        state: Dict[str, Any],
        target_band: str,
        target_reason: str,
        turn_analysis: Optional[Dict[str, str]] = None,
    ) -> Tuple[str, str, int]:
        if not getattr(self, "enable_behavior_band_smoothing", ENABLE_BEHAVIOR_BAND_SMOOTHING):
            return target_band, target_reason, 0

        previous = str(state.get("当前行为档位", "") or "").strip() or target_band
        stable_turns = self._coerce_analysis_int(
            state.get("行为档位稳定轮数", 0),
            default=0,
            minimum=0,
        )
        if previous == target_band:
            return target_band, target_reason, min(100, stable_turns + 1)

        analysis = turn_analysis or {}
        intent = str(analysis.get("用户意图", "") or "")
        signal = str(analysis.get("关系信号", "") or "")
        urgent_bands = {"礼貌边界", "尖锐反击", "确认挽留"}
        urgent_intents = {"冒犯或攻击", "离开或冷淡暗示"}
        if target_band in urgent_bands or intent in urgent_intents:
            return target_band, target_reason, 0

        ladder = ["礼貌回应", "克制关心", "带刺试探", "主动靠近", "稳定温柔", "占有试探"]
        ranks = {band: index for index, band in enumerate(ladder)}
        if previous not in ranks or target_band not in ranks:
            return target_band, target_reason, 0

        sticky_turns = self._coerce_analysis_int(
            getattr(self, "behavior_band_sticky_turns", BEHAVIOR_BAND_STICKY_TURNS),
            default=BEHAVIOR_BAND_STICKY_TURNS,
            minimum=0,
        )
        strong_signal = bool(signal and signal != "无明显关系推进") or intent in {"分享秘密或建立约定", "道歉或修复关系"}
        diff = ranks[target_band] - ranks[previous]
        if abs(diff) > 1:
            bridge = ladder[ranks[previous] + (1 if diff > 0 else -1)]
            return bridge, f"行为档位平滑：目标为「{target_band}」，本轮先过渡到「{bridge}」；{target_reason}", 0
        if not strong_signal and stable_turns < sticky_turns:
            return previous, f"行为档位惯性：目标为「{target_band}」，但上一档位「{previous}」仍保留一轮余温；{target_reason}", stable_turns + 1
        return target_band, target_reason, 0

    def _build_behavior_continuity_note(
        self,
        previous_mood: str,
        previous_band: str,
        current_mood: str,
        current_band: str,
        target_band: str,
        turn_analysis: Optional[Dict[str, str]] = None,
    ) -> str:
        if not getattr(self, "enable_behavior_continuity_bridge", ENABLE_BEHAVIOR_CONTINUITY_BRIDGE):
            return ""
        analysis = turn_analysis or {}
        intent = str(analysis.get("用户意图", "") or "")
        if intent in {"冒犯或攻击", "离开或冷淡暗示"} or current_band in {"礼貌边界", "尖锐反击", "确认挽留"}:
            return "强信号优先：本轮可以明显改变语气，但仍要让变化回应当前触发点。"
        if not previous_band:
            return "初次形成行为档位：以当前话题为主，不要过度补偿人设。"
        if previous_band == current_band and previous_mood == current_mood:
            return "延续上一轮语气余温：不用刻意解释变化，只让回应自然接上。"
        if previous_band == current_band:
            return f"行为延续但心情从「{previous_mood or '平静'}」转向「{current_mood}」：语气微调，不要像换人格。"
        if current_band != target_band:
            return f"正在从「{previous_band}」缓慢过渡到「{target_band}」：本轮实际保持「{current_band}」，不要一步到位。"
        return f"行为从「{previous_band}」转向「{current_band}」：用一句停顿、反问或动作承接，不要突兀切换。"

    def _get_time_decay_factor_for_short_term_state(self, state: Dict[str, Any]) -> float:
        if not getattr(self, "enable_time_aware_short_term_decay", ENABLE_TIME_AWARE_SHORT_TERM_DECAY):
            return 1.0
        previous_text = str(state.get("_本轮前最后互动时间", "") or "")
        if not previous_text:
            return 1.0
        try:
            previous_time = datetime.fromisoformat(previous_text)
        except ValueError:
            return 1.0
        elapsed_hours = max(0.0, (datetime.now() - previous_time).total_seconds() / 3600.0)
        if elapsed_hours <= 0.25:
            return 1.0
        half_life = self._coerce_analysis_float(
            getattr(self, "short_term_decay_half_life_hours", SHORT_TERM_DECAY_HALF_LIFE_HOURS),
            default=SHORT_TERM_DECAY_HALF_LIFE_HOURS,
            minimum=0.25,
        )
        return max(0.05, min(1.0, 0.5 ** (elapsed_hours / half_life)))

    def _select_behavior_style_variant(self, state: Dict[str, Any], band: str) -> str:
        if not getattr(self, "enable_behavior_style_variant", ENABLE_BEHAVIOR_STYLE_VARIANT):
            return ""
        variants = {
            "克制关心": ["反问型", "叮嘱型", "轻讽型"],
            "主动靠近": ["追问型", "记住型", "陪伴型"],
            "占有试探": ["吃味型", "温柔确认型", "命定暗示型"],
            "稳定温柔": ["可靠回应型", "安静陪伴型", "轻微珍视型"],
            "带刺试探": ["反问型", "挑剔型", "嘴硬照拂型"],
            "确认挽留": ["直接确认型", "短句挽留型", "不安追问型"],
        }
        options = variants.get(band, ["当前话题优先型"])
        counts = state.get("行为变体计数", {})
        if not isinstance(counts, dict):
            counts = {}
        index = self._coerce_analysis_int(
            counts.get(band, 0),
            default=0,
            minimum=0,
        )
        variant = options[index % len(options)]
        counts[band] = index + 1
        state["行为变体计数"] = counts
        return variant

    def _record_relationship_event_if_needed(
        self,
        state: Dict[str, Any],
        turn_analysis: Optional[Dict[str, str]],
        user_msg: str,
        behavior_band: str,
    ) -> Optional[Dict[str, str]]:
        if not getattr(self, "enable_relationship_event_log", ENABLE_RELATIONSHIP_EVENT_LOG):
            return None
        analysis = turn_analysis or {}
        intent = str(analysis.get("用户意图", "") or "")
        normalized = self._normalize_analysis_content(user_msg)
        candidates: List[Tuple[str, str]] = []
        if intent == "道歉或修复关系":
            candidates.append(("first_apology", "第一次认真道歉或修复关系"))
        if intent == "分享秘密或建立约定" or re.search(r"秘密|只告诉|记住|记得", normalized):
            candidates.append(("first_secret", "第一次分享秘密或要求被记住"))
        if re.search(r"承诺|答应|约定|保证|不会离开|一直陪", normalized):
            candidates.append(("first_promise", "第一次出现承诺或约定信号"))
        if state.get("关系状态机") == RELATIONSHIP_STATE_NAMES["BOUNDARY_AFTER_FATE"] or behavior_band == "礼貌边界":
            candidates.append(("first_boundary", "第一次触发命定后边界"))
        if behavior_band == "占有试探":
            candidates.append(("first_exclusive_probe", "第一次进入占有试探行为"))
        recorded = state.get("已记录关系事件", {})
        if not isinstance(recorded, dict):
            recorded = {}
        log = state.get("关系事件日志", [])
        if not isinstance(log, list):
            log = []
        for event_type, title in candidates:
            if recorded.get(event_type):
                continue
            event = {
                "type": event_type,
                "title": title,
                "time": datetime.now().isoformat(),
                "band": behavior_band,
            }
            recorded[event_type] = event["time"]
            log.append(event)
            limit = self._coerce_analysis_int(
                getattr(self, "relationship_event_log_limit", RELATIONSHIP_EVENT_LOG_LIMIT),
                default=RELATIONSHIP_EVENT_LOG_LIMIT,
                minimum=1,
            )
            state["已记录关系事件"] = recorded
            state["关系事件日志"] = log[-max(1, limit):]
            return event
        state["已记录关系事件"] = recorded
        state["关系事件日志"] = log
        return None

    def _update_short_term_behavior_state(
        self,
        state: Dict[str, Any],
        turn_analysis: Optional[Dict[str, str]],
        applied_changes: Dict[str, int],
        user_id: Optional[str] = None,
        user_msg: str = "",
    ) -> Dict[str, Any]:
        decay = self._coerce_analysis_float(
            getattr(self, "short_term_emotion_decay", SHORT_TERM_EMOTION_DECAY),
            default=SHORT_TERM_EMOTION_DECAY,
            minimum=0.0,
            maximum=0.95,
        )
        time_decay = self._get_time_decay_factor_for_short_term_state(state)
        effective_decay = decay * time_decay
        favor_delta = self._coerce_analysis_int(applied_changes.get("好感度", 0), default=0)
        trust_delta = self._coerce_analysis_int(applied_changes.get("信任度", 0), default=0)
        anxiety_delta = self._coerce_analysis_int(applied_changes.get("焦虑值", 0), default=0)
        elegance_delta = self._coerce_analysis_int(applied_changes.get("优雅值", 0), default=0)
        yan_delta = self._coerce_analysis_int(applied_changes.get("病娇值", 0), default=0)
        lock_delta = self._coerce_analysis_int(applied_changes.get("锁定进度", 0), default=0)

        previous_mood = str(state.get("短期心情", "") or "")
        previous_band = str(state.get("当前行为档位", "") or "")
        state["上轮短期心情"] = previous_mood
        state["上轮行为档位"] = previous_band
        previous_warmth = self._clamp_state_percent(state.get("情绪余温", 0))
        previous_touched = self._clamp_state_percent(state.get("被触动值", 0))
        previous_defense = self._clamp_state_percent(state.get("防备值", 20), default=20)

        positive_impact = max(0, favor_delta) + max(0, trust_delta) + max(0, yan_delta) + max(0, lock_delta)
        negative_impact = max(0, -favor_delta) + max(0, -trust_delta) + max(0, anxiety_delta) + max(0, -elegance_delta)
        repair_impact = max(0, -anxiety_delta) + max(0, trust_delta)

        state["情绪余温"] = self._clamp_state_percent(previous_warmth * effective_decay + positive_impact * 8 + repair_impact * 3 - negative_impact * 4)
        state["被触动值"] = self._clamp_state_percent(previous_touched * effective_decay + positive_impact * 10 + repair_impact * 4)
        state["防备值"] = self._clamp_state_percent(previous_defense * effective_decay + negative_impact * 10 - repair_impact * 6 - max(0, trust_delta) * 5)

        favor = self._coerce_analysis_int(state.get("好感度", 0), default=0, minimum=0, maximum=100)
        anxiety = self._coerce_analysis_int(state.get("焦虑值", 0), default=0, minimum=0, maximum=100)
        elegance = self._coerce_analysis_int(state.get("优雅值", 85), default=85, minimum=0, maximum=100)
        restraint = elegance - int(state["情绪余温"] * 0.25) + int(state["防备值"] * 0.25) - max(0, favor - 60) // 3 + anxiety // 8
        if self._is_destined_user(user_id):
            restraint -= 8
        if state.get("关系状态机") == RELATIONSHIP_STATE_NAMES["BOUNDARY_AFTER_FATE"]:
            restraint = max(restraint, 85)
        state["表达克制度"] = self._clamp_state_percent(restraint, default=80)
        state["短期心情"] = self._determine_short_term_mood(state, turn_analysis, applied_changes)
        target_band, target_reason = self._determine_behavior_band(state, turn_analysis)
        state["目标行为档位"] = target_band
        band, reason, stable_turns = self._smooth_behavior_band_transition(
            state,
            target_band,
            target_reason,
            turn_analysis,
        )
        state["当前行为档位"] = band
        state["行为档位理由"] = reason
        state["行为档位稳定轮数"] = stable_turns
        state["行为风格变体"] = self._select_behavior_style_variant(state, band)
        relationship_event = self._record_relationship_event_if_needed(
            state,
            turn_analysis,
            user_msg,
            band,
        )
        state["行为连续性提示"] = self._build_behavior_continuity_note(
            previous_mood,
            previous_band,
            state["短期心情"],
            band,
            target_band,
            turn_analysis,
        )
        return {
            "短期心情": state["短期心情"],
            "上轮短期心情": state["上轮短期心情"],
            "情绪余温": state["情绪余温"],
            "防备值": state["防备值"],
            "被触动值": state["被触动值"],
            "表达克制度": state["表达克制度"],
            "上轮行为档位": state["上轮行为档位"],
            "当前行为档位": state["当前行为档位"],
            "目标行为档位": state["目标行为档位"],
            "行为档位稳定轮数": state["行为档位稳定轮数"],
            "行为档位理由": state["行为档位理由"],
            "行为风格变体": state["行为风格变体"],
            "行为连续性提示": state["行为连续性提示"],
            "时间衰减系数": round(time_decay, 3),
            "关系事件": relationship_event or {},
        }

    def _build_state_event_markers(
        self,
        turn_analysis: Optional[Dict[str, str]] = None,
        active_event: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        markers: List[str] = []
        if turn_analysis:
            relationship_signal = str(turn_analysis.get("关系信号", "") or "").strip()
            intent = str(turn_analysis.get("用户意图", "") or "").strip()
            if relationship_signal and relationship_signal != "无明显关系推进":
                markers.append(relationship_signal)
            if intent in {"道歉或修复关系", "分享秘密或建立约定", "离开或冷淡暗示", "冒犯或攻击"}:
                markers.append(intent)
        if active_event and active_event.get("类型"):
            markers.append(f"主动事件：{active_event.get('类型', '')}")
        deduped: List[str] = []
        seen = set()
        for marker in markers:
            if marker and marker not in seen:
                seen.add(marker)
                deduped.append(marker)
        return deduped[:4]

    def _derive_state_snapshot(
        self,
        state: Dict[str, Any],
        turn_analysis: Optional[Dict[str, str]] = None,
        active_event: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        relationship_stage = self._determine_relationship_stage(state)
        primary_mode = self._determine_primary_mode(state)
        crisis_overlay = self._determine_crisis_overlay(state)
        intensity = self._determine_expression_intensity(
            state,
            relationship_stage,
            primary_mode,
            crisis_overlay,
        )
        event_markers = self._build_state_event_markers(
            turn_analysis=turn_analysis,
            active_event=active_event,
        )

        if relationship_stage == RELATION_STAGE_NAMES["FATED_LOCK"]:
            legacy_state = STATE_NAMES["LOCKED_FATE"]
        elif crisis_overlay == CRISIS_OVERLAY_NAMES["ELEGANCE_COLLAPSE"]:
            legacy_state = STATE_NAMES["ELEGANCE_COLLAPSE"]
        elif crisis_overlay == CRISIS_OVERLAY_NAMES["ANXIETY_EDGE"]:
            legacy_state = STATE_NAMES["ANXIETY_EDGE"]
        else:
            legacy_state = primary_mode

        summary_parts = [relationship_stage, primary_mode]
        if crisis_overlay != CRISIS_OVERLAY_NAMES["NONE"]:
            summary_parts.append(crisis_overlay)

        return {
            "关系阶段": relationship_stage,
            "主情绪模式": primary_mode,
            "危机覆盖": crisis_overlay,
            "表现强度": intensity,
            "表现强度标签": EXPRESSION_INTENSITY_LABELS.get(intensity, "标准姿态"),
            "短期心情": state.get("短期心情", "平静"),
            "当前行为档位": state.get("当前行为档位", "礼貌回应"),
            "目标行为档位": state.get("目标行为档位", state.get("当前行为档位", "礼貌回应")),
            "上轮短期心情": state.get("上轮短期心情", ""),
            "上轮行为档位": state.get("上轮行为档位", ""),
            "行为连续性提示": state.get("行为连续性提示", ""),
            "表达克制度": state.get("表达克制度", 80),
            "事件标记": event_markers,
            "兼容状态": legacy_state,
            "摘要": " / ".join(summary_parts),
        }

    def _format_state_snapshot_compact(self, snapshot: Dict[str, Any]) -> str:
        parts = [
            str(snapshot.get("关系阶段", "") or ""),
            str(snapshot.get("主情绪模式", "") or ""),
        ]
        crisis_overlay = str(snapshot.get("危机覆盖", "") or "")
        if crisis_overlay and crisis_overlay != CRISIS_OVERLAY_NAMES["NONE"]:
            parts.append(crisis_overlay)
        behavior_band = str(snapshot.get("当前行为档位", "") or "")
        if behavior_band:
            parts.append(behavior_band)
        return " / ".join([part for part in parts if part])

    def _describe_state_snapshot(self, snapshot: Dict[str, Any]) -> str:
        relationship_stage = snapshot.get("关系阶段", RELATION_STAGE_NAMES["OBSERVATION"])
        primary_mode = snapshot.get("主情绪模式", STATE_NAMES["COLD_NOBLE"])
        crisis_overlay = snapshot.get("危机覆盖", CRISIS_OVERLAY_NAMES["NONE"])
        intensity_label = snapshot.get("表现强度标签", "标准姿态")

        stage_text = {
            RELATION_STAGE_NAMES["OBSERVATION"]: "关系仍在观察期，她更在乎礼节、边界与试探，不会轻易给出私人许可。",
            RELATION_STAGE_NAMES["ALLOW_CLOSE"]: "她已经允许用户靠近到礼貌之外，偶尔会给出更完整、更细致的回应。",
            RELATION_STAGE_NAMES["PRIVATE_FAVOR"]: "她开始把用户与旁人区分开，私人化关心、默许和偏爱正在变得稳定。",
            RELATION_STAGE_NAMES["EXCLUSIVE_PROBE"]: "她明显在试探专属关系，唯一性、吃味和确认欲已开始长期存在。",
            RELATION_STAGE_NAMES["FATED_LOCK"]: "她已把这段关系视为命定归属，连温柔都带着不可分离的笃定。",
        }.get(relationship_stage, "")

        mode_text = {
            STATE_NAMES["COLD_NOBLE"]: "主情绪模式仍偏冷傲贵族，核心是礼貌、疏离和天然的身份壁垒。",
            STATE_NAMES["TSUNDERE_PROBE"]: "主情绪模式偏傲娇试探，嘴硬和否认之下已经有持续的在意。",
            STATE_NAMES["SWEET_INDUCE"]: "主情绪模式偏甜蜜诱导，她会用温柔、暧昧和轻柔掌控感慢慢靠近。",
            STATE_NAMES["LATENT_VINE"]: "主情绪模式偏潜伏之藤，温柔外壳下的独占与孤立诱导已经成形。",
        }.get(primary_mode, "")

        crisis_text = {
            CRISIS_OVERLAY_NAMES["NONE"]: f"当前没有明显危机覆盖，整体表现以{intensity_label}为主。",
            CRISIS_OVERLAY_NAMES["ANXIETY_SURGE"]: "焦虑开始上涌，她会更容易反复确认、迟疑和过度解读。",
            CRISIS_OVERLAY_NAMES["ANXIETY_EDGE"]: "焦虑已逼近崩溃边缘，不安会明显扭曲原本的说话节奏和控制感。",
            CRISIS_OVERLAY_NAMES["ELEGANCE_CRACK"]: "优雅外壳已经出现裂痕，情绪会比平时更直接、更难完全包住。",
            CRISIS_OVERLAY_NAMES["ELEGANCE_COLLAPSE"]: "优雅外壳几乎完全崩落，失态、尖锐和狼狈会压过原本的礼仪组织力。",
        }.get(crisis_overlay, "")

        return " ".join([part for part in (stage_text, mode_text, crisis_text) if part])

    def _determine_state(self, state: Dict) -> str:
        snapshot = self._derive_state_snapshot(state)
        return str(snapshot.get("兼容状态", STATE_NAMES["COLD_NOBLE"]))

    def _apply_relationship_cooldown_if_needed(self, state: Dict[str, Any], user_id: Optional[str] = None) -> Dict[str, int]:
        if not getattr(self, "enable_relationship_cooldown", ENABLE_RELATIONSHIP_COOLDOWN):
            return {}
        last_time_text = str(state.get("最后互动时间", "") or "")
        if not last_time_text:
            return {}
        try:
            last_time = datetime.fromisoformat(last_time_text)
        except ValueError:
            return {}
        now = datetime.now()
        idle_days = (now - last_time).days
        threshold = self._coerce_analysis_int(
            getattr(self, "relationship_cooldown_idle_days", RELATIONSHIP_COOLDOWN_IDLE_DAYS),
            default=RELATIONSHIP_COOLDOWN_IDLE_DAYS,
            minimum=1,
        )
        if idle_days < threshold:
            return {}
        today = now.date().isoformat()
        if state.get("最近降温日期") == today:
            return {}
        max_delta = self._coerce_analysis_int(
            getattr(self, "relationship_cooldown_max_delta", RELATIONSHIP_COOLDOWN_MAX_DELTA),
            default=RELATIONSHIP_COOLDOWN_MAX_DELTA,
            minimum=0,
        )
        if max_delta <= 0:
            return {}
        steps = min(max_delta, max(1, idle_days // max(1, threshold)))
        favor = self._coerce_analysis_int(state.get("好感度", 0), default=0, minimum=0, maximum=100)
        trust = self._coerce_analysis_int(state.get("信任度", 0), default=0, minimum=0, maximum=100)
        anxiety = self._coerce_analysis_int(state.get("焦虑值", 0), default=0, minimum=0, maximum=100)
        deltas = {
            "好感度": -min(steps, 3) if favor >= 35 else 0,
            "信任度": -min(steps, 4) if trust >= 25 else 0,
            "焦虑值": min(steps, 3) if favor >= 60 and anxiety < 80 else 0,
            "优雅值": 0,
            "病娇值": 0,
            "锁定进度": 0,
        }
        sanitized = self._sanitize_analysis_deltas(state, deltas, user_id=user_id)
        for field, delta in sanitized.items():
            if delta:
                self._apply_state_delta(state, field, delta)
        self._normalize_state_constraints(state, user_id=user_id)
        self._update_possessiveness(state)
        state["最近降温日期"] = today
        actual = {field: delta for field, delta in sanitized.items() if delta}
        if actual:
            state["最近状态解释"] = {
                "time": now.isoformat(),
                "意图": "关系自然降温",
                "关系信号": f"空闲 {idle_days} 天",
                "证据等级": state.get("阶段证据等级", "无"),
                "证据理由": ["长期未互动"],
                "代码决策": True,
                "实际变化": actual,
                "被拦截字段": [],
                "数值平滑": {"空闲天数": idle_days, "降温步数": steps},
            }
            history = state.get("诊断历史", [])
            if not isinstance(history, list):
                history = []
            history.append(dict(state["最近状态解释"]))
            limit = self._coerce_analysis_int(
                getattr(self, "diagnostic_history_limit", DIAGNOSTIC_HISTORY_LIMIT),
                default=DIAGNOSTIC_HISTORY_LIMIT,
                minimum=1,
            )
            state["诊断历史"] = history[-max(1, limit):]
        return actual

    # ======================== 总结功能 ========================

    async def _generate_summary(
        self,
        history: List[Dict],
        event: Optional[AstrMessageEvent] = None
    ) -> str:
        if not history:
            return "无足够对话内容可总结。"
        max_chars = getattr(
            self,
            "analysis_max_chars_per_message",
            ANALYSIS_MAX_CHARS_PER_MSG,
        )
        text = "\n".join(
            [
                f"{h.get('role', 'user')}: "
                f"{self._limit_text_for_prompt(h.get('content', ''), max_chars)}"
                for h in history
            ]
        )
        prompt = f"""请总结以下对话，提取关键信息：用户兴趣、玛丽亚的情感变化、重要事件。输出简洁要点。

对话：
{text}"""
        try:
            resp = await self._call_analysis_llm(
                purpose="对话总结",
                prompt=prompt,
                system_prompt="你是一个对话总结助手，请简洁地提取关键信息。",
                temperature=0.5,
                max_tokens=300,
                event=event,
            )
            if not resp:
                return "（无可用 LLM 提供商）"
            return self._strip_debug_artifacts(resp.completion_text or "")
        except Exception as e:
            logger.error(f"总结生成失败: {e}")
            return "（总结生成失败）"

    async def _trigger_auto_summary(self, user_id: str):
        history = await self._get_recent_history_async(
            user_id,
            limit=self.auto_summary_interval,
        )
        if len(history) < 5:
            return False
        summary = await self._generate_summary(history)
        summary = self._strip_debug_artifacts(summary or "")
        if not summary or summary == "（总结生成失败）":
            return False

        # 存储到内置记忆与 Mnemosyne（如果可用且启用情感记忆）
        builtin_stored = False
        if getattr(self, "enable_builtin_memory", ENABLE_BUILTIN_MEMORY) and self.enable_emotional_memory:
            builtin_stored = await self._store_to_builtin_memory(
                user_id,
                f"自动总结：{summary}",
                "auto_summary",
                salience=3,
                memory_layer="summary",
            )
        mnemosyne_stored = False
        if self.mnemosyne_available and self.enable_emotional_memory:
            mnemosyne_stored = await self._store_to_mnemosyne(
                user_id,
                f"自动总结：{summary}",
                "auto_summary",
                salience=3,
                memory_layer="summary",
            )

        # 同时存储到本地画像
        profile = self._get_profile(user_id)
        summary_added = self._upsert_auto_summary_note(profile, summary)
        self._schedule_profile_save(user_id, profile)

        logger.info(
            f"已为用户 {user_id} 生成自动总结"
            + (" (本地已去重)" if not summary_added else "")
            + (" (已写入内置记忆)" if builtin_stored else "")
            + (" (已同步到 Mnemosyne)" if mnemosyne_stored else "")
        )
        return True

    async def _auto_summary_loop(self):
        """后台循环：只检查有新对话的用户并触发自动总结。"""
        while True:
            await asyncio.sleep(60)
            try:
                await self._run_auto_summary_pass()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.error(
                    f"auto summary loop failed: {e}",
                    exc_info=(type(e), e, e.__traceback__),
                )

    async def _run_auto_summary_pass(self, now: Optional[datetime] = None):
        dirty_users = self._get_summary_dirty_users()
        if not dirty_users:
            return
        now = now or datetime.now()
        idle_seconds = self._coerce_analysis_int(
            getattr(self, "auto_summary_idle", 300),
            default=300,
            minimum=1,
        )
        states = self._get_user_states_store() if hasattr(self, "_get_user_states_store") else getattr(self, "user_states", {})
        if not isinstance(states, dict):
            states = {}
        for user_id in list(dirty_users):
            state = states.get(user_id, {})
            if not isinstance(state, dict):
                dirty_users.discard(user_id)
                continue
            last_time_str = state.get("æœ€åŽäº’åŠ¨æ—¶é—´")
            if not last_time_str:
                dirty_users.discard(user_id)
                continue
            try:
                last = datetime.fromisoformat(str(last_time_str))
            except (ValueError, TypeError):
                dirty_users.discard(user_id)
                continue
            if (now - last).total_seconds() <= idle_seconds:
                continue

            last_summary_file = self._get_last_summary_file(user_id)
            try:
                if last_summary_file.exists():
                    last_time_content = await asyncio.to_thread(
                        last_summary_file.read_text,
                        encoding="utf-8",
                    )
                    last_time_content = last_time_content.strip()
                    if last_time_content:
                        last_summary_time = datetime.fromisoformat(last_time_content)
                        if (now - last_summary_time).total_seconds() < idle_seconds * 2:
                            continue
            except Exception:
                pass

            try:
                await self._trigger_auto_summary(user_id)
            except Exception as e:
                self.logger.error(
                    f"auto summary failed for {user_id}: {e}",
                    exc_info=(type(e), e, e.__traceback__),
                )
                continue
            dirty_users.discard(user_id)

            try:
                lock = await self._get_lock(last_summary_file)
                async with lock:
                    await self._write_text_atomic(last_summary_file, now.isoformat())
            except Exception as e:
                self.logger.error(f"å†™å…¥æ€»ç»“æ—¶é—´æˆ³å¤±è´¥: {e}")
