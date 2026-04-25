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
        scan_limit = max(0, int(limit or 0))
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
        latest_terms = self._extract_mnemosyne_terms(latest_user_msg)
        if latest_terms or len(latest_normalized) >= 8:
            lookback = scan_limit
        else:
            lookback = min(scan_limit, max(1, int(recent_context_limit or 0)))
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

        recent_context_count = max(0, int(recent_context_limit or 0))
        relevant_count = max(0, int(relevant_limit or 0))
        selected_indexes = {
            entry["index"]
            for entry in candidate_entries[-recent_context_count:]
        } if recent_context_count else set()

        scored_entries = [
            entry for entry in candidate_entries
            if entry.get("score", 0) > 0 and entry["index"] not in selected_indexes
        ]
        scored_entries.sort(
            key=lambda entry: (
                int(entry.get("score", 0)),
                int(entry.get("index", 0)),
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
        salience = int(entry.get("salience", 0) or 0)
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
    ) -> List[Dict[str, str]]:
        started_at = time.perf_counter()
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
        if self.mnemosyne_available and self.enable_emotional_memory and mnemosyne_limit > 0:
            try:
                memories = await self._retrieve_from_mnemosyne(
                    user_id,
                    latest_user_msg,
                    limit=mnemosyne_limit,
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
    ) -> str:
        payload = {
            "session_key": session_key,
            "user_msg": self._normalize_analysis_content(user_msg),
            "history": [
                {
                    "role": item.get("role", "user"),
                    "content": self._normalize_analysis_content(item.get("content", "")),
                }
                for item in history_entries
            ],
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
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
            return float(self.favor_multiplier)
        if field == "病娇值":
            return float(self.yan_multiplier)
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
            current_value = max(0, min(100, int(state.get(field, 0) or 0)))
            if raw_delta > 0:
                attenuation_start = 20 if field == "好感度" else 15
                if current_value > attenuation_start:
                    progress = min(1.0, (current_value - attenuation_start) / (100 - attenuation_start))
                    multiplier *= 1.0 - progress * 0.45

        if field == "焦虑值" and raw_delta < 0:
            anxiety = max(0, min(100, int(state.get("焦虑值", 0) or 0)))
            if anxiety > 60:
                anxiety_progress = min(1.0, (anxiety - 60) / 40.0)
                multiplier *= 1.0 + anxiety_progress * 0.6

        if field == "优雅值" and raw_delta > 0:
            elegance = max(0, min(100, int(state.get("优雅值", 0) or 0)))
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
        old_value = int(state.get(field, 0))
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

        if user_id and self._is_destined_user(user_id):
            state["好感度"] = max(60, int(state.get("好感度", 0) or 0))

        favor = int(state.get("好感度", 0))
        if favor < 30:
            state["病娇值"] = 0
            state["锁定进度"] = 0
            state["焦虑值"] = 0
        if favor < 60:
            state["病娇值"] = 0
            state["锁定进度"] = 0
            state["占有欲"] = 0
            state["已触发锁定事件"] = False

        if user_id and self._get_destined_one_info() and not self._is_destined_user(user_id):
            if int(state.get("锁定进度", 0)) >= self.lock_threshold:
                state["锁定进度"] = max(0, self.lock_threshold - 1)
                state["已触发锁定事件"] = False

        state["当前状态"] = self._determine_state(state)

    async def _reconcile_destined_one_state(self, user_id: str, state: Dict[str, Any]) -> bool:
        """确保全局命定记录与当前用户状态一致。"""
        if not self._is_destined_user(user_id):
            return False
        if int(state.get("锁定进度", 0) or 0) >= self.lock_threshold:
            return False
        await self._clear_destined_one()
        state["已触发锁定事件"] = False
        return True

    def _get_analysis_delta_limits(
        self,
        state: Dict[str, Any],
        user_id: Optional[str] = None,
    ) -> Dict[str, Tuple[int, int]]:
        favor = int(state.get("好感度", 0))
        trust = int(state.get("信任度", 0))
        yan = int(state.get("病娇值", 0))
        lock = int(state.get("锁定进度", 0))
        anxiety = int(state.get("焦虑值", 0))

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
            limits["病娇值"] = (min(limits["病娇值"][0], 0), 0)
            limits["锁定进度"] = (min(limits["锁定进度"][0], 0), 0)
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
        favor = int(state.get("好感度", 0))
        yan = int(state.get("病娇值", 0))
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
                "当前用户不是命定之人，因此病娇值与锁定进度不允许正向增长，占有欲也不应被理解为继续上升。"
            )
        elif user_id and self._is_destined_user(user_id):
            destined_rule = (
                "当前用户就是全局命定之人。"
                "无论本轮发生什么，好感度都不得被分析到 60 以下；若语义上需要表现失望、受伤或冷却，也只能在 60 以上波动。"
            )

        intro = f"{current_stage}\n"
        if destined_rule:
            intro += f"{destined_rule}\n"

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

    def _sanitize_analysis_deltas(
        self,
        state: Dict[str, Any],
        deltas: Dict[str, int],
        user_id: Optional[str] = None,
    ) -> Dict[str, int]:
        limits = self._get_analysis_delta_limits(state, user_id=user_id)
        sanitized: Dict[str, int] = {}
        for field, (low, high) in limits.items():
            value = int(deltas.get(field, 0) or 0)
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
            try:
                value = int(deltas.get(field, 0) or 0)
            except (TypeError, ValueError):
                value = 0
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
        elif re.search(r"喜欢|爱你|想你|抱|亲|陪我|只要你|只有你", normalized):
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

        if int(deltas.get("焦虑值", 0) or 0) > 0 and signal == "无明显关系推进":
            signal = "带来不安"
        elif int(deltas.get("信任度", 0) or 0) > 0 and signal == "无明显关系推进":
            signal = "增强信任"
        elif int(deltas.get("好感度", 0) or 0) > 0 and signal == "无明显关系推进":
            signal = "释放善意"

        return {
            "用户意图": intent,
            "用户情绪": emotion,
            "关系信号": signal,
            "回应目标": goal,
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
            if isinstance(value, (int, float)):
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
            entries = await self._get_analysis_memory_entries(user_id, user_msg)
        history_text = self._format_analysis_history_entries(entries)
        rules_text = self._build_analysis_rules_text(state, user_id=user_id)
        prompt = f"""你是“玛丽亚情绪状态分析器”。请根据用户最新发言和相关聊天记忆，判断本轮应如何修改数值。

只输出 JSON，不要输出解释、Markdown 或代码块。

规则：
1. 以语义理解为准，不要做关键词匹配式判断。
2. 大多数变更应当克制，优先返回 0、±1、±2；只有最新发言的语义证据非常强时，才允许接近当前阶段上限。
3. 严格遵守不同好感阶段和字段范围限制，不允许越级修改数值。
4. “优雅值”体现玛丽亚维持体面和克制的程度；被冒犯、羞辱、失控时下降，被赞美、尊重、温柔安抚时可小幅上升。
5. 信息不足时填 0；如果只是普通寒暄、礼貌回应或轻微试探，不要让多个字段同时大幅波动。
6. 本轮数值变化只根据“最新用户发言 + 与它相关的聊天记忆”判断；聊天记忆用于理解上下文、关系连续性和未明说指代，不要把无关旧历史或旧事件再次计为本轮变化。
7. 输出要拟人：情绪有惯性、习惯化和余温。相同善意重复出现时增幅变小；触碰旧伤、旧承诺或旧边界时反应更真实但仍克制。

分层判断流程（只在心里执行，不要输出）：
1. 直接依据：先只看“最新用户发言”，判断它本身能触发哪些字段变化。
2. 影响权重：再看“相关聊天记忆”，只允许它改变这些已被触发字段的幅度、方向侧重或优先级。
3. 禁止项：如果某个字段只能从旧记忆中找到依据，而最新用户发言没有触碰它，该字段必须返回 0。
4. 例外：最新发言中有“又、再、还、上次、记得、忘了、承诺、约定、秘密、边界”等回指词时，相关记忆可以更强地影响本轮判断，但仍不能重复结算旧事件。

详细规则：
{rules_text}

当前状态：
{json.dumps(analysis_state, ensure_ascii=False)}

相关聊天记忆：
{history_text}

最新用户发言：
{user_msg}

返回格式：
{{
  "好感度": 0,
  "病娇值": 0,
  "锁定进度": 0,
  "信任度": 0,
  "焦虑值": 0,
  "优雅值": 0,
  "用户意图": "问候/提问/调情/试探/安抚/道歉/承诺/冒犯/离开暗示/分享秘密/普通回应",
  "用户情绪": "平静/开心/疲惫/不安/亲近/挑衅/敷衍/依赖/认真",
  "关系信号": "主动靠近/释放善意/触碰边界/提供信任/制造不确定/关系稳定感下降/无明显关系推进",
  "回应目标": "用一句话概括玛丽亚本轮最自然的回应目标"
}}"""

        try:
            resp = await self._call_analysis_llm(
                purpose="状态分析",
                prompt=prompt,
                system_prompt="你是严谨的状态分析器，只能输出一个 JSON 对象。",
                temperature=0.2,
                max_tokens=420,
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
                if isinstance(value, (int, float)):
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
            self._log_perf("analyze_state_changes", started_at, user_id, threshold_ms=5.0)
            return {**humanized_deltas, "__turn_analysis": turn_analysis}
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
            field: int(state.get(field, 0) or 0)
            for field in tracked_fields
        }
        applied_changes: Dict[str, int] = {}
        scaled_deltas = self._scale_analysis_deltas(state, deltas)
        sanitized_deltas = self._sanitize_analysis_deltas(state, scaled_deltas, user_id=user_id)
        for field in ("好感度", "病娇值", "锁定进度", "信任度", "焦虑值", "优雅值"):
            raw_delta = sanitized_deltas.get(field, 0)
            if not isinstance(raw_delta, (int, float)):
                continue
            self._apply_state_delta(state, field, int(raw_delta))

        self._normalize_state_constraints(state, user_id=user_id)
        self._update_possessiveness(state)
        if self._get_destined_one_info() and not self._is_destined_user(user_id):
            state["占有欲"] = min(old_values["占有欲"], int(state.get("占有欲", 0)))
        for field in tracked_fields:
            applied_changes[field] = int(state.get(field, 0) or 0) - old_values[field]
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
            delta_int = int(delta)
            if delta_int != 0:
                return f"{value}({delta_int:+d})"
        return str(value)

    def _build_debug_footer(self, state: Dict[str, Any], deltas: Dict[str, int]) -> str:
        snapshot = self._derive_state_snapshot(state)
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
            f"兼容状态：{snapshot.get('兼容状态', state.get('当前状态', '未知'))}\n"
            f"关系阶段：{snapshot.get('关系阶段', '未知')}\n"
            f"主情绪模式：{snapshot.get('主情绪模式', '未知')}\n"
            f"危机覆盖：{snapshot.get('危机覆盖', CRISIS_OVERLAY_NAMES['NONE'])}\n"
            f"表现强度：{snapshot.get('表现强度标签', '标准姿态')}\n"
            f"状态摘要：{self._format_state_snapshot_compact(snapshot)}"
            f"{destined_line}"
        )

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
        lines.append(f"累计互动：{stats.get('总互动次数', 0)} 次")
        return "\n".join(lines)

    def _update_possessiveness(self, state: Dict):
        fav = state["好感度"]
        yan = state["病娇值"]
        lock = state["锁定进度"]
        anxiety = state["焦虑值"]
        if fav < 60:
            state["占有欲"] = 0
            return

        possess = int(max(0, fav - 60) * 0.20 + yan * 0.45 + lock * 0.35)
        if anxiety > 20:
            possess += int((anxiety - 20) * 0.18)
        if state["锁定进度"] >= self.lock_threshold:
            possess += 20
        state["占有欲"] = max(0, min(100, possess))

    def _determine_relationship_stage(self, state: Dict[str, Any]) -> str:
        favor = int(state.get("好感度", 0) or 0)
        trust = int(state.get("信任度", 0) or 0)
        yan = int(state.get("病娇值", 0) or 0)
        lock = int(state.get("锁定进度", 0) or 0)
        interactions = int(state.get("互动计数", 0) or 0)

        if lock >= self.lock_threshold:
            return RELATION_STAGE_NAMES["FATED_LOCK"]
        if (
            (favor >= 78 and trust >= 65 and interactions >= 18)
            or (favor >= 72 and trust >= 58 and yan >= 45 and interactions >= 12)
            or lock >= max(1, int(self.lock_threshold * 0.65))
        ):
            return RELATION_STAGE_NAMES["EXCLUSIVE_PROBE"]
        if (
            (favor >= 58 and trust >= 45 and interactions >= 10)
            or (favor >= 65 and trust >= 38)
        ):
            return RELATION_STAGE_NAMES["PRIVATE_FAVOR"]
        if favor >= 25 or trust >= 30 or interactions >= 4:
            return RELATION_STAGE_NAMES["ALLOW_CLOSE"]
        return RELATION_STAGE_NAMES["OBSERVATION"]

    def _determine_primary_mode(self, state: Dict[str, Any]) -> str:
        favor = int(state.get("好感度", 0) or 0)
        yan = int(state.get("病娇值", 0) or 0)
        lock = int(state.get("锁定进度", 0) or 0)

        if favor < 30:
            return STATE_NAMES["COLD_NOBLE"]
        if favor < 60:
            return STATE_NAMES["TSUNDERE_PROBE"]
        if yan >= 50 or (yan >= 35 and lock >= max(20, int(self.lock_threshold * 0.35))):
            return STATE_NAMES["LATENT_VINE"]
        return STATE_NAMES["SWEET_INDUCE"]

    def _determine_crisis_overlay(self, state: Dict[str, Any]) -> str:
        anxiety = int(state.get("焦虑值", 0) or 0)
        elegance = int(state.get("优雅值", 0) or 0)

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
        favor = int(state.get("好感度", 0) or 0)
        anxiety = int(state.get("焦虑值", 0) or 0)
        elegance = int(state.get("优雅值", 0) or 0)

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

        # 存储到 Mnemosyne（如果可用且启用情感记忆）
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
            + (" (已同步到 Mnemosyne)" if mnemosyne_stored else "")
        )
        return True

    async def _auto_summary_loop(self):
        """后台循环：只检查有新对话的用户并触发自动总结。"""
        while True:
            await asyncio.sleep(60)  # 每分钟检查一次
            if not self._summary_dirty_users:
                continue
            now = datetime.now()
            for user_id in list(self._summary_dirty_users):
                state = self.user_states.get(user_id, {})
                last_time_str = state.get("最后互动时间")
                if not last_time_str:
                    self._summary_dirty_users.discard(user_id)
                    continue
                try:
                    last = datetime.fromisoformat(last_time_str)
                except (ValueError, TypeError):
                    self._summary_dirty_users.discard(user_id)
                    continue
                if (now - last).total_seconds() <= self.auto_summary_idle:
                    continue

                # 避免重复总结：检查最近一次总结时间。
                last_summary_file = self.data_dir / f"last_summary_{user_id}.txt"
                try:
                    if last_summary_file.exists():
                        last_time_content = await asyncio.to_thread(
                            last_summary_file.read_text,
                            encoding='utf-8',
                        )
                        last_time_content = last_time_content.strip()
                        if last_time_content:
                            last_summary_time = datetime.fromisoformat(last_time_content)
                            if (now - last_summary_time).total_seconds() < self.auto_summary_idle * 2:
                                continue
                except Exception:
                    pass

                await self._trigger_auto_summary(user_id)
                self._summary_dirty_users.discard(user_id)

                # 异步写入最后总结时间
                try:
                    lock = await self._get_lock(last_summary_file)
                    async with lock:
                        await self._write_text_atomic(last_summary_file, now.isoformat())
                except Exception as e:
                    self.logger.error(f"写入总结时间戳失败: {e}")

