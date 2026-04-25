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
        hit_count = max(0, int(entry.get("hit_count", 0) or 0))
        salience = max(0, int(entry.get("salience", 0) or 0))
        layer = entry.get("memory_layer", "impression")
        decay_days = max(1, int(getattr(self, "memory_decay_days", MEMORY_DECAY_DAYS) or MEMORY_DECAY_DAYS))
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
            int(
                getattr(
                    self,
                    "memory_hard_cleanup_days",
                    MEMORY_HARD_CLEANUP_DAYS,
                ) or MEMORY_HARD_CLEANUP_DAYS
            ),
        )
        hit_count = max(0, int(entry.get("hit_count", 0) or 0))
        salience = max(0, int(entry.get("salience", 0) or 0))

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
            int(primary.get("salience", 0) or 0),
            int(duplicate.get("salience", 0) or 0),
        )
        merged["hit_count"] = max(
            int(primary.get("hit_count", 0) or 0),
            int(duplicate.get("hit_count", 0) or 0),
        )
        merged["reinforcement_count"] = max(
            int(primary.get("reinforcement_count", 0) or 0),
            int(duplicate.get("reinforcement_count", 0) or 0),
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
        old_reinforcement = int(entry.get("reinforcement_count", 0) or 0)
        new_reinforcement = old_reinforcement + 1
        if int(entry.get("reinforcement_count", 0) or 0) != new_reinforcement:
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

        incoming_salience = int(incoming_entry.get("salience", 0) or 0)
        current_salience = int(entry.get("salience", 0) or 0)
        target_salience = max(current_salience, incoming_salience)
        if new_reinforcement in {2, 4, 7} and target_salience < 10:
            target_salience += 1
        target_salience = max(0, min(10, target_salience))
        if current_salience != target_salience:
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

            current_salience = int(entry.get("salience", 0) or 0)
            incoming_salience = int(new_entry.get("salience", 0) or 0)
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

            next_hits = max(0, int(entry.get("hit_count", 0) or 0)) + 1
            if int(entry.get("hit_count", 0) or 0) != next_hits:
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
                and int(entry.get("salience", 0) or 0) < 10
            ):
                entry["salience"] = int(entry.get("salience", 0) or 0) + 1
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
        try:
            salience = int(entry.get("salience", default_salience) or default_salience)
        except (TypeError, ValueError):
            salience = default_salience
        hydrated["salience"] = max(0, min(10, salience))
        memory_layer = str(entry.get("memory_layer", "") or "").strip()
        if memory_layer not in {"profile", "impression", "event", "summary"}:
            memory_layer = self._infer_mnemosyne_memory_layer(
                hydrated["type"],
                raw_content,
                hydrated["salience"],
            )
        hydrated["memory_layer"] = memory_layer
        try:
            hit_count = int(entry.get("hit_count", 0) or 0)
        except (TypeError, ValueError):
            hit_count = 0
        hydrated["hit_count"] = max(0, hit_count)
        try:
            reinforcement_count = int(entry.get("reinforcement_count", 0) or 0)
        except (TypeError, ValueError):
            reinforcement_count = 0
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
        return [dict(entry) for entry in entries]

    def _get_mnemosyne_file_signature(self, memory_file: Path) -> Optional[Tuple[int, int]]:
        if not memory_file.exists():
            self._mnemosyne_entries_cache.pop(str(memory_file), None)
            return None
        try:
            stat = memory_file.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            self._mnemosyne_entries_cache.pop(str(memory_file), None)
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
        cached = self._mnemosyne_entries_cache.get(cache_key)
        if (
            isinstance(cached, dict)
            and cached.get("signature") == signature
            and isinstance(cached.get("entries"), list)
        ):
            return self._copy_mnemosyne_entries(cached["entries"])

        entries = self._read_mnemosyne_entries_uncached(memory_file)
        self._mnemosyne_entries_cache[cache_key] = {
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
        self._mnemosyne_entries_cache[str(memory_file)] = {
            "signature": signature,
            "entries": self._copy_mnemosyne_entries(entries),
        }

    def _build_mnemosyne_query_cache_key(
        self,
        memory_file: Path,
        signature: Tuple[int, int],
        query_terms: List[str],
        limit: int,
    ) -> str:
        payload = {
            "file": str(memory_file),
            "signature": signature,
            "terms": query_terms,
            "limit": int(limit or 0),
            "quotas": self._get_memory_layer_quotas(),
            "decay_days": getattr(self, "memory_decay_days", MEMORY_DECAY_DAYS),
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(serialized.encode("utf-8")).hexdigest()

    def _prune_mnemosyne_query_cache(self):
        cutoff = time.monotonic() - MNEMOSYNE_QUERY_CACHE_TTL_SECONDS
        for key, value in list(self._mnemosyne_query_cache.items()):
            if not isinstance(value, dict):
                del self._mnemosyne_query_cache[key]
                continue
            try:
                if float(value.get("_created_at", 0)) < cutoff:
                    del self._mnemosyne_query_cache[key]
            except (TypeError, ValueError):
                del self._mnemosyne_query_cache[key]
        self._trim_dict_cache(
            self._mnemosyne_query_cache,
            MNEMOSYNE_QUERY_CACHE_MAX_ENTRIES,
        )

    def _get_cached_mnemosyne_query(
        self,
        cache_key: str,
    ) -> Optional[List[Dict[str, Any]]]:
        cached = self._mnemosyne_query_cache.get(cache_key)
        if not isinstance(cached, dict):
            return None
        try:
            age = time.monotonic() - float(cached.get("_created_at", 0))
        except (TypeError, ValueError):
            self._mnemosyne_query_cache.pop(cache_key, None)
            return None
        if age > MNEMOSYNE_QUERY_CACHE_TTL_SECONDS:
            self._mnemosyne_query_cache.pop(cache_key, None)
            return None
        result = cached.get("result")
        if not isinstance(result, list):
            self._mnemosyne_query_cache.pop(cache_key, None)
            return None
        return self._copy_mnemosyne_entries(result)

    def _cache_mnemosyne_query_result(
        self,
        cache_key: str,
        selected: List[Dict[str, Any]],
    ):
        self._mnemosyne_query_cache[cache_key] = {
            "_created_at": time.monotonic(),
            "result": self._copy_mnemosyne_entries(selected),
        }
        self._trim_dict_cache(
            self._mnemosyne_query_cache,
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
            json.dumps(entry, ensure_ascii=False) for entry in entries
        )
        if payload:
            payload += "\n"
        await self._write_text_atomic(memory_file, payload)
        self._refresh_mnemosyne_entries_cache(memory_file, entries)
        self._mnemosyne_query_cache.clear()

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
        self._mnemosyne_flush_tasks[cache_key] = task
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
        if self._mnemosyne_flush_tasks.get(cache_key) is done_task:
            self._mnemosyne_flush_tasks.pop(cache_key, None)
        if self._mnemosyne_write_buffers.get(cache_key):
            current_task = self._mnemosyne_flush_tasks.get(cache_key)
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
        self._mnemosyne_write_buffers.setdefault(cache_key, []).append(memory_entry)
        self._mnemosyne_write_waiters.setdefault(cache_key, []).append(waiter)

        task = self._mnemosyne_flush_tasks.get(cache_key)
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
        entries = self._mnemosyne_write_buffers.pop(cache_key, [])
        waiters = self._mnemosyne_write_waiters.pop(cache_key, [])
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
            logger.error(f"批量存储记忆到 Mnemosyne 失败: {e}")
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(success)

    async def _drain_mnemosyne_flush_tasks(self):
        """等待所有已排队的 Mnemosyne 写入完成，用于卸载前收尾。"""
        while True:
            for cache_key, task in list(self._mnemosyne_flush_tasks.items()):
                if task.done():
                    self._mnemosyne_flush_tasks.pop(cache_key, None)

            active_tasks = [
                task for task in self._mnemosyne_flush_tasks.values()
                if not task.done()
            ]
            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)
                continue

            pending_keys = list(self._mnemosyne_write_buffers.keys())
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
        score += min(6, int(entry.get("salience", 0) or 0))
        score += min(3, int(entry.get("hit_count", 0) or 0))
        if self._get_mnemosyne_entry_age_days(entry) <= 7:
            score += 1
        score -= self._get_mnemosyne_decay_penalty(entry)
        return score

    def _get_memory_layer_quotas(self) -> Dict[str, int]:
        return {
            "event": getattr(self, "memory_prompt_event_limit", MEMORY_PROMPT_EVENT_LIMIT),
            "impression": getattr(self, "memory_prompt_impression_limit", MEMORY_PROMPT_IMPRESSION_LIMIT),
            "summary": getattr(self, "memory_prompt_summary_limit", MEMORY_PROMPT_SUMMARY_LIMIT),
            "profile": getattr(self, "memory_prompt_profile_limit", MEMORY_PROMPT_PROFILE_LIMIT),
        }

    def _select_layered_mnemosyne_memories(
        self,
        memories: List[Dict[str, Any]],
        query_terms: List[str],
        limit: int,
    ) -> List[Dict[str, Any]]:
        effective_limit = max(0, int(limit or 0))
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

        quotas = self._get_memory_layer_quotas()
        for layer in ("event", "impression", "summary", "profile"):
            quota = max(0, int(quotas.get(layer, 0) or 0))
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
        salience = int(entry.get("salience", 0) or 0)
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
            value = int(deltas.get(field, 0) or 0)
            if value:
                parts.append(f"{label}{value:+d}")
        return "、".join(parts)

    def _has_personal_memory_cue(self, user_msg: str) -> bool:
        return bool(PERSONAL_MEMORY_CUE_PATTERN.search(self._normalize_analysis_content(user_msg)))

    def _should_update_user_profile(self, user_msg: str, state: Dict[str, Any]) -> bool:
        normalized = self._normalize_analysis_content(user_msg)
        if not normalized or normalized.startswith("/"):
            return False
        if self._has_personal_memory_cue(normalized):
            return True
        if len(normalized) < PROFILE_UPDATE_MIN_CHARS:
            return False
        turn_count = int(state.get("互动计数", 0) or 0) if isinstance(state, dict) else 0
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
        if key in self._profile_update_running:
            self._profile_update_rerun[key] = payload
            return
        self._profile_update_running.add(key)
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
                current = self._profile_update_rerun.pop(user_id, None)
        finally:
            self._profile_update_running.discard(user_id)

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
        abs_values = [abs(int(deltas.get(field, 0) or 0)) for field in core_fields]
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
        total_delta = sum(abs(int(deltas.get(field, 0) or 0)) for field in core_fields)
        analysis_text = " ".join((turn_analysis or {}).values())
        return (
            total_delta >= getattr(self, "interaction_memory_min_delta", INTERACTION_MEMORY_MIN_DELTA)
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
            logger.error(f"存储记忆到 Mnemosyne 失败: {e}")
            return False

    async def _retrieve_from_mnemosyne(self, user_id: str, query: str = "", limit: int = 3) -> List[Dict]:
        """从共享文件检索相关记忆"""
        started_at = time.perf_counter()
        if not self.mnemosyne_available:
            return []
        effective_limit = max(0, int(limit or 0))
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
            logger.error(f"从 Mnemosyne 检索记忆失败: {e}")
            return []

