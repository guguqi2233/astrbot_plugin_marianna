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
    def _coerce_store_int(
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

    def _touch_state_interaction(self, state: Dict[str, Any]):
        state["最后互动时间"] = datetime.now().isoformat()
        state["互动计数"] = self._coerce_store_int(state.get("互动计数", 0), default=0, minimum=0) + 1

    def _get_state(self, user_id: str, count_interaction: bool = True) -> Dict[str, Any]:
        """获取用户状态"""
        try:
            user_states = self._get_user_states_store()
            if user_id not in user_states:
                # 使用深拷贝避免多用户共享同一列表/字典对象
                state = copy.deepcopy(DEFAULT_STATE)
                if count_interaction:
                    state["最后互动时间"] = datetime.now().isoformat()
                user_states[user_id] = state
                # 异步合并保存（不等待）
                self._schedule_state_save(user_id, state)
                self.logger.info(f"为用户 {user_id} 创建新状态")

            state = user_states[user_id]
            if not isinstance(state, dict):
                state = copy.deepcopy(DEFAULT_STATE)
                user_states[user_id] = state
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
        state_versions = self._get_store_dict_cache("_state_versions")
        state_dirty_users = self._get_store_set_cache("_state_dirty_users")
        dirty_versions = {
            str(dirty_user_id): self._coerce_store_int(
                state_versions.get(dirty_user_id, 0),
                default=0,
                minimum=0,
            )
            for dirty_user_id in list(state_dirty_users)
        }
        try:
            payload = self._build_persistable_user_states()
            await self._save_json_async(
                self._get_store_file_path("user_states_file", "user_states.json"),
                payload,
            )
            for dirty_user_id, version in dirty_versions.items():
                if self._coerce_store_int(
                    state_versions.get(dirty_user_id, 0),
                    default=0,
                    minimum=0,
                ) == version:
                    state_dirty_users.discard(dirty_user_id)
        except Exception as e:
            self.logger.error(f"保存用户状态失败: {e}", exc_info=True)

    def _get_store_dict_cache(self, attr_name: str) -> Dict[Any, Any]:
        cache = getattr(self, attr_name, None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, attr_name, cache)
        return cache

    def _get_store_set_cache(self, attr_name: str) -> Set[Any]:
        cache = getattr(self, attr_name, None)
        if not isinstance(cache, set):
            cache = set()
            setattr(self, attr_name, cache)
        return cache

    def _get_global_state(self) -> Dict[str, Any]:
        state = getattr(self, "global_state", None)
        if not isinstance(state, dict):
            state = {}
            self.global_state = state
        return state

    def _get_user_states_store(self) -> Dict[str, Dict[str, Any]]:
        states = getattr(self, "user_states", None)
        if not isinstance(states, dict):
            states = {}
            self.user_states = states
        return states

    def _get_user_profiles_store(self) -> Dict[str, Dict[str, Any]]:
        profiles = getattr(self, "user_profiles", None)
        if not isinstance(profiles, dict):
            profiles = {}
            self.user_profiles = profiles
        return profiles

    def _get_store_prompt_cache(self, attr_name: str) -> Dict[Any, Any]:
        cache = getattr(self, attr_name, None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, attr_name, cache)
        return cache

    def _is_store_task_pending(self, task: Any) -> bool:
        return hasattr(task, "done") and not task.done()

    def _make_json_safe(self, value: Any, *, _depth: int = 0) -> Any:
        if _depth > 8:
            return str(value)
        if isinstance(value, float) and (
            value != value or value in (float("inf"), float("-inf"))
        ):
            return 0.0
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {
                str(key): self._make_json_safe(item, _depth=_depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._make_json_safe(item, _depth=_depth + 1) for item in value]
        if isinstance(value, set):
            return [
                self._make_json_safe(item, _depth=_depth + 1)
                for item in sorted(value, key=lambda item: str(item))
            ]
        return str(value)

    def _get_store_file_path(self, attr_name: str, fallback_name: str) -> Path:
        configured = getattr(self, attr_name, None)
        if isinstance(configured, (str, os.PathLike)) and str(configured).strip():
            path = Path(configured)
        else:
            data_dir = getattr(
                self,
                "data_dir",
                Path(__file__).resolve().parents[1] / "data",
            )
            if not isinstance(data_dir, (str, os.PathLike)) or not str(data_dir).strip():
                data_dir = Path(__file__).resolve().parents[1] / "data"
            path = Path(data_dir) / fallback_name
        setattr(self, attr_name, path)
        return path

    def _build_persistable_user_states(self) -> Dict[str, Dict[str, Any]]:
        transient_keys = {
            "_scene_memory_policy",
            "鏈疆鍦烘櫙璁板繂绛栫暐",
        }
        return {
            str(user_id): {
                key: self._make_json_safe(value)
                for key, value in state.items()
                if key not in transient_keys
            }
            for user_id, state in self._get_user_states_store().items()
            if isinstance(state, dict)
        }

    def _build_persistable_user_profiles(self) -> Dict[str, Dict[str, Any]]:
        return {
            str(user_id): self._make_json_safe(profile)
            for user_id, profile in self._get_user_profiles_store().items()
            if isinstance(profile, dict)
        }

    def _build_persistable_global_state(self) -> Dict[str, Any]:
        payload = self._make_json_safe(self._get_global_state())
        return payload if isinstance(payload, dict) else {}

    def _schedule_state_save(self, user_id: str, state: Dict[str, Any]):
        """合并短时间内的状态写入，避免每轮对话都整文件落盘。"""
        state_versions = self._get_store_dict_cache("_state_versions")
        state_dirty_users = self._get_store_set_cache("_state_dirty_users")
        version = self._coerce_store_int(state_versions.get(user_id, 0), default=0, minimum=0) + 1
        state_versions[user_id] = version
        self._get_user_states_store()[user_id] = state
        state_dirty_users.add(user_id)
        state_save_task = getattr(self, "_state_save_task", None)
        if not self._is_store_task_pending(state_save_task):
            self._state_save_task = self._spawn_task(self._debounced_save_states())

    async def _debounced_save_states(self):
        try:
            await asyncio.sleep(SAVE_DEBOUNCE_SECONDS)
            await self._save_state("", {})
        finally:
            self._state_save_task = None
            if self._get_store_set_cache("_state_dirty_users"):
                self._state_save_task = self._spawn_task(self._debounced_save_states())

    async def _save_state_versioned(
        self,
        user_id: str,
        state: Dict[str, Any],
        version: int,
    ):
        if version != self._coerce_store_int(self._get_store_dict_cache("_state_versions").get(user_id, 0), default=0, minimum=0):
            return
        await self._save_state(user_id, state)

    def _get_profile(self, user_id: str) -> Dict[str, Any]:
        """获取用户画像"""
        try:
            user_profiles = self._get_user_profiles_store()
            if user_id not in user_profiles:
                user_profiles[user_id] = {
                    "基本信息": {},
                    "兴趣爱好": {"音乐": [], "书籍": [], "食物": [], "颜色": []},
                    "性格特征": {"主要情绪": [], "沟通风格": ""},
                    "互动记录": {"首次互动": datetime.now().isoformat(), "总互动次数": 0},
                    "玛丽亚学习笔记": {"喜欢的话题": [], "反感的话题": [], "自动总结": []}
                }
                self.logger.info(f"为用户 {user_id} 创建新画像")
            profile = user_profiles[user_id]
            if not isinstance(profile, dict):
                profile = {}
                user_profiles[user_id] = profile
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
        raw = self._get_global_state().get("destined_one", {})
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
            return str(getattr(self, "lock_threshold", 100))
        user_id = info.get("user_id", "")
        user_name = info.get("user_name", "")
        return f"{user_id}({user_name})" if user_name else user_id

    def _get_relationship_state_machine(
        self,
        user_id: Optional[str],
        state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """返回当前用户在全局命定关系中的许可状态。"""
        destined_info = self._get_destined_one_info()
        if destined_info and self._is_destined_user(user_id):
            state_name = RELATIONSHIP_STATE_NAMES["FATED_ONE"]
            policy = "允许命定叙事、专属表达和高强度病娇关系推进。"
            allow_romance = True
        elif destined_info:
            state_name = RELATIONSHIP_STATE_NAMES["BOUNDARY_AFTER_FATE"]
            policy = (
                "全局命定之人已存在；当前用户只能停留在礼貌、观察、照拂或普通熟人互动，"
                "不得进入暧昧、吃醋、专属承诺、命定叙事、诱导性孤立或恋人式亲密。"
            )
            allow_romance = False
        else:
            state_name = RELATIONSHIP_STATE_NAMES["OPEN"]
            policy = "尚未出现全局命定之人；当前用户仍可按数值阶段自然推进关系。"
            allow_romance = True

        return {
            "状态": state_name,
            "允许暧昧": allow_romance,
            "命定之人": destined_info,
            "策略": policy,
            "当前用户是命定之人": bool(user_id and self._is_destined_user(user_id)),
        }

    def _apply_relationship_state_machine_constraints(
        self,
        user_id: Optional[str],
        state: Dict[str, Any],
    ) -> bool:
        machine = self._get_relationship_state_machine(user_id, state)
        old_values = {
            "好感度": self._coerce_store_int(state.get("好感度", 0), default=0, minimum=0, maximum=100),
            "病娇值": self._coerce_store_int(state.get("病娇值", 0), default=0, minimum=0, maximum=100),
            "锁定进度": self._coerce_store_int(state.get("锁定进度", 0), default=0, minimum=0, maximum=100),
            "占有欲": self._coerce_store_int(state.get("占有欲", 0), default=0, minimum=0, maximum=100),
            "已触发锁定事件": bool(state.get("已触发锁定事件", False)),
            "关系状态机": str(state.get("关系状态机", "") or ""),
        }
        state["关系状态机"] = machine["状态"]
        if machine["状态"] == RELATIONSHIP_STATE_NAMES["BOUNDARY_AFTER_FATE"]:
            state["好感度"] = min(
                self._coerce_store_int(state.get("好感度", 0), default=0, minimum=0, maximum=100),
                NON_DESTINED_FAVOR_CAP,
            )
            state["病娇值"] = 0
            state["锁定进度"] = 0
            state["占有欲"] = 0
            state["已触发锁定事件"] = False
        elif machine["状态"] == RELATIONSHIP_STATE_NAMES["FATED_ONE"]:
            state["好感度"] = max(
                60,
                self._coerce_store_int(state.get("好感度", 0), default=0, minimum=0, maximum=100),
            )
        return any(
            old_values.get(key) != (
                bool(state.get(key, False))
                if key == "已触发锁定事件"
                else state.get(key)
            )
            for key in old_values
        )

    def _apply_destined_one_boundary_to_other_states(self, destined_user_id: str) -> bool:
        changed = False
        for other_user_id, other_state in list(self._get_user_states_store().items()):
            if str(other_user_id) == str(destined_user_id) or not isinstance(other_state, dict):
                continue
            if self._apply_relationship_state_machine_constraints(str(other_user_id), other_state):
                other_state["当前状态"] = self._determine_state(other_state)
                self._get_user_states_store()[str(other_user_id)] = other_state
                self._get_store_set_cache("_state_dirty_users").add(str(other_user_id))
                changed = True
        return changed

    def _format_lock_progress_display(
        self,
        state: Dict[str, Any],
        deltas: Optional[Dict[str, int]] = None,
    ) -> str:
        current = self._format_state_value_with_delta(state, deltas or {}, "锁定进度")
        return f"{current}/{self._format_destined_one_label()}"

    async def _save_global_state(self):
        try:
            await self._save_json_async(
                self._get_store_file_path("global_state_file", "global_state.json"),
                self._build_persistable_global_state(),
            )
        except Exception as e:
            self.logger.error(f"保存全局命定状态失败: {e}", exc_info=True)

    async def _set_destined_one(self, user_id: str, user_name: str):
        self._get_global_state()["destined_one"] = {
            "user_id": str(user_id),
            "user_name": str(user_name or "").strip(),
            "locked_at": datetime.now().isoformat(),
        }
        if (
            self._apply_destined_one_boundary_to_other_states(str(user_id))
            and not self._is_store_task_pending(getattr(self, "_state_save_task", None))
        ):
            self._state_save_task = self._spawn_task(self._debounced_save_states())
        self._get_store_prompt_cache("_dynamic_prompt_cache").clear()
        await self._save_global_state()

    async def _clear_destined_one(self):
        global_state = self._get_global_state()
        if "destined_one" in global_state:
            del global_state["destined_one"]
            self._get_store_prompt_cache("_dynamic_prompt_cache").clear()
            await self._save_global_state()

    async def _save_profile(self, user_id: str, profile: Dict[str, Any]):
        """保存用户画像（异步）"""
        self._schedule_profile_save(user_id, profile)

    def _schedule_profile_file_save(self, user_id: Optional[str] = None):
        profile_versions = self._get_store_dict_cache("_profile_versions")
        profile_dirty_users = self._get_store_set_cache("_profile_dirty_users")
        if user_id:
            version = self._coerce_store_int(
                profile_versions.get(user_id, 0),
                default=0,
                minimum=0,
            ) + 1
            profile_versions[user_id] = version
            profile_dirty_users.add(user_id)
        profile_save_task = getattr(self, "_profile_save_task", None)
        if not self._is_store_task_pending(profile_save_task):
            self._profile_save_task = self._spawn_task(self._debounced_save_profiles())

    def _schedule_profile_save(self, user_id: str, profile: Dict[str, Any]):
        """合并短时间内的用户画像写入。"""
        self._get_user_profiles_store()[user_id] = profile
        self._schedule_profile_file_save(user_id)

    async def _debounced_save_profiles(self):
        profile_versions = self._get_store_dict_cache("_profile_versions")
        profile_dirty_users = self._get_store_set_cache("_profile_dirty_users")
        dirty_versions = {
            str(dirty_user_id): self._coerce_store_int(
                profile_versions.get(dirty_user_id, 0),
                default=0,
                minimum=0,
            )
            for dirty_user_id in list(profile_dirty_users)
        }
        try:
            await asyncio.sleep(SAVE_DEBOUNCE_SECONDS)
            await self._save_json_async(
                self._get_store_file_path("user_profiles_file", "user_profiles.json"),
                self._build_persistable_user_profiles(),
            )
            for dirty_user_id, version in dirty_versions.items():
                if self._coerce_store_int(
                    profile_versions.get(dirty_user_id, 0),
                    default=0,
                    minimum=0,
                ) == version:
                    profile_dirty_users.discard(dirty_user_id)
        except Exception as e:
            self.logger.error(f"保存用户画像失败: {e}", exc_info=True)
        finally:
            self._profile_save_task = None
            if self._get_store_set_cache("_profile_dirty_users"):
                self._profile_save_task = self._spawn_task(self._debounced_save_profiles())

