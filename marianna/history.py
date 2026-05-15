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
        max_chars = self._coerce_history_limit(
            getattr(self, "history_max_entry_chars", HISTORY_MAX_ENTRY_CHARS),
            default=HISTORY_MAX_ENTRY_CHARS,
            minimum=1,
        )
        text = self._limit_text_for_prompt(text, max_chars)
        return text.rstrip()

    def _normalize_history_role(self, role: Any) -> str:
        normalized = str(role or "user").strip().lower()
        if normalized in {"assistant", "bot", "marianna"}:
            return "assistant"
        if normalized in {"memory", "summary", "profile"}:
            return "memory"
        if normalized in {"system", "tool"}:
            return normalized
        return "user"

    def _history_json_dumps(self, value: Any) -> str:
        if hasattr(self, "_analysis_json_dumps"):
            return self._analysis_json_dumps(value)
        return json.dumps(value, ensure_ascii=False, allow_nan=False)

    def _limit_text_for_prompt(self, content: Any, max_chars: Optional[int]) -> str:
        """按字符限制裁剪提示词片段，0/None 表示不主动裁剪。"""
        text = content if isinstance(content, str) else str(content or "")
        if max_chars and max_chars > 0 and len(text) > max_chars:
            if max_chars <= 1:
                return text[:max_chars]
            return text[: max_chars - 1] + "…"
        return text

    def _trim_repetitive_reply_template(self, text: str) -> str:
        if not getattr(self, "enable_reply_template_trim", ENABLE_REPLY_TEMPLATE_TRIM):
            return text
        max_actions = self._coerce_history_limit(
            getattr(self, "reply_template_trim_max_actions", REPLY_TEMPLATE_TRIM_MAX_ACTIONS),
            default=REPLY_TEMPLATE_TRIM_MAX_ACTIONS,
            minimum=0,
        )
        if max_actions >= 0:
            total_kept = 0
            category_seen: Dict[str, int] = {}

            def action_category(content: str) -> str:
                if re.search(r"垂眼|垂下眼|轻笑|轻轻笑|脸红|眨眼|抿唇|低头|沉默|停顿", content):
                    return "emotion"
                if re.search(r"靠近|牵|抱|触碰|握住|贴近|抚", content):
                    return "proximity"
                if re.search(r"裙摆|端茶|行礼|手套|衣领|发丝|茶杯", content):
                    return "etiquette"
                return "other"

            def keep_action(match: re.Match) -> str:
                nonlocal total_kept
                content = (match.group(1) or "").strip()
                if not content:
                    return ""
                category = action_category(content)
                category_seen[category] = category_seen.get(category, 0) + 1
                if category_seen[category] <= 1 and total_kept < max_actions:
                    total_kept += 1
                    return match.group(0)
                return ""

            text = re.sub(r"[（(]([^（）()]{1,48})[）)]", keep_action, text)

        lines = text.splitlines()
        question_tail_count = 0
        trimmed_lines: List[str] = []
        for line in reversed(lines):
            stripped = line.strip()
            if stripped.endswith(("？", "?")):
                question_tail_count += 1
                if question_tail_count > 1 and len(stripped) <= 36:
                    continue
            trimmed_lines.append(line)
        text = "\n".join(reversed(trimmed_lines))
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _collapse_repeated_group_boundary_phrases(self, text: str) -> str:
        """Collapse duplicated public-boundary replacements into one natural clause."""
        private_phrase = "\u8fd9\u4ef6\u4e8b\u4e0d\u9002\u5408\u5728\u8fd9\u91cc\u7ec6\u8bf4"
        boundary_phrase = "\u6211\u4f1a\u4fdd\u6301\u5206\u5bf8"
        for phrase in (private_phrase, boundary_phrase):
            escaped = re.escape(phrase)
            text = re.sub(
                rf"(?:{escaped})(?:[\u7684\u4e86\u554a\u5462\u5427\u3002\uff01\uff1f!?,，、\s]+{escaped})+",
                phrase,
                text,
            )
            text = re.sub(rf"(?:{escaped}[。！？!?；;，,、\s]*){{2,}}", phrase, text)

        parts = re.split(r"([。！？!?；;，,、\n]+)", text)
        normalized_parts: List[str] = []
        last_meaningful = ""
        for index, part in enumerate(parts):
            if not part:
                continue
            if index % 2 == 1:
                if normalized_parts:
                    normalized_parts.append(part)
                continue
            compact = re.sub(r"[\s。！？!?；;，,、]+", "", part)
            if compact and compact == last_meaningful:
                continue
            normalized_parts.append(part)
            if compact:
                last_meaningful = compact
        normalized = "".join(normalized_parts)
        normalized = re.sub(r"([。！？!?；;，,、])(?:\s*\1)+", r"\1", normalized)
        normalized = re.sub(r"([。！？!?；;，,、])\s+([。！？!?；;，,、])", r"\1", normalized)
        return normalized

    def _normalize_group_boundary_reply(self, text: str) -> str:
        """Clean up awkward fragments after replacing private wording in group chats."""
        if not isinstance(text, str) or not text:
            return ""
        private_phrase = "\u8fd9\u4ef6\u4e8b\u4e0d\u9002\u5408\u5728\u8fd9\u91cc\u7ec6\u8bf4"
        boundary_phrase = "\u6211\u4f1a\u4fdd\u6301\u5206\u5bf8"
        text = text.replace(
            f"\u8fd9\u662f{private_phrase}\u7684{private_phrase}",
            private_phrase,
        )
        text = text.replace(
            f"\u4f60\u662f{boundary_phrase}{boundary_phrase}",
            boundary_phrase,
        )
        for phrase in (private_phrase, boundary_phrase):
            escaped = re.escape(phrase)
            text = re.sub(rf"(?:{escaped})(?:\u7684)?(?:{escaped})+", phrase, text)
            text = re.sub(rf"(?:{escaped})(?:[，,、\s]+{escaped})+", phrase, text)
        text = re.sub(rf"\u8fd9\u662f({re.escape(private_phrase)})", r"\1", text)
        text = re.sub(rf"\u4f60\u662f({re.escape(boundary_phrase)})", r"\1", text)
        text = re.sub(r"[，,、]{2,}", "\uff0c", text)
        text = self._collapse_repeated_group_boundary_phrases(text)
        text = re.sub(r"\s{2,}", " ", text)
        return text.strip()

    def _drop_intimate_boundary_sentences(self, text: str) -> str:
        intimate_pattern = (
            r"\u547d\u5b9a|\u552f\u4e00|\u53ea\u5c5e\u4e8e|\u604b\u4eba|\u4eb2\u543b|"
            r"\u62b1\u7d27|\u5403\u918b|\u4e0d\u8bb8\u79bb\u5f00|\u6c38\u8fdc\u966a\u7740\u6211"
        )
        parts = re.split(r"(?<=[\u3002\uff01\uff1f!?])", text or "")
        kept = [part for part in parts if part and not re.search(intimate_pattern, part)]
        cleaned = "".join(kept).strip()
        return cleaned

    def _self_check_reply(
        self,
        reply: str,
        state: Dict[str, Any],
        user_msg: str = "",
    ) -> str:
        """回复后本地自检：不额外调用 LLM，只做泄露、边界和长度保护。"""
        text = self._strip_debug_artifacts(reply or "")
        if not getattr(self, "enable_reply_self_check", ENABLE_REPLY_SELF_CHECK):
            return text

        text = re.sub(
            r"^【(?:灵魂层|人格层|记忆层|情绪识别层|对话层|行为层|主动事件层|回复长度策略|人格一致性守卫)[^】]*】[ \t]*\n?",
            "",
            text,
            flags=re.MULTILINE,
        )
        text = re.sub(
            r"^(?:系统提示规则块|数值调制层|关系状态机层)[:：].*$",
            "",
            text,
            flags=re.MULTILINE,
        )
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        text = self._trim_repetitive_reply_template(text)

        if state.get("\u5f53\u524d\u662f\u5426\u7fa4\u804a\u4f5c\u7528\u57df"):
            private_leak = bool(
                re.search(r"\u53ea\u544a\u8bc9\u4f60|\u79c1\u804a|\u79d8\u5bc6|\u547d\u5b9a|\u552f\u4e00|\u53ea\u5c5e\u4e8e|\u604b\u4eba|\u4eb2\u543b|\u62b1\u7d27|\u4e0d\u8bb8\u79bb\u5f00|\u6c38\u8fdc\u966a\u7740\u6211", text)
            )
            if private_leak:
                text = re.sub(r"\u53ea\u544a\u8bc9\u4f60|\u79c1\u804a|\u79d8\u5bc6", "\u8fd9\u4ef6\u4e8b\u4e0d\u9002\u5408\u5728\u8fd9\u91cc\u7ec6\u8bf4", text)
                text = re.sub(r"\u547d\u5b9a|\u552f\u4e00|\u53ea\u5c5e\u4e8e|\u604b\u4eba|\u4eb2\u543b|\u62b1\u7d27|\u4e0d\u8bb8\u79bb\u5f00|\u6c38\u8fdc\u966a\u7740\u6211", "\u6211\u4f1a\u4fdd\u6301\u5206\u5bf8", text)
                text = self._normalize_group_boundary_reply(text)

        if state.get("关系状态机") == RELATIONSHIP_STATE_NAMES["BOUNDARY_AFTER_FATE"]:
            intimate_leak = bool(
                re.search(r"命定|唯一|只属于|恋人|亲吻|抱紧|吃醋|不许离开|永远陪着我", text)
            )
            if intimate_leak:
                text = self._drop_intimate_boundary_sentences(text)
                text = (
                    self._limit_text_for_prompt(text, 260).rstrip("…")
                    + "\n\n（我垂下眼，语气重新归于礼貌。）请别误会，我会保持应有的分寸。"
                )

        max_lines = self._coerce_history_limit(
            getattr(self, "reply_self_check_max_lines", REPLY_SELF_CHECK_MAX_LINES),
            default=REPLY_SELF_CHECK_MAX_LINES,
            minimum=0,
        )
        if max_lines > 0:
            lines = text.splitlines()
            if len(lines) > max_lines:
                text = "\n".join(lines[:max_lines]).rstrip() + "\n…"

        max_chars = self._coerce_history_limit(
            getattr(self, "reply_self_check_max_chars", REPLY_SELF_CHECK_MAX_CHARS),
            default=REPLY_SELF_CHECK_MAX_CHARS,
            minimum=0,
        )
        if max_chars > 0 and len(text) > max_chars:
            text = self._limit_text_for_prompt(text, max_chars)

        return text.strip()

    def _get_history_jsonl_file(self, user_id: str) -> Path:
        return self._get_history_dir() / f"{self._safe_user_file_stem(user_id)}.jsonl"

    def _get_legacy_history_json_file(self, user_id: str) -> Path:
        return self._get_history_dir() / f"{self._safe_user_file_stem(user_id)}.json"

    def _get_history_dir(self) -> Path:
        history_dir = getattr(self, "conv_history_dir", None)
        if not isinstance(history_dir, (str, os.PathLike)) or not str(history_dir).strip():
            data_dir = getattr(self, "data_dir", Path(__file__).resolve().parents[1] / "data")
            if not isinstance(data_dir, (str, os.PathLike)) or not str(data_dir).strip():
                data_dir = Path(__file__).resolve().parents[1] / "data"
            history_dir = Path(data_dir) / "conversation_history"
            self.conv_history_dir = history_dir
        history_dir = Path(history_dir)
        history_dir.mkdir(parents=True, exist_ok=True)
        return history_dir

    def _get_history_dict_cache(self, attr_name: str) -> Dict[Any, Any]:
        cache = getattr(self, attr_name, None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, attr_name, cache)
        return cache

    def _get_summary_dirty_users(self) -> Set[str]:
        users = getattr(self, "_summary_dirty_users", None)
        if not isinstance(users, set):
            users = set()
            self._summary_dirty_users = users
        return users

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
        entries: Any,
    ) -> List[Dict[str, str]]:
        if not isinstance(entries, list):
            return []
        return [dict(entry) for entry in entries if isinstance(entry, dict)]

    def _coerce_history_limit(self, limit: Any, default: int = 0, *, minimum: int = 0) -> int:
        try:
            value = int(limit if limit is not None else default)
        except (TypeError, ValueError, OverflowError):
            value = int(default or 0)
        return max(minimum, value)

    def _build_recent_history_cache_key(
        self,
        user_id: str,
        limit: int,
    ) -> Tuple[Any, ...]:
        history_file = self._get_history_jsonl_file(user_id)
        legacy_file = self._get_legacy_history_json_file(user_id)
        return (
            str(user_id),
            self._coerce_history_limit(limit),
            self._get_file_signature(history_file),
            self._get_file_signature(legacy_file),
        )

    def _invalidate_recent_history_cache(self, user_id: Optional[str] = None):
        recent_cache = self._get_history_dict_cache("_recent_history_cache")
        style_cache = self._get_history_dict_cache("_style_fingerprint_cache")
        if user_id is None:
            recent_cache.clear()
            style_cache.clear()
            return
        user_key = str(user_id)
        for key in list(recent_cache.keys()):
            if isinstance(key, tuple) and key and key[0] == user_key:
                del recent_cache[key]
        for key in list(style_cache.keys()):
            if isinstance(key, tuple) and key and key[0] == user_key:
                del style_cache[key]

    def _mark_summary_dirty(self, user_id: str):
        if user_id and self._coerce_history_limit(getattr(self, "auto_summary_interval", 0)) > 0:
            self._get_summary_dirty_users().add(str(user_id))

    def _normalize_history_entry(self, item: Any) -> Optional[Dict[str, str]]:
        if not isinstance(item, dict):
            return None
        role = self._normalize_history_role(item.get("role", "user"))
        content = self._sanitize_history_content(role, item.get("content", ""))
        if not content:
            return None
        return {
            "role": role,
            "content": content,
            "time": str(item.get("time", "") or ""),
        }

    def _read_history_jsonl_tail(self, history_file: Path, limit: int) -> List[Dict[str, str]]:
        effective_limit = self._coerce_history_limit(limit)
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
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if entry:
                    entries.append(entry)
            return entries
        except Exception as e:
            self.logger.error(f"读取 JSONL 对话历史失败: {e}", exc_info=True)
            return []

    async def _append_history_jsonl(self, history_file: Path, entry: Dict[str, str]):
        normalized_entry = self._normalize_history_entry(entry)
        if not normalized_entry:
            return
        line = self._history_json_dumps(normalized_entry) + "\n"
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

        payload = "\n".join(self._history_json_dumps(item) for item in entries)
        await self._write_text_atomic(history_file, payload + "\n")

    async def _compact_history_jsonl_if_due(
        self,
        user_id: str,
        history_file: Path,
        retention_limit: int,
    ):
        count = self._coerce_history_limit(
            self._get_history_dict_cache("_history_append_counts").get(user_id, 0),
            default=0,
        ) + 1
        self._get_history_dict_cache("_history_append_counts")[user_id] = count
        if count % HISTORY_COMPACT_INTERVAL != 0:
            return

        entries = await asyncio.to_thread(
            self._read_history_jsonl_tail,
            history_file,
            retention_limit,
        )
        payload = "\n".join(self._history_json_dumps(item) for item in entries)
        if payload:
            payload += "\n"
        await self._write_text_atomic(history_file, payload)
        self._invalidate_recent_history_cache(user_id)

    def _is_recent_duplicate_history_entry(
        self,
        existing: Dict[str, str],
        role: str,
        content: str,
        now: datetime,
    ) -> bool:
        if existing.get("role") != role or existing.get("content") != content:
            return False
        try:
            previous_time = datetime.fromisoformat(str(existing.get("time", "") or ""))
        except (TypeError, ValueError):
            return False
        window = self._coerce_history_limit(
            getattr(
                self,
                "history_duplicate_window_seconds",
                HISTORY_DUPLICATE_WINDOW_SECONDS,
            )
        )
        if window <= 0:
            return False
        return abs((now - previous_time).total_seconds()) <= window

    async def _add_to_history(self, user_id: str, role: str, content: str):
        """添加对话到历史记录（异步）"""
        try:
            history_file = self._get_history_jsonl_file(user_id)
            lock = await self._get_lock(history_file)
            async with lock:
                normalized_role = self._normalize_history_role(role)
                sanitized_content = self._sanitize_history_content(normalized_role, content)
                if not sanitized_content:
                    return
                now = datetime.now()
                entry = {
                    "role": normalized_role,
                    "content": sanitized_content,
                    "time": now.isoformat()
                }
                retention_limit = getattr(
                    self,
                    "history_retention_limit",
                    CONVERSATION_HISTORY_RETENTION_LIMIT,
                )
                retention_limit = self._coerce_history_limit(
                    retention_limit,
                    CONVERSATION_HISTORY_RETENTION_LIMIT,
                    minimum=1,
                )
                await self._seed_history_jsonl_from_legacy(
                    user_id,
                    history_file,
                    retention_limit,
                )
                latest_entries = self._read_history_jsonl_tail(history_file, 1)
                if latest_entries and self._is_recent_duplicate_history_entry(
                    latest_entries[-1],
                    normalized_role,
                    sanitized_content,
                    now,
                ):
                    return
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
            effective_limit = self._coerce_history_limit(limit)
            if effective_limit <= 0:
                return []
            cache_key = self._build_recent_history_cache_key(user_id, effective_limit)
            recent_cache = self._get_history_dict_cache("_recent_history_cache")
            cached_history = recent_cache.get(cache_key)
            if cached_history is not None:
                if not isinstance(cached_history, list):
                    recent_cache.pop(cache_key, None)
                else:
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
            recent_cache[cache_key] = self._copy_history_entries(result)
            self._trim_dict_cache(
                recent_cache,
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
        effective_limit = self._coerce_history_limit(limit)
        if effective_limit <= 0:
            return []
        cache_key = self._build_recent_history_cache_key(user_id, effective_limit)
        recent_cache = self._get_history_dict_cache("_recent_history_cache")
        cached_history = recent_cache.get(cache_key)
        if cached_history is not None:
            if not isinstance(cached_history, list):
                recent_cache.pop(cache_key, None)
            else:
                return self._copy_history_entries(cached_history)
        return await asyncio.to_thread(
            self._get_recent_history,
            user_id,
            effective_limit,
        )

