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

class MariannaStateStoreMixin:
    def _touch_state_interaction(self, state: Dict[str, Any]):
        state["最后互动时间"] = datetime.now().isoformat()
        state["互动计数"] = int(state.get("互动计数", 0) or 0) + 1

    def _get_state(self, user_id: str, count_interaction: bool = True) -> Dict[str, Any]:
        """获取用户状态"""
        try:
            if user_id not in self.user_states:
                # 使用深拷贝避免多用户共享同一列表/字典对象
                state = copy.deepcopy(DEFAULT_STATE)
                if count_interaction:
                    state["最后互动时间"] = datetime.now().isoformat()
                self.user_states[user_id] = state
                # 异步合并保存（不等待）
                self._schedule_state_save(user_id, state)
                self.logger.info(f"为用户 {user_id} 创建新状态")

            state = self.user_states[user_id]
            state.setdefault("调试模式", self.default_debug_mode)
            self._get_delta_residuals(state)
            self._normalize_state_constraints(state, user_id=user_id)
            if count_interaction:
                self._touch_state_interaction(state)
            return state
        except Exception as e:
            self.logger.error(f"获取用户状态失败: {e}", exc_info=True)
            return copy.deepcopy(DEFAULT_STATE)

    async def _save_state(self, user_id: str, state: Dict[str, Any]):
        """保存用户状态（异步）"""
        try:
            self._state_dirty_users.clear()
            await self._save_json_async(self.user_states_file, self.user_states)
        except Exception as e:
            self.logger.error(f"保存用户状态失败: {e}", exc_info=True)

    def _schedule_state_save(self, user_id: str, state: Dict[str, Any]):
        """合并短时间内的状态写入，避免每轮对话都整文件落盘。"""
        version = int(self._state_versions.get(user_id, 0) or 0) + 1
        self._state_versions[user_id] = version
        self.user_states[user_id] = state
        self._state_dirty_users.add(user_id)
        if self._state_save_task is None or self._state_save_task.done():
            self._state_save_task = self._spawn_task(self._debounced_save_states())

    async def _debounced_save_states(self):
        try:
            await asyncio.sleep(SAVE_DEBOUNCE_SECONDS)
            await self._save_state("", {})
        finally:
            self._state_save_task = None
            if self._state_dirty_users:
                self._state_save_task = self._spawn_task(self._debounced_save_states())

    async def _save_state_versioned(
        self,
        user_id: str,
        state: Dict[str, Any],
        version: int,
    ):
        if version != int(self._state_versions.get(user_id, 0) or 0):
            return
        await self._save_state(user_id, state)

    def _get_profile(self, user_id: str) -> Dict[str, Any]:
        """获取用户画像"""
        try:
            if user_id not in self.user_profiles:
                self.user_profiles[user_id] = {
                    "基本信息": {},
                    "兴趣爱好": {"音乐": [], "书籍": [], "食物": [], "颜色": []},
                    "性格特征": {"主要情绪": [], "沟通风格": ""},
                    "互动记录": {"首次互动": datetime.now().isoformat(), "总互动次数": 0},
                    "玛丽亚学习笔记": {"喜欢的话题": [], "反感的话题": [], "自动总结": []}
                }
                self.logger.info(f"为用户 {user_id} 创建新画像")
            profile = self.user_profiles[user_id]
            self._ensure_profile_shape(profile)
            return profile
        except Exception as e:
            self.logger.error(f"获取用户画像失败: {e}", exc_info=True)
            return {}

    def _ensure_profile_shape(self, profile: Dict[str, Any]):
        basic = profile.setdefault("基本信息", {})
        if not isinstance(basic, dict):
            profile["基本信息"] = {}

        hobbies = profile.setdefault("兴趣爱好", {})
        if not isinstance(hobbies, dict):
            hobbies = {}
            profile["兴趣爱好"] = hobbies
        for key in ("音乐", "书籍", "食物", "颜色"):
            if not isinstance(hobbies.get(key), list):
                hobbies[key] = []

        traits = profile.setdefault("性格特征", {})
        if not isinstance(traits, dict):
            traits = {}
            profile["性格特征"] = traits
        if not isinstance(traits.get("主要情绪"), list):
            traits["主要情绪"] = []
        traits.setdefault("沟通风格", "")

        stats = profile.setdefault("互动记录", {})
        if not isinstance(stats, dict):
            stats = {}
            profile["互动记录"] = stats
        stats.setdefault("首次互动", datetime.now().isoformat())
        stats.setdefault("总互动次数", 0)

        notes = profile.setdefault("玛丽亚学习笔记", {})
        if not isinstance(notes, dict):
            notes = {}
            profile["玛丽亚学习笔记"] = notes
        for key in ("喜欢的话题", "反感的话题", "自动总结"):
            if not isinstance(notes.get(key), list):
                notes[key] = []

    def _get_destined_one_info(self) -> Dict[str, str]:
        raw = self.global_state.get("destined_one", {})
        if not isinstance(raw, dict):
            return {}
        user_id = str(raw.get("user_id", "") or "").strip()
        if not user_id:
            return {}
        user_name = str(raw.get("user_name", "") or "").strip()
        return {
            "user_id": user_id,
            "user_name": user_name,
            "locked_at": str(raw.get("locked_at", "") or "").strip(),
        }

    def _is_destined_user(self, user_id: Optional[str]) -> bool:
        if not user_id:
            return False
        info = self._get_destined_one_info()
        return bool(info) and str(user_id) == info.get("user_id")

    def _format_destined_one_label(self) -> str:
        info = self._get_destined_one_info()
        if not info:
            return str(self.lock_threshold)
        user_id = info.get("user_id", "")
        user_name = info.get("user_name", "")
        return f"{user_id}({user_name})" if user_name else user_id

    def _format_lock_progress_display(
        self,
        state: Dict[str, Any],
        deltas: Optional[Dict[str, int]] = None,
    ) -> str:
        current = self._format_state_value_with_delta(state, deltas or {}, "锁定进度")
        return f"{current}/{self._format_destined_one_label()}"

    async def _save_global_state(self):
        try:
            await self._save_json_async(self.global_state_file, self.global_state)
        except Exception as e:
            self.logger.error(f"保存全局命定状态失败: {e}", exc_info=True)

    async def _set_destined_one(self, user_id: str, user_name: str):
        self.global_state["destined_one"] = {
            "user_id": str(user_id),
            "user_name": str(user_name or "").strip(),
            "locked_at": datetime.now().isoformat(),
        }
        self._dynamic_prompt_cache.clear()
        await self._save_global_state()

    async def _clear_destined_one(self):
        if "destined_one" in self.global_state:
            del self.global_state["destined_one"]
            self._dynamic_prompt_cache.clear()
            await self._save_global_state()

    async def _save_profile(self, user_id: str, profile: Dict[str, Any]):
        """保存用户画像（异步）"""
        self._schedule_profile_save(user_id, profile)

    def _schedule_profile_file_save(self, user_id: Optional[str] = None):
        if user_id:
            self._profile_dirty_users.add(user_id)
        if self._profile_save_task is None or self._profile_save_task.done():
            self._profile_save_task = self._spawn_task(self._debounced_save_profiles())

    def _schedule_profile_save(self, user_id: str, profile: Dict[str, Any]):
        """合并短时间内的用户画像写入。"""
        self.user_profiles[user_id] = profile
        self._schedule_profile_file_save(user_id)

    async def _debounced_save_profiles(self):
        try:
            await asyncio.sleep(SAVE_DEBOUNCE_SECONDS)
            self._profile_dirty_users.clear()
            await self._save_json_async(self.user_profiles_file, self.user_profiles)
        except Exception as e:
            self.logger.error(f"保存用户画像失败: {e}", exc_info=True)
        finally:
            self._profile_save_task = None
            if self._profile_dirty_users:
                self._profile_save_task = self._spawn_task(self._debounced_save_profiles())

