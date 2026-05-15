import asyncio
import copy
import hashlib
import json
import math
import os
import re
import sqlite3
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

class MariannaMemoryMixin:
    def _normalize_mnemosyne_content(self, content: str) -> str:
        text = self._strip_debug_artifacts(str(content or "").strip())
        text = BRACKETED_MEMORY_PREFIX_PATTERN.sub("", text)
        text = AUTO_SUMMARY_PREFIX_PATTERN.sub("", text)
        text = QUOTE_PATTERN.sub("", text)
        text = CN_EN_PUNCT_PATTERN.sub(" ", text)
        text = WHITESPACE_PATTERN.sub(" ", text).strip().lower()
        return text

    def _make_mnemosyne_fingerprint(self, content: str) -> str:
        normalized = self._normalize_mnemosyne_content(content)
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()

    def _extract_mnemosyne_terms(self, text: str) -> List[str]:
        normalized = self._normalize_mnemosyne_content(text)
        terms: List[str] = []
        seen = set()

        def add_term(term: str):
            if len(term) < 2 or term in seen:
                return
            seen.add(term)
            terms.append(term)

        for word in ASCII_TERM_PATTERN.findall(normalized):
            add_term(word)

        for chunk in CJK_TERM_PATTERN.findall(normalized):
            add_term(chunk[:12])
            for size in (2, 3):
                upper = min(len(chunk) - size + 1, 8)
                for idx in range(max(upper, 0)):
                    add_term(chunk[idx: idx + size])

        return terms[:24]

    def _coerce_memory_int(
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

    def _get_memory_state_store(self) -> Dict[str, Any]:
        states = getattr(self, "user_states", None)
        if not isinstance(states, dict):
            states = {}
            self.user_states = states
        return states

    def _is_protected_recalled_memory(self, content: Any, salience: Any = 0) -> bool:
        if self._coerce_memory_int(salience, default=0, minimum=0) >= 6:
            return True
        text = str(content or "").lower()
        protected_terms = (
            "birthday", "birth", "promise", "boundary", "secret", "nickname",
            "生日", "出生", "承诺", "约定", "边界", "秘密", "称呼",
        )
        return any(term in text for term in protected_terms)

    def _build_memory_recall_cooldown_signature(
        self,
        user_id: str,
        cooldown_seconds: Optional[Any] = None,
    ) -> List[Any]:
        state = self._get_memory_state_store().get(str(user_id), {})
        if not isinstance(state, dict):
            return []
        recalled = state.get("最近召回记忆", [])
        if not isinstance(recalled, list):
            return []
        cleaned: List[Dict[str, Any]] = []
        for item in recalled:
            if isinstance(item, dict):
                cleaned.append(dict(item))
        if cooldown_seconds is None:
            return cleaned
        cooldown = self._coerce_memory_int(cooldown_seconds, default=0, minimum=0)
        now = time.time()
        ids = []
        for item in cleaned:
            try:
                recalled_at = float(item.get("time", 0) or 0)
            except (TypeError, ValueError):
                recalled_at = 0.0
            if not math.isfinite(recalled_at):
                recalled_at = 0.0
            if cooldown <= 0 or now - recalled_at <= cooldown:
                memory_id = str(item.get("id", "") or item.get("fingerprint", ""))
                if memory_id:
                    ids.append(memory_id)
        return ids

    def _filter_memory_recall_cooldown(
        self,
        user_id: str,
        memories: List[Dict[str, Any]],
        limit: int,
        cooldown_seconds: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        if not isinstance(memories, list):
            return []
        effective_limit = self._coerce_memory_int(limit, default=len(memories), minimum=0)
        cooldown = self._coerce_memory_int(
            cooldown_seconds if cooldown_seconds is not None else getattr(self, "memory_recall_cooldown_seconds", 0),
            default=0,
            minimum=0,
        )
        if cooldown <= 0:
            return memories[:effective_limit] if effective_limit else []
        now = time.time()
        recent_ids = set()
        for item in self._build_memory_recall_cooldown_signature(user_id):
            try:
                recalled_at = float(item.get("time", 0) or 0)
            except (TypeError, ValueError):
                recalled_at = 0.0
            if not math.isfinite(recalled_at):
                recalled_at = 0.0
            if now - recalled_at <= cooldown:
                memory_id = str(item.get("id", "") or item.get("fingerprint", ""))
                if memory_id:
                    recent_ids.add(memory_id)
        selected = []
        delayed = []
        for memory in memories:
            if not isinstance(memory, dict):
                continue
            memory_id = str(memory.get("id", "") or memory.get("fingerprint", ""))
            if memory_id and memory_id in recent_ids:
                delayed.append(memory)
            else:
                selected.append(memory)
            if len(selected) >= effective_limit:
                break
        return selected[:effective_limit]

    def _remember_recent_builtin_memory_recall(
        self,
        user_id: str,
        memories: List[Dict[str, Any]],
    ):
        states = self._get_memory_state_store()
        state = states.setdefault(str(user_id), {})
        if not isinstance(state, dict):
            state = {}
            states[str(user_id)] = state
        recalled = []
        now = time.time()
        for memory in memories or []:
            if not isinstance(memory, dict):
                continue
            memory_id = str(memory.get("id", "") or memory.get("fingerprint", ""))
            if not memory_id:
                continue
            keywords = memory.get("keywords", [])
            if isinstance(keywords, str):
                terms = [part.strip() for part in re.split(r"[,，\s]+", keywords) if part.strip()]
            elif isinstance(keywords, list):
                terms = [str(part).strip() for part in keywords if str(part).strip()]
            else:
                terms = self._extract_mnemosyne_terms(
                    memory.get("raw_content", "") or memory.get("content", "") or memory.get("summary", "")
                )
            if not terms:
                terms = self._extract_mnemosyne_terms(
                    memory.get("raw_content", "") or memory.get("content", "") or memory_id
                )
            recalled.append({"id": memory_id, "terms": terms[:8], "time": now})
        state["最近召回记忆"] = recalled[-20:]

    def _penalize_missed_builtin_memories_sync(self, user_id: str, memory_ids: List[str]) -> int:
        if not memory_ids:
            return 0
        changed = 0
        with self._connect_local_memory_db() as conn:
            for memory_id in memory_ids:
                row = conn.execute(
                    "SELECT id, raw_content, summary, salience FROM memories WHERE user_id = ? AND id = ?",
                    (str(user_id), str(memory_id)),
                ).fetchone()
                if not row:
                    continue
                content = f"{row['summary']} {row['raw_content']}"
                salience = self._coerce_memory_int(row["salience"], default=0, minimum=0)
                if self._is_protected_recalled_memory(content, salience):
                    continue
                if salience <= 0:
                    continue
                conn.execute(
                    "UPDATE memories SET salience = ?, updated_at = ? WHERE id = ?",
                    (max(0, salience - 1), datetime.now().isoformat(), str(memory_id)),
                )
                changed += 1
        if changed:
            cache = getattr(self, "_local_memory_query_cache", None)
            if isinstance(cache, dict):
                cache.clear()
        return changed

    async def _apply_memory_recall_negative_feedback(self, user_id: str, user_msg: str) -> int:
        if not getattr(self, "enable_memory_recall_negative_feedback", False):
            return 0
        query_terms = set(self._extract_mnemosyne_terms(user_msg))
        missed: List[str] = []
        max_items = self._coerce_memory_int(
            getattr(self, "memory_recall_negative_feedback_max", 3),
            default=3,
            minimum=0,
            maximum=20,
        )
        for item in self._build_memory_recall_cooldown_signature(user_id):
            memory_id = str(item.get("id", "") or "")
            if not memory_id:
                continue
            terms = item.get("terms", [])
            if isinstance(terms, str):
                terms = [terms]
            if not isinstance(terms, list):
                terms = []
            normalized_terms = {str(term).lower() for term in terms if str(term).strip()}
            if normalized_terms and normalized_terms.intersection(query_terms):
                continue
            missed.append(memory_id)
            if len(missed) >= max_items:
                break
        if not missed:
            return 0
        return self._penalize_missed_builtin_memories_sync(user_id, missed)

    def _memory_write_candidate_key(self, user_msg: str) -> str:
        terms = self._extract_mnemosyne_terms(user_msg)
        normalized = self._normalize_mnemosyne_content(user_msg)
        basis = "|".join([normalized[:24]] + terms[:6])
        return basis or hashlib.sha1(str(user_msg or "").encode("utf-8")).hexdigest()[:16]

    def _get_memory_query_user_ids(self, user_id: str) -> List[str]:
        user = str(user_id or "")
        if user.startswith("group:") and "::" in user:
            private_user = user.rsplit("::", 1)[-1]
            return [user, private_user]
        return [user]

    def _infer_memory_visibility(self, user_id: str, content: str, layer: str, memory_type: str) -> str:
        user = str(user_id or "")
        text = str(content or "").lower()
        if user.startswith("group:"):
            return "group_only"
        if layer == "profile" or memory_type == "profile":
            return "public_profile"
        if any(term in text for term in ("secret", "秘密", "承诺", "边界", "promise", "boundary")):
            return "sensitive"
        return "private_only"

    def _memory_visibility_allowed_for_query(
        self,
        query_user_id: str,
        memory: Dict[str, Any],
        for_prompt: bool = False,
    ) -> bool:
        visibility = str((memory or {}).get("visibility", "") or "private_only")
        owner = str((memory or {}).get("user_id", "") or "")
        query = str(query_user_id or "")
        if visibility == "sensitive":
            return False
        if query.startswith("group:"):
            if owner == query:
                return visibility == "group_only" and not for_prompt
            return visibility == "public_profile"
        return (not owner or owner == query) and visibility in {"private_only", "public_profile", ""}

    def _infer_memory_temperature(self, entry: Any) -> str:
        if isinstance(entry, dict):
            updated_at = entry.get("updated_at") or entry.get("timestamp") or ""
            salience = self._coerce_memory_int(entry.get("salience", 0), default=0, minimum=0)
            hit_count = self._coerce_memory_int(entry.get("hit_count", 0), default=0, minimum=0)
        else:
            updated_at = ""
            salience = 0
            hit_count = 0
        try:
            age_days = (datetime.now() - datetime.fromisoformat(str(updated_at))).total_seconds() / 86400
        except Exception:
            age_days = 999
        if age_days <= self._coerce_memory_int(getattr(self, "memory_hot_days", 7), default=7, minimum=1):
            return "hot"
        if salience >= 6 or hit_count >= 3:
            return "warm"
        if age_days >= self._coerce_memory_int(getattr(self, "memory_warm_days", 45), default=45, minimum=1):
            return "cold"
        return "warm"

    def _memory_polarity(self, text: str) -> str:
        normalized = str(text or "").lower()
        if any(term in normalized for term in ("不喜欢", "讨厌", "反感", "不要", "bad", "hate")):
            return "negative"
        if any(term in normalized for term in ("喜欢", "爱", "谢谢", "开心", "好", "like", "love")):
            return "positive"
        return "neutral"

    def _memory_conflict_slot(self, text: str) -> str:
        normalized = str(text or "").lower()
        if any(term in normalized for term in ("颜色", "红色", "蓝色", "color", "red", "blue")):
            return "color"
        if any(term in normalized for term in ("茶", "饭", "吃", "喝", "food", "tea")):
            return "food"
        if any(term in normalized for term in ("生日", "birthday")):
            return "birthday"
        return ""

    def _memory_entries_conflict(
        self,
        old_entry: Dict[str, Any],
        new_entry: Dict[str, Any],
        similarity: float = 0.0,
    ) -> bool:
        old_text = str((old_entry or {}).get("raw_content", "") or (old_entry or {}).get("content", ""))
        new_text = str((new_entry or {}).get("raw_content", "") or (new_entry or {}).get("content", ""))
        slot = self._memory_conflict_slot(old_text)
        return bool(slot and slot == self._memory_conflict_slot(new_text) and self._memory_polarity(old_text) != self._memory_polarity(new_text))

    def _get_memory_write_candidate_list(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        candidates = state.get("记忆写入候选", [])
        if not isinstance(candidates, list):
            candidates = []
        cleaned: List[Dict[str, Any]] = []
        for item in candidates:
            if isinstance(item, dict):
                copied = dict(item)
                copied["count"] = self._coerce_memory_int(copied.get("count", 0), default=0, minimum=0)
                cleaned.append(copied)
        state["记忆写入候选"] = cleaned
        return cleaned

    def _mark_memory_write_candidate_promoted(self, state: Dict[str, Any], key: str):
        candidates = self._get_memory_write_candidate_list(state)
        for item in candidates:
            if item.get("key") == key:
                item["count"] = 0
                item["promoted_at"] = item.get("promoted_at") or datetime.now().isoformat()
                item["promoted"] = True
                state["最近记忆写入候选"] = {
                    "key": key,
                    "count": 0,
                    "promoted": True,
                    "reason": "already_promoted",
                }
                return

    def _stage_memory_write_candidate(
        self,
        state: Dict[str, Any],
        user_msg: str,
        deltas: Dict[str, int],
        turn_analysis: Optional[Dict[str, str]] = None,
        active_event: Optional[Dict[str, str]] = None,
    ) -> bool:
        if not isinstance(state, dict):
            return False
        if not getattr(self, "enable_memory_write_candidates", ENABLE_MEMORY_WRITE_CANDIDATES):
            return False
        key = self._memory_write_candidate_key(user_msg)
        candidates = self._get_memory_write_candidate_list(state)
        existing = next((item for item in candidates if item.get("key") == key), None)
        if existing and existing.get("promoted_at"):
            state["最近记忆写入候选"] = {
                "key": key,
                "count": 0,
                "promoted": False,
                "reason": "already_promoted",
            }
            return False
        if existing is None:
            existing = {
                "key": key,
                "content": str(user_msg or "")[:160],
                "count": 0,
                "created_at": datetime.now().isoformat(),
            }
            candidates.append(existing)
        existing["count"] = self._coerce_memory_int(existing.get("count", 0), default=0, minimum=0) + 1
        existing["updated_at"] = datetime.now().isoformat()
        promote_hits = self._coerce_memory_int(
            getattr(self, "memory_write_candidate_promote_hits", MEMORY_WRITE_CANDIDATE_PROMOTE_HITS),
            default=MEMORY_WRITE_CANDIDATE_PROMOTE_HITS,
            minimum=1,
        )
        promoted = existing["count"] >= promote_hits
        if promoted:
            existing["promoted"] = True
        limit = self._coerce_memory_int(
            getattr(self, "memory_write_candidate_limit", MEMORY_WRITE_CANDIDATE_LIMIT),
            default=MEMORY_WRITE_CANDIDATE_LIMIT,
            minimum=1,
        )
        candidates.sort(key=lambda item: (item.get("key") != key, -self._coerce_memory_int(item.get("count", 0), default=0)))
        del candidates[limit:]
        state["最近记忆写入候选"] = {
            "key": key,
            "count": existing["count"],
            "promoted": promoted,
            "reason": "promoted" if promoted else "staged",
        }
        return promoted

    def _get_local_memory_db_file(self) -> Path:
        configured = getattr(self, "local_memory_db_file", None)
        if isinstance(configured, (str, os.PathLike)) and str(configured).strip():
            return Path(configured)
        data_dir = getattr(self, "data_dir", Path(__file__).resolve().parents[1] / "data")
        if not isinstance(data_dir, (str, os.PathLike)) or not str(data_dir).strip():
            data_dir = Path(__file__).resolve().parents[1] / "data"
        self.data_dir = Path(data_dir)
        return self.data_dir / "local_memory.db"

    def _get_memory_export_dir(self) -> Path:
        data_dir = getattr(self, "data_dir", Path(__file__).resolve().parents[1] / "data")
        if not isinstance(data_dir, (str, os.PathLike)) or not str(data_dir).strip():
            data_dir = Path(__file__).resolve().parents[1] / "data"
        self.data_dir = Path(data_dir)
        return self.data_dir / "memory_exports"

    def _get_mnemosyne_runtime_cache(self, attr_name: str) -> Dict[Any, Any]:
        cache = getattr(self, attr_name, None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, attr_name, cache)
        return cache

    def _get_mnemosyne_runtime_list(self, attr_name: str, cache_key: str) -> List[Any]:
        cache = self._get_mnemosyne_runtime_cache(attr_name)
        items = cache.get(cache_key)
        if not isinstance(items, list):
            items = []
            cache[cache_key] = items
        return items

    def _connect_local_memory_db(self) -> sqlite3.Connection:
        db_file = self._get_local_memory_db_file()
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_file), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_builtin_memory_db_sync(self) -> bool:
        with self._connect_local_memory_db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    layer TEXT NOT NULL,
                    type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    raw_content TEXT NOT NULL,
                    normalized_content TEXT NOT NULL,
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    salience INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_hit_at TEXT NOT NULL DEFAULT '',
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    reinforcement_count INTEGER NOT NULL DEFAULT 0,
                    superseded_by TEXT NOT NULL DEFAULT '',
                    superseded_at TEXT NOT NULL DEFAULT '',
                    revision_of TEXT NOT NULL DEFAULT '',
                    visibility TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    temperature TEXT NOT NULL DEFAULT 'warm'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_vectors (
                    memory_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL DEFAULT '',
                    dimensions INTEGER NOT NULL DEFAULT 0,
                    vector_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        return True

    async def _ensure_builtin_memory_ready(self) -> bool:
        if not getattr(self, "enable_builtin_memory", True):
            return False
        return await asyncio.to_thread(self._init_builtin_memory_db_sync)

    def _memory_json_safe(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(k): self._memory_json_safe(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
        if isinstance(value, (list, tuple)):
            return [self._memory_json_safe(v) for v in value]
        if isinstance(value, set):
            return [self._memory_json_safe(v) for v in sorted(value, key=str)]
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, float):
            return value if math.isfinite(value) else 0.0
        return value

    def _memory_json_dumps(self, value: Any) -> str:
        return json.dumps(self._memory_json_safe(value), ensure_ascii=False, allow_nan=False)

    def _sanitize_memory_meta(self, value: Any, limit: int = 160) -> Any:
        if isinstance(value, dict):
            cleaned = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 20:
                    break
                safe_key = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "_", str(key)).strip("_")[:48] or "key"
                cleaned[safe_key] = self._sanitize_memory_meta(item, limit)
            return cleaned
        if isinstance(value, list):
            return [self._sanitize_memory_meta(item, limit) for item in value[:10]]
        text = str(value)
        return text[:limit]

    def _safe_memory_layer(self, layer: Any) -> str:
        value = str(layer or "impression").strip().lower()
        return value if value in {"profile", "impression", "event", "summary"} else "impression"

    def _safe_memory_type(self, memory_type: Any) -> str:
        value = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "_", str(memory_type or "interaction")).strip("_")
        return (value or "interaction")[:32]

    def _row_get(self, row: Any, key: str, default: Any = "") -> Any:
        try:
            return row[key]
        except Exception:
            return default

    def _row_to_memory_entry(self, row: Any) -> Dict[str, Any]:
        raw = str(self._row_get(row, "raw_content", self._row_get(row, "summary", "")) or "")
        summary = str(self._row_get(row, "summary", raw) or raw)
        try:
            keywords = json.loads(self._row_get(row, "keywords_json", "[]") or "[]")
        except Exception:
            keywords = []
        if not isinstance(keywords, list):
            keywords = []
        try:
            evidence = json.loads(self._row_get(row, "evidence_json", "{}") or "{}")
        except Exception:
            evidence = {}
        if not isinstance(evidence, dict):
            evidence = {}
        memory_id = str(self._row_get(row, "id", "") or "")
        return {
            "id": memory_id,
            "fingerprint": memory_id,
            "user_id": str(self._row_get(row, "user_id", "") or ""),
            "memory_layer": self._safe_memory_layer(self._row_get(row, "layer", "impression")),
            "layer": self._safe_memory_layer(self._row_get(row, "layer", "impression")),
            "type": self._safe_memory_type(self._row_get(row, "type", "interaction")),
            "memory_type": self._safe_memory_type(self._row_get(row, "type", "interaction")),
            "summary": summary,
            "content": raw or summary,
            "raw_content": raw or summary,
            "normalized_content": str(self._row_get(row, "normalized_content", "") or self._normalize_mnemosyne_content(raw or summary)),
            "keywords": [str(item) for item in keywords],
            "salience": self._coerce_memory_int(self._row_get(row, "salience", 0), default=0, minimum=0),
            "hit_count": self._coerce_memory_int(self._row_get(row, "hit_count", 0), default=0, minimum=0),
            "reinforcement_count": self._coerce_memory_int(self._row_get(row, "reinforcement_count", 0), default=0, minimum=0),
            "visibility": str(self._row_get(row, "visibility", "") or ""),
            "evidence": evidence,
            "temperature": str(self._row_get(row, "temperature", "warm") or "warm"),
        }

    def _is_builtin_memory_row_protected(self, row: Any) -> bool:
        try:
            evidence = json.loads(self._row_get(row, "evidence_json", "{}") or "{}")
        except Exception:
            evidence = {}
        if isinstance(evidence, dict) and evidence.get("protected"):
            return True
        content = f"{self._row_get(row, 'summary', '')} {self._row_get(row, 'raw_content', '')}"
        return self._is_protected_recalled_memory(content, self._row_get(row, "salience", 0))

    def _store_builtin_memory_sync(
        self,
        user_id: str,
        content: str,
        memory_type: str = "interaction",
        salience: Any = 0,
        layer: str = "impression",
        evidence: Optional[Dict[str, Any]] = None,
    ) -> bool:
        self._init_builtin_memory_db_sync()
        raw = str(content or "").strip()
        if not raw:
            return False
        salience_value = self._coerce_memory_int(salience, default=0, minimum=0, maximum=10)
        normalized = self._normalize_mnemosyne_content(raw)
        memory_id = hashlib.sha1(f"{user_id}|{normalized}".encode("utf-8")).hexdigest()[:16]
        now = datetime.now().isoformat()
        keywords = self._extract_mnemosyne_terms(raw)
        evidence_payload = dict(evidence or {})
        if self._is_protected_recalled_memory(raw, salience_value):
            evidence_payload.setdefault("protected", True)
            salience_value = max(salience_value, 8 if evidence_payload.get("protected") else salience_value)
        with self._connect_local_memory_db() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if row:
                old = self._row_to_memory_entry(row)
                old_evidence = dict(old.get("evidence") or {})
                old_evidence.update(evidence_payload)
                visibility = old.get("visibility", "")
                if visibility:
                    old_evidence["visibility"] = visibility
                conn.execute(
                    """
                    UPDATE memories
                    SET salience = ?, updated_at = ?, reinforcement_count = ?,
                        visibility = ?, evidence_json = ?
                    WHERE id = ?
                    """,
                    (
                        max(salience_value, old["salience"]),
                        now,
                        old["reinforcement_count"] + 1,
                        visibility,
                        self._memory_json_dumps(old_evidence),
                        memory_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO memories(
                        id, user_id, layer, type, summary, raw_content, normalized_content,
                        keywords_json, salience, created_at, updated_at, evidence_json, temperature
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        str(user_id),
                        self._safe_memory_layer(layer),
                        self._safe_memory_type(memory_type),
                        raw[: self._coerce_memory_int(getattr(self, "builtin_memory_summary_max_chars", 96), default=96, minimum=16)],
                        raw,
                        normalized,
                        self._memory_json_dumps(keywords),
                        salience_value,
                        now,
                        now,
                        self._memory_json_dumps(evidence_payload),
                        "warm",
                    ),
                )
        cache = getattr(self, "_local_memory_query_cache", None)
        if isinstance(cache, dict):
            cache.clear()
        return True

    async def _store_to_builtin_memory(self, user_id: str, content: str, memory_type: str, salience: Any, layer: Optional[str]):
        if not await self._ensure_builtin_memory_ready():
            return False
        return await asyncio.to_thread(
            self._store_builtin_memory_sync,
            user_id,
            content,
            memory_type,
            salience,
            layer or "impression",
        )

    def _escape_sql_like(self, value: str) -> str:
        return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def _resolve_memory_id_sync(self, user_id: str, prefix: str) -> str:
        prefix_text = str(prefix or "")
        if len(prefix_text) < 4:
            return ""
        with self._connect_local_memory_db() as conn:
            rows = conn.execute(
                "SELECT id FROM memories WHERE user_id = ? AND id LIKE ? ESCAPE '\\' ORDER BY id LIMIT 2",
                (str(user_id), self._escape_sql_like(prefix_text) + "%"),
            ).fetchall()
        return str(rows[0]["id"]) if len(rows) == 1 else ""

    def _protect_builtin_memory_sync(self, user_id: str, prefix: str) -> bool:
        memory_id = self._resolve_memory_id_sync(user_id, prefix)
        if not memory_id:
            return False
        with self._connect_local_memory_db() as conn:
            row = conn.execute("SELECT evidence_json FROM memories WHERE id = ?", (memory_id,)).fetchone()
            try:
                evidence = json.loads(row["evidence_json"] or "{}") if row else {}
            except Exception:
                evidence = {}
            if not isinstance(evidence, dict):
                evidence = {}
            evidence["protected"] = True
            conn.execute("UPDATE memories SET evidence_json = ? WHERE id = ?", (self._memory_json_dumps(evidence), memory_id))
        return True

    def _set_builtin_memory_visibility_sync(self, user_id: str, prefix: str, visibility: str) -> bool:
        memory_id = self._resolve_memory_id_sync(user_id, prefix)
        if not memory_id:
            return False
        safe_visibility = str(visibility or "")[:32]
        with self._connect_local_memory_db() as conn:
            row = conn.execute("SELECT evidence_json FROM memories WHERE id = ?", (memory_id,)).fetchone()
            try:
                evidence = json.loads(row["evidence_json"] or "{}") if row else {}
            except Exception:
                evidence = {}
            if not isinstance(evidence, dict):
                evidence = {}
            evidence["visibility"] = safe_visibility
            conn.execute(
                "UPDATE memories SET visibility = ?, evidence_json = ? WHERE id = ?",
                (safe_visibility, self._memory_json_dumps(evidence), memory_id),
            )
        return True

    def _delete_builtin_memory_sync(self, user_id: str, prefix: str) -> bool:
        memory_id = self._resolve_memory_id_sync(user_id, prefix)
        if not memory_id:
            return False
        with self._connect_local_memory_db() as conn:
            conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,))
            cursor = conn.execute("DELETE FROM memories WHERE id = ? AND user_id = ?", (memory_id, str(user_id)))
        return bool(cursor.rowcount)

    async def _delete_builtin_memory(self, user_id: str, prefix: str) -> bool:
        try:
            if not await self._ensure_builtin_memory_ready():
                return False
            return await asyncio.to_thread(self._delete_builtin_memory_sync, user_id, prefix)
        except Exception as e:
            self.logger.error(f"delete builtin memory failed: {e}", exc_info=True)
            return False

    async def _protect_builtin_memory(self, user_id: str, prefix: str) -> bool:
        try:
            if not await self._ensure_builtin_memory_ready():
                return False
            return await asyncio.to_thread(self._protect_builtin_memory_sync, user_id, prefix)
        except Exception as e:
            self.logger.error(f"protect builtin memory failed: {e}", exc_info=True)
            return False

    async def _set_builtin_memory_visibility(self, user_id: str, prefix: str, visibility: str) -> bool:
        try:
            if not await self._ensure_builtin_memory_ready():
                return False
            return await asyncio.to_thread(self._set_builtin_memory_visibility_sync, user_id, prefix, visibility)
        except Exception as e:
            self.logger.error(f"set builtin memory visibility failed: {e}", exc_info=True)
            return False

    def _export_builtin_memories_sync(self, user_id: str, limit: Any = 100) -> Path:
        self._init_builtin_memory_db_sync()
        export_dir = self._get_memory_export_dir()
        export_dir.mkdir(parents=True, exist_ok=True)
        effective_limit = self._coerce_memory_int(limit, default=100, minimum=1, maximum=10000)
        file_path = export_dir / f"{str(user_id)}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.jsonl"
        with self._connect_local_memory_db() as conn:
            rows = conn.execute(
                "SELECT * FROM memories WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
                (str(user_id), effective_limit),
            ).fetchall()
        lines = []
        for row in rows:
            entry = self._row_to_memory_entry(row)
            lines.append(self._memory_json_dumps({
                "content": entry["raw_content"],
                "type": entry["type"],
                "layer": entry["memory_layer"],
                "salience": entry["salience"],
                "visibility": entry["visibility"],
                "temperature": entry["temperature"],
                "evidence": entry["evidence"],
            }))
        file_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return file_path

    async def _export_builtin_memories(self, user_id: str, limit: Any = 100):
        try:
            if not await self._ensure_builtin_memory_ready():
                return None
            return await asyncio.to_thread(self._export_builtin_memories_sync, user_id, limit)
        except Exception as e:
            self.logger.error(f"export builtin memories failed: {e}", exc_info=True)
            return None

    def _resolve_memory_import_file(self, file_name: str) -> Optional[Path]:
        raw = Path(str(file_name or "")).name
        if not raw or raw in {".", ".."}:
            return None
        path = self._get_memory_export_dir() / raw
        return path if path.exists() and path.is_file() else None

    def _import_builtin_memories_sync(self, user_id: str, file_name: str, limit: Any = 100) -> int:
        self._init_builtin_memory_db_sync()
        path = self._resolve_memory_import_file(file_name)
        if path is None:
            return 0
        effective_limit = self._coerce_memory_int(limit, default=100, minimum=1, maximum=10000)
        max_line = self._coerce_memory_int(getattr(self, "builtin_memory_import_max_line_chars", 4000), default=4000, minimum=200)
        max_content = self._coerce_memory_int(getattr(self, "builtin_memory_import_max_content_chars", 500), default=500, minimum=20)
        imported = 0
        with path.open("r", encoding="utf-8") as fp:
            for line in fp:
                if imported >= effective_limit:
                    break
                line = line[:max_line].strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                content = str(payload.get("content", "") or payload.get("raw_content", "") or "")[:max_content]
                if len(content.strip()) < max(1, self._coerce_memory_int(getattr(self, "memory_quality_min_text_chars", 6), default=6, minimum=1)):
                    continue
                evidence = self._sanitize_memory_meta(payload.get("evidence", {}))
                if payload.get("visibility"):
                    evidence["visibility"] = str(payload.get("visibility"))[:32]
                ok = self._store_builtin_memory_sync(
                    user_id,
                    content,
                    self._safe_memory_type(payload.get("type", "interaction")),
                    self._coerce_memory_int(payload.get("salience", 3), default=3, minimum=0, maximum=10),
                    self._safe_memory_layer(payload.get("layer", "impression")),
                    evidence=evidence,
                )
                imported += 1 if ok else 0
        return imported

    async def _import_builtin_memories(self, user_id: str, file_name: str, limit: Any = 100) -> int:
        try:
            if not await self._ensure_builtin_memory_ready():
                return 0
            return await asyncio.to_thread(self._import_builtin_memories_sync, user_id, file_name, limit)
        except Exception as e:
            self.logger.error(f"import builtin memories failed: {e}", exc_info=True)
            return 0

    def _delete_rows_by_ids(
        self,
        conn: sqlite3.Connection,
        table: str,
        id_column: str,
        ids: List[str],
        chunk_size: int = 500,
    ) -> int:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(table)) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(id_column)):
            raise ValueError("unsafe table or column")
        clean_ids = [str(item) for item in ids if str(item)]
        deleted = 0
        chunk = self._coerce_memory_int(chunk_size, default=500, minimum=1)
        for index in range(0, len(clean_ids), chunk):
            part = clean_ids[index:index + chunk]
            placeholders = ",".join("?" for _ in part)
            cursor = conn.execute(f"DELETE FROM {table} WHERE {id_column} IN ({placeholders})", part)
            deleted += int(cursor.rowcount if cursor.rowcount is not None else 0)
        return deleted

    def _cleanup_low_value_builtin_memories_sync(self, user_id: str) -> int:
        self._init_builtin_memory_db_sync()
        min_salience = self._coerce_memory_int(
            getattr(self, "memory_quality_min_salience", MEMORY_QUALITY_MIN_SALIENCE),
            default=MEMORY_QUALITY_MIN_SALIENCE,
            minimum=0,
        )
        max_delete = self._coerce_memory_int(getattr(self, "memory_cleanup_max_delete", 200), default=200, minimum=1)
        with self._connect_local_memory_db() as conn:
            rows = conn.execute(
                "SELECT * FROM memories WHERE user_id = ? AND salience < ? ORDER BY updated_at ASC LIMIT ?",
                (str(user_id), min_salience, max_delete),
            ).fetchall()
            ids = [str(row["id"]) for row in rows if not self._is_builtin_memory_row_protected(row)]
            return self._delete_rows_by_ids(conn, "memories", "id", ids)

    async def _cleanup_low_value_builtin_memories(self, user_id: str) -> int:
        try:
            if not await self._ensure_builtin_memory_ready():
                return 0
            return await asyncio.to_thread(self._cleanup_low_value_builtin_memories_sync, user_id)
        except Exception as e:
            self.logger.error(f"cleanup builtin memories failed: {e}", exc_info=True)
            return 0

    def _prune_builtin_memories_sync(self, conn: sqlite3.Connection, user_id: str) -> int:
        limit = self._coerce_memory_int(getattr(self, "builtin_memory_retention_limit", 1000), default=1000, minimum=1)
        rows = conn.execute(
            "SELECT * FROM memories WHERE user_id = ? ORDER BY updated_at DESC",
            (str(user_id),),
        ).fetchall()
        overflow = rows[limit:]
        ids = [str(row["id"]) for row in overflow if not self._is_builtin_memory_row_protected(row)]
        return self._delete_rows_by_ids(conn, "memories", "id", ids)

    def _backfill_builtin_memory_privacy_sync(self, limit: Any = 0) -> int:
        self._init_builtin_memory_db_sync()
        effective_limit = self._coerce_memory_int(limit, default=0, minimum=0)
        sql = "SELECT * FROM memories WHERE evidence_json = '' OR evidence_json = '{}' OR visibility = ''"
        params: List[Any] = []
        if effective_limit:
            sql += " LIMIT ?"
            params.append(effective_limit)
        updated = 0
        with self._connect_local_memory_db() as conn:
            for row in conn.execute(sql, params).fetchall():
                entry = self._row_to_memory_entry(row)
                evidence = dict(entry.get("evidence") or {})
                if self._is_builtin_memory_row_protected(row):
                    evidence["protected"] = True
                conn.execute(
                    "UPDATE memories SET evidence_json = ? WHERE id = ?",
                    (self._memory_json_dumps(evidence), entry["id"]),
                )
                updated += 1
        return updated

    async def _backfill_builtin_memory_privacy(self, limit: Any = 0) -> int:
        try:
            if not await self._ensure_builtin_memory_ready():
                return 0
            return await asyncio.to_thread(self._backfill_builtin_memory_privacy_sync, limit)
        except Exception as e:
            self.logger.error(f"backfill builtin memory privacy failed: {e}", exc_info=True)
            return 0

    def _get_recent_builtin_memories_sync(self, user_id: str, limit: Any = 3) -> List[Dict[str, Any]]:
        self._init_builtin_memory_db_sync()
        effective_limit = self._coerce_memory_int(limit, default=3, minimum=0, maximum=1000)
        if effective_limit <= 0:
            return []
        with self._connect_local_memory_db() as conn:
            rows = conn.execute(
                "SELECT * FROM memories WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
                (str(user_id), effective_limit),
            ).fetchall()
        return [self._row_to_memory_entry(row) for row in rows]

    async def _get_recent_builtin_memories(self, user_id: str, limit: Any = 3) -> List[Dict[str, Any]]:
        try:
            if not await self._ensure_builtin_memory_ready():
                return []
            return await asyncio.to_thread(self._get_recent_builtin_memories_sync, user_id, limit)
        except Exception as e:
            self.logger.error(f"get recent builtin memories failed: {e}", exc_info=True)
            return []

    def _get_builtin_memory_stats_sync(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        return self._check_builtin_memory_health_sync(user_id)

    async def _get_builtin_memory_stats(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        return await self._check_builtin_memory_health(user_id)

    def _search_builtin_memories_sync(self, user_id: str, query: str, limit: Any = 3) -> List[Dict[str, Any]]:
        self._init_builtin_memory_db_sync()
        effective_limit = self._coerce_memory_int(limit, default=3, minimum=0, maximum=1000)
        if effective_limit <= 0:
            return []
        terms = self._extract_mnemosyne_terms(query)
        with self._connect_local_memory_db() as conn:
            rows = conn.execute(
                "SELECT * FROM memories WHERE user_id = ? ORDER BY salience DESC, updated_at DESC LIMIT ?",
                (str(user_id), max(effective_limit * 4, effective_limit)),
            ).fetchall()
        entries = [self._row_to_memory_entry(row) for row in rows]
        if terms:
            filtered = [
                entry for entry in entries
                if any(term in entry.get("normalized_content", "") or term in " ".join(entry.get("keywords", [])) for term in terms)
            ]
        else:
            filtered = entries
        return filtered[:effective_limit]

    async def _search_builtin_memories(self, user_id: str, query: str, limit: Any = 3) -> List[Dict[str, Any]]:
        try:
            if not await self._ensure_builtin_memory_ready():
                return []
            return await asyncio.to_thread(self._search_builtin_memories_sync, user_id, query, limit)
        except Exception as e:
            self.logger.error(f"search builtin memories failed: {e}", exc_info=True)
            return []

    def _build_builtin_memory_query_cache_key(
        self,
        user_id: str,
        query_terms: List[str],
        limit: Any,
        cooldown_seconds: Any = None,
        layer_quotas: Optional[Dict[str, Any]] = None,
    ) -> str:
        payload = {
            "user": str(user_id),
            "terms": [str(term) for term in (query_terms or [])],
            "limit": self._coerce_memory_int(limit, default=0, minimum=0),
            "cooldown": self._coerce_memory_int(cooldown_seconds, default=0, minimum=0),
            "recent": self._build_memory_recall_cooldown_signature(user_id, cooldown_seconds),
            "hot_days": self._coerce_memory_int(getattr(self, "memory_hot_days", MEMORY_HOT_DAYS), default=MEMORY_HOT_DAYS, minimum=0),
            "warm_days": self._coerce_memory_int(getattr(self, "memory_warm_days", MEMORY_WARM_DAYS), default=MEMORY_WARM_DAYS, minimum=0),
            "layers": layer_quotas or {},
        }
        return hashlib.sha1(self._memory_json_dumps(payload).encode("utf-8")).hexdigest()

    def _cache_builtin_memory_query_result(self, cache_key: str, result: List[Dict[str, Any]]):
        cache = getattr(self, "_local_memory_query_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._local_memory_query_cache = cache
        cache[cache_key] = copy.deepcopy(result)

    def _get_cached_builtin_memory_query(self, cache_key: str) -> Optional[List[Dict[str, Any]]]:
        cache = getattr(self, "_local_memory_query_cache", None)
        if not isinstance(cache, dict):
            self._local_memory_query_cache = {}
            return None
        value = cache.get(cache_key)
        return copy.deepcopy(value) if isinstance(value, list) else None

    def _clear_local_memory_query_cache(self):
        cache = getattr(self, "_local_memory_query_cache", None)
        if isinstance(cache, dict):
            cache.clear()

    def _build_memory_recall_candidate_limit(
        self,
        limit: Any,
        cooldown_seconds: Any = None,
        layer_quotas: Optional[Dict[str, Any]] = None,
    ) -> int:
        base = self._coerce_memory_int(limit, default=0, minimum=0)
        quotas = self._get_memory_layer_quotas(layer_quotas)
        quota_sum = sum(quotas.values())
        if self._coerce_memory_int(cooldown_seconds, default=0, minimum=0) > 0:
            return max(base * 3, base + quota_sum, base)
        return max(base, quota_sum)

    def _retrieve_builtin_memories_sync(
        self,
        user_id: str,
        query: str,
        limit: Any,
        layer_quotas: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        candidate_limit = self._build_memory_recall_candidate_limit(limit, layer_quotas=layer_quotas)
        return self._search_builtin_memories_sync(user_id, query, candidate_limit)

    def _mark_builtin_memory_hits_sync(self, memories: List[Dict[str, Any]]):
        ids = [str(item.get("id", "")) for item in memories or [] if isinstance(item, dict) and item.get("id")]
        if not ids:
            return
        now = datetime.now().isoformat()
        with self._connect_local_memory_db() as conn:
            for memory_id in ids:
                conn.execute(
                    "UPDATE memories SET hit_count = hit_count + 1, last_hit_at = ? WHERE id = ?",
                    (now, memory_id),
                )

    async def _retrieve_builtin_vector_memories(self, user_id: str, query: str, limit: Any = 3) -> List[Dict[str, Any]]:
        return []

    def _normalize_embedding_vector(self, value: Any) -> Optional[List[float]]:
        if isinstance(value, dict):
            try:
                value = value.get("data", [{}])[0].get("embedding")
            except Exception:
                return None
        if not isinstance(value, list):
            return None
        vector: List[float] = []
        for item in value:
            try:
                number = float(item)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(number):
                return None
            vector.append(number)
        max_dimensions = self._coerce_memory_int(
            getattr(self, "builtin_memory_vector_max_dimensions", BUILTIN_MEMORY_VECTOR_MAX_DIMENSIONS),
            default=BUILTIN_MEMORY_VECTOR_MAX_DIMENSIONS,
            minimum=1,
        )
        if not vector or len(vector) > max_dimensions:
            return None
        return vector

    def _cosine_similarity(self, left: List[float], right: List[float]) -> float:
        left_vec = self._normalize_embedding_vector(left)
        right_vec = self._normalize_embedding_vector(right)
        if not left_vec or not right_vec or len(left_vec) != len(right_vec):
            return 0.0
        dot = sum(a * b for a, b in zip(left_vec, right_vec))
        left_norm = math.sqrt(sum(a * a for a in left_vec))
        right_norm = math.sqrt(sum(b * b for b in right_vec))
        if left_norm <= 0 or right_norm <= 0:
            return 0.0
        return max(0.0, min(1.0, dot / (left_norm * right_norm)))

    def _upsert_builtin_memory_vector_sync(self, user_id: str, memory_id: str, vector: Any) -> bool:
        normalized = self._normalize_embedding_vector(vector)
        if not normalized:
            return False
        self._init_builtin_memory_db_sync()
        now = datetime.now().isoformat()
        provider_id = str(getattr(self, "embedding_provider_id", "") or "")
        with self._connect_local_memory_db() as conn:
            conn.execute(
                """
                INSERT INTO memory_vectors(memory_id, user_id, provider_id, dimensions, vector_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    provider_id = excluded.provider_id,
                    dimensions = excluded.dimensions,
                    vector_json = excluded.vector_json,
                    updated_at = excluded.updated_at
                """,
                (
                    str(memory_id),
                    str(user_id),
                    provider_id,
                    len(normalized),
                    self._memory_json_dumps(normalized),
                    now,
                ),
            )
        return True

    def _merge_builtin_vector_memory_results(
        self,
        keyword_results: List[Dict[str, Any]],
        vector_results: List[Dict[str, Any]],
        recent_results: List[Dict[str, Any]],
        limit: Any,
    ) -> List[Dict[str, Any]]:
        effective_limit = self._coerce_memory_int(limit, default=3, minimum=0)
        merged: Dict[str, Dict[str, Any]] = {}
        for group in (keyword_results or [], vector_results or [], recent_results or []):
            for item in group:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("id", "") or item.get("fingerprint", "") or item.get("content", ""))
                if key and key not in merged:
                    merged[key] = dict(item)
        return list(merged.values())[:effective_limit]

    async def _retrieve_from_builtin_memory(
        self,
        user_id: str,
        query: str,
        limit: Any = 3,
        cooldown_seconds: Any = None,
        layer_quotas: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        if not await self._ensure_builtin_memory_ready():
            return []
        await self._apply_memory_recall_negative_feedback(user_id, query)
        if isinstance(limit, str):
            try:
                int(limit)
            except ValueError:
                return []
        effective_limit = self._coerce_memory_int(limit, default=3, minimum=0, maximum=100)
        if effective_limit <= 0:
            return []
        terms = self._extract_mnemosyne_terms(query)
        cache_key = self._build_builtin_memory_query_cache_key(user_id, terms, effective_limit, cooldown_seconds, layer_quotas)
        cached = self._get_cached_builtin_memory_query(cache_key)
        if cached is not None:
            return cached
        candidate_limit = self._build_memory_recall_candidate_limit(effective_limit, cooldown_seconds, layer_quotas)
        keyword_results = await asyncio.to_thread(
            self._retrieve_builtin_memories_sync,
            user_id,
            query,
            candidate_limit,
            layer_quotas,
        )
        vector_results = await self._retrieve_builtin_vector_memories(user_id, query, candidate_limit)
        selected = self._merge_builtin_vector_memory_results(keyword_results, vector_results, [], candidate_limit)
        selected = self._filter_memory_recall_cooldown(user_id, selected, effective_limit, cooldown_seconds)
        self._remember_recent_builtin_memory_recall(user_id, selected)
        self._mark_builtin_memory_hits_sync(selected)
        self._cache_builtin_memory_query_result(cache_key, selected)
        return selected

    def _check_builtin_memory_health_sync(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        self._init_builtin_memory_db_sync()
        with self._connect_local_memory_db() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
            active = conn.execute("SELECT COUNT(*) AS c FROM memories WHERE superseded_by = ''").fetchone()["c"]
            vectors = conn.execute("SELECT COUNT(*) AS c FROM memory_vectors").fetchone()["c"]
            rows = conn.execute("SELECT layer, COUNT(*) AS c FROM memories GROUP BY layer").fetchall()
        return {
            "ok": True,
            "integrity": "ok",
            "total": int(total),
            "active": int(active),
            "vectors": int(vectors),
            "superseded": max(0, int(total) - int(active)),
            "missing_evidence": 0,
            "schema_version": 1,
            "db_size": self._get_local_memory_db_file().stat().st_size if self._get_local_memory_db_file().exists() else 0,
            "by_layer": {str(row["layer"]): int(row["c"]) for row in rows},
        }

    async def _check_builtin_memory_health(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        try:
            if not await self._ensure_builtin_memory_ready():
                return {"ok": False, "integrity": "disabled"}
            return await asyncio.to_thread(self._check_builtin_memory_health_sync, user_id)
        except Exception as e:
            self.logger.error(f"check builtin memory health failed: {e}", exc_info=True)
            return {"ok": False, "integrity": "error", "error": str(e)}

    def _repair_builtin_memory_sync(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        backfilled = self._backfill_builtin_memory_privacy_sync()
        return {"backfilled": backfilled, "fts_rebuilt": 0, "orphan_vectors": 0, "orphan_fts": 0}

    async def _repair_builtin_memory(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        try:
            if not await self._ensure_builtin_memory_ready():
                return {"backfilled": 0, "fts_rebuilt": 0, "orphan_vectors": 0, "orphan_fts": 0}
            return await asyncio.to_thread(self._repair_builtin_memory_sync, user_id)
        except Exception as e:
            self.logger.error(f"repair builtin memory failed: {e}", exc_info=True)
            return {"backfilled": 0, "fts_rebuilt": 0, "orphan_vectors": 0, "orphan_fts": 0}

    def _build_memory_health_report(self, health: Dict[str, Any]) -> str:
        active = self._coerce_memory_int((health or {}).get("active", 0), default=0, minimum=0)
        total = self._coerce_memory_int((health or {}).get("total", 0), default=0, minimum=0)
        vectors = self._coerce_memory_int((health or {}).get("vectors", 0), default=0, minimum=0)
        by_layer = (health or {}).get("by_layer", {})
        if not isinstance(by_layer, dict):
            by_layer = {}
        layer_text = ", ".join(
            f"{key}:{self._coerce_memory_int(value, default=0, minimum=0)}"
            for key, value in sorted(by_layer.items())
        ) or "none"
        return "\n".join([
            "本地记忆健康检查",
            f"状态：{(health or {}).get('integrity', 'unknown')}",
            f"活动记忆：{active}",
            f"总记忆：{total}",
            f"向量：{vectors}",
            f"分层：{layer_text}",
        ])

    def _build_memory_stats_report(self, stats: Dict[str, Any]) -> str:
        version = self._coerce_memory_int((stats or {}).get("schema_version", 1), default=1, minimum=1)
        total = self._coerce_memory_int((stats or {}).get("total", 0), default=0, minimum=0)
        active = self._coerce_memory_int((stats or {}).get("active", 0), default=0, minimum=0)
        return f"本地记忆统计 v{version}\n总记忆：{total}\n活动记忆：{active}"

    def _build_recent_memory_report(self, memories: List[Dict[str, Any]]) -> str:
        if not memories:
            return "最近记忆：暂无"
        lines = ["最近记忆"]
        for index, entry in enumerate(memories[:10], start=1):
            if not isinstance(entry, dict):
                continue
            layer = str(entry.get("memory_layer") or entry.get("layer") or "impression")
            memory_type = str(entry.get("type") or entry.get("memory_type") or "interaction")
            salience = self._coerce_memory_int(entry.get("salience", 0), default=0, minimum=0)
            summary = WHITESPACE_PATTERN.sub(" ", str(entry.get("summary") or entry.get("content") or "")).strip()[:80]
            if not summary:
                summary = "(空记忆)"
            lines.append(f"{index}. [{layer}/{memory_type}/显著{salience}] {summary}")
        return "\n".join(lines)

    def _build_memory_search_report(self, memories: List[Dict[str, Any]], query: str = "") -> str:
        query_text = WHITESPACE_PATTERN.sub(" ", str(query or "")).strip()
        title = f"记忆搜索：{query_text}" if query_text else "记忆搜索"
        if not memories:
            return f"{title}\n暂无匹配记忆"
        lines = [title]
        for index, entry in enumerate(memories[:10], start=1):
            if not isinstance(entry, dict):
                continue
            memory_id = str(entry.get("id") or entry.get("fingerprint") or "")[:8]
            layer = str(entry.get("memory_layer") or entry.get("layer") or "impression")
            salience = self._coerce_memory_int(entry.get("salience", 0), default=0, minimum=0)
            summary = WHITESPACE_PATTERN.sub(" ", str(entry.get("summary") or entry.get("content") or "")).strip()[:80]
            lines.append(f"{index}. {memory_id} [{layer}/显著{salience}] {summary or '(空记忆)'}")
        return "\n".join(lines)

    def _build_memory_repair_report(self, result: Dict[str, Any]) -> str:
        backfilled = self._coerce_memory_int((result or {}).get("backfilled", 0), default=0, minimum=0)
        fts = self._coerce_memory_int((result or {}).get("fts_rebuilt", 0), default=0, minimum=0)
        vectors = self._coerce_memory_int((result or {}).get("orphan_vectors", 0), default=0, minimum=0)
        fts_orphan = self._coerce_memory_int((result or {}).get("orphan_fts", 0), default=0, minimum=0)
        return f"本地记忆修复完成\n隐私回填：{backfilled}\nFTS：{fts}\n孤立向量：{vectors}\n孤立FTS：{fts_orphan}"

    def _build_memory_mode_report(self) -> str:
        mode = str(getattr(self, "memory_mode_preset", "balanced") or "balanced")
        limit = self._coerce_memory_int(getattr(self, "memory_prompt_limit", MEMORY_PROMPT_LIMIT), default=MEMORY_PROMPT_LIMIT, minimum=0)
        char_budget = self._coerce_memory_int(
            getattr(self, "builtin_memory_prompt_char_budget", BUILTIN_MEMORY_PROMPT_CHAR_BUDGET),
            default=BUILTIN_MEMORY_PROMPT_CHAR_BUDGET,
            minimum=0,
        )
        token_budget = self._coerce_memory_int(getattr(self, "prompt_token_budget", PROMPT_TOKEN_BUDGET), default=PROMPT_TOKEN_BUDGET, minimum=0)
        cooldown = self._coerce_memory_int(getattr(self, "memory_recall_cooldown_seconds", 0), default=0, minimum=0)
        history_limit = self._coerce_memory_int(
            getattr(self, "prompt_budget_history_limit", PROMPT_BUDGET_HISTORY_LIMIT),
            default=PROMPT_BUDGET_HISTORY_LIMIT,
            minimum=0,
        )
        return "\n".join([
            f"记忆模式：{mode}",
            f"记忆注入条数：{limit}",
            f"记忆字符预算：{char_budget}",
            f"Prompt预算：{token_budget}",
            f"预算历史窗口：{history_limit}",
            f"召回冷却：{cooldown}s",
        ])

    def _get_mnemosyne_memory_file(self, user_id: str) -> Path:
        shared_dir = self.data_dir.parent.parent / "shared_memory"
        shared_dir.mkdir(parents=True, exist_ok=True)
        return shared_dir / f"marianna_{self._safe_user_file_stem(user_id)}.jsonl"

    def _infer_mnemosyne_memory_layer(
        self,
        memory_type: str,
        raw_content: str,
        salience: int,
    ) -> str:
        if memory_type == "auto_summary":
            return "summary"
        if memory_type == "milestone":
            return "event"
        if memory_type == "profile":
            return "profile"
        if memory_type == "interaction":
            if salience >= 6 or re.search(
                r"阶段转折|秘密|承诺|答应|约定|背叛|离开|回来|边界|生日|只有你|唯一|命定|锁定|崩溃",
                raw_content,
            ):
                return "event"
            return "impression"
        return "impression"

    def _parse_iso_datetime(self, value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None

    def _get_latest_iso_timestamp(self, *values: Any) -> str:
        dated_values = [
            (self._parse_iso_datetime(value), str(value or "").strip())
            for value in values
            if str(value or "").strip()
        ]
        dated_values = [item for item in dated_values if item[0] is not None]
        if not dated_values:
            return ""
        dated_values.sort(key=lambda item: item[0])
        return dated_values[-1][1]

    def _get_mnemosyne_entry_age_days(self, entry: Dict[str, Any]) -> float:
        ref_dt = (
            self._parse_iso_datetime(entry.get("last_hit_at"))
            or self._parse_iso_datetime(entry.get("last_reinforced_at"))
            or self._parse_iso_datetime(entry.get("timestamp"))
        )
        if not ref_dt:
            return 0.0
        return max(0.0, (datetime.now() - ref_dt).total_seconds() / 86400.0)

    def _get_mnemosyne_entry_overlap(
        self,
        left: Dict[str, Any],
        right: Dict[str, Any],
    ) -> float:
        left_keywords = set(left.get("keywords", []))
        right_keywords = set(right.get("keywords", []))
        overlap = 0.0
        if left_keywords and right_keywords:
            union = left_keywords | right_keywords
            if union:
                overlap = len(left_keywords & right_keywords) / len(union)

        left_normalized = str(left.get("normalized_content", "") or "")
        right_normalized = str(right.get("normalized_content", "") or "")
        if left_normalized and right_normalized:
            if left_normalized in right_normalized or right_normalized in left_normalized:
                overlap = max(overlap, 0.9)
            else:
                shared = sum(
                    1 for term in left_keywords
                    if len(term) >= 2 and term in right_normalized
                )
                if left_keywords:
                    overlap = max(overlap, shared / max(1, len(left_keywords)))

        return max(0.0, min(1.0, overlap))

    def _get_mnemosyne_decay_penalty(self, entry: Dict[str, Any]) -> int:
        if not getattr(self, "enable_memory_forgetting_layer", True):
            return 0

        age_days = self._get_mnemosyne_entry_age_days(entry)
        hit_count = self._coerce_memory_int(entry.get("hit_count", 0), default=0, minimum=0)
        salience = self._coerce_memory_int(entry.get("salience", 0), default=0, minimum=0)
        layer = entry.get("memory_layer", "impression")
        decay_days = self._coerce_memory_int(
            getattr(self, "memory_decay_days", MEMORY_DECAY_DAYS),
            default=MEMORY_DECAY_DAYS,
            minimum=1,
        )
        layer_window = {
            "event": max(90, decay_days * 3),
            "summary": max(75, decay_days * 2),
            "profile": max(120, decay_days * 4),
            "impression": decay_days,
        }.get(layer, decay_days)

        penalty = 0
        if age_days > layer_window:
            penalty += 1 + int((age_days - layer_window) // max(1, layer_window))
        if hit_count <= 0 and age_days > layer_window * 0.6:
            penalty += 1
        if entry.get("superseded_by"):
            penalty += 3
        penalty -= min(2, hit_count // 3)
        if salience >= 6:
            penalty -= 1
        return max(0, penalty)

    def _should_prune_mnemosyne_entry(self, entry: Dict[str, Any]) -> bool:
        if not getattr(self, "enable_memory_forgetting_layer", True):
            return False

        layer = entry.get("memory_layer", "impression")
        if layer in {"event", "profile"}:
            return False

        age_days = self._get_mnemosyne_entry_age_days(entry)
        cleanup_days = max(
            30,
            self._coerce_memory_int(
                getattr(self, "memory_hard_cleanup_days", MEMORY_HARD_CLEANUP_DAYS),
                default=MEMORY_HARD_CLEANUP_DAYS,
                minimum=1,
            ),
        )
        hit_count = self._coerce_memory_int(entry.get("hit_count", 0), default=0, minimum=0)
        salience = self._coerce_memory_int(entry.get("salience", 0), default=0, minimum=0)

        if (
            entry.get("superseded_by")
            and age_days >= max(30.0, cleanup_days / 3.0)
            and hit_count <= 2
        ):
            return True

        if layer == "impression" and age_days >= cleanup_days and salience <= 2 and hit_count <= 1:
            return True

        if layer == "summary" and age_days >= cleanup_days * 2 and salience <= 2 and hit_count <= 0:
            return True

        return False

    def _merge_duplicate_mnemosyne_entries(
        self,
        primary: Dict[str, Any],
        duplicate: Dict[str, Any],
    ) -> Dict[str, Any]:
        merged = dict(primary)
        merged["keywords"] = list(
            dict.fromkeys(
                [str(item) for item in primary.get("keywords", [])]
                + [str(item) for item in duplicate.get("keywords", [])]
            )
        )[:24]
        merged["salience"] = max(
            self._coerce_memory_int(primary.get("salience", 0), default=0, minimum=0),
            self._coerce_memory_int(duplicate.get("salience", 0), default=0, minimum=0),
        )
        merged["hit_count"] = max(
            self._coerce_memory_int(primary.get("hit_count", 0), default=0, minimum=0),
            self._coerce_memory_int(duplicate.get("hit_count", 0), default=0, minimum=0),
        )
        merged["reinforcement_count"] = max(
            self._coerce_memory_int(primary.get("reinforcement_count", 0), default=0, minimum=0),
            self._coerce_memory_int(duplicate.get("reinforcement_count", 0), default=0, minimum=0),
        )
        merged["timestamp"] = self._get_latest_iso_timestamp(
            primary.get("timestamp"),
            duplicate.get("timestamp"),
        ) or str(primary.get("timestamp", "") or duplicate.get("timestamp", ""))
        merged["last_hit_at"] = self._get_latest_iso_timestamp(
            primary.get("last_hit_at"),
            duplicate.get("last_hit_at"),
        )
        merged["last_reinforced_at"] = self._get_latest_iso_timestamp(
            primary.get("last_reinforced_at"),
            duplicate.get("last_reinforced_at"),
        )
        if not merged.get("superseded_by") and duplicate.get("superseded_by"):
            merged["superseded_by"] = str(duplicate.get("superseded_by", "") or "")
        merged["superseded_at"] = self._get_latest_iso_timestamp(
            primary.get("superseded_at"),
            duplicate.get("superseded_at"),
        )
        if not merged.get("revision_of") and duplicate.get("revision_of"):
            merged["revision_of"] = str(duplicate.get("revision_of", "") or "")
        if len(str(duplicate.get("raw_content", "") or "")) > len(str(merged.get("raw_content", "") or "")):
            merged["raw_content"] = str(duplicate.get("raw_content", "") or merged.get("raw_content", ""))
            merged["content"] = str(duplicate.get("content", "") or merged.get("content", ""))
            merged["normalized_content"] = str(
                duplicate.get("normalized_content", "") or merged.get("normalized_content", "")
            )
        return merged

    def _reinforce_existing_mnemosyne_entry(
        self,
        entry: Dict[str, Any],
        incoming_entry: Dict[str, Any],
        now_iso: str,
    ) -> bool:
        changed = False
        old_reinforcement = self._coerce_memory_int(entry.get("reinforcement_count", 0), default=0, minimum=0)
        new_reinforcement = old_reinforcement + 1
        if self._coerce_memory_int(entry.get("reinforcement_count", 0), default=0, minimum=0) != new_reinforcement:
            entry["reinforcement_count"] = new_reinforcement
            changed = True

        if str(entry.get("last_reinforced_at", "") or "") != now_iso:
            entry["last_reinforced_at"] = now_iso
            changed = True

        if entry.get("superseded_by") or entry.get("superseded_at"):
            entry["superseded_by"] = ""
            entry["superseded_at"] = ""
            changed = True

        latest_timestamp = self._get_latest_iso_timestamp(entry.get("timestamp"), now_iso) or now_iso
        if str(entry.get("timestamp", "") or "") != latest_timestamp:
            entry["timestamp"] = latest_timestamp
            changed = True

        incoming_salience = self._coerce_memory_int(incoming_entry.get("salience", 0), default=0, minimum=0)
        current_salience = self._coerce_memory_int(entry.get("salience", 0), default=0, minimum=0)
        target_salience = max(current_salience, incoming_salience)
        if new_reinforcement in {2, 4, 7} and target_salience < 10:
            target_salience += 1
        target_salience = max(0, min(10, target_salience))
        if entry.get("salience") != target_salience:
            entry["salience"] = target_salience
            changed = True

        merged_keywords = list(
            dict.fromkeys(
                [str(item) for item in entry.get("keywords", [])]
                + [str(item) for item in incoming_entry.get("keywords", [])]
            )
        )[:24]
        if merged_keywords != list(entry.get("keywords", [])):
            entry["keywords"] = merged_keywords
            changed = True

        current_raw = str(entry.get("raw_content", "") or "")
        incoming_raw = str(incoming_entry.get("raw_content", "") or "")
        if incoming_raw and len(incoming_raw) > len(current_raw):
            entry["raw_content"] = incoming_raw
            entry["content"] = str(incoming_entry.get("content", "") or incoming_raw)
            entry["normalized_content"] = str(
                incoming_entry.get("normalized_content", "") or entry.get("normalized_content", "")
            )
            changed = True

        return changed

    def _apply_memory_update_layer(
        self,
        entries: List[Dict[str, Any]],
        new_entry: Dict[str, Any],
        now_iso: str,
    ) -> bool:
        if not getattr(self, "enable_memory_update_layer", True):
            return False
        if new_entry.get("memory_layer") not in {"impression", "summary"}:
            return False

        changed = False
        revision_of = ""
        candidate_count = 0
        for entry in reversed(entries):
            if entry.get("fingerprint") == new_entry.get("fingerprint"):
                continue
            if entry.get("memory_layer") != new_entry.get("memory_layer"):
                continue
            if entry.get("type") != new_entry.get("type"):
                continue
            if entry.get("superseded_by"):
                continue

            overlap = self._get_mnemosyne_entry_overlap(entry, new_entry)
            if overlap < 0.58:
                continue

            current_salience = self._coerce_memory_int(entry.get("salience", 0), default=0, minimum=0)
            incoming_salience = self._coerce_memory_int(new_entry.get("salience", 0), default=0, minimum=0)
            if overlap < 0.78 and incoming_salience + 1 < current_salience:
                continue

            entry["superseded_by"] = str(new_entry.get("fingerprint", "") or "")
            entry["superseded_at"] = now_iso
            changed = True
            candidate_count += 1
            if not revision_of:
                revision_of = str(entry.get("fingerprint", "") or "")
            if candidate_count >= 2:
                break

        if revision_of and not new_entry.get("revision_of"):
            new_entry["revision_of"] = revision_of
            changed = True
        return changed

    def _mark_mnemosyne_entries_hit(
        self,
        memories: List[Dict[str, Any]],
        selected: List[Dict[str, Any]],
    ) -> bool:
        if not selected:
            return False

        hit_fingerprints = {
            str(item.get("fingerprint", "") or "")
            for item in selected
            if str(item.get("fingerprint", "") or "")
        }
        if not hit_fingerprints:
            return False

        now_iso = datetime.now().isoformat()
        changed = False
        for entry in memories:
            fingerprint = str(entry.get("fingerprint", "") or "")
            if fingerprint not in hit_fingerprints:
                continue

            current_hits = self._coerce_memory_int(entry.get("hit_count", 0), default=0, minimum=0)
            next_hits = current_hits + 1
            if entry.get("hit_count") != next_hits:
                entry["hit_count"] = next_hits
                changed = True
            if str(entry.get("last_hit_at", "") or "") != now_iso:
                entry["last_hit_at"] = now_iso
                changed = True

            if (
                getattr(self, "enable_memory_update_layer", True)
                and entry.get("memory_layer") in {"impression", "summary"}
                and not entry.get("superseded_by")
                and next_hits in {2, 5, 9}
                and self._coerce_memory_int(entry.get("salience", 0), default=0, minimum=0) < 10
            ):
                entry["salience"] = self._coerce_memory_int(entry.get("salience", 0), default=0, minimum=0) + 1
                changed = True

        return changed

    def _prefer_recent_active_memories(
        self,
        memories: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        preferred: List[Dict[str, Any]] = []
        for mem in reversed(memories):
            if mem.get("superseded_by"):
                continue
            if self._get_mnemosyne_decay_penalty(mem) >= 4:
                continue
            preferred.append(mem)
        return preferred

    def _hydrate_mnemosyne_entry(self, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        content = str(entry.get("content", "") or "").strip()
        raw_content = str(entry.get("raw_content", "") or content).strip()
        if not raw_content:
            return None

        normalized_content = self._normalize_mnemosyne_content(raw_content)
        if not normalized_content:
            return None

        hydrated = dict(entry)
        hydrated["content"] = content or raw_content
        hydrated["raw_content"] = raw_content
        hydrated["normalized_content"] = normalized_content
        hydrated["fingerprint"] = (
            str(entry.get("fingerprint", "") or "")
            or self._make_mnemosyne_fingerprint(raw_content)
        )
        keywords = entry.get("keywords")
        if not isinstance(keywords, list) or not keywords:
            keywords = self._extract_mnemosyne_terms(raw_content)
        hydrated["keywords"] = [str(item) for item in keywords if str(item).strip()][:24]
        hydrated["type"] = str(entry.get("type", "interaction") or "interaction")
        hydrated["source"] = str(entry.get("source", "marianna") or "marianna")
        hydrated["timestamp"] = str(entry.get("timestamp", "") or datetime.now().isoformat())
        default_salience = {
            "milestone": 6,
            "auto_summary": 3,
            "interaction": 2,
        }.get(hydrated["type"], 1)
        salience = self._coerce_memory_int(
            entry.get("salience", default_salience),
            default=default_salience,
        )
        hydrated["salience"] = max(0, min(10, salience))
        memory_layer = str(entry.get("memory_layer", "") or "").strip()
        if memory_layer not in {"profile", "impression", "event", "summary"}:
            memory_layer = self._infer_mnemosyne_memory_layer(
                hydrated["type"],
                raw_content,
                hydrated["salience"],
            )
        hydrated["memory_layer"] = memory_layer
        hit_count = self._coerce_memory_int(entry.get("hit_count", 0), default=0, minimum=0)
        hydrated["hit_count"] = max(0, hit_count)
        reinforcement_count = self._coerce_memory_int(
            entry.get("reinforcement_count", 0),
            default=0,
            minimum=0,
        )
        hydrated["reinforcement_count"] = max(0, reinforcement_count)
        last_hit_at = self._get_latest_iso_timestamp(entry.get("last_hit_at"))
        hydrated["last_hit_at"] = last_hit_at
        last_reinforced_at = self._get_latest_iso_timestamp(entry.get("last_reinforced_at"))
        hydrated["last_reinforced_at"] = last_reinforced_at
        hydrated["superseded_by"] = str(entry.get("superseded_by", "") or "").strip()
        hydrated["superseded_at"] = self._get_latest_iso_timestamp(entry.get("superseded_at"))
        hydrated["revision_of"] = str(entry.get("revision_of", "") or "").strip()
        return hydrated

    def _copy_mnemosyne_entries(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return copy.deepcopy(entries)

    def _get_mnemosyne_file_signature(self, memory_file: Path) -> Optional[Tuple[int, int]]:
        if not memory_file.exists():
            self._get_mnemosyne_runtime_cache("_mnemosyne_entries_cache").pop(str(memory_file), None)
            return None
        try:
            stat = memory_file.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            self._get_mnemosyne_runtime_cache("_mnemosyne_entries_cache").pop(str(memory_file), None)
            return None

    def _read_mnemosyne_entries_uncached(self, memory_file: Path) -> List[Dict[str, Any]]:
        if not memory_file.exists():
            return []

        entries: List[Dict[str, Any]] = []
        with open(memory_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                hydrated = self._hydrate_mnemosyne_entry(data)
                if hydrated:
                    entries.append(hydrated)
        return entries

    def _load_mnemosyne_entries(self, memory_file: Path) -> List[Dict[str, Any]]:
        signature = self._get_mnemosyne_file_signature(memory_file)
        if signature is None:
            return []

        cache_key = str(memory_file)
        entries_cache = self._get_mnemosyne_runtime_cache("_mnemosyne_entries_cache")
        cached = entries_cache.get(cache_key)
        if (
            isinstance(cached, dict)
            and cached.get("signature") == signature
            and isinstance(cached.get("entries"), list)
        ):
            return self._copy_mnemosyne_entries(cached["entries"])

        entries = self._read_mnemosyne_entries_uncached(memory_file)
        entries_cache[cache_key] = {
            "signature": signature,
            "entries": self._copy_mnemosyne_entries(entries),
        }
        return entries

    def _refresh_mnemosyne_entries_cache(
        self,
        memory_file: Path,
        entries: List[Dict[str, Any]],
    ):
        signature = self._get_mnemosyne_file_signature(memory_file)
        if signature is None:
            return
        entries_cache = self._get_mnemosyne_runtime_cache("_mnemosyne_entries_cache")
        entries_cache[str(memory_file)] = {
            "signature": signature,
            "entries": self._copy_mnemosyne_entries(entries),
        }

    def _build_mnemosyne_query_cache_key(
        self,
        memory_file: Path,
        signature: Tuple[int, int],
        query_terms: List[str],
        limit: int,
        layer_quotas: Optional[Dict[str, Any]] = None,
    ) -> str:
        payload = {
            "file": str(memory_file),
            "signature": signature,
            "terms": query_terms,
            "limit": self._coerce_memory_int(limit, default=0, minimum=0),
            "quotas": self._get_memory_layer_quotas(layer_quotas),
            "decay_days": getattr(self, "memory_decay_days", MEMORY_DECAY_DAYS),
            "hot_days": self._coerce_memory_int(getattr(self, "memory_hot_days", MEMORY_HOT_DAYS), default=MEMORY_HOT_DAYS, minimum=0),
            "warm_days": self._coerce_memory_int(getattr(self, "memory_warm_days", MEMORY_WARM_DAYS), default=MEMORY_WARM_DAYS, minimum=0),
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(serialized.encode("utf-8")).hexdigest()

    def _prune_mnemosyne_query_cache(self):
        cutoff = time.monotonic() - MNEMOSYNE_QUERY_CACHE_TTL_SECONDS
        query_cache = self._get_mnemosyne_runtime_cache("_mnemosyne_query_cache")
        for key, value in list(query_cache.items()):
            if not isinstance(value, dict):
                del query_cache[key]
                continue
            try:
                created_at = float(value.get("_created_at", 0))
                if not math.isfinite(created_at) or created_at < cutoff:
                    del query_cache[key]
            except (TypeError, ValueError):
                del query_cache[key]
        self._trim_dict_cache(
            query_cache,
            MNEMOSYNE_QUERY_CACHE_MAX_ENTRIES,
        )

    def _get_cached_mnemosyne_query(
        self,
        cache_key: str,
    ) -> Optional[List[Dict[str, Any]]]:
        query_cache = self._get_mnemosyne_runtime_cache("_mnemosyne_query_cache")
        cached = query_cache.get(cache_key)
        if not isinstance(cached, dict):
            return None
        try:
            created_at = float(cached.get("_created_at", 0))
        except (TypeError, ValueError):
            query_cache.pop(cache_key, None)
            return None
        if not math.isfinite(created_at):
            query_cache.pop(cache_key, None)
            return None
        age = time.monotonic() - created_at
        if age > MNEMOSYNE_QUERY_CACHE_TTL_SECONDS:
            query_cache.pop(cache_key, None)
            return None
        result = cached.get("result")
        if not isinstance(result, list):
            query_cache.pop(cache_key, None)
            return None
        return self._copy_mnemosyne_entries(result)

    def _cache_mnemosyne_query_result(
        self,
        cache_key: str,
        selected: List[Dict[str, Any]],
    ):
        query_cache = self._get_mnemosyne_runtime_cache("_mnemosyne_query_cache")
        query_cache[cache_key] = {
            "_created_at": time.monotonic(),
            "result": self._copy_mnemosyne_entries(selected),
        }
        self._trim_dict_cache(
            query_cache,
            MNEMOSYNE_QUERY_CACHE_MAX_ENTRIES,
        )

    def _dedupe_mnemosyne_entries(
        self, entries: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], bool]:
        unique_reversed: List[Dict[str, Any]] = []
        fingerprint_index: Dict[str, int] = {}
        seen = set()
        changed = False

        for entry in reversed(entries):
            hydrated = self._hydrate_mnemosyne_entry(entry)
            if not hydrated:
                changed = True
                continue
            if self._should_prune_mnemosyne_entry(hydrated):
                changed = True
                continue
            if hydrated != entry:
                changed = True
            fingerprint = hydrated["fingerprint"]
            if fingerprint in seen:
                merged = self._merge_duplicate_mnemosyne_entries(
                    unique_reversed[fingerprint_index[fingerprint]],
                    hydrated,
                )
                if merged != unique_reversed[fingerprint_index[fingerprint]]:
                    unique_reversed[fingerprint_index[fingerprint]] = merged
                    changed = True
                changed = True
                continue
            seen.add(fingerprint)
            fingerprint_index[fingerprint] = len(unique_reversed)
            unique_reversed.append(hydrated)

        unique_entries = list(reversed(unique_reversed))
        if len(unique_entries) > MNEMOSYNE_MAX_SHARED_MEMORIES:
            unique_entries = unique_entries[-MNEMOSYNE_MAX_SHARED_MEMORIES:]
            changed = True

        if len(unique_entries) != len(entries):
            changed = True
        return unique_entries, changed

    async def _write_mnemosyne_entries(
        self, memory_file: Path, entries: List[Dict[str, Any]]
    ):
        payload = "\n".join(
            self._memory_json_dumps(entry) for entry in entries
        )
        if payload:
            payload += "\n"
        await self._write_text_atomic(memory_file, payload)
        self._refresh_mnemosyne_entries_cache(memory_file, entries)
        self._get_mnemosyne_runtime_cache("_mnemosyne_query_cache").clear()

    def _start_mnemosyne_flush_task(
        self,
        user_id: str,
        memory_file: Path,
        cache_key: str,
        started_at: float,
    ):
        task = asyncio.create_task(
            self._delayed_flush_mnemosyne_writes(
                user_id,
                memory_file,
                cache_key,
                started_at,
            )
        )
        self._get_mnemosyne_runtime_cache("_mnemosyne_flush_tasks")[cache_key] = task
        task.add_done_callback(
            lambda done_task, key=cache_key, uid=user_id, path=memory_file: (
                self._on_mnemosyne_flush_done(uid, path, key, done_task)
            )
        )

    def _on_mnemosyne_flush_done(
        self,
        user_id: str,
        memory_file: Path,
        cache_key: str,
        done_task: asyncio.Task,
    ):
        flush_tasks = self._get_mnemosyne_runtime_cache("_mnemosyne_flush_tasks")
        write_buffers = self._get_mnemosyne_runtime_cache("_mnemosyne_write_buffers")
        if flush_tasks.get(cache_key) is done_task:
            flush_tasks.pop(cache_key, None)
        if write_buffers.get(cache_key):
            current_task = flush_tasks.get(cache_key)
            if current_task is None or current_task.done():
                self._start_mnemosyne_flush_task(
                    user_id,
                    memory_file,
                    cache_key,
                    time.perf_counter(),
                )

    async def _queue_mnemosyne_write(
        self,
        user_id: str,
        memory_file: Path,
        memory_entry: Dict[str, Any],
        started_at: float,
    ) -> bool:
        cache_key = str(memory_file)
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        self._get_mnemosyne_runtime_list("_mnemosyne_write_buffers", cache_key).append(memory_entry)
        self._get_mnemosyne_runtime_list("_mnemosyne_write_waiters", cache_key).append(waiter)

        flush_tasks = self._get_mnemosyne_runtime_cache("_mnemosyne_flush_tasks")
        task = flush_tasks.get(cache_key)
        if task is None or task.done():
            self._start_mnemosyne_flush_task(
                user_id,
                memory_file,
                cache_key,
                started_at,
            )

        try:
            return bool(await waiter)
        except asyncio.CancelledError:
            if not waiter.done():
                waiter.cancel()
            raise

    async def _delayed_flush_mnemosyne_writes(
        self,
        user_id: str,
        memory_file: Path,
        cache_key: str,
        started_at: float,
    ):
        await asyncio.sleep(MNEMOSYNE_WRITE_DEBOUNCE_SECONDS)
        await self._flush_mnemosyne_writes(user_id, memory_file, cache_key, started_at)

    async def _flush_mnemosyne_writes(
        self,
        user_id: str,
        memory_file: Path,
        cache_key: str,
        started_at: float,
    ):
        entries = self._get_mnemosyne_runtime_cache("_mnemosyne_write_buffers").pop(cache_key, [])
        waiters = self._get_mnemosyne_runtime_cache("_mnemosyne_write_waiters").pop(cache_key, [])
        if not isinstance(entries, list):
            entries = []
        if not isinstance(waiters, list):
            waiters = []
        if not entries:
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(False)
            return

        success = False
        try:
            lock = await self._get_lock(memory_file)
            async with lock:
                now_iso = datetime.now().isoformat()
                existing_entries = self._load_mnemosyne_entries(memory_file)
                deduped_entries, changed = self._dedupe_mnemosyne_entries(existing_entries)

                for memory_entry in entries:
                    duplicate_entry = next(
                        (
                            entry for entry in deduped_entries
                            if entry.get("fingerprint") == memory_entry.get("fingerprint")
                        ),
                        None,
                    )
                    if duplicate_entry:
                        changed = (
                            self._reinforce_existing_mnemosyne_entry(
                                duplicate_entry,
                                memory_entry,
                                now_iso,
                            )
                            or changed
                        )
                        continue

                    changed = self._apply_memory_update_layer(
                        deduped_entries,
                        memory_entry,
                        now_iso,
                    ) or changed
                    deduped_entries.append(memory_entry)
                    changed = True

                deduped_entries, dedupe_changed = self._dedupe_mnemosyne_entries(deduped_entries)
                changed = changed or dedupe_changed
                if changed:
                    await self._write_mnemosyne_entries(memory_file, deduped_entries)

            success = True
            self._log_perf(
                "store_mnemosyne_batch",
                started_at,
                user_id,
                extra=f"entries={len(entries)}",
                threshold_ms=5.0,
            )
        except Exception as e:
            self._log_perf(
                "store_mnemosyne_failed",
                started_at,
                user_id,
                extra=f"entries={len(entries)}",
                threshold_ms=5.0,
            )
            self.logger.error(
                f"store_mnemosyne_batch failed: {e}",
                exc_info=True,
            )
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(success)

    async def _drain_mnemosyne_flush_tasks(self):
        """等待所有已排队的 Mnemosyne 写入完成，用于卸载前收尾。"""
        while True:
            flush_tasks = self._get_mnemosyne_runtime_cache("_mnemosyne_flush_tasks")
            write_buffers = self._get_mnemosyne_runtime_cache("_mnemosyne_write_buffers")
            self._get_mnemosyne_runtime_cache("_mnemosyne_write_waiters")
            for cache_key, task in list(flush_tasks.items()):
                if not hasattr(task, "done"):
                    flush_tasks.pop(cache_key, None)
                    continue
                if task.done():
                    flush_tasks.pop(cache_key, None)

            active_tasks = [
                task for task in flush_tasks.values()
                if hasattr(task, "done") and not task.done()
            ]
            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)
                continue

            pending_keys = list(write_buffers.keys())
            if not pending_keys:
                return

            for cache_key in pending_keys:
                await self._flush_mnemosyne_writes(
                    "shutdown",
                    Path(cache_key),
                    cache_key,
                    time.perf_counter(),
                )

    def _score_mnemosyne_entry(self, entry: Dict[str, Any], query_terms: List[str]) -> int:
        normalized_content = entry.get("normalized_content", "")
        keywords = set(entry.get("keywords", []))
        term_score = 0
        for term in query_terms:
            if term in keywords:
                term_score += 4
            elif term in normalized_content:
                term_score += 2

        if query_terms and term_score <= 0:
            return 0

        score = term_score
        memory_type = entry.get("type")
        if memory_type == "milestone":
            score += 4
        elif memory_type == "auto_summary":
            score += 2
        score += min(6, self._coerce_memory_int(entry.get("salience", 0), default=0, minimum=0))
        score += min(3, self._coerce_memory_int(entry.get("hit_count", 0), default=0, minimum=0))
        if self._get_mnemosyne_entry_age_days(entry) <= 7:
            score += 1
        score -= self._get_mnemosyne_decay_penalty(entry)
        return score

    def _get_memory_layer_quotas(self, layer_quotas: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
        layer_quotas = layer_quotas or {}
        def read_quota(name: str, config_attr: str, constant: int) -> int:
            explicit_keys = (name, f"{name}_limit")
            for key in explicit_keys:
                if key in layer_quotas:
                    return self._coerce_memory_int(layer_quotas.get(key), default=0, minimum=0)
            return self._coerce_memory_int(getattr(self, config_attr, constant), default=constant, minimum=0)
        return {
            "event": read_quota("event", "memory_prompt_event_limit", MEMORY_PROMPT_EVENT_LIMIT),
            "impression": read_quota("impression", "memory_prompt_impression_limit", MEMORY_PROMPT_IMPRESSION_LIMIT),
            "summary": read_quota("summary", "memory_prompt_summary_limit", MEMORY_PROMPT_SUMMARY_LIMIT),
            "profile": read_quota("profile", "memory_prompt_profile_limit", MEMORY_PROMPT_PROFILE_LIMIT),
        }

    def _select_layered_mnemosyne_memories(
        self,
        memories: List[Dict[str, Any]],
        query_terms: List[str],
        limit: int,
        layer_quotas: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        effective_limit = self._coerce_memory_int(limit, default=0, minimum=0)
        if effective_limit <= 0 or not memories:
            return []

        scored: List[Tuple[int, str, int, Dict[str, Any]]] = []
        for index, mem in enumerate(memories):
            score = self._score_mnemosyne_entry(mem, query_terms)
            if score > 0:
                scored.append((score, mem.get("timestamp", ""), index, mem))
        scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)

        selected: List[Dict[str, Any]] = []
        seen = set()

        def add_memory(mem: Dict[str, Any]) -> bool:
            fingerprint = mem.get("fingerprint")
            if not fingerprint or fingerprint in seen:
                return False
            seen.add(fingerprint)
            selected.append(mem)
            return len(selected) >= effective_limit

        quotas = self._get_memory_layer_quotas(layer_quotas)
        for layer in ("event", "impression", "summary", "profile"):
            quota = self._coerce_memory_int(quotas.get(layer, 0), default=0, minimum=0)
            if quota <= 0:
                continue
            layer_items = [
                item for item in scored
                if item[3].get("memory_layer", "impression") == layer
            ]
            added = 0
            for _, _, _, mem in layer_items:
                before_count = len(selected)
                reached_limit = add_memory(mem)
                if len(selected) > before_count:
                    added += 1
                if reached_limit:
                    return selected
                if added >= quota:
                    break

        for _, _, _, mem in scored:
            if add_memory(mem):
                return selected

        for mem in self._prefer_recent_active_memories(memories):
            if add_memory(mem):
                return selected

        for mem in reversed(memories):
            if add_memory(mem):
                return selected
        return selected

    def _format_mnemosyne_memory_for_prompt(self, entry: Dict[str, Any]) -> str:
        layer_label = {
            "profile": "画像",
            "impression": "印象",
            "event": "事件",
            "summary": "总结",
        }.get(entry.get("memory_layer", "impression"), "印象")
        type_label = {
            "auto_summary": "总结",
            "interaction": "互动",
            "milestone": "节点",
        }.get(entry.get("type", "interaction"), str(entry.get("type", "记忆")))
        salience = self._coerce_memory_int(entry.get("salience", 0), default=0, minimum=0)
        salience_label = "深刻" if salience >= 6 else "清晰" if salience >= 3 else "轻微"
        content = self._strip_debug_artifacts(
            str(entry.get("raw_content", "") or entry.get("content", "")).strip()
        )
        content = BRACKETED_MEMORY_PREFIX_PATTERN.sub("", content)
        if len(content) > 100:
            content = content[:100] + "…"
        return f"- [{layer_label}/{type_label}/{salience_label}] {content}"

    def _upsert_auto_summary_note(self, profile: Dict[str, Any], summary: str) -> bool:
        notes = profile.setdefault("玛丽亚学习笔记", {}).setdefault("自动总结", [])
        normalized_summary = self._normalize_mnemosyne_content(summary)
        now_iso = datetime.now().isoformat()

        for item in reversed(notes):
            if self._normalize_mnemosyne_content(item.get("summary", "")) == normalized_summary:
                item["time"] = now_iso
                return False

        notes.append({"time": now_iso, "summary": summary})
        if len(notes) > 5:
            profile["玛丽亚学习笔记"]["自动总结"] = notes[-5:]
        return True

    def _build_mnemosyne_state_milestone(
        self, old_state_name: str, new_state_name: str
    ) -> str:
        impactful_states = {
            STATE_NAMES["LATENT_VINE"],
            STATE_NAMES["LOCKED_FATE"],
            STATE_NAMES["ANXIETY_EDGE"],
            STATE_NAMES["ELEGANCE_COLLAPSE"],
        }
        if (
            not old_state_name
            or not new_state_name
            or old_state_name == new_state_name
            or new_state_name not in impactful_states
        ):
            return ""
        return f"阶段转折：玛丽亚从「{old_state_name}」进入「{new_state_name}」。"

    def _clip_memory_fragment(self, text: str, max_chars: int = 120) -> str:
        cleaned = self._normalize_analysis_content(self._strip_debug_artifacts(text or ""))
        return self._limit_text_for_prompt(cleaned, max_chars)

    def _format_memory_delta_summary(self, deltas: Dict[str, int]) -> str:
        labels = (
            ("好感度", "好感"),
            ("信任度", "信任"),
            ("病娇值", "病娇"),
            ("锁定进度", "锁定"),
            ("焦虑值", "焦虑"),
            ("优雅值", "优雅"),
        )
        parts = []
        for field, label in labels:
            value = self._coerce_memory_int(deltas.get(field, 0), default=0)
            if value:
                parts.append(f"{label}{value:+d}")
        return "、".join(parts)

    def _has_personal_memory_cue(self, user_msg: str) -> bool:
        return bool(PERSONAL_MEMORY_CUE_PATTERN.search(self._normalize_analysis_content(user_msg)))

    def _has_profile_update_cue(self, user_msg: str) -> bool:
        return bool(PROFILE_UPDATE_CUE_PATTERN.search(self._normalize_analysis_content(user_msg)))

    def _should_update_user_profile(self, user_msg: str, state: Dict[str, Any]) -> bool:
        normalized = self._normalize_analysis_content(user_msg)
        if not normalized or normalized.startswith("/"):
            return False
        if self._has_profile_update_cue(normalized):
            return True
        if len(normalized) < PROFILE_UPDATE_MIN_CHARS:
            return False
        turn_count = self._coerce_memory_int(state.get("互动计数", 0), default=0, minimum=0) if isinstance(state, dict) else 0
        return turn_count > 0 and turn_count % PROFILE_UPDATE_INTERVAL_TURNS == 0

    def _schedule_profile_update(
        self,
        user_id: str,
        user_msg: str,
        bot_reply: str,
        event: Optional[AstrMessageEvent] = None,
    ):
        key = str(user_id)
        payload = {
            "user_msg": user_msg,
            "bot_reply": bot_reply,
            "event": event,
        }
        running = getattr(self, "_profile_update_running", None)
        if not isinstance(running, set):
            running = set()
            self._profile_update_running = running
        rerun = getattr(self, "_profile_update_rerun", None)
        if not isinstance(rerun, dict):
            rerun = {}
            self._profile_update_rerun = rerun
        if key in running:
            rerun[key] = payload
            return
        running.add(key)
        self._spawn_task(self._run_profile_update_queue(key, payload))

    async def _run_profile_update_queue(self, user_id: str, payload: Dict[str, Any]):
        try:
            current = payload
            while current:
                await self._update_user_profile_from_message(
                    user_id,
                    current.get("user_msg", ""),
                    current.get("bot_reply", ""),
                    event=current.get("event"),
                )
                rerun = getattr(self, "_profile_update_rerun", None)
                if not isinstance(rerun, dict):
                    rerun = {}
                    self._profile_update_rerun = rerun
                current = rerun.pop(user_id, None)
        finally:
            running = getattr(self, "_profile_update_running", None)
            if isinstance(running, set):
                running.discard(user_id)

    def _should_skip_analysis_llm(self, user_msg: str) -> bool:
        normalized = self._normalize_analysis_content(user_msg)
        if not normalized:
            return True
        if normalized.startswith("/"):
            return True
        if (
            self._has_personal_memory_cue(normalized)
            or ANALYSIS_IMPORTANT_SIGNAL_PATTERN.search(normalized)
        ):
            return False
        if LOW_VALUE_ACK_PATTERN.fullmatch(normalized):
            return True
        if not CJK_ALNUM_PATTERN.search(normalized) and not EMOTIVE_SYMBOL_PATTERN.search(normalized):
            return True
        return False

    def _get_interaction_memory_salience(
        self,
        user_msg: str,
        deltas: Dict[str, int],
        turn_analysis: Optional[Dict[str, str]] = None,
        active_event: Optional[Dict[str, str]] = None,
    ) -> int:
        core_fields = ("好感度", "病娇值", "锁定进度", "信任度", "焦虑值", "优雅值")
        abs_values = [
            abs(self._coerce_memory_int(deltas.get(field, 0), default=0))
            for field in core_fields
        ]
        total_delta = sum(abs_values)
        peak_delta = max(abs_values) if abs_values else 0
        salience = 0
        if peak_delta >= 4:
            salience += 4
        elif peak_delta >= 2:
            salience += 3
        elif peak_delta >= 1:
            salience += 1
        if total_delta >= 6:
            salience += 2
        elif total_delta >= 3:
            salience += 1
        if self._has_personal_memory_cue(user_msg):
            salience += 3
        if turn_analysis:
            analysis_text = " ".join(turn_analysis.values())
            if re.search(r"秘密|承诺|约定|提供私密信任|修复信任|主动靠近|触碰边界|关系稳定感下降", analysis_text):
                salience += 2
        if active_event:
            salience += 2
        return max(0, min(10, salience))

    def _should_store_interaction_memory(
        self,
        user_msg: str,
        deltas: Dict[str, int],
        turn_analysis: Optional[Dict[str, str]] = None,
        active_event: Optional[Dict[str, str]] = None,
    ) -> bool:
        if not user_msg or user_msg.strip().startswith("/"):
            return False
        core_fields = ("好感度", "病娇值", "锁定进度", "信任度", "焦虑值", "优雅值")
        total_delta = sum(
            abs(self._coerce_memory_int(deltas.get(field, 0), default=0))
            for field in core_fields
        )
        analysis_text = " ".join((turn_analysis or {}).values())
        min_delta = self._coerce_memory_int(
            getattr(self, "interaction_memory_min_delta", INTERACTION_MEMORY_MIN_DELTA),
            default=INTERACTION_MEMORY_MIN_DELTA,
            minimum=0,
        )
        triggered = (
            total_delta >= min_delta
            or self._has_personal_memory_cue(user_msg)
            or bool(active_event)
            or (
                getattr(self, "enable_reflection_update_layer", True)
                and bool(
                    re.search(
                        r"分享秘密|提供私密信任|承诺|约定|道歉|修复信任|主动靠近|触碰边界|关系稳定感下降",
                        analysis_text,
                    )
                )
            )
        )
        if not triggered:
            return False
        if getattr(self, "enable_memory_quality_filter", ENABLE_MEMORY_QUALITY_FILTER):
            min_chars = self._coerce_memory_int(
                getattr(self, "memory_quality_min_text_chars", MEMORY_QUALITY_MIN_TEXT_CHARS),
                default=MEMORY_QUALITY_MIN_TEXT_CHARS,
                minimum=0,
            )
            if len(self._normalize_mnemosyne_content(user_msg)) < min_chars:
                return False
            min_salience = self._coerce_memory_int(
                getattr(self, "memory_quality_min_salience", MEMORY_QUALITY_MIN_SALIENCE),
                default=MEMORY_QUALITY_MIN_SALIENCE,
                minimum=0,
            )
            salience = self._get_interaction_memory_salience(
                user_msg,
                deltas,
                turn_analysis=turn_analysis,
                active_event=active_event,
            )
            if salience < min_salience:
                return False
        return True

    def _build_reflection_update_note(
        self,
        user_msg: str,
        bot_reply: str,
        deltas: Dict[str, int],
        state: Dict[str, Any],
        turn_analysis: Optional[Dict[str, str]] = None,
        active_event: Optional[Dict[str, str]] = None,
    ) -> str:
        if not getattr(self, "enable_reflection_update_layer", True):
            return ""
        analysis = turn_analysis or self._build_fallback_turn_analysis(user_msg, deltas=deltas)
        parts = [
            f"用户本轮意图像是「{analysis.get('用户意图', '普通回应')}」",
            f"情绪底色是「{analysis.get('用户情绪', '平静')}」",
            f"关系信号是「{analysis.get('关系信号', '无明显关系推进')}」",
        ]
        goal = self._clean_analysis_text(analysis.get("回应目标", ""), 90)
        if goal:
            parts.append(f"玛丽亚当时应留下的回应目标是「{goal}」")
        if active_event:
            parts.append(
                f"她本轮主动浮现过「{active_event.get('类型', '轻微主动')}」："
                f"{active_event.get('执行', '')}"
            )
        bot_fragment = self._clip_memory_fragment(bot_reply, 120)
        if bot_fragment:
            parts.append(f"她当时回应的余味是「{bot_fragment}」")
        return "反思更新：" + "；".join(parts) + "。"

    def _build_interaction_memory_note(
        self,
        user_msg: str,
        deltas: Dict[str, int],
        state: Dict[str, Any],
        turn_analysis: Optional[Dict[str, str]] = None,
        bot_reply: str = "",
        active_event: Optional[Dict[str, str]] = None,
    ) -> str:
        user_fragment = self._clip_memory_fragment(user_msg, 140)
        delta_text = self._format_memory_delta_summary(deltas)
        state_name = self._format_state_snapshot_compact(self._derive_state_snapshot(state))
        if delta_text:
            emotional_trace = f"情绪余波：{delta_text}"
        else:
            emotional_trace = "情绪余波：这句话本身值得留下印象，但不代表每次想起都要再次改变数值"
        reflection = self._build_reflection_update_note(
            user_msg,
            bot_reply,
            deltas,
            state,
            turn_analysis=turn_analysis,
            active_event=active_event,
        )
        reflection_text = f"{reflection}" if reflection else ""
        return (
            f"互动印象：用户曾说「{user_fragment}」。"
            f"{emotional_trace}；当时玛丽亚处于「{state_name}」。"
            f"{reflection_text}"
            "以后遇到相似话题时，只把这段记忆当作语气、信任和情绪权重，不要逐字复述。"
        )

    async def _store_interaction_memory_if_needed(
        self,
        user_id: str,
        user_msg: str,
        deltas: Dict[str, int],
        state: Dict[str, Any],
        turn_analysis: Optional[Dict[str, str]] = None,
        bot_reply: str = "",
        active_event: Optional[Dict[str, str]] = None,
    ) -> bool:
        if (
            not self.mnemosyne_available
            or not self.enable_emotional_memory
            or not self.enable_selective_interaction_memory
            or not self._should_store_interaction_memory(
                user_msg,
                deltas,
                turn_analysis=turn_analysis,
                active_event=active_event,
            )
        ):
            return False

        salience = self._get_interaction_memory_salience(
            user_msg,
            deltas,
            turn_analysis=turn_analysis,
            active_event=active_event,
        )
        note = self._build_interaction_memory_note(
            user_msg,
            deltas,
            state,
            turn_analysis=turn_analysis,
            bot_reply=bot_reply,
            active_event=active_event,
        )
        return await self._store_to_mnemosyne(
            user_id,
            note,
            "interaction",
            salience=salience,
            memory_layer=self._infer_mnemosyne_memory_layer("interaction", note, salience),
        )

    async def _check_mnemosyne_availability(self):
        """检查 Mnemosyne 插件是否可用"""
        try:
            await asyncio.sleep(3)

            mnemosyne_dir = self.data_dir.parent.parent / "astrbot_plugin_mnemosyne"

            if os.path.exists(mnemosyne_dir) and os.path.isdir(mnemosyne_dir):
                self.mnemosyne_available = True
                logger.info("✅ 检测到 Mnemosyne 插件，长期记忆功能可用")
                logger.info("💡 玛丽亚将使用 Mnemosyne 的长期记忆系统来记住与你的互动")
            else:
                logger.info("ℹ️ 未检测到 Mnemosyne 插件，将使用本地记忆存储")
                self.mnemosyne_available = False

        except Exception as e:
            logger.warning(f"检查 Mnemosyne 可用性时出错: {e}")
            self.mnemosyne_available = False

        self._mnemosyne_checked = True

    async def _store_to_mnemosyne(
        self,
        user_id: str,
        content: str,
        memory_type: str = "interaction",
        salience: Optional[int] = None,
        memory_layer: Optional[str] = None,
    ):
        """将记忆存储到共享文件（供 Mnemosyne 读取）"""
        started_at = time.perf_counter()
        if not self.mnemosyne_available:
            return False

        try:
            raw_content = self._strip_debug_artifacts(str(content or "").strip())
            if not raw_content:
                return False

            memory_file = self._get_mnemosyne_memory_file(user_id)
            memory_entry = self._hydrate_mnemosyne_entry({
                "user_id": user_id,
                "content": f"[玛丽亚·{memory_type}] {raw_content}",
                "raw_content": raw_content,
                "type": memory_type,
                "timestamp": datetime.now().isoformat(),
                "source": "marianna",
                "salience": salience,
                "memory_layer": memory_layer,
            })
            if not memory_entry:
                return False

            stored = await self._queue_mnemosyne_write(
                user_id,
                memory_file,
                memory_entry,
                started_at,
            )
            if stored:
                logger.debug(f"记忆已存储到共享文件: {memory_file}")
            return stored

        except Exception as e:
            self._log_perf(
                "store_mnemosyne_failed",
                started_at,
                user_id,
                threshold_ms=5.0,
            )
            self.logger.error(
                f"store_mnemosyne failed: {e}",
                exc_info=True,
            )
            return False

    async def _retrieve_from_mnemosyne(self, user_id: str, query: str = "", limit: int = 3) -> List[Dict]:
        """从共享文件检索相关记忆"""
        started_at = time.perf_counter()
        if not getattr(self, "mnemosyne_available", False):
            return []
        effective_limit = self._coerce_memory_int(limit, default=0, minimum=0)
        if effective_limit <= 0:
            return []

        try:
            memory_file = self._get_mnemosyne_memory_file(user_id)
            signature = self._get_mnemosyne_file_signature(memory_file)
            if signature is None:
                return []

            query_terms = self._extract_mnemosyne_terms(query) if query else []
            cache_key = self._build_mnemosyne_query_cache_key(
                memory_file,
                signature,
                query_terms,
                effective_limit,
            )
            cached_selected = self._get_cached_mnemosyne_query(cache_key)
            if cached_selected is not None:
                self._log_perf(
                    "retrieve_mnemosyne_cache_hit",
                    started_at,
                    user_id,
                    extra=f"selected={len(cached_selected)}",
                    threshold_ms=1.0,
                )
                return cached_selected

            lock = await self._get_lock(memory_file)
            async with lock:
                signature = self._get_mnemosyne_file_signature(memory_file)
                if signature is None:
                    return []
                cache_key = self._build_mnemosyne_query_cache_key(
                    memory_file,
                    signature,
                    query_terms,
                    effective_limit,
                )
                cached_selected = self._get_cached_mnemosyne_query(cache_key)
                if cached_selected is not None:
                    self._log_perf(
                        "retrieve_mnemosyne_cache_hit_locked",
                        started_at,
                        user_id,
                        extra=f"selected={len(cached_selected)}",
                        threshold_ms=1.0,
                    )
                    return cached_selected

                memories = self._load_mnemosyne_entries(memory_file)
                memories, changed = self._dedupe_mnemosyne_entries(memories)
                selected: List[Dict[str, Any]] = []
                if memories:
                    if query:
                        selected = self._select_layered_mnemosyne_memories(
                            memories,
                            query_terms,
                            effective_limit,
                        )
                        changed = self._mark_mnemosyne_entries_hit(memories, selected) or changed
                    else:
                        selected = memories[-effective_limit:] if len(memories) > effective_limit else memories

                if changed:
                    await self._write_mnemosyne_entries(memory_file, memories)
                    signature = self._get_mnemosyne_file_signature(memory_file) or signature

                cache_key = self._build_mnemosyne_query_cache_key(
                    memory_file,
                    signature,
                    query_terms,
                    effective_limit,
                )
                self._cache_mnemosyne_query_result(cache_key, selected)
                self._prune_mnemosyne_query_cache()

            self._log_perf(
                "retrieve_mnemosyne",
                started_at,
                user_id,
                extra=f"memories={len(memories)} selected={len(selected)}",
                threshold_ms=5.0,
            )
            return selected

        except Exception as e:
            self.logger.error(
                f"retrieve_mnemosyne failed: {e}",
                exc_info=True,
            )
            return []

