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

class MariannaHistoryMixin:
    def _strip_debug_artifacts(self, text: str) -> str:
        """移除误混入回复正文的调试尾注。"""
        if not isinstance(text, str):
            return ""
        cleaned = DEBUG_FOOTER_PATTERN.sub("", text)
        return cleaned.rstrip()

    def _sanitize_history_content(self, role: str, content: str) -> str:
        text = content if isinstance(content, str) else ""
        if role == "assistant":
            text = self._strip_debug_artifacts(text)
        return text.rstrip()

    def _limit_text_for_prompt(self, content: Any, max_chars: Optional[int]) -> str:
        """按字符限制裁剪提示词片段，0/None 表示不主动裁剪。"""
        text = content if isinstance(content, str) else str(content or "")
        if max_chars and max_chars > 0 and len(text) > max_chars:
            if max_chars <= 1:
                return text[:max_chars]
            return text[: max_chars - 1] + "…"
        return text

    def _get_history_jsonl_file(self, user_id: str) -> Path:
        return self.conv_history_dir / f"{self._safe_user_file_stem(user_id)}.jsonl"

    def _get_legacy_history_json_file(self, user_id: str) -> Path:
        return self.conv_history_dir / f"{self._safe_user_file_stem(user_id)}.json"

    def _get_file_signature(self, path: Path) -> Optional[Tuple[int, int]]:
        if not path.exists():
            return None
        try:
            stat = path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def _copy_history_entries(
        self,
        entries: List[Dict[str, str]],
    ) -> List[Dict[str, str]]:
        return [dict(entry) for entry in entries]

    def _build_recent_history_cache_key(
        self,
        user_id: str,
        limit: int,
    ) -> Tuple[Any, ...]:
        history_file = self._get_history_jsonl_file(user_id)
        legacy_file = self._get_legacy_history_json_file(user_id)
        return (
            str(user_id),
            int(limit or 0),
            self._get_file_signature(history_file),
            self._get_file_signature(legacy_file),
        )

    def _invalidate_recent_history_cache(self, user_id: Optional[str] = None):
        if user_id is None:
            self._recent_history_cache.clear()
            return
        user_key = str(user_id)
        for key in list(self._recent_history_cache.keys()):
            if isinstance(key, tuple) and key and key[0] == user_key:
                del self._recent_history_cache[key]

    def _mark_summary_dirty(self, user_id: str):
        if user_id and int(getattr(self, "auto_summary_interval", 0) or 0) > 0:
            self._summary_dirty_users.add(str(user_id))

    def _normalize_history_entry(self, item: Any) -> Optional[Dict[str, str]]:
        if not isinstance(item, dict):
            return None
        role = str(item.get("role", "user") or "user")
        content = self._sanitize_history_content(role, item.get("content", ""))
        if not content:
            return None
        return {
            "role": role,
            "content": content,
            "time": str(item.get("time", "") or ""),
        }

    def _read_history_jsonl_tail(self, history_file: Path, limit: int) -> List[Dict[str, str]]:
        effective_limit = max(0, int(limit or 0))
        if effective_limit <= 0 or not history_file.exists():
            return []

        try:
            with history_file.open("rb") as f:
                f.seek(0, os.SEEK_END)
                pos = f.tell()
                chunks: List[bytes] = []
                line_breaks = 0
                while pos > 0 and line_breaks <= effective_limit:
                    read_size = min(HISTORY_TAIL_BLOCK_SIZE, pos)
                    pos -= read_size
                    f.seek(pos)
                    chunk = f.read(read_size)
                    chunks.append(chunk)
                    line_breaks += chunk.count(b"\n")

            text = b"".join(reversed(chunks)).decode("utf-8", errors="ignore")
            entries: List[Dict[str, str]] = []
            for line in text.splitlines()[-effective_limit:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = self._normalize_history_entry(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if entry:
                    entries.append(entry)
            return entries
        except Exception as e:
            self.logger.error(f"读取 JSONL 对话历史失败: {e}", exc_info=True)
            return []

    async def _append_history_jsonl(self, history_file: Path, entry: Dict[str, str]):
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        if AIOFILES_AVAILABLE:
            async with aiofiles.open(history_file, "a", encoding="utf-8") as f:
                await f.write(line)
        else:
            with open(history_file, "a", encoding="utf-8") as f:
                f.write(line)

    async def _seed_history_jsonl_from_legacy(
        self,
        user_id: str,
        history_file: Path,
        retention_limit: int,
    ):
        if history_file.exists():
            return

        legacy_file = self._get_legacy_history_json_file(user_id)
        if not legacy_file.exists():
            return

        legacy_history = self._load_json(legacy_file, [])
        if not isinstance(legacy_history, list):
            return

        entries: List[Dict[str, str]] = []
        for item in legacy_history[-retention_limit:]:
            entry = self._normalize_history_entry(item)
            if entry:
                entries.append(entry)

        if not entries:
            return

        payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in entries)
        await self._write_text_atomic(history_file, payload + "\n")

    async def _compact_history_jsonl_if_due(
        self,
        user_id: str,
        history_file: Path,
        retention_limit: int,
    ):
        count = int(self._history_append_counts.get(user_id, 0) or 0) + 1
        self._history_append_counts[user_id] = count
        if count % HISTORY_COMPACT_INTERVAL != 0:
            return

        entries = await asyncio.to_thread(
            self._read_history_jsonl_tail,
            history_file,
            retention_limit,
        )
        payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in entries)
        if payload:
            payload += "\n"
        await self._write_text_atomic(history_file, payload)
        self._invalidate_recent_history_cache(user_id)

    async def _add_to_history(self, user_id: str, role: str, content: str):
        """添加对话到历史记录（异步）"""
        try:
            history_file = self._get_history_jsonl_file(user_id)
            lock = await self._get_lock(history_file)
            async with lock:
                sanitized_content = self._sanitize_history_content(role, content)
                if not sanitized_content:
                    return
                entry = {
                    "role": role,
                    "content": sanitized_content,
                    "time": datetime.now().isoformat()
                }
                retention_limit = getattr(
                    self,
                    "history_retention_limit",
                    CONVERSATION_HISTORY_RETENTION_LIMIT,
                )
                retention_limit = max(
                    1,
                    int(retention_limit or CONVERSATION_HISTORY_RETENTION_LIMIT),
                )
                await self._seed_history_jsonl_from_legacy(
                    user_id,
                    history_file,
                    retention_limit,
                )
                await self._append_history_jsonl(history_file, entry)
                await self._compact_history_jsonl_if_due(
                    user_id,
                    history_file,
                    retention_limit,
                )
                self._invalidate_recent_history_cache(user_id)
                self._mark_summary_dirty(user_id)
            self.logger.debug(f"已保存用户 {user_id} 的对话历史")
        except Exception as e:
            self.logger.error(f"保存对话历史失败: {e}", exc_info=True)

    def _get_recent_history(self, user_id: str, limit: int = 20) -> List[Dict[str, str]]:
        """获取最近的对话历史"""
        try:
            effective_limit = max(0, int(limit or 0))
            if effective_limit <= 0:
                return []
            cache_key = self._build_recent_history_cache_key(user_id, effective_limit)
            cached_history = self._recent_history_cache.get(cache_key)
            if cached_history is not None:
                return self._copy_history_entries(cached_history)

            history_file = self._get_history_jsonl_file(user_id)
            legacy_file = self._get_legacy_history_json_file(user_id)
            cleaned_history: List[Dict[str, str]] = self._read_history_jsonl_tail(
                history_file,
                effective_limit,
            )

            remaining = effective_limit - len(cleaned_history)
            if remaining > 0 and legacy_file.exists():
                legacy_history = self._load_json(legacy_file, [])
                if isinstance(legacy_history, list):
                    legacy_entries: List[Dict[str, str]] = []
                    for item in legacy_history[-remaining:]:
                        entry = self._normalize_history_entry(item)
                        if entry:
                            legacy_entries.append(entry)
                    cleaned_history = legacy_entries + cleaned_history

            deduped_history: List[Dict[str, str]] = []
            seen = set()
            for item in cleaned_history:
                key = (
                    item.get("role", ""),
                    item.get("content", ""),
                    item.get("time", ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                deduped_history.append(item)

            result = deduped_history[-effective_limit:]
            self._recent_history_cache[cache_key] = self._copy_history_entries(result)
            self._trim_dict_cache(
                self._recent_history_cache,
                RECENT_HISTORY_CACHE_MAX_ENTRIES,
            )
            return result
        except Exception as e:
            self.logger.error(f"获取对话历史失败: {e}", exc_info=True)
            return []

    async def _get_recent_history_async(
        self,
        user_id: str,
        limit: int = 20,
    ) -> List[Dict[str, str]]:
        """在线程池中读取最近历史，避免在事件循环里做磁盘 tail 扫描。"""
        effective_limit = max(0, int(limit or 0))
        if effective_limit <= 0:
            return []
        cache_key = self._build_recent_history_cache_key(user_id, effective_limit)
        cached_history = self._recent_history_cache.get(cache_key)
        if cached_history is not None:
            return self._copy_history_entries(cached_history)
        return await asyncio.to_thread(
            self._get_recent_history,
            user_id,
            effective_limit,
        )

