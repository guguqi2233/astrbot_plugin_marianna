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

class MariannaProfileMixin:
    def _clean_profile_item(self, value: Any, max_chars: int = 30) -> str:
        text = self._normalize_analysis_content(str(value or ""))
        text = text.strip(" ：:，,。.!！?？“”\"'`")
        text = re.sub(r"^(?:是|叫|为|喜欢|讨厌|不喜欢|害怕|吃|听|看)+", "", text)
        text = re.sub(r"(?:了|啦|呀|啊|吧|呢|哦|喔)$", "", text)
        text = text.strip(" ：:，,。.!！?？“”\"'`")
        if not text or len(text) > max_chars:
            return ""
        if text in {"你", "你呀", "你啊", "玛丽亚", "这个", "那个", "这样", "聊天"}:
            return ""
        if not CJK_ALNUM_PATTERN.search(text):
            return ""
        return text

    def _split_profile_items(self, value: Any, max_items: int = 4) -> List[str]:
        text = str(value or "")
        text = re.split(r"但是|不过|然后|因为|所以|如果", text, maxsplit=1)[0]
        items: List[str] = []
        for part in PROFILE_ITEM_SPLIT_PATTERN.split(text):
            cleaned = self._clean_profile_item(part)
            if cleaned and cleaned not in items:
                items.append(cleaned)
            if len(items) >= max_items:
                break
        return items

    def _add_profile_list_items(
        self,
        target: List[Any],
        items: List[Any],
        *,
        limit: int = 20,
    ) -> bool:
        changed = False
        existing = [str(item).strip() for item in target if str(item).strip()]
        for item in items:
            cleaned = self._clean_profile_item(item)
            if not cleaned or cleaned in existing:
                continue
            existing.append(cleaned)
            changed = True
            if len(existing) >= limit:
                break
        if changed:
            target[:] = existing[-limit:]
        return changed

    def _classify_local_profile_like(self, item: str) -> Optional[str]:
        if PROFILE_COLOR_PATTERN.search(item):
            return "颜色"
        if PROFILE_FOOD_HINT_PATTERN.search(item):
            return "食物"
        if PROFILE_MUSIC_HINT_PATTERN.search(item):
            return "音乐"
        if PROFILE_BOOK_HINT_PATTERN.search(item):
            return "书籍"
        return None

    def _extract_local_profile_updates(self, user_msg: str) -> Dict[str, Any]:
        text = self._normalize_analysis_content(user_msg)
        updates: Dict[str, Any] = {
            "基本信息": {},
            "兴趣爱好": {"音乐": [], "书籍": [], "食物": [], "颜色": []},
            "喜欢的话题": [],
            "反感的话题": [],
        }

        basic_patterns = (
            ("称呼", LOCAL_PROFILE_NAME_PATTERN),
            ("生日", LOCAL_PROFILE_BIRTHDAY_PATTERN),
            ("职业", LOCAL_PROFILE_JOB_PATTERN),
            ("所在地", LOCAL_PROFILE_LOCATION_PATTERN),
        )
        for field, pattern in basic_patterns:
            match = pattern.search(text)
            if not match:
                continue
            value = self._clean_profile_item(match.group(1), max_chars=30)
            if value:
                updates["基本信息"][field] = value

        for match in LOCAL_PROFILE_LIKE_PATTERN.finditer(text):
            for item in self._split_profile_items(match.group(1)):
                category = self._classify_local_profile_like(item)
                if category:
                    updates["兴趣爱好"][category].append(item)
                else:
                    updates["喜欢的话题"].append(item)

        for match in LOCAL_PROFILE_DISLIKE_PATTERN.finditer(text):
            updates["反感的话题"].extend(self._split_profile_items(match.group(1)))

        return updates

    def _merge_profile_update_data(
        self,
        profile: Dict[str, Any],
        data: Dict[str, Any],
    ) -> bool:
        self._ensure_profile_shape(profile)
        changed = False

        basic_info = data.get("基本信息", {})
        if isinstance(basic_info, dict):
            for key, value in basic_info.items():
                cleaned = self._clean_profile_item(value)
                if cleaned and profile["基本信息"].get(key) != cleaned:
                    profile["基本信息"][key] = cleaned
                    changed = True

        hobbies = data.get("兴趣爱好", {})
        if isinstance(hobbies, dict):
            for category, items in hobbies.items():
                if category not in profile["兴趣爱好"]:
                    continue
                if isinstance(items, str):
                    items = self._split_profile_items(items)
                if isinstance(items, list):
                    changed = self._add_profile_list_items(
                        profile["兴趣爱好"][category],
                        items,
                    ) or changed

        traits = data.get("性格特征", {})
        if isinstance(traits, dict):
            emotions = traits.get("主要情绪", [])
            if isinstance(emotions, str):
                emotions = self._split_profile_items(emotions)
            if isinstance(emotions, list):
                changed = self._add_profile_list_items(
                    profile["性格特征"]["主要情绪"],
                    emotions,
                ) or changed
            style = self._clean_profile_item(traits.get("沟通风格", ""), max_chars=40)
            if style and profile["性格特征"].get("沟通风格") != style:
                profile["性格特征"]["沟通风格"] = style
                changed = True

        liked_topics = data.get("喜欢的话题", [])
        if isinstance(liked_topics, str):
            liked_topics = self._split_profile_items(liked_topics)
        if isinstance(liked_topics, list):
            changed = self._add_profile_list_items(
                profile["玛丽亚学习笔记"]["喜欢的话题"],
                liked_topics,
            ) or changed

        disliked_topics = data.get("反感的话题", [])
        if isinstance(disliked_topics, str):
            disliked_topics = self._split_profile_items(disliked_topics)
        if isinstance(disliked_topics, list):
            changed = self._add_profile_list_items(
                profile["玛丽亚学习笔记"]["反感的话题"],
                disliked_topics,
            ) or changed

        return changed

    async def _update_user_profile_from_message(
        self,
        user_id: str,
        user_msg: str,
        bot_reply: str,
        event: Optional[AstrMessageEvent] = None
    ):
        if not self.enable_profile:
            return
        profile = self._get_profile(user_id)
        local_updates = self._extract_local_profile_updates(user_msg)
        if self._merge_profile_update_data(profile, local_updates):
            profile["互动记录"]["总互动次数"] = int(
                profile["互动记录"].get("总互动次数", 0) or 0
            ) + 1
            self._schedule_profile_save(user_id, profile)
            self.logger.debug(f"[profile] user={user_id} local_profile_update=1")
            return

        clean_bot_reply = self._strip_debug_artifacts(bot_reply)
        json_schema = (
            '{\n'
            '  "基本信息": {"称呼": "", "生日": "", "职业": "", "所在地": ""},\n'
            '  "兴趣爱好": {"音乐": [], "书籍": [], "食物": [], "颜色": []},\n'
            '  "性格特征": {"主要情绪": [], "沟通风格": ""},\n'
            '  "喜欢的话题": [],\n'
            '  "反感的话题": []\n'
            '}'
        )
        prompt = (
            f"分析对话，提取用户画像信息。只返回 JSON，格式如下：\n"
            f"{json_schema}\n"
            f"对话：\n用户：{user_msg}\n玛丽亚：{clean_bot_reply}\n"
        )
        try:
            resp = await self._call_analysis_llm(
                purpose="用户画像分析",
                prompt=prompt,
                system_prompt="你是一个用户画像分析助手，只输出 JSON，不要有任何额外说明。",
                temperature=0.3,
                max_tokens=400,
                event=event,
            )
            if not resp:
                return
            data = self._parse_json_response(resp.completion_text or "")
            if not data:
                self.logger.warning(f"用户画像分析未返回有效 JSON: {resp.completion_text!r}")
                return
            if self._merge_profile_update_data(profile, data):
                profile["互动记录"]["总互动次数"] = int(
                    profile["互动记录"].get("总互动次数", 0) or 0
                ) + 1
                self._schedule_profile_save(user_id, profile)
        except Exception as e:
            logger.error(f"用户画像更新失败: {e}")

