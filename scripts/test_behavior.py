"""Lightweight local checks for Marianna behavior rules.

Run with:
    python scripts/test_behavior.py
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _DummyLogger:
    def debug(self, *args, **kwargs):  # pragma: no cover - tiny import shim
        pass

    def info(self, *args, **kwargs):  # pragma: no cover - tiny import shim
        pass

    def warning(self, *args, **kwargs):  # pragma: no cover - tiny import shim
        pass

    def error(self, *args, **kwargs):  # pragma: no cover - tiny import shim
        pass


def _install_astrbot_shims() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    provider = types.ModuleType("astrbot.api.provider")

    api.logger = _DummyLogger()
    event.AstrMessageEvent = object
    provider.ProviderRequest = object
    provider.LLMResponse = object

    sys.modules.setdefault("astrbot", astrbot)
    sys.modules.setdefault("astrbot.api", api)
    sys.modules.setdefault("astrbot.api.event", event)
    sys.modules.setdefault("astrbot.api.provider", provider)


_install_astrbot_shims()

from marianna.analysis import MariannaAnalysisMixin  # noqa: E402
from marianna.constants import (  # noqa: E402
    DEFAULT_STATE,
    PLUGIN_VERSION,
    TOKEN_OPT_CONTEXT_HISTORY_LIMIT,
    TOKEN_OPT_CONTEXT_MAX_CHARS_PER_MSG,
)
from marianna.history import MariannaHistoryMixin  # noqa: E402
from marianna.memory import MariannaMemoryMixin  # noqa: E402
from marianna.profile import MariannaProfileMixin  # noqa: E402
from marianna.prompts import MariannaPromptMixin  # noqa: E402
from marianna.runtime import MariannaRuntimeMixin  # noqa: E402
from marianna.state_store import MariannaStateStoreMixin  # noqa: E402
from marianna.turn import MariannaTurnMixin  # noqa: E402


class Harness(MariannaRuntimeMixin, MariannaMemoryMixin, MariannaStateStoreMixin, MariannaHistoryMixin, MariannaAnalysisMixin, MariannaProfileMixin, MariannaPromptMixin, MariannaTurnMixin):
    def __init__(self) -> None:
        self.logger = _DummyLogger()
        self.global_state = {}
        self.user_states = {}
        self.user_profiles = {}
        self.config = {}
        self._static_prompt_cache = {}
        self._dynamic_prompt_cache = {}
        self._style_fingerprint_cache = {}
        self.lock_threshold = 100
        self.default_debug_mode = False
        self.inject_state_details = True
        self.enable_profile = True
        self.enable_token_cost_optimization = True
        self.enable_state_delta_smoothing = True
        self.enable_memory_evidence_stage_gate = True
        self.enable_memory_evidence_grading = True
        self.state_smooth_repeat_decay_start = 2
        self.state_smooth_high_value_start = 85
        self.state_smooth_near_max_start = 95
        self.state_smooth_high_anxiety_start = 80
        self.enable_memory_quality_filter = True
        self.memory_quality_min_text_chars = 6
        self.memory_quality_min_salience = 3
        self.interaction_memory_min_delta = 2
        self.enable_reflection_update_layer = True
        self.enable_relationship_cooldown = True
        self.relationship_cooldown_idle_days = 7
        self.relationship_cooldown_max_delta = 6
        self.diagnostic_history_limit = 20
        self.enable_behavior_style_layer = True
        self.short_term_emotion_decay = 0.65
        self.enable_behavior_band_smoothing = True
        self.enable_behavior_continuity_bridge = True
        self.enable_behavior_action_budget = True
        self.enable_reply_variety_guard = True
        self.enable_recent_style_fingerprint = True
        self.recent_style_fingerprint_limit = 4
        self.enable_reply_template_trim = True
        self.reply_template_trim_max_actions = 1
        self.enable_time_aware_short_term_decay = True
        self.short_term_decay_half_life_hours = 6
        self.enable_relationship_event_log = True
        self.relationship_event_log_limit = 12
        self.enable_behavior_style_variant = True
        self.enable_memory_recall_negative_feedback = True
        self.memory_recall_negative_feedback_max = 3
        self.enable_memory_privacy_layer = True
        self.enable_memory_scene_bridge = True
        self.enable_memory_evidence_trace = True
        self.enable_memory_conflict_resolution = True
        self.enable_memory_temperature_layer = True
        self.memory_hot_days = 3
        self.memory_warm_days = 45
        self.memory_recall_cooldown_seconds = 300
        self.memory_mode_preset = "balanced"
        self.memory_prompt_limit = 3
        self.enable_adaptive_lightweight_prompt = True
        self.adaptive_lightweight_prompt_max_chars = 36
        self.enable_prompt_budget_guard = True
        self.prompt_token_budget = 2200
        self.enable_prompt_budget_memory_anchor = True
        self.prompt_budget_memory_anchor_chars = 160
        self.prompt_budget_history_limit = 12
        self.enable_prompt_cost_profile_stats = True
        self.prompt_cost_profile_window = 20
        self.enable_prompt_cost_auto_memory_mode = True
        self.prompt_cost_auto_lean_hit_rate = 45
        self.prompt_cost_auto_balanced_hit_rate = 20
        self.prompt_cost_auto_mode_sticky_turns = 2
        self.enable_prompt_budget_auto_throttle = True
        self.prompt_budget_auto_throttle_min_streak = 2
        self.prompt_budget_auto_throttle_recovery_turns = 2
        self.enable_prompt_budget_compression_tiers = True
        self.prompt_budget_heavy_compact_streak = 5
        self.prompt_budget_throttle_log_limit = 8
        self.prompt_budget_throttle_escalation_hits = 3
        self.prompt_budget_throttle_escalation_recovery_clear = 1
        self.enable_prompt_budget_memory_mode_adaptation = True
        self.prompt_budget_memory_mode_pressure_hit_rate = 34
        self.prompt_budget_memory_mode_lean_limit = 1
        self.prompt_budget_memory_mode_balanced_limit = 2
        self.prompt_budget_memory_mode_rich_limit = 3
        self.prompt_budget_memory_mode_lean_chars = 120
        self.prompt_budget_memory_mode_balanced_chars = 180
        self.prompt_budget_memory_mode_rich_chars = 260
        self.enable_prompt_budget_memory_value_priority = True
        self.prompt_budget_memory_priority_char_trigger = 260
        self.enable_prompt_budget_memory_candidate_expansion = True
        self.prompt_budget_memory_candidate_multiplier = 3
        self.prompt_budget_memory_candidate_max = 12
        self.enable_prompt_memory_slot_dedup = True
        self.enable_prompt_memory_selection_trace = True
        self.prompt_memory_selection_trace_limit = 3
        self.active_event_idle_hours = 24
        self.enable_builtin_memory_vector = False
        self.embedding_provider_id = ""
        self.builtin_memory_vector_min_similarity = 0.35
        self.builtin_memory_vector_weight = 4
        self.builtin_memory_vector_candidate_limit = 96
        self.behavior_band_sticky_turns = 2
        self.state_scope_mode = "private_global_group_scene"

    def _schedule_state_save(self, user_id, state) -> None:
        return None


def _state(**overrides):
    state = copy.deepcopy(DEFAULT_STATE)
    state.update(overrides)
    return state


def test_state_delta_smoothing() -> None:
    h = Harness()
    h.favor_multiplier = "bad"
    h.yan_multiplier = "bad"
    bad_state = _state(**{"好感度": "bad", "焦虑值": "bad", "优雅值": "bad"})
    assert h._apply_state_delta(bad_state, "好感度", 2) == 2
    assert h._get_dynamic_state_delta_multiplier(bad_state, "好感度", 2) == 1.0
    applied = h._apply_llm_state_changes(
        "u_bad",
        _state(**{"好感度": "bad", "信任度": "bad", "病娇值": "bad", "占有欲": "bad"}),
        {"信任度": 1},
    )
    assert isinstance(applied["信任度"], int)

    state = _state(好感度=92, 信任度=90, 焦虑值=81)
    deltas = {"好感度": 5, "信任度": 4, "焦虑值": 3, "病娇值": 0, "优雅值": 0, "锁定进度": 0}
    turn_analysis = {"用户意图": "认真亲近", "关系信号": "反复表达喜欢"}
    evidence = {"level": "中", "reasons": ["测试"]}
    first = h._smooth_state_deltas(state, "我喜欢你，请相信我。", turn_analysis, deltas, evidence)
    second = h._smooth_state_deltas(state, "我喜欢你，请相信我。", turn_analysis, deltas, evidence)
    assert first["好感度"] < deltas["好感度"]
    assert second["好感度"] < first["好感度"]
    assert second["焦虑值"] <= first["焦虑值"]


def test_relationship_cooldown() -> None:
    h = Harness()
    h.relationship_cooldown_idle_days = "bad"
    h.relationship_cooldown_max_delta = "bad"
    h.diagnostic_history_limit = "bad"
    state = _state(
        好感度=70,
        信任度=50,
        焦虑值=20,
        最后互动时间=(datetime.now() - timedelta(days=15)).isoformat(),
    )
    changes = h._apply_relationship_cooldown_if_needed(state, user_id="u1")
    assert changes["好感度"] < 0
    assert changes["信任度"] < 0
    assert changes["焦虑值"] > 0
    assert state["最近降温日期"] == datetime.now().date().isoformat()
    assert state["诊断历史"]

    bad_state = _state(
        好感度="bad",
        信任度="bad",
        焦虑值="bad",
        最后互动时间=(datetime.now() - timedelta(days=15)).isoformat(),
    )
    assert h._apply_relationship_cooldown_if_needed(bad_state, user_id="u_bad") == {}


def test_memory_quality_filter() -> None:
    h = Harness()
    h.interaction_memory_min_delta = "bad"
    h.memory_quality_min_salience = "bad"
    h.memory_quality_min_text_chars = "bad"
    assert not h._should_store_interaction_memory(
        "这是一个足够长的普通互动印象",
        {"好感度": "bad"},
        {"用户意图": "普通回应"},
        {"类型": "event", "执行": "记住这次互动"},
    )
    assert not h._should_store_interaction_memory("嗯", {}, {"用户意图": "普通回应"}, None)
    assert h._should_store_interaction_memory(
        "我的生日是一月一日，请你记住。",
        {},
        {"用户意图": "普通回应"},
        None,
    )


def test_memory_write_candidate_staging() -> None:
    h = Harness()
    state = {}
    deltas = {"好感度": 0, "病娇值": 0, "锁定进度": 0, "信任度": 0, "焦虑值": 0, "优雅值": 0}
    turn = {"用户意图": "普通回应", "关系信号": "暂无明显关系推进"}
    assert not h._should_store_interaction_memory("薄荷茶还挺好喝", deltas, turn, None)
    assert not h._stage_memory_write_candidate(state, "薄荷茶还挺好喝", deltas, turn, None)
    assert state["最近记忆写入候选"]["count"] == 1
    assert h._stage_memory_write_candidate(state, "薄荷茶还挺好喝", deltas, turn, None)
    assert state["最近记忆写入候选"]["promoted"]
    for item in state["记忆写入候选"]:
        item["count"] = "bad"
    h._mark_memory_write_candidate_promoted(state, state["最近记忆写入候选"]["key"])
    assert state["最近记忆写入候选"]["count"] == 0
    assert not h._stage_memory_write_candidate(state, "薄荷茶还挺好喝", deltas, turn, None)
    assert state["最近记忆写入候选"]["reason"] == "already_promoted"


def test_memory_write_candidate_refresh_before_limit_trim() -> None:
    h = Harness()
    h.memory_write_candidate_limit = 2
    state = {}
    deltas = {"å¥½æ„Ÿåº¦": 0, "ç—…å¨‡å€¼": 0, "é”å®šè¿›åº¦": 0, "ä¿¡ä»»åº¦": 0, "ç„¦è™‘å€¼": 0, "ä¼˜é›…å€¼": 0}
    turn = {"\u7528\u6237\u610f\u56fe": "\u666e\u901a\u56de\u5e94", "\u5173\u7cfb\u4fe1\u53f7": "\u6682\u65e0\u660e\u663e\u5173\u7cfb\u63a8\u8fdb"}
    message = "\u8584\u8377\u8336\u8fd8\u633a\u597d\u559d"
    assert not h._stage_memory_write_candidate(state, message, deltas, turn, None)
    candidate_field = next(field for field, value in state.items() if isinstance(value, list))
    key = state[candidate_field][0]["key"]
    state[candidate_field] = [
        state[candidate_field][0],
        {"key": "old-1", "count": 1},
        {"key": "old-2", "count": 1},
    ]
    promoted = h._stage_memory_write_candidate(state, message, deltas, turn, None)
    keys = [item.get("key") for item in state[candidate_field]]
    assert promoted
    assert key in keys
    assert len(keys) == 2


def test_memory_write_candidate_tolerates_bad_counts() -> None:
    h = Harness()
    h.memory_write_candidate_limit = "bad"
    h.memory_write_candidate_promote_hits = "bad"
    state = {
        "记忆写入候选": [
            {"key": "old", "count": "bad"},
        ]
    }
    deltas = {"好感度": 0, "病娇值": 0, "锁定进度": 0, "信任度": 0, "焦虑值": 0, "优雅值": 0}
    turn = {"用户意图": "普通回应", "关系信号": "暂无明显关系推进"}
    assert not h._stage_memory_write_candidate(state, "薄荷茶还挺好喝", deltas, turn, None)
    assert state["最近记忆写入候选"]["count"] == 1
    assert h._stage_memory_write_candidate(state, "薄荷茶还挺好喝", deltas, turn, None)

    promoted_state = {
        "记忆写入候选": [
            {
                "key": state["最近记忆写入候选"]["key"],
                "count": "bad",
                "promoted_at": "2026-01-01T00:00:00",
            }
        ]
    }
    assert not h._stage_memory_write_candidate(promoted_state, "薄荷茶还挺好喝", deltas, turn, None)
    assert promoted_state["最近记忆写入候选"]["count"] == 0


def test_topic_resonance_gives_small_trust_delta() -> None:
    h = Harness()
    state = _state(好感度=0, 信任度=15)
    message = "或许你可以尝试出去看看，亲身经历的话感觉会不一样"

    analysis = h._build_local_state_analysis(state, message, user_id="u_topic")

    assert analysis is not None
    turn = analysis["__turn_analysis"]
    deltas = analysis
    assert turn["用户意图"] == "话题共鸣或温和建议"
    assert turn["关系信号"] == "认真接住话题"
    assert deltas["信任度"] == 1
    assert deltas["好感度"] == 1
    assert deltas["病娇值"] == 0
    assert deltas["锁定进度"] == 0


def test_subtle_social_signals_prevent_frozen_opening_values() -> None:
    h = Harness()
    state = _state(**{"\u597d\u611f\u5ea6": 0, "\u4fe1\u4efb\u5ea6": 15})

    intro = h._decide_state_deltas_from_intent(
        state,
        "\u5fd8\u8bb0\u81ea\u6211\u4ecb\u7ecd\u4e86\uff0c\u6211\u662f\u5495\u5495\u675e\uff0c\u4e00\u4f4d\u6765\u81ea\u4e1c\u65b9\u7684\u65c5\u8005",
        {"\u7528\u6237\u610f\u56fe": "\u666e\u901a\u56de\u5e94", "\u5173\u7cfb\u4fe1\u53f7": "\u65e0\u660e\u663e\u5173\u7cfb\u63a8\u8fdb"},
        user_id="u_subtle",
        memory_evidence={"level": "\u65e0", "reasons": []},
    )
    assert intro["\u4fe1\u4efb\u5ea6"] == 1
    assert intro["\u75c5\u5a07\u503c"] == 0
    assert intro["\u9501\u5b9a\u8fdb\u5ea6"] == 0

    curious = h._decide_state_deltas_from_intent(
        _state(**{"\u597d\u611f\u5ea6": 0, "\u4fe1\u4efb\u5ea6": 15}),
        "\u6211\u6709\u70b9\u597d\u5947\u4f60\u73b0\u5728\u5728\u505a\u4ec0\u4e48\u5462\uff1f",
        {"\u7528\u6237\u610f\u56fe": "\u63d0\u95ee\u6216\u8bf7\u6c42", "\u5173\u7cfb\u4fe1\u53f7": "\u6682\u65e0\u660e\u663e\u5173\u7cfb\u63a8\u8fdb"},
        user_id="u_subtle",
        memory_evidence={"level": "\u65e0", "reasons": []},
    )
    assert curious["\u597d\u611f\u5ea6"] == 1
    assert curious["\u75c5\u5a07\u503c"] == 0
    assert curious["\u9501\u5b9a\u8fdb\u5ea6"] == 0

    help_request = h._decide_state_deltas_from_intent(
        _state(**{"\u597d\u611f\u5ea6": 0, "\u4fe1\u4efb\u5ea6": 15}),
        "\u4eba\u751f\u5730\u4e0d\u719f\u7684\uff0c\u6211\u60f3\u95ee\u95ee\u8fd9\u91cc\u662f\u54ea\u91cc",
        {"\u7528\u6237\u610f\u56fe": "\u63d0\u95ee\u6216\u8bf7\u6c42", "\u5173\u7cfb\u4fe1\u53f7": "\u6682\u65e0\u660e\u663e\u5173\u7cfb\u63a8\u8fdb"},
        user_id="u_subtle",
        memory_evidence={"level": "\u65e0", "reasons": []},
    )
    assert help_request["\u4fe1\u4efb\u5ea6"] == 1
    assert help_request["\u75c5\u5a07\u503c"] == 0
    assert help_request["\u9501\u5b9a\u8fdb\u5ea6"] == 0

def test_recent_memory_command_helpers() -> None:
    h = Harness()
    h.enable_builtin_memory = True
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.local_memory_db_file = Path(tmp_dir) / "memory.db"
        h._init_builtin_memory_db_sync()
        now = datetime.now().isoformat()
        with h._connect_local_memory_db() as conn:
            conn.execute(
                """
                INSERT INTO memories(
                    id, user_id, layer, type, summary, raw_content, normalized_content,
                    keywords_json, salience, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "recent-1",
                    "u_recent",
                    "impression",
                    "preference",
                    "喜欢亲身旅行",
                    "用户建议玛丽亚亲身出去看看",
                    "用户建议玛丽亚亲身出去看看",
                    "[]",
                    4,
                    now,
                    now,
                ),
            )
        memories = asyncio.run(h._get_recent_builtin_memories("u_recent", limit=5))
        assert len(memories) == 1
        report = h._build_recent_memory_report(memories)
        assert "最近记忆" in report
        assert "喜欢亲身旅行" in report
        assert "暂无" in h._build_recent_memory_report([])


def test_short_term_behavior_state() -> None:
    h = Harness()
    bad_state = _state(
        **{
            "好感度": "bad",
            "信任度": "bad",
            "病娇值": "bad",
            "锁定进度": "bad",
            "焦虑值": "bad",
            "优雅值": "bad",
            "占有欲": "bad",
            "被触动值": "bad",
            "防备值": "bad",
            "行为档位稳定轮数": "bad",
            "行为变体计数": {"克制关心": "bad"},
        }
    )
    bad_result = h._update_short_term_behavior_state(
        bad_state,
        {"用户意图": "普通回应", "用户情绪": "平静", "关系信号": "暂无明显关系推进"},
        {"好感度": "bad", "信任度": "bad", "焦虑值": "bad", "优雅值": "bad"},
        user_id="u_bad",
    )
    assert bad_result["短期心情"]
    assert h._smooth_behavior_band_transition(
        {"当前行为档位": "克制关心", "行为档位稳定轮数": "bad"},
        "克制关心",
        "bad counter",
    ) == ("克制关心", "bad counter", 1)
    assert h._select_behavior_style_variant(
        {"行为变体计数": {"克制关心": "bad"}},
        "克制关心",
    ) == "反问型"

    state = _state(好感度=62, 信任度=50, 焦虑值=20, 优雅值=86)
    result = h._update_short_term_behavior_state(
        state,
        {"用户意图": "分享秘密或建立约定", "用户情绪": "平静", "关系信号": "建立信任"},
        {"好感度": 2, "信任度": 2, "焦虑值": -1, "优雅值": 0, "病娇值": 0, "锁定进度": 0},
        user_id="u1",
    )
    assert result["短期心情"] in {"被触动", "被安抚"}
    assert result["被触动值"] > 0
    assert result["当前行为档位"] in {"主动靠近", "克制关心", "稳定温柔"}
    snapshot = h._derive_state_snapshot(state)
    assert snapshot["当前行为档位"] == state["当前行为档位"]


def test_behavior_band_smoothing() -> None:
    h = Harness()
    state = _state(
        好感度=82,
        信任度=70,
        焦虑值=15,
        优雅值=90,
        情绪余温=20,
        被触动值=10,
        当前行为档位="礼貌回应",
        行为档位稳定轮数=3,
    )
    result = h._update_short_term_behavior_state(
        state,
        {"用户意图": "普通回应", "用户情绪": "平静", "关系信号": "无明显关系推进"},
        {"好感度": 0, "信任度": 0, "焦虑值": 0, "优雅值": 0, "病娇值": 0, "锁定进度": 0},
        user_id="u1",
    )
    assert result["目标行为档位"] == "稳定温柔"
    assert result["当前行为档位"] == "克制关心"
    assert "平滑" in result["行为档位理由"]

    urgent = h._update_short_term_behavior_state(
        state,
        {"用户意图": "离开或冷淡暗示", "用户情绪": "平静", "关系信号": "无明显关系推进"},
        {"好感度": 0, "信任度": 0, "焦虑值": 5, "优雅值": 0, "病娇值": 0, "锁定进度": 0},
        user_id="u1",
    )
    assert urgent["当前行为档位"] == "确认挽留"


def test_behavior_continuity_bridge() -> None:
    h = Harness()
    state = _state(
        好感度=62,
        信任度=50,
        焦虑值=20,
        优雅值=86,
        短期心情="被触动",
        当前行为档位="主动靠近",
    )
    result = h._update_short_term_behavior_state(
        state,
        {"用户意图": "普通回应", "用户情绪": "平静", "关系信号": "无明显关系推进"},
        {"好感度": 0, "信任度": 0, "焦虑值": 0, "优雅值": 0, "病娇值": 0, "锁定进度": 0},
        user_id="u1",
    )
    assert result["上轮短期心情"] == "被触动"
    assert result["上轮行为档位"] == "主动靠近"
    assert result["行为连续性提示"]


def test_behavior_action_budget_prompt() -> None:
    h = Harness()
    state = _state(当前行为档位="占有试探")
    prompt = h._build_behavior_action_budget_prompt(
        state,
        {"用户意图": "普通回应"},
        compact=False,
    )
    assert "专属潜台词最多1处" in prompt
    assert "不要把预算用满" in prompt


def test_reply_variety_guard_prompt() -> None:
    h = Harness()
    state = _state(当前行为档位="克制关心", 短期心情="平静")
    prompt = h._build_reply_variety_guard_prompt(state)
    assert "回复变化守卫" in prompt
    assert "固定动作" in prompt


def test_reply_style_fingerprint() -> None:
    h = Harness()
    fingerprint = h._extract_reply_style_fingerprint("（我垂下眼）别误会，我会记住。")
    assert "我垂下眼" in fingerprint["actions"]
    assert "别误会" in fingerprint["terms"]
    assert fingerprint["endings"]
    prompt = h._format_recent_style_fingerprint_prompt([
        "（我垂下眼）别误会，我会记住。",
        "（我轻轻整理裙摆）我在。",
    ])
    assert "回复变化参考" in prompt
    assert "最近动作" in prompt


def test_numeric_coercion_handles_infinite_values() -> None:
    h = Harness()
    assert h._coerce_prompt_int(float("inf"), default=7, minimum=0, maximum=100) == 7
    assert h._coerce_analysis_int(float("-inf"), default=8, minimum=0, maximum=100) == 8
    assert h._coerce_memory_int(float("inf"), default=9, minimum=0, maximum=100) == 9
    assert h._coerce_runtime_int(float("-inf"), default=10, minimum=0, maximum=100) == 10
    assert h._coerce_store_int(float("inf"), default=11, minimum=0, maximum=100) == 11
    assert h._coerce_history_limit(float("-inf"), default=12, minimum=0) == 12
    assert h._build_style_fingerprint_cache_key("u1", "bad")[1] == 0
    assert h._scale_analysis_deltas({}, {"x": float("inf"), "y": float("nan")}) == {}
    assert h._format_state_value_with_delta({"x": 5}, {"x": float("inf")}, "x") == "5"
    assert h._clamp_state_percent(float("inf"), default=13) == 13


def test_reply_template_trim() -> None:
    h = Harness()
    h.reply_template_trim_max_actions = "bad"
    h.reply_self_check_max_lines = "bad"
    h.reply_self_check_max_chars = "bad"
    state = _state()
    reply = "（我垂下眼）第一句。\n（我轻轻整理裙摆）第二句？\n还要继续吗？"
    checked = h._self_check_reply(reply, state, "")
    assert "我垂下眼" in checked
    assert "整理裙摆" not in checked
    assert checked.count("？") <= 1


def test_reply_template_trim_categories() -> None:
    h = Harness()
    h.reply_template_trim_max_actions = 3
    reply = "（我垂下眼）一。\n（我轻轻笑了笑）二。\n（我整理裙摆）三。\n（我靠近一步）四。"
    checked = h._trim_repetitive_reply_template(reply)
    assert "我垂下眼" in checked
    assert "轻轻笑" not in checked
    assert "整理裙摆" in checked
    assert "靠近一步" in checked


def test_group_boundary_reply_dedup() -> None:
    h = Harness()
    private_phrase = "\u8fd9\u4ef6\u4e8b\u4e0d\u9002\u5408\u5728\u8fd9\u91cc\u7ec6\u8bf4"
    boundary_phrase = "\u6211\u4f1a\u4fdd\u6301\u5206\u5bf8"
    repeated = f"{private_phrase}\u7684{private_phrase}\u3002{boundary_phrase}\uff0c{boundary_phrase}\u3002"
    checked = h._normalize_group_boundary_reply(repeated)
    assert checked.count(private_phrase) == 1
    assert checked.count(boundary_phrase) == 1
    assert "\u3002\u3002" not in checked
    assert "\uff0c\uff0c" not in checked

    state = {"\u5f53\u524d\u662f\u5426\u7fa4\u804a\u4f5c\u7528\u57df": True}
    reply = "\u8fd9\u662f\u79d8\u5bc6\u7684\u79d8\u5bc6\u3002\u4f60\u662f\u547d\u5b9a\uff0c\u552f\u4e00\u3002"
    guarded = h._self_check_reply(reply, state)
    assert "\u79d8\u5bc6" not in guarded
    assert "\u547d\u5b9a" not in guarded
    assert "\u552f\u4e00" not in guarded
    assert guarded.count(private_phrase) == 1
    assert guarded.count(boundary_phrase) == 1


def test_time_decay_and_event_log() -> None:
    h = Harness()
    h.short_term_decay_half_life_hours = "bad"
    h.short_term_emotion_decay = "bad"
    h.relationship_event_log_limit = "bad"
    state = _state(
        好感度=65,
        信任度=55,
        焦虑值=20,
        优雅值=86,
        情绪余温=80,
        被触动值=60,
        防备值=20,
        _本轮前最后互动时间=(datetime.now() - timedelta(hours=12)).isoformat(),
    )
    result = h._update_short_term_behavior_state(
        state,
        {"用户意图": "道歉或修复关系", "用户情绪": "愧疚", "关系信号": "修复关系"},
        {"好感度": 0, "信任度": 1, "焦虑值": -2, "优雅值": 1, "病娇值": 0, "锁定进度": 0},
        user_id="u1",
        user_msg="对不起，我保证以后不会这样。",
    )
    assert result["时间衰减系数"] < 1
    assert state["关系事件日志"]
    assert state["行为风格变体"]


def test_relationship_stage_uses_event_log() -> None:
    h = Harness()
    state = _state(好感度=55, 信任度=46, 互动计数=9)
    assert h._determine_relationship_stage(state) != "私下偏爱"
    state["关系事件日志"] = [{"type": "first_secret", "title": "第一次分享秘密"}]
    assert h._determine_relationship_stage(state) == "私下偏爱"


def test_memory_recall_protection() -> None:
    h = Harness()
    assert h._is_protected_recalled_memory("用户生日是一月一日", 2)
    assert h._is_protected_recalled_memory("普通闲聊印象", 7)
    assert not h._is_protected_recalled_memory("普通闲聊印象", 2)


def test_missed_memory_penalty_uses_raw_content_and_protection() -> None:
    h = Harness()
    h.enable_builtin_memory = True
    h.enable_memory_recall_negative_feedback = True
    h.memory_recall_negative_feedback_max = "bad"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.local_memory_db_file = Path(tmp_dir) / "memory.db"
        h._local_memory_query_cache = {"stale": {"result": []}}
        h._init_builtin_memory_db_sync()
        now = datetime.now().isoformat()
        rows = [
            ("ordinary", "u1", "impression", "interaction", "ordinary", "ordinary casual chat", 3),
            (
                "birthday",
                "u1",
                "impression",
                "interaction",
                "birthday",
                "\u7528\u6237\u751f\u65e5\u662f1\u67081\u65e5",
                3,
            ),
        ]
        with h._connect_local_memory_db() as conn:
            for memory_id, user_id, layer, memory_type, summary, raw_content, salience in rows:
                conn.execute(
                    """
                    INSERT INTO memories(
                        id, user_id, layer, type, summary, raw_content, normalized_content,
                        keywords_json, salience, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        user_id,
                        layer,
                        memory_type,
                        summary,
                        raw_content,
                        h._normalize_mnemosyne_content(raw_content),
                        "[]",
                        salience,
                        now,
                        now,
                    ),
                )
        changed = h._penalize_missed_builtin_memories_sync("u1", ["ordinary", "birthday"])
        with h._connect_local_memory_db() as conn:
            salience = {
                str(row["id"]): int(row["salience"])
                for row in conn.execute("SELECT id, salience FROM memories").fetchall()
            }
        assert changed == 1
        assert salience["ordinary"] == 2
        assert salience["birthday"] == 3
        h.user_states = {
            "u1": {
                "最近召回记忆": [
                    {"id": "ordinary", "terms": ["ordinary"]},
                    {"id": "birthday", "terms": ["birthday"]},
                ]
            }
        }
        assert asyncio.run(h._apply_memory_recall_negative_feedback("u1", "unrelated")) == 1
        assert h._local_memory_query_cache == {}


def test_builtin_memory_reinforcement_preserves_manual_metadata() -> None:
    h = Harness()
    h.enable_builtin_memory = True
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.local_memory_db_file = Path(tmp_dir) / "memory.db"
        h._init_builtin_memory_db_sync()
        content = "\u8bb0\u5f97\u8fd9\u662f\u53ea\u544a\u8bc9\u4f60\u7684\u627f\u8bfa"
        assert h._store_builtin_memory_sync("u1", content, "interaction", 4, "impression")
        with h._connect_local_memory_db() as conn:
            row = conn.execute("SELECT id FROM memories WHERE user_id = ?", ("u1",)).fetchone()
            memory_id = str(row["id"])
        assert h._resolve_memory_id_sync("u1", "%") == ""
        assert h._resolve_memory_id_sync("u1", "_") == ""
        assert h._resolve_memory_id_sync("u1", memory_id[:3]) == ""
        assert h._protect_builtin_memory_sync("u1", memory_id[:10])
        assert h._set_builtin_memory_visibility_sync("u1", memory_id[:10], "sensitive")
        assert h._store_builtin_memory_sync("u1", content, "interaction", 4, "impression")
        with h._connect_local_memory_db() as conn:
            row = conn.execute("SELECT visibility, evidence_json, salience FROM memories WHERE id = ?", (memory_id,)).fetchone()
        evidence = json.loads(row["evidence_json"])
        assert row["visibility"] == "sensitive"
        assert evidence["visibility"] == "sensitive"
        assert evidence["protected"] is True
        assert int(row["salience"]) >= 8
        assert asyncio.run(h._protect_builtin_memory("u1", memory_id[:10]))
        assert asyncio.run(h._set_builtin_memory_visibility("u1", memory_id[:10], "public_profile"))
        searched = asyncio.run(h._search_builtin_memories("u1", "承诺", limit=5))
        assert searched
        search_report = h._build_memory_search_report(searched, "承诺")
        assert "记忆搜索" in search_report
        assert memory_id[:8] in search_report
        assert asyncio.run(h._delete_builtin_memory("u1", memory_id[:10]))
        with h._connect_local_memory_db() as conn:
            deleted = conn.execute("SELECT id FROM memories WHERE id = ?", (memory_id,)).fetchone()
        assert deleted is None


def test_builtin_memory_write_coerces_dirty_numeric_fields() -> None:
    h = Harness()
    h.enable_builtin_memory = True
    h.builtin_memory_summary_max_chars = "bad"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.local_memory_db_file = Path(tmp_dir) / "memory.db"
        h._init_builtin_memory_db_sync()
        content = "user likes quiet rainy evenings"
        assert h._store_builtin_memory_sync("u1", content, "interaction", "bad", "impression")
        assert asyncio.run(h._store_to_builtin_memory("u1", "user likes warm milk", "interaction", "bad", None))
        with h._connect_local_memory_db() as conn:
            row = conn.execute("SELECT id FROM memories WHERE user_id = ?", ("u1",)).fetchone()
            memory_id = str(row["id"])
            conn.execute(
                "UPDATE memories SET reinforcement_count = ?, salience = ? WHERE id = ?",
                ("bad", "bad", memory_id),
            )
        assert h._store_builtin_memory_sync("u1", content, "interaction", 4, "impression")
        with h._connect_local_memory_db() as conn:
            row = conn.execute("SELECT reinforcement_count, salience FROM memories WHERE id = ?", (memory_id,)).fetchone()
        assert int(row["reinforcement_count"]) == 1
        assert int(row["salience"]) == 4


def test_builtin_memory_export_uses_unique_files() -> None:
    h = Harness()
    h.enable_builtin_memory = True
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.data_dir = Path(tmp_dir)
        h.local_memory_db_file = Path(tmp_dir) / "memory.db"
        h._init_builtin_memory_db_sync()
        assert h._store_builtin_memory_sync("u1", "用户认真承诺过一件事", "interaction", 4, "impression")

        first = h._export_builtin_memories_sync("u1", limit=10)
        second = h._export_builtin_memories_sync("u1", limit=10)

        assert first != second
        assert first.exists()
        assert second.exists()
        assert first.read_text(encoding="utf-8").strip()
        assert second.read_text(encoding="utf-8").strip()


def test_builtin_memory_export_tolerates_bad_limit() -> None:
    h = Harness()
    h.enable_builtin_memory = True
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.data_dir = Path(tmp_dir)
        h.local_memory_db_file = Path(tmp_dir) / "memory.db"
        h._init_builtin_memory_db_sync()
        assert h._store_builtin_memory_sync("u1", "export fallback limit memory", "interaction", 4, "impression")

        exported = h._export_builtin_memories_sync("u1", limit="not-a-number")

        assert exported.exists()
        assert exported.read_text(encoding="utf-8").strip()


def test_builtin_memory_export_disabled_returns_none() -> None:
    h = Harness()
    h.enable_builtin_memory = False
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.data_dir = Path(tmp_dir)
        result = asyncio.run(h._export_builtin_memories("u1", limit=10))

    assert result is None


def test_builtin_memory_maintenance_wrappers_tolerate_failures() -> None:
    h = Harness()

    async def ready():
        return True

    def fail_export(*args, **kwargs):
        raise RuntimeError("export failed")

    def fail_import(*args, **kwargs):
        raise RuntimeError("import failed")

    def fail_cleanup(*args, **kwargs):
        raise RuntimeError("cleanup failed")

    def fail_backfill(*args, **kwargs):
        raise RuntimeError("backfill failed")

    h._ensure_builtin_memory_ready = ready
    h._export_builtin_memories_sync = fail_export
    h._import_builtin_memories_sync = fail_import
    h._cleanup_low_value_builtin_memories_sync = fail_cleanup
    h._backfill_builtin_memory_privacy_sync = fail_backfill

    assert asyncio.run(h._export_builtin_memories("u1", limit=10)) is None
    assert asyncio.run(h._import_builtin_memories("u1", "import.jsonl", limit=10)) == 0
    assert asyncio.run(h._cleanup_low_value_builtin_memories("u1")) == 0
    assert asyncio.run(h._backfill_builtin_memory_privacy(limit=10)) == 0


def test_builtin_memory_import_caps_content_and_tolerates_bad_limit() -> None:
    h = Harness()
    h.enable_builtin_memory = True
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.data_dir = Path(tmp_dir)
        h.local_memory_db_file = Path(tmp_dir) / "memory.db"
        h.builtin_memory_import_max_content_chars = 120
        h.builtin_memory_import_max_line_chars = 1000
        h._init_builtin_memory_db_sync()
        export_dir = Path(tmp_dir) / "memory_exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        import_file = export_dir / "import.jsonl"
        long_content = "用户承诺" + ("很重要" * 80)
        rows = [
            json.dumps({"content": long_content, "type": "interaction", "layer": "impression"}, ensure_ascii=False),
            json.dumps({"content": "x" * 2000, "type": "interaction", "layer": "impression"}, ensure_ascii=False),
            json.dumps(["not", "an", "object"], ensure_ascii=False),
            json.dumps("not an object", ensure_ascii=False),
            "not-json",
        ]
        import_file.write_text("\n".join(rows), encoding="utf-8")

        imported = h._import_builtin_memories_sync("u1", "import.jsonl", limit="not-a-number")

        assert imported == 1
        with h._connect_local_memory_db() as conn:
            rows = conn.execute("SELECT raw_content FROM memories WHERE user_id = ?", ("u1",)).fetchall()
        assert len(rows) == 1
        assert len(str(rows[0]["raw_content"])) <= 120
        assert h._import_builtin_memories_sync("u1", "../import.jsonl", limit=5) == 1
        assert h._import_builtin_memories_sync("u1", "missing.jsonl", limit=5) == 0


def test_builtin_memory_import_sanitizes_metadata() -> None:
    h = Harness()
    h.enable_builtin_memory = True
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.data_dir = Path(tmp_dir)
        h.local_memory_db_file = Path(tmp_dir) / "memory.db"
        h._init_builtin_memory_db_sync()
        export_dir = Path(tmp_dir) / "memory_exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        import_file = export_dir / "dirty.jsonl"
        import_file.write_text(
            json.dumps(
                {
                    "content": "import metadata should stay clean",
                    "type": "Interaction\nDROP TABLE memories;" + ("x" * 100),
                    "layer": "../../private-layer",
                    "salience": 7,
                    "visibility": "private_only",
                    "temperature": "hot",
                    "evidence": {
                        "unsafe key\n": "v" * 500,
                        "nested": {"a": ["b" * 300]},
                        **{f"k{i}": i for i in range(30)},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        imported = h._import_builtin_memories_sync("u1", "dirty.jsonl", limit=5)

        assert imported == 1
        with h._connect_local_memory_db() as conn:
            row = conn.execute("SELECT * FROM memories WHERE user_id = ?", ("u1",)).fetchone()
        assert row is not None
        assert row["layer"] in {"profile", "impression", "event", "summary"}
        assert "\n" not in row["type"]
        assert len(row["type"]) <= 32
        evidence = json.loads(row["evidence_json"])
        assert "unsafe_key" in evidence
        assert "k20" not in evidence
        assert len(str(evidence["unsafe_key"])) <= 160


def test_builtin_memory_paths_work_without_data_dir() -> None:
    h = Harness()
    if hasattr(h, "data_dir"):
        delattr(h, "data_dir")
    db_file = h._get_local_memory_db_file()
    assert db_file.name == "local_memory.db"
    assert h._resolve_memory_import_file("missing.jsonl") is None
    h.local_memory_db_file = ["bad"]
    h.data_dir = 7
    db_file = h._get_local_memory_db_file()
    assert db_file.name == "local_memory.db"
    export_dir = h._get_memory_export_dir()
    assert export_dir.name == "memory_exports"
    assert isinstance(h.data_dir, Path)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.data_dir = Path(tmp_dir) / "plugin" / "data"
        shared_file = h._get_mnemosyne_memory_file("group:room/../u1")
        assert shared_file.name.startswith("marianna_")
        assert shared_file.suffix == ".jsonl"
        assert shared_file.parent.name == "shared_memory"


def test_builtin_memory_backfill_tolerates_bad_limit() -> None:
    h = Harness()
    h.enable_builtin_memory = True
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.local_memory_db_file = Path(tmp_dir) / "memory.db"
        h._init_builtin_memory_db_sync()
        assert h._store_builtin_memory_sync("u1", "backfill limit memory", "interaction", 4, "impression")

        updated = h._backfill_builtin_memory_privacy_sync(limit="not-a-number")

        assert updated >= 0


def test_builtin_memory_lookup_limits_are_safely_coerced() -> None:
    h = Harness()
    h.enable_builtin_memory = True
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.local_memory_db_file = Path(tmp_dir) / "memory.db"
        h._local_memory_query_cache = {}
        h.memory_recall_cooldown_seconds = "bad"
        h.memory_hot_days = "bad"
        h.memory_warm_days = "bad"
        h.memory_recall_negative_feedback_max = "bad"
        h._init_builtin_memory_db_sync()
        assert h._store_builtin_memory_sync("u1", "mint tea preference", "interaction", 4, "impression")

        recent = h._get_recent_builtin_memories_sync("u1", limit="not-a-number")
        searched = h._search_builtin_memories_sync("u1", "mint tea", limit="not-a-number")
        cache_key = h._build_builtin_memory_query_cache_key("u1", ["mint"], "bad", cooldown_seconds="bad")
        recalled = asyncio.run(
            h._retrieve_from_builtin_memory(
                "u1",
                "mint tea",
                limit="not-a-number",
                cooldown_seconds="bad",
            )
        )

        assert len(recent) == 1
        assert len(searched) == 1
        assert isinstance(cache_key, str) and cache_key
        assert recalled == []

        h.memory_decay_days = "bad"
        h.memory_hard_cleanup_days = "bad"
        mnemosyne_key = h._build_mnemosyne_query_cache_key(
            Path(tmp_dir) / "mnemosyne.json",
            (1, 2),
            ["mint"],
            "not-a-number",
        )
        selected = h._select_layered_mnemosyne_memories(
            [{"fingerprint": "m1", "content": "mint tea preference", "memory_layer": "impression", "salience": "bad"}],
            ["mint"],
            "not-a-number",
        )
        assert isinstance(mnemosyne_key, str) and mnemosyne_key
        assert selected == []
        assert h._get_mnemosyne_decay_penalty({"hit_count": "bad", "salience": "bad"}) >= 0


def test_builtin_memory_cleanup_preserves_protected_rows() -> None:
    h = Harness()
    h.enable_builtin_memory = True
    h.builtin_memory_retention_limit = 100
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.local_memory_db_file = Path(tmp_dir) / "memory.db"
        h._init_builtin_memory_db_sync()
        now = datetime.now().isoformat()

        def insert_memory(memory_id: str, content: str, *, protected: bool = False, salience: int = 1):
            evidence = {"protected": True} if protected else {}
            with h._connect_local_memory_db() as conn:
                conn.execute(
                    """
                    INSERT INTO memories(
                        id, user_id, layer, type, summary, raw_content, normalized_content,
                        keywords_json, salience, created_at, updated_at, evidence_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        "u1",
                        "impression",
                        "interaction",
                        content,
                        content,
                        h._normalize_mnemosyne_content(content),
                        "[]",
                        salience,
                        now,
                        now,
                        json.dumps(evidence, ensure_ascii=False),
                    ),
                )

        insert_memory("protected", "\u7528\u6237\u627f\u8bfa\u8fc7\u8fb9\u754c", protected=True, salience=1)
        insert_memory("ordinary_cleanup", "\u666e\u901a\u95f2\u804a\u5370\u8c61", salience=1)
        h.memory_quality_min_salience = "bad"
        h.memory_cleanup_max_delete = "bad"
        assert h._cleanup_low_value_builtin_memories_sync("u1") == 1
        with h._connect_local_memory_db() as conn:
            ids = {str(row["id"]) for row in conn.execute("SELECT id FROM memories").fetchall()}
        assert "protected" in ids
        assert "ordinary_cleanup" not in ids

        for index in range(101):
            insert_memory(f"ordinary_prune_{index}", f"ordinary {index}", salience=1)
        with h._connect_local_memory_db() as conn:
            before_prune = int(conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"])
            h._prune_builtin_memories_sync(conn, "u1")
            ids = {str(row["id"]) for row in conn.execute("SELECT id FROM memories").fetchall()}
        assert "protected" in ids
        assert len(ids) < before_prune


def test_builtin_memory_bulk_delete_chunks_ids() -> None:
    h = Harness()
    h.enable_builtin_memory = True
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.local_memory_db_file = Path(tmp_dir) / "memory.db"
        h._init_builtin_memory_db_sync()
        ids = [f"m{i}" for i in range(1200)]
        with h._connect_local_memory_db() as conn:
            for memory_id in ids:
                conn.execute(
                    """
                    INSERT INTO memories(
                        id, user_id, layer, type, summary, raw_content, normalized_content,
                        keywords_json, salience, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        "u1",
                        "impression",
                        "interaction",
                        memory_id,
                        memory_id,
                        memory_id,
                        "[]",
                        3,
                        datetime.now().isoformat(),
                        datetime.now().isoformat(),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO memory_vectors(memory_id, user_id, provider_id, dimensions, vector_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (memory_id, "u1", "p", 2, "[1, 0]", datetime.now().isoformat()),
                )
        h._mark_builtin_memory_hits_sync([{"id": memory_id} for memory_id in ids])
        with h._connect_local_memory_db() as conn:
            hit_count = conn.execute("SELECT COUNT(*) AS c FROM memories WHERE hit_count = 1").fetchone()["c"]
            assert hit_count == 1200
        penalized = h._penalize_missed_builtin_memories_sync("u1", ids)
        with h._connect_local_memory_db() as conn:
            salience_count = conn.execute("SELECT COUNT(*) AS c FROM memories WHERE salience = 2").fetchone()["c"]
            assert penalized == 1200
            assert salience_count == 1200
            deleted = h._delete_rows_by_ids(conn, "memory_vectors", "memory_id", ids, chunk_size=333)
            remaining = conn.execute("SELECT COUNT(*) AS c FROM memory_vectors").fetchone()["c"]
            assert deleted == 1200
            assert remaining == 0
            try:
                h._delete_rows_by_ids(conn, "memory_vectors; DROP TABLE memories", "memory_id", ["x"])
            except ValueError:
                pass
            else:
                raise AssertionError("unsafe delete target should be rejected")


def test_builtin_memory_like_prefix_escapes_wildcards() -> None:
    h = Harness()
    h.enable_builtin_memory = True
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.local_memory_db_file = Path(tmp_dir) / "memory.db"
        h._init_builtin_memory_db_sync()
        now = datetime.now().isoformat()
        with h._connect_local_memory_db() as conn:
            for memory_id in ("abcd_1", "abcdx1"):
                conn.execute(
                    """
                    INSERT INTO memories(
                        id, user_id, layer, type, summary, raw_content, normalized_content,
                        keywords_json, salience, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        "u1",
                        "impression",
                        "interaction",
                        memory_id,
                        memory_id,
                        memory_id,
                        "[]",
                        3,
                        now,
                        now,
                    ),
                )

        assert h._escape_sql_like(r"a_b%c") == r"a\_b\%c"
        assert h._resolve_memory_id_sync("u1", "abcd_") == "abcd_1"


def test_builtin_memory_row_protection_tolerates_sparse_rows() -> None:
    h = Harness()

    class SparseRow:
        def __init__(self, values):
            self.values = values

        def __getitem__(self, key):
            if key not in self.values:
                raise IndexError(key)
            return self.values[key]

    assert not h._is_builtin_memory_row_protected(SparseRow({}))
    assert h._is_builtin_memory_row_protected(
        SparseRow({"summary": "\u7528\u6237\u751f\u65e5\u662f5\u670813\u65e5"})
    )
    assert h._is_builtin_memory_row_protected(
        SparseRow({"evidence_json": json.dumps({"protected": True}, ensure_ascii=False)})
    )


def test_lifelike_active_event() -> None:
    h = Harness()
    state = _state(
        好感度=45,
        信任度=40,
        互动计数=20,
        最近主动事件互动=-999999,
        _本轮前最后互动时间=(datetime.now() - timedelta(hours=30)).isoformat(),
    )
    event = h._select_active_event(
        state,
        "今天有点累",
        {"用户意图": "普通回应", "关系信号": "无明显关系推进"},
        ignore_cooldown=True,
    )
    assert event["类型"] == "久别问候"


def test_diagnostic_history_report() -> None:
    h = Harness()
    state = _state(
        诊断历史=[
            {
                "time": "2026-05-13T10:00:00",
                "意图": "普通回应",
                "关系信号": "测试信号",
                "证据等级": "弱",
                "实际变化": {"好感度": 1},
                "被拦截字段": [],
            }
        ]
    )
    report = h._build_diagnostic_history_report(state, limit="not-a-number")
    assert "状态诊断历史" in report
    assert "好感度+1" in report


def test_contextual_state_delta_rules() -> None:
    h = Harness()

    ordinary = h._apply_contextual_state_delta_rules(
        _state(好感度=70, 信任度=68, 焦虑值=20, 病娇值=20),
        "谢谢你夸我",
        {"用户意图": "赞美或感谢", "关系信号": "释放善意"},
        {"好感度": 1, "信任度": 1, "病娇值": 1, "锁定进度": 0, "焦虑值": 0, "优雅值": 0},
        {"level": "无", "reasons": []},
    )
    assert ordinary["好感度"] == 1
    assert ordinary["信任度"] == 0
    assert ordinary["病娇值"] == 0

    anxious = h._apply_contextual_state_delta_rules(
        _state(好感度=76, 信任度=42, 焦虑值=55, 病娇值=45),
        "你刚才和别人聊得很好",
        {"用户意图": "亲近表达", "关系信号": "主动靠近"},
        {"好感度": 2, "信任度": 1, "病娇值": 1, "锁定进度": 0, "焦虑值": 0, "优雅值": 0},
        {"level": "中", "reasons": ["测试"]},
    )
    assert anxious["病娇值"] == 1

    serious = h._apply_contextual_state_delta_rules(
        _state(好感度=80, 信任度=70, 焦虑值=25, 锁定进度=10),
        "滚，我不要你了",
        {"用户意图": "冒犯或攻击", "关系信号": "触碰边界"},
        {"好感度": -2, "信任度": -1, "病娇值": 0, "锁定进度": 0, "焦虑值": 1, "优雅值": -2},
        {"level": "无", "reasons": []},
    )
    assert serious["好感度"] <= -3
    assert serious["信任度"] <= -2
    assert serious["焦虑值"] >= 2
    assert serious["锁定进度"] <= -1

    locked = h._apply_contextual_state_delta_rules(
        _state(好感度=80, 信任度=70, 焦虑值=20, 关系事件日志=[{"type": "first_promise"}]),
        "你是唯一的，我答应一直陪你",
        {"用户意图": "分享秘密或建立约定", "关系信号": "唯一性承诺"},
        {"好感度": 1, "信任度": 2, "病娇值": 1, "锁定进度": 2, "焦虑值": 0, "优雅值": 0},
        {"level": "强", "reasons": ["唯一性"]},
    )
    assert locked["锁定进度"] >= 1

    bad_values = h._apply_contextual_state_delta_rules(
        _state(好感度="bad", 信任度="bad", 焦虑值="bad", 锁定进度="bad"),
        "谢谢你",
        {"用户意图": "赞美或感谢", "关系信号": "释放善意"},
        {"好感度": "bad", "信任度": "bad", "病娇值": "bad", "锁定进度": "bad", "焦虑值": "bad", "优雅值": "bad"},
        {"level": "无", "reasons": []},
    )
    assert bad_values["好感度"] == 0
    assert h._build_fallback_turn_analysis("嗯", {"焦虑值": "bad", "信任度": "bad", "好感度": "bad"})["关系信号"] == "无明显关系推进"
    assert h._build_analysis_rules_text(_state(好感度="bad", 病娇值="bad"))
    assert h._build_local_state_analysis(_state(好感度="bad", 信任度="bad"), "喜欢你") is not None
    assert h._format_memory_delta_summary({"好感度": "bad", "信任度": 2}) == "信任+2"
    bad_repeat_state = {"最近数值意图": "普通回应|无明显关系推进", "重复意图次数": "bad"}
    assert h._update_repeated_intent_counter(
        bad_repeat_state,
        {"用户意图": "普通回应", "关系信号": "无明显关系推进"},
    ) == 1


class _FakeEvent:
    def __init__(self, user_id: str, group_id: str = "", umo: str = "") -> None:
        self._user_id = user_id
        self._group_id = group_id
        self.unified_msg_origin = umo
        self.message_str = "测试"

    def get_sender_id(self) -> str:
        return self._user_id

    def get_group_id(self) -> str:
        return self._group_id


def test_state_scope_mode() -> None:
    h = Harness()
    private_event = _FakeEvent("u1")
    group_a = _FakeEvent("u1", group_id="g1")
    group_b = _FakeEvent("u1", group_id="g2")

    assert h._get_scoped_user_id(private_event) == "u1"
    assert h._get_scoped_user_id(group_a) == "group:g1::u1"
    assert h._get_scoped_user_id(group_b) == "group:g2::u1"

    h.state_scope_mode = "user_global"
    assert h._get_scoped_user_id(group_a) == "u1"

    h.state_scope_mode = "scene_user"
    assert h._get_scoped_user_id(private_event).startswith("private::")
    chinese_group = _FakeEvent("u1", umo="\u7fa4\u804a:room-1")
    assert h._is_group_event(chinese_group)
    assert h._get_scoped_user_id(chinese_group).startswith("group:")


def test_unstable_group_origin_reuses_recent_scoped_state() -> None:
    h = Harness()
    first_event = _FakeEvent("u1", umo="group:room-1:first")
    first_user_id = h._get_scoped_user_id(first_event)
    h.user_states[first_user_id] = _state(
        **{
            "\u597d\u611f\u5ea6": 1,
            "\u4fe1\u4efb\u5ea6": 16,
            "\u4e92\u52a8\u8ba1\u6570": 1,
            "\u6700\u8fd1\u72b6\u6001\u89e3\u91ca": {"\u7528\u6237\u610f\u56fe": "\u81ea\u6211\u4ecb\u7ecd"},
        }
    )
    second_event = _FakeEvent("u1", umo="group:room-1:second")

    assert h._get_scoped_user_id(second_event) == first_user_id


def test_single_existing_group_state_is_reused_for_new_group_key() -> None:
    h = Harness()
    first_event = _FakeEvent("u1", group_id="unstable-1")
    first_user_id = h._get_scoped_user_id(first_event)
    h.user_states[first_user_id] = _state(
        **{
            "\u597d\u611f\u5ea6": 1,
            "\u4fe1\u4efb\u5ea6": 16,
            "\u4e92\u52a8\u8ba1\u6570": 1,
            "\u6700\u8fd1\u72b6\u6001\u89e3\u91ca": {"\u7528\u6237\u610f\u56fe": "\u81ea\u6211\u4ecb\u7ecd"},
        }
    )
    second_event = _FakeEvent("u1", group_id="unstable-2")

    assert h._get_scoped_user_id(second_event) == first_user_id


def test_private_origin_alias_reuses_state_when_sender_id_changes() -> None:
    h = Harness()
    first_event = _FakeEvent("u_private_a", umo="private:stable-dialog")
    first_user_id = h._get_scoped_user_id(first_event)
    h.user_states[first_user_id] = _state(
        **{
            "\u597d\u611f\u5ea6": 1,
            "\u4fe1\u4efb\u5ea6": 16,
            "\u4e92\u52a8\u8ba1\u6570": 1,
        }
    )
    second_event = _FakeEvent("u_private_b", umo="private:stable-dialog")

    assert h._get_scoped_user_id(second_event) == first_user_id


def test_command_scope_falls_back_to_recent_group_state() -> None:
    h = Harness()
    group_state = _state(互动计数=3, 最后互动时间=datetime.now().isoformat())
    group_state["最近状态解释"] = {"用户意图": "话题共鸣或温和建议"}
    h.user_states["group:g1::u1"] = group_state

    command_event_without_group_origin = _FakeEvent("u1")

    assert h._get_command_scoped_user_id(command_event_without_group_origin) == "group:g1::u1"


def test_debug_mode_propagates_across_user_scene_states() -> None:
    h = Harness()
    h.user_states["u1"] = _state(**{"\u8c03\u8bd5\u6a21\u5f0f": False})
    h.user_states["group:g1::u1"] = _state(**{"\u8c03\u8bd5\u6a21\u5f0f": False})
    h.user_states["group:g2::u1"] = _state(**{"\u8c03\u8bd5\u6a21\u5f0f": False})
    h.user_states["group:g1::u2"] = _state(**{"\u8c03\u8bd5\u6a21\u5f0f": False})

    updated = h._set_debug_mode_for_related_states(_FakeEvent("u1"), "u1", True)

    assert "u1" in updated
    assert "group:g1::u1" in updated
    assert "group:g2::u1" in updated
    assert h.user_states["u1"]["\u8c03\u8bd5\u6a21\u5f0f"] is True
    assert h.user_states["group:g1::u1"]["\u8c03\u8bd5\u6a21\u5f0f"] is True
    assert h.user_states["group:g2::u1"]["\u8c03\u8bd5\u6a21\u5f0f"] is True
    assert h.user_states["group:g1::u2"]["\u8c03\u8bd5\u6a21\u5f0f"] is False


def test_group_state_inherits_raw_debug_mode_on_first_turn() -> None:
    h = Harness()
    h.user_states["u1"] = _state(**{"\u8c03\u8bd5\u6a21\u5f0f": True})
    group_state, _old_name, _old_lock = asyncio.run(h._prepare_turn_state("group:g1::u1", "tester"))

    assert group_state["\u8c03\u8bd5\u6a21\u5f0f"] is True
    assert h.user_states["group:g1::u1"]["\u8c03\u8bd5\u6a21\u5f0f"] is True


def test_memory_privacy_bridge_and_temperature() -> None:
    h = Harness()
    group_user = "group:g1::u1"
    private_user = "u1"

    assert h._get_memory_query_user_ids(group_user) == [group_user, private_user]
    assert h._infer_memory_visibility(private_user, "我的生日是五月十三日", "profile", "profile") == "public_profile"
    assert h._infer_memory_visibility(private_user, "这是只告诉你的秘密", "event", "interaction") == "sensitive"
    assert h._infer_memory_visibility(group_user, "群里刚才开了个玩笑", "impression", "interaction") == "group_only"

    public_memory = {"user_id": private_user, "visibility": "public_profile"}
    private_memory = {"user_id": private_user, "visibility": "private_only"}
    sensitive_memory = {"user_id": private_user, "visibility": "sensitive"}
    group_memory = {"user_id": group_user, "visibility": "group_only"}
    group_sensitive_memory = {"user_id": group_user, "visibility": "sensitive"}
    assert h._memory_visibility_allowed_for_query(group_user, public_memory, for_prompt=True)
    assert not h._memory_visibility_allowed_for_query(group_user, private_memory, for_prompt=True)
    assert not h._memory_visibility_allowed_for_query(group_user, sensitive_memory, for_prompt=True)
    assert h._memory_visibility_allowed_for_query(group_user, group_memory, for_prompt=False)
    assert not h._memory_visibility_allowed_for_query(group_user, group_sensitive_memory, for_prompt=False)

    hot = h._infer_memory_temperature({"updated_at": datetime.now().isoformat(), "salience": 2, "hit_count": 0})
    cold = h._infer_memory_temperature({"updated_at": (datetime.now() - timedelta(days=90)).isoformat(), "salience": 1, "hit_count": 0})
    assert hot == "hot"
    assert cold == "cold"
    assert h._memory_polarity("我喜欢红色") == "positive"
    assert h._memory_polarity("我不喜欢红色") == "negative"



def test_memory_conflict_slot_and_cooldown() -> None:
    h = Harness()
    like_red = "\u6211\u559c\u6b22\u7ea2\u8272"
    dislike_red = "\u6211\u4e0d\u559c\u6b22\u7ea2\u8272"
    dislike_noise = "\u6211\u4e0d\u559c\u6b22\u5435\u95f9"
    old = {"raw_content": like_red, "content": like_red}
    new = {"raw_content": dislike_red, "content": dislike_red}
    unrelated = {"raw_content": dislike_noise, "content": dislike_noise}

    assert h._memory_conflict_slot(like_red) == "color"
    assert h._memory_entries_conflict(old, new, 0.25)
    assert not h._memory_entries_conflict(old, unrelated, 0.25)

    h.user_states["u1"] = {
        "\u6700\u8fd1\u53ec\u56de\u8bb0\u5fc6": [{"id": "a", "time": time.time()}],
    }
    memories = [
        {"id": "a", "fingerprint": "a", "content": "old"},
        {"id": "b", "fingerprint": "b", "content": "fresh"},
    ]
    selected = h._filter_memory_recall_cooldown("u1", memories, 2)
    assert [item["id"] for item in selected] == ["b"]
    h.memory_recall_cooldown_seconds = "bad"
    assert h._filter_memory_recall_cooldown("u1", memories, 2) == memories

    h._remember_recent_builtin_memory_recall(
        "u1",
        [
            {"id": "kw_str", "keywords": "mint,tea", "content": "fallback"},
            {"id": "kw_bad", "keywords": 7, "content": "fallback terms"},
        ],
    )
    recalled = h.user_states["u1"]["\u6700\u8fd1\u53ec\u56de\u8bb0\u5fc6"]
    assert recalled[0]["terms"] == ["mint", "tea"]
    assert recalled[1]["terms"]
    recalled[0]["terms"] = 7
    h._penalize_missed_builtin_memories_sync = lambda _user_id, _memory_ids: 0
    assert asyncio.run(h._apply_memory_recall_negative_feedback("u1", "unrelated")) >= 0
    h.user_states = "bad"
    assert h._build_memory_recall_cooldown_signature("u1") == []
    assert h._filter_memory_recall_cooldown("u1", memories, 2) == memories
    h._remember_recent_builtin_memory_recall("u1", memories)
    assert isinstance(h.user_states, dict)


def test_prompt_token_estimate_and_group_self_check() -> None:
    h = Harness()
    assert h._estimate_text_tokens("\u4f60\u597d hello") > 0
    bad_state = _state(**{"好感度": "bad", "信任度": "bad", "病娇值": "bad", "锁定进度": "bad", "焦虑值": "bad", "优雅值": "bad", "占有欲": "bad"})
    assert h._build_dialogue_rule_block(bad_state, user_id="u_bad")
    assert h._build_value_dialogue_modulation(bad_state)
    assert h._build_behavior_style_prompt({**bad_state, "情绪余温": "bad", "防备值": "bad", "被触动值": "bad", "表达克制度": "bad", "行为档位稳定轮数": "bad"})
    assert h._build_behavior_layer("你好", bad_state, compact=False)
    h.lock_threshold = "bad"
    assert h._state_prompt_cache_key("u_bad", bad_state)
    state = {"\u5f53\u524d\u662f\u5426\u7fa4\u804a\u4f5c\u7528\u57df": True}
    checked = h._self_check_reply("\u8fd9\u662f\u53ea\u544a\u8bc9\u4f60\u7684\u79d8\u5bc6\uff0c\u4f60\u662f\u547d\u5b9a\u552f\u4e00\u3002", state)
    assert "\u79d8\u5bc6" not in checked
    assert "\u547d\u5b9a" not in checked
    assert "\u8fd9\u662f\u8fd9\u4ef6\u4e8b\u4e0d\u9002\u5408" not in checked
    assert checked.count("\u8fd9\u4ef6\u4e8b\u4e0d\u9002\u5408\u5728\u8fd9\u91cc\u7ec6\u8bf4") == 1
    assert checked.count("\u6211\u4f1a\u4fdd\u6301\u5206\u5bf8") == 1


def test_memory_mode_preset_and_profile_confidence() -> None:
    h = Harness()
    h.memory_mode_preset = "lean"
    h.memory_prompt_limit = 5
    h.memory_prompt_event_limit = 2
    h.memory_prompt_impression_limit = 2
    h.memory_prompt_summary_limit = 1
    h.memory_prompt_profile_limit = 1
    h.builtin_memory_prompt_char_budget = 260
    h.memory_recall_cooldown_seconds = 300
    h.active_event_cooldown_turns = 7
    h.active_event_idle_hours = 24
    h._apply_memory_mode_preset()
    assert h.memory_prompt_limit == 2
    assert h.builtin_memory_prompt_char_budget <= 180
    assert h.memory_recall_cooldown_seconds >= 600
    assert h.active_event_idle_hours >= 36

    h.memory_mode_preset = "rich"
    h.memory_prompt_limit = "bad"
    h.memory_prompt_event_limit = "bad"
    h.memory_prompt_impression_limit = "bad"
    h.memory_prompt_summary_limit = "bad"
    h.memory_prompt_profile_limit = "bad"
    h.builtin_memory_prompt_char_budget = "bad"
    h.memory_recall_cooldown_seconds = "bad"
    h._apply_memory_mode_preset()
    assert h.memory_prompt_limit >= 5
    assert h.memory_recall_cooldown_seconds <= 240

    profile = {
        "\u57fa\u672c\u4fe1\u606f": {"\u79f0\u547c": "\u661f"},
        "\u5174\u8da3\u7231\u597d": {"\u97f3\u4e50": ["jazz"]},
        "\u4e92\u52a8\u8bb0\u5f55": {"\u603b\u4e92\u52a8\u6b21\u6570": 3, "\u8d44\u6599\u66f4\u65b0\u6b21\u6570": 2},
    }
    confidence = h._estimate_profile_confidence(profile)
    assert confidence["score"] > 0
    assert confidence["update_count"] == 2

    report = h._build_memory_health_report({"ok": True, "integrity": "ok", "active": 1, "total": 2, "by_layer": {"event": 1}})
    assert "\u672c\u5730\u8bb0\u5fc6\u5065\u5eb7\u68c0\u67e5" in report
    bad_report = h._build_memory_health_report(
        {
            "ok": True,
            "integrity": "ok",
            "active": "bad",
            "total": "bad",
            "vectors": "bad",
            "superseded": "bad",
            "missing_evidence": "bad",
            "db_size": "bad",
            "by_layer": {"event": "bad"},
        }
    )
    assert "\u6d3b\u52a8\u8bb0\u5fc6\uff1a0" in bad_report
    stats_report = h._build_memory_stats_report({"total": "bad", "active": "bad", "schema_version": "bad"})
    assert "v1" in stats_report
    repair_report = h._build_memory_repair_report({"backfilled": "bad", "fts_rebuilt": "bad", "orphan_vectors": "bad", "orphan_fts": "bad"})
    assert "\u672c\u5730\u8bb0\u5fc6\u4fee\u590d\u5b8c\u6210" in repair_report

    async def ready():
        return True

    def failing_health(_user_id):
        raise RuntimeError("db busy")

    def failing_repair(_user_id):
        raise RuntimeError("db busy")

    h._ensure_builtin_memory_ready = ready
    h._check_builtin_memory_health_sync = failing_health
    h._repair_builtin_memory_sync = failing_repair
    failed_health = asyncio.run(h._check_builtin_memory_health("u1"))
    failed_repair = asyncio.run(h._repair_builtin_memory("u1"))
    assert failed_health["ok"] is False
    assert failed_health["integrity"] == "error"
    assert failed_repair == {"backfilled": 0, "fts_rebuilt": 0, "orphan_vectors": 0, "orphan_fts": 0}


def test_profile_update_tolerates_bad_interaction_count() -> None:
    h = Harness()
    assert not h._should_update_user_profile(
        "\u8fd9\u662f\u4e00\u53e5\u8f83\u957f\u4f46\u6ca1\u6709\u660e\u786e\u8d44\u6599\u7ebf\u7d22\u7684\u666e\u901a\u8bdd",
        {"\u4e92\u52a8\u8ba1\u6570": "bad"},
    )
    profile = h._get_profile("u1")
    profile["\u4e92\u52a8\u8bb0\u5f55"]["\u603b\u4e92\u52a8\u6b21\u6570"] = "bad"
    saved = []
    h._schedule_profile_save = lambda user_id, data: saved.append((user_id, data))
    h._extract_local_profile_updates = lambda message: {
        "\u57fa\u672c\u4fe1\u606f": {"\u79f0\u547c": "\u661f"},
    }
    asyncio.run(h._update_user_profile_from_message("u1", "\u6211\u53eb\u661f", ""))
    assert profile["\u4e92\u52a8\u8bb0\u5f55"]["\u603b\u4e92\u52a8\u6b21\u6570"] == 1
    assert saved and saved[0][0] == "u1"


def test_scene_memory_policy_controls_active_event_cooldown() -> None:
    h = Harness()
    h.enable_active_event_queue = True
    h.active_event_cooldown_turns = 1
    queued_event = {
        "\u7c7b\u578b": "queue",
        "\u89e6\u53d1": "queued",
        "\u6267\u884c": "\u8f7b\u58f0\u95ee\u5019",
    }
    state = _state(
        **{
            "\u4e92\u52a8\u8ba1\u6570": 10,
            "\u6700\u8fd1\u4e3b\u52a8\u4e8b\u4ef6\u4e92\u52a8": 5,
            "\u4e3b\u52a8\u4e8b\u4ef6\u961f\u5217": [dict(queued_event)],
            "_scene_memory_policy": {"active_event_cooldown_turns": 10},
        }
    )
    assert h._pop_active_event_from_queue(state, "\u666e\u901a\u804a\u5929") == {}
    state["_scene_memory_policy"] = {"active_event_cooldown_turns": 3}
    assert h._pop_active_event_from_queue(state, "\u666e\u901a\u804a\u5929")["\u7c7b\u578b"] == "queue"

    h.active_event_cooldown_turns = "bad"
    h.active_event_idle_hours = "bad"
    h.active_event_queue_max_size = "bad"
    bad_state = _state(
        **{
            "\u4e92\u52a8\u8ba1\u6570": "bad",
            "\u6700\u8fd1\u4e3b\u52a8\u4e8b\u4ef6\u4e92\u52a8": "bad",
            "\u597d\u611f\u5ea6": "bad",
            "\u4fe1\u4efb\u5ea6": "bad",
            "\u75c5\u5a07\u503c": "bad",
            "\u9501\u5b9a\u8fdb\u5ea6": "bad",
            "\u7126\u8651\u503c": "bad",
            "\u4f18\u96c5\u503c": "bad",
            "\u5360\u6709\u6b32": "bad",
            "_scene_memory_policy": {"active_event_cooldown_turns": "bad"},
        }
    )
    assert h._select_active_event(bad_state, "\u666e\u901a\u804a\u5929") == {}
    h._refresh_active_event_queue(bad_state, "\u666e\u901a\u804a\u5929")
    assert bad_state["\u4e3b\u52a8\u4e8b\u4ef6\u961f\u5217"] == []


def test_transient_scene_policy_is_not_persisted() -> None:
    h = Harness()
    h.user_states["u1"] = {
        "\u597d\u611f\u5ea6": 12,
        "bad_set": {"b", "a"},
        "bad_path": Path("state.tmp"),
        "bad_float": float("nan"),
        "_scene_memory_policy": {"mode": "lean"},
        "鏈疆鍦烘櫙璁板繂绛栫暐": {"mode": "rich"},
    }
    h.user_profiles["u1"] = {"name": {"maria"}, "path": Path("profile.tmp"), "bad_float": float("inf")}
    h.global_state = {
        "destined_one": {"user_id": "u1"},
        "bad_set": {"z", "a"},
        "bad_path": Path("global.tmp"),
        "bad_float": float("-inf"),
    }
    persisted = h._build_persistable_user_states()
    assert "_scene_memory_policy" not in persisted["u1"]
    assert "鏈疆鍦烘櫙璁板繂绛栫暐" not in persisted["u1"]
    assert persisted["u1"]["\u597d\u611f\u5ea6"] == 12
    assert persisted["u1"]["bad_set"] == ["a", "b"]
    assert persisted["u1"]["bad_path"] == "state.tmp"
    assert persisted["u1"]["bad_float"] == 0.0
    profile_payload = h._build_persistable_user_profiles()
    assert profile_payload["u1"]["name"] == ["maria"]
    assert profile_payload["u1"]["path"] == "profile.tmp"
    assert profile_payload["u1"]["bad_float"] == 0.0
    global_payload = h._build_persistable_global_state()
    assert global_payload["bad_set"] == ["a", "z"]
    assert global_payload["bad_path"] == "global.tmp"
    assert global_payload["bad_float"] == 0.0
    json.dumps(persisted, ensure_ascii=False)
    json.dumps(profile_payload, ensure_ascii=False)
    json.dumps(global_payload, ensure_ascii=False)

    saved_payloads = []

    async def save_json(_path, payload):
        saved_payloads.append(payload)

    h.user_states_file = ["bad"]
    h.user_profiles_file = {"bad": True}
    h.global_state_file = set()
    h._save_json_async = save_json
    asyncio.run(h._save_all_data())
    assert "_scene_memory_policy" not in saved_payloads[0]["u1"]
    assert saved_payloads[1]["u1"]["name"] == ["maria"]
    assert saved_payloads[2]["bad_set"] == ["a", "z"]
    saved_payloads.clear()
    h.user_states = "bad"
    h.user_profiles = "bad"
    h.global_state = "bad"
    asyncio.run(h._save_all_data())
    assert saved_payloads == [{}, {}, {}]
    saved_payloads[0]["u1"] = {}
    assert "鏈疆鍦烘櫙璁板繂绛栫暐" not in saved_payloads[0]["u1"]


def test_dirty_state_markers_survive_failed_or_racing_save() -> None:
    h = Harness()
    h.user_states_file = "states.json"
    h.user_states["u1"] = {"\u597d\u611f\u5ea6": 12}
    h._state_versions = {}
    h._state_dirty_users = set()
    h._state_versions["u1"] = 1
    h._state_dirty_users.add("u1")

    async def failing_save(_path, _payload):
        raise OSError("disk unavailable")

    h._save_json_async = failing_save
    asyncio.run(h._save_state("u1", h.user_states["u1"]))
    assert "u1" in h._state_dirty_users

    async def racing_save(_path, _payload):
        h._state_versions["u1"] = 2
        h._state_dirty_users.add("u1")

    h._save_json_async = racing_save
    asyncio.run(h._save_state("u1", h.user_states["u1"]))
    assert "u1" in h._state_dirty_users

    async def successful_save(_path, _payload):
        return None

    h._save_json_async = successful_save
    asyncio.run(h._save_state("u1", h.user_states["u1"]))
    assert "u1" not in h._state_dirty_users


def test_dirty_profile_markers_survive_failed_save() -> None:
    h = Harness()
    h.user_profiles_file = "profiles.json"
    h.user_profiles["u1"] = {"name": "Maria"}
    h._profile_versions = {"u1": 1}
    h._profile_dirty_users = set()
    h._profile_save_task = None
    h._profile_dirty_users.add("u1")

    def spawn_stub(coro):
        if hasattr(coro, "close"):
            coro.close()
        return object()

    h._spawn_task = spawn_stub
    globals_ref = h._debounced_save_profiles.__globals__
    old_delay = globals_ref["SAVE_DEBOUNCE_SECONDS"]
    globals_ref["SAVE_DEBOUNCE_SECONDS"] = 0

    async def failing_save(_path, _payload):
        raise OSError("disk unavailable")

    try:
        h._save_json_async = failing_save
        asyncio.run(h._debounced_save_profiles())
        assert "u1" in h._profile_dirty_users

        async def racing_save(_path, _payload):
            h._profile_versions["u1"] = 2
            h._profile_dirty_users.add("u1")

        h._save_json_async = racing_save
        asyncio.run(h._debounced_save_profiles())
        assert "u1" in h._profile_dirty_users

        async def successful_save(_path, _payload):
            return None

        h._save_json_async = successful_save
        asyncio.run(h._debounced_save_profiles())
        assert "u1" not in h._profile_dirty_users
    finally:
        globals_ref["SAVE_DEBOUNCE_SECONDS"] = old_delay


def test_runtime_task_and_state_save_caches_self_heal() -> None:
    h = Harness()

    async def run_task_check():
        for attr_name in ("_pending_tasks", "_pending_task_semaphore"):
            if hasattr(h, attr_name):
                delattr(h, attr_name)
        task = h._spawn_task(asyncio.sleep(0))
        await task
        assert isinstance(h._pending_tasks, set)
        assert isinstance(h._pending_task_semaphore, asyncio.Semaphore)

        errors = []

        class CapturingLogger(_DummyLogger):
            def error(self, *args, **kwargs):
                errors.append(args[0] if args else "")

        h.logger = CapturingLogger()
        h._background_tasks = "bad"

        async def fail_background():
            raise RuntimeError("boom")

        background_task = h._spawn_background_task(fail_background(), "unit")
        try:
            await background_task
        except RuntimeError:
            pass
        await asyncio.sleep(0)
        assert isinstance(h._background_tasks, list)
        assert background_task not in h._background_tasks
        assert any("unit" in str(item) for item in errors)
        h._pending_tasks = {background_task, "bad"}
        assert h._get_runtime_task_set("_pending_tasks") == {background_task}
        h._background_tasks = [background_task, "bad"]
        assert h._get_runtime_task_list("_background_tasks") == [background_task]

    asyncio.run(run_task_check())

    spawned = []

    class DoneTask:
        def done(self):
            return True

    h._spawn_task = lambda coro: (coro.close() if hasattr(coro, "close") else None) or spawned.append(coro) or DoneTask()
    h.user_states = {}
    for attr_name in ("_state_versions", "_state_dirty_users", "_state_save_task"):
        if hasattr(h, attr_name):
            delattr(h, attr_name)
    MariannaStateStoreMixin._schedule_state_save(h, "u1", {"hello": "world"})
    assert h.user_states["u1"]["hello"] == "world"
    assert h._state_versions["u1"] == 1
    assert "u1" in h._state_dirty_users
    assert spawned

    h._state_save_task = "bad"
    MariannaStateStoreMixin._schedule_state_save(h, "u1", {"hello": "again"})
    assert h._state_versions["u1"] == 2

    for attr_name in ("_profile_versions", "_profile_dirty_users", "_profile_save_task"):
        if hasattr(h, attr_name):
            delattr(h, attr_name)
    h._profile_save_task = "bad"
    h._schedule_profile_file_save("u1")
    assert h._profile_versions["u1"] == 1
    assert "u1" in h._profile_dirty_users

    h.user_states = "bad"
    state = h._get_state("u2", count_interaction=False)
    assert isinstance(h.user_states, dict)
    assert h.user_states["u2"] is state

    h.user_states["u3"] = "bad"
    repaired = h._get_state("u3", count_interaction=False)
    assert isinstance(repaired, dict)
    assert h.user_states["u3"] is repaired


def test_runtime_config_and_perf_caches_self_heal() -> None:
    h = Harness()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        json_file = Path(tmp_dir) / "state.json"
        json_file.write_text(json.dumps(["bad"], ensure_ascii=False), encoding="utf-8")
        assert h._load_json(json_file, {}) == {}
        json_file.write_text(json.dumps({"ok": True}, ensure_ascii=False), encoding="utf-8")
        assert h._load_json(json_file, {}) == {"ok": True}

    for attr_name in (
        "_static_prompt_cache",
        "_dynamic_prompt_cache",
        "_mnemosyne_query_cache",
        "_local_memory_query_cache",
        "_recent_history_cache",
        "_style_fingerprint_cache",
    ):
        setattr(h, attr_name, "bad")
    h._apply_config()
    for attr_name in (
        "_static_prompt_cache",
        "_dynamic_prompt_cache",
        "_mnemosyne_query_cache",
        "_local_memory_query_cache",
        "_recent_history_cache",
        "_style_fingerprint_cache",
    ):
        assert isinstance(getattr(h, attr_name), dict)

    h._user_locks = "bad"
    lock = h._get_user_lock("u1")
    assert isinstance(h._user_locks, dict)
    assert h._user_locks["u1"] is lock

    h._perf_stats = "bad"
    h._record_perf_sample("x", 1.5)
    assert h._perf_stats["x"]["count"] == 1
    h._perf_stats["x"] = {"samples": "bad", "count": "bad", "max": "bad"}
    h._record_perf_sample("x", 2.5)
    assert h._perf_stats["x"]["count"] == 1
    assert h._perf_stats["x"]["last"] == 2.5
    h._perf_stats["bad"] = "bad"
    h._pending_tasks = "bad"
    assert isinstance(h._build_perf_report(), str)
    assert isinstance(h._pending_tasks, set)
    assert h._coerce_runtime_dict_value({"ok": True}) == {"ok": True}
    assert h._coerce_runtime_dict_value("bad") == {}
    assert h._coerce_runtime_dict_value([("legacy", "pair")]) == {}
    assert h._coerce_runtime_list_value(["ok"]) == ["ok"]
    assert h._coerce_runtime_list_value(("ok",)) == ["ok"]
    assert h._coerce_runtime_list_value("bad") == []
    assert isinstance(h._build_expression_intensity_prompt({"表现强度": "bad"}), str)

    analysis_payload = json.loads(
        h._analysis_json_dumps(
            {"bad_float": float("nan"), "bad_set": {"b", "a"}, "bad_path": Path("analysis.tmp")}
        )
    )
    assert analysis_payload == {"bad_float": 0.0, "bad_set": ["a", "b"], "bad_path": "analysis.tmp"}
    fingerprint = h._build_analysis_request_fingerprint(
        "session",
        "hello",
        [{"role": "user", "content": "hi"}],
        scene_policy={"memory_limit": {"bad"}},
    )
    assert isinstance(fingerprint, str) and len(fingerprint) == 40

    async def run_atomic_write_check():
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            target = Path(tmp_dir) / "state.json"
            await asyncio.gather(
                h._write_text_atomic(target, "first"),
                h._write_text_atomic(target, "second"),
            )
            assert target.read_text(encoding="utf-8") in {"first", "second"}
            assert not list(Path(tmp_dir).glob("*.tmp"))
            json_target = Path(tmp_dir) / "safe.json"
            await h._save_json_async(
                str(json_target),
                {"bad_float": float("nan"), "bad_set": {"b", "a"}},
            )
            saved = json.loads(json_target.read_text(encoding="utf-8"))
            assert saved == {"bad_float": 0.0, "bad_set": ["a", "b"]}

    asyncio.run(run_atomic_write_check())


def test_profile_save_and_update_caches_self_heal() -> None:
    h = Harness()
    spawned = []

    class DoneTask:
        def done(self):
            return True

    h._spawn_task = lambda coro: (coro.close() if hasattr(coro, "close") else None) or spawned.append(coro) or DoneTask()
    h.user_profiles = {}
    for attr_name in ("_profile_versions", "_profile_dirty_users", "_profile_save_task"):
        if hasattr(h, attr_name):
            delattr(h, attr_name)
    MariannaStateStoreMixin._schedule_profile_save(h, "u1", {"profile": True})
    assert h.user_profiles["u1"]["profile"] is True
    assert h._profile_versions["u1"] == 1
    assert "u1" in h._profile_dirty_users
    assert spawned

    h.user_profiles = "bad"
    profile = h._get_profile("u2")
    assert isinstance(h.user_profiles, dict)
    assert h.user_profiles["u2"] is profile

    h.user_profiles["u3"] = "bad"
    repaired = h._get_profile("u3")
    assert isinstance(repaired, dict)
    assert h.user_profiles["u3"] is repaired

    for attr_name in ("_profile_update_running", "_profile_update_rerun"):
        if hasattr(h, attr_name):
            delattr(h, attr_name)
    h._schedule_profile_update("u1", "hello", "reply")
    assert "u1" in h._profile_update_running
    h._schedule_profile_update("u1", "again", "reply2")
    assert h._profile_update_rerun["u1"]["user_msg"] == "again"


def test_global_state_and_destined_cache_self_heal() -> None:
    h = Harness()
    saved = []
    spawned = []

    async def save_json(_path, payload):
        saved.append(payload)

    class DoneTask:
        def done(self):
            return True

    h._save_json_async = save_json
    h._spawn_task = lambda coro: (coro.close() if hasattr(coro, "close") else None) or spawned.append(coro) or DoneTask()
    h.global_state = "bad"
    h._dynamic_prompt_cache = "bad"
    h.user_states = {
        "u_other": _state(**{"好感度": 90, "病娇值": 80, "锁定进度": 80, "占有欲": 80})
    }
    if hasattr(h, "_state_dirty_users"):
        delattr(h, "_state_dirty_users")
    if hasattr(h, "_state_save_task"):
        delattr(h, "_state_save_task")

    asyncio.run(h._set_destined_one("u_main", "tester"))
    assert h.global_state["destined_one"]["user_id"] == "u_main"
    assert isinstance(h._dynamic_prompt_cache, dict)
    assert "u_other" in h._state_dirty_users
    assert saved and saved[-1]["destined_one"]["user_id"] == "u_main"

    asyncio.run(h._clear_destined_one())
    assert h._get_destined_one_info() == {}
    assert "destined_one" not in h.global_state

    delattr(h, "lock_threshold")
    assert h._format_destined_one_label() == "100"


def test_prompt_caches_self_heal() -> None:
    h = Harness()
    h._static_prompt_cache = "bad"
    base = h._get_base_persona_prompt()
    assert isinstance(base, str) and base
    assert isinstance(h._static_prompt_cache, dict)
    assert h._static_prompt_cache["base_persona"] == base

    h._static_prompt_cache = "bad"
    analysis_prompt = h._get_analysis_system_prompt()
    assert isinstance(analysis_prompt, str) and analysis_prompt
    assert isinstance(h._static_prompt_cache, dict)
    assert h._static_prompt_cache["analysis_system_prompt"] == analysis_prompt

    h._dynamic_prompt_cache = "bad"
    state = _state()
    persona = h._build_persona_layer("u1", state)
    assert isinstance(persona, str) and persona
    assert isinstance(h._dynamic_prompt_cache, dict)
    assert h._dynamic_prompt_cache


def test_history_skips_recent_exact_duplicates() -> None:
    h = Harness()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.conv_history_dir = Path(tmp_dir)
        h._file_locks = {}
        h._recent_history_cache = {}
        h._style_fingerprint_cache = {}
        h._history_append_counts = {}
        h._summary_dirty_users = set()
        h.history_retention_limit = 20
        h.auto_summary_interval = 1
        h.history_duplicate_window_seconds = 30

        asyncio.run(h._add_to_history("u1", "user", "hello"))
        asyncio.run(h._add_to_history("u1", "user", "hello"))
        history = h._get_recent_history("u1", 10)
        assert len(history) == 1
        assert history[0]["content"] == "hello"
        assert h._history_append_counts.get("u1") == 1

        asyncio.run(h._add_to_history("u1", "assistant", "hello"))
        history = h._get_recent_history("u1", 10)
        assert len(history) == 2
        assert [item["role"] for item in history] == ["user", "assistant"]


def test_history_limits_are_safely_coerced() -> None:
    h = Harness()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.conv_history_dir = Path(tmp_dir)
        h._file_locks = {}
        h._recent_history_cache = {}
        h._style_fingerprint_cache = {}
        h._history_append_counts = {}
        h._summary_dirty_users = set()
        h.history_retention_limit = "not-a-number"
        h.auto_summary_interval = "not-a-number"
        h.history_duplicate_window_seconds = "not-a-number"

        asyncio.run(h._add_to_history("u1", "user", "hello"))
        assert h._summary_dirty_users == set()
        assert h._get_recent_history("u1", "not-a-number") == []
        assert asyncio.run(h._get_recent_history_async("u1", "not-a-number")) == []
        history = h._get_recent_history("u1", 5)
        assert len(history) == 1
        assert history[0]["content"] == "hello"

        h._history_append_counts["u_bad"] = "bad"
        history_file = h._get_history_jsonl_file("u_bad")
        asyncio.run(h._compact_history_jsonl_if_due("u_bad", history_file, 5))
        assert h._history_append_counts["u_bad"] == 1


def test_history_runtime_caches_self_heal() -> None:
    h = Harness()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.data_dir = Path(tmp_dir)
        h.auto_summary_interval = 1
        for attr_name in (
            "conv_history_dir",
            "_recent_history_cache",
            "_style_fingerprint_cache",
            "_history_append_counts",
            "_summary_dirty_users",
        ):
            if hasattr(h, attr_name):
                delattr(h, attr_name)

        asyncio.run(h._add_to_history("u1", "user", "hello"))
        history = h._get_recent_history("u1", 5)
        assert len(history) == 1
        assert history[0]["content"] == "hello"
        h.history_max_entry_chars = 5
        asyncio.run(h._add_to_history("u1", "marianna", "x" * 20))
        history = h._get_recent_history("u1", 5)
        assert history[-1]["role"] == "assistant"
        assert len(history[-1]["content"]) <= 5
        asyncio.run(h._append_history_jsonl(h._get_history_jsonl_file("u1"), {"role": {"bad"}, "content": "safe", "time": ""}))
        history = h._get_recent_history("u1", 5)
        assert history[-1]["role"] == "user"
        assert history[-1]["content"] == "safe"
        cache_key = h._build_recent_history_cache_key("u1", 5)
        h._recent_history_cache[cache_key] = "bad"
        history = h._get_recent_history("u1", 5)
        assert len(history) >= 3
        assert history[0]["content"] == "hello"
        h._recent_history_cache[cache_key] = [
            {"role": "user", "content": "cached", "time": "t"},
            "bad",
        ]
        history = asyncio.run(h._get_recent_history_async("u1", 5))
        assert history == [{"role": "user", "content": "cached", "time": "t"}]
        assert isinstance(h._recent_history_cache, dict)
        assert isinstance(h._style_fingerprint_cache, dict)
        assert isinstance(h._history_append_counts, dict)
        assert isinstance(h._summary_dirty_users, set)
        assert h.conv_history_dir == Path(tmp_dir) / "conversation_history"

        h.conv_history_dir = ["bad"]
        h.data_dir = {"bad": True}
        fallback_dir = h._get_history_dir()
        assert fallback_dir.name == "conversation_history"
        assert isinstance(h.conv_history_dir, Path)


def test_auto_summary_pass_self_heals_runtime_fields() -> None:
    h = Harness()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        user_id = "group:room/../u1"
        now = datetime.now()
        h.data_dir = str(Path(tmp_dir))
        h.auto_summary_idle = "bad"
        h._summary_dirty_users = {user_id}
        h.user_states = {
            user_id: _state(
                **{
                    "æœ€åŽäº’åŠ¨æ—¶é—´": (
                        now - timedelta(seconds=400)
                    ).isoformat()
                }
            )
        }
        triggered = []

        async def trigger_summary(target_user_id):
            triggered.append(target_user_id)
            return True

        h._trigger_auto_summary = trigger_summary
        asyncio.run(h._run_auto_summary_pass(now=now))
        assert triggered == [user_id]
        assert h._summary_dirty_users == set()
        summary_files = list(Path(tmp_dir).glob("last_summary_*.txt"))
        assert len(summary_files) == 1
        assert ":" not in summary_files[0].name
        assert "/" not in summary_files[0].name
        assert h._get_last_summary_file(user_id).parent == Path(tmp_dir)
        h.data_dir = ["bad"]
        fallback_file = h._get_last_summary_file(user_id)
        assert fallback_file.name.startswith("last_summary_")
        assert ":" not in fallback_file.name


def test_auto_summary_pass_isolates_user_failures() -> None:
    h = Harness()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        now = datetime.now()
        h.data_dir = Path(tmp_dir)
        h.auto_summary_idle = 300
        h._summary_dirty_users = {"bad", "good"}
        old_time = (now - timedelta(seconds=400)).isoformat()
        h.user_states = {
            "bad": _state(**{"æœ€åŽäº’åŠ¨æ—¶é—´": old_time}),
            "good": _state(**{"æœ€åŽäº’åŠ¨æ—¶é—´": old_time}),
        }
        triggered = []

        async def trigger_summary(target_user_id):
            if target_user_id == "bad":
                raise RuntimeError("summary failed")
            triggered.append(target_user_id)
            return True

        h._trigger_auto_summary = trigger_summary
        asyncio.run(h._run_auto_summary_pass(now=now))
        assert triggered == ["good"]
        assert "good" not in h._summary_dirty_users
        assert "bad" in h._summary_dirty_users


def test_auto_summary_loop_has_single_pass_entrypoint() -> None:
    source = (ROOT / "marianna" / "analysis.py").read_text(encoding="utf-8")
    start = source.index("    async def _auto_summary_loop")
    end = source.index("    async def _run_auto_summary_pass", start)
    loop_source = source[start:end]
    assert loop_source.count("_run_auto_summary_pass") == 1
    assert loop_source.count("await asyncio.sleep(60)") == 1
    assert "auto summary loop failed" in loop_source


def test_memory_repair_mode_report_and_profile_prompt_confidence() -> None:
    h = Harness()
    h.memory_mode_preset = "rich"
    h.memory_prompt_limit = 5
    h.builtin_memory_prompt_char_budget = 360
    h.memory_recall_cooldown_seconds = 240
    h.active_event_cooldown_turns = 7
    h.active_event_idle_hours = 24
    mode_report = h._build_memory_mode_report()
    assert "rich" in mode_report
    assert "\u8bb0\u5fc6\u5b57\u7b26\u9884\u7b97" in mode_report
    h.memory_prompt_limit = "bad"
    h.builtin_memory_prompt_char_budget = "bad"
    h.prompt_token_budget = "bad"
    assert "\u8bb0\u5fc6\u6ce8\u5165\u6761\u6570\uff1a" in h._build_memory_mode_report()

    repair_report = h._build_memory_repair_report({"backfilled": 1, "fts_rebuilt": 2, "orphan_vectors": 3, "orphan_fts": 4})
    assert "\u672c\u5730\u8bb0\u5fc6\u4fee\u590d\u5b8c\u6210" in repair_report
    assert "3" in repair_report

    h.enable_profile = True
    profile = h._get_profile("u1")
    profile["\u57fa\u672c\u4fe1\u606f"].update(
        {"\u79f0\u547c": "\u661f", "\u804c\u4e1a": "\u5de5\u7a0b\u5e08", "\u6240\u5728\u5730": "\u4e0a\u6d77"}
    )
    profile["\u4e92\u52a8\u8bb0\u5f55"]["\u603b\u4e92\u52a8\u6b21\u6570"] = 1
    prompt = h._build_profile_memory_text("u1")
    assert "\u7528\u6237\u5e0c\u671b\u88ab\u79f0\u547c\u4e3a" in prompt
    assert "\u8bc1\u636e\u4ecd\u5c11" in prompt


def test_adaptive_lightweight_prompt_and_diagnostic_estimate() -> None:
    h = Harness()
    neutral = {"\u7528\u6237\u610f\u56fe": "\u666e\u901a\u56de\u5e94", "\u5173\u7cfb\u4fe1\u53f7": "\u65e0\u660e\u663e\u5173\u7cfb\u63a8\u8fdb"}
    assert h._should_use_adaptive_lightweight_prompt(
        "\u597d\u7684",
        neutral,
        {},
        skip_memory_retrieval=True,
    )
    strong = {"\u7528\u6237\u610f\u56fe": "\u5206\u4eab\u79d8\u5bc6", "\u5173\u7cfb\u4fe1\u53f7": "\u79d8\u5bc6\u5206\u4eab"}
    assert not h._should_use_adaptive_lightweight_prompt(
        "\u6211\u6709\u4e00\u4e2a\u79d8\u5bc6",
        strong,
        {},
        skip_memory_retrieval=True,
    )
    state = {
        "\u6700\u8fd1\u662f\u5426\u8f7b\u91cfPrompt": True,
        "\u6700\u8fd1Prompt\u4f30\u7b97": {
            "tokens": 88,
            "chars": 120,
            "compact": True,
            "memory_injection_limit": 1,
            "memory_candidate_limit": 3,
            "memory_candidate_expanded": True,
            "memory_value_priority": True,
            "memory_char_budget": 120,
            "memory_slot_dedup_saved_chars": "bad",
            "memory_slot_dedup_trace": [{"slot": "nickname", "saved_chars": "bad"}],
        },
        "\u6700\u8fd1\u8bb0\u5fc6\u53ec\u56de\u7b56\u7565": {"skipped": True, "reason": "\u77ed\u53e5\u4e14\u65e0\u5f3a\u5173\u7cfb\u4fe1\u53f7"},
    }
    report = h._build_diagnostic_report(state)
    assert "\u8f7b\u91cfPrompt\uff1a\u662f" in report
    assert "\u8bb0\u5fc6\u53ec\u56de\uff1a\u5df2\u8df3\u8fc7" in report
    assert "88 token" in report
    assert "\u8bb0\u5fc6\u5019\u9009\u6c60" in report
    assert "\u5019\u9009\u22643\u6761" in report
    dirty_explanation_report = h._build_diagnostic_report(
        {
            "\u6700\u8fd1\u72b6\u6001\u89e3\u91ca": "bad",
            "\u72b6\u6001\u89e3\u91ca\u5386\u53f2": ["bad", {"\u6570\u503c\u5e73\u6ed1": "bad", "\u77ed\u671f\u5fc3\u7406": "bad"}],
        }
    )
    assert isinstance(dirty_explanation_report, str)
    h.enable_state_explanation_log = True
    footer = h._build_debug_footer({}, {}, state_explanation="bad")
    assert isinstance(footer, str)

    bad_profile_report = h._build_diagnostic_report(
        {
            "最近Prompt估算": {
                "cost_profile": {
                    "samples": "bad",
                    "window": "bad",
                    "avg_original_tokens": "bad",
                    "budget_hit_rate": "bad",
                    "compact_rate": "bad",
                    "memory_slot_dedup_rate": "bad",
                },
                "hot_layer": {"name": "memory", "tokens": "bad", "share": "bad"},
            }
        }
    )
    assert "成本画像" in bad_profile_report
    assert "Prompt热层" in bad_profile_report
    assert h._format_delta_map_for_report({"好感度": "bad", "信任度": 2}) == "信任度+2"


def test_prompt_budget_guard_falls_back_to_compact() -> None:
    h = Harness()
    assert h._should_apply_prompt_budget_guard(2300, compact_prompt=False)
    assert not h._should_apply_prompt_budget_guard(2300, compact_prompt=True)
    h.prompt_token_budget = "not-a-number"
    assert h._should_apply_prompt_budget_guard("not-a-number", compact_prompt=False) is False

    h.prompt_token_budget = 300
    h.enable_profile = False
    h.enable_emotional_memory = False
    h.mnemosyne_available = False
    h.enable_builtin_memory = False
    h.enable_recent_style_fingerprint = False
    state = _state()
    prompt = asyncio.run(
        h._build_system_prompt(
            "u1",
            state,
            "\u4eca\u5929\u60f3\u548c\u4f60\u804a\u4e00\u4f1a",
            turn_analysis={"\u7528\u6237\u610f\u56fe": "\u666e\u901a\u56de\u5e94", "\u5173\u7cfb\u4fe1\u53f7": "\u65e0\u660e\u663e\u5173\u7cfb\u63a8\u8fdb"},
            active_event={},
            skip_memory_retrieval=False,
            compact_prompt=False,
        )
    )
    estimate = state.get("\u6700\u8fd1Prompt\u4f30\u7b97", {})
    policy = state.get("\u6700\u8fd1\u8bb0\u5fc6\u53ec\u56de\u7b56\u7565", {})
    assert prompt
    assert estimate.get("budget_guard_applied") is True
    assert estimate.get("compact") is True
    assert estimate.get("hot_layer", {}).get("name")
    assert estimate.get("initial_layer_tokens")
    assert policy.get("skipped") is True
    report = h._build_diagnostic_report(state)
    assert "\u9884\u7b97\u4fdd\u62a4=\u662f" in report
    assert "Prompt\u70ed\u5c42" in report


def test_group_lean_cache_first_prompt_is_small_and_stable() -> None:
    h = Harness()
    h.enable_profile = False
    h.enable_emotional_memory = False
    h.mnemosyne_available = False
    h.enable_builtin_memory = False
    state = _state(好感度=0, 信任度=15)
    state["_scene_memory_policy"] = {
        "is_group": True,
        "memory_mode": "lean",
        "context_injection_enabled": False,
    }
    prompt_a = asyncio.run(
        h._build_system_prompt(
            "group:g1::u1",
            state,
            "晚上好",
            turn_analysis={"用户意图": "普通回应", "关系信号": "无明显关系推进"},
            active_event={},
            skip_memory_retrieval=True,
            compact_prompt=True,
        )
    )
    state["互动计数"] = 12
    state["最近状态解释"] = {"time": datetime.now().isoformat()}
    prompt_b = asyncio.run(
        h._build_system_prompt(
            "group:g1::u1",
            state,
            "今晚在读什么",
            turn_analysis={"用户意图": "提问或请求", "关系信号": "无明显关系推进"},
            active_event={},
            skip_memory_retrieval=True,
            compact_prompt=True,
        )
    )
    assert "群聊缓存优先动态层" in prompt_a
    assert state["最近Prompt估算"]["cache_first_group"] is True
    assert state["最近记忆召回策略"]["skipped"] is True
    assert h._common_prefix_chars(prompt_a, prompt_b) > 1200
    assert len(prompt_b) < 2200


def test_prompt_budget_memory_anchor_selection() -> None:
    h = Harness()
    memories = [
        {"id": "a", "fingerprint": "a", "memory_layer": "impression", "content": "\u666e\u901a\u95ee\u5019", "salience": 9},
        {"id": "b", "fingerprint": "b", "memory_layer": "event", "content": "\u7b2c\u4e00\u6b21\u8ba4\u771f\u627f\u8bfa", "salience": 6, "temperature": "hot"},
        {"id": "c", "fingerprint": "c", "memory_layer": "profile", "content": "\u7528\u6237\u5e0c\u671b\u88ab\u79f0\u547c\u4e3a\u661f", "salience": 4},
        {"id": "d", "fingerprint": "d", "memory_layer": "summary", "content": "\u957f\u671f\u603b\u7ed3", "salience": 8},
    ]
    anchors = h._select_prompt_budget_memory_anchors("u1", memories)
    assert len(anchors) == 2
    joined = "\n".join(anchors)
    assert "\u627f\u8bfa" in joined
    assert "\u79f0\u547c" in joined
    assert "\u666e\u901a\u95ee\u5019" not in joined
    h.prompt_budget_memory_anchor_chars = "bad"
    bad_anchors = h._select_prompt_budget_memory_anchors(
        "u1",
        [{"id": "bad", "fingerprint": "bad", "memory_layer": "event", "content": "\u627f\u8bfa", "salience": "bad", "hit_count": "bad"}],
    )
    assert bad_anchors


def test_prompt_budget_history_and_advice() -> None:
    h = Harness()
    h.prompt_budget_history_limit = "not-a-number"
    state = {}
    bad_stats = h._record_prompt_budget_sample(
        state,
        {
            "tokens": "not-a-number",
            "original_tokens": "not-a-number",
            "budget": "not-a-number",
            "compact": False,
            "budget_guard_applied": False,
            "hot_layer": {"name": "\u8bb0\u5fc6\u5c42", "tokens": "not-a-number"},
        },
    )
    assert bad_stats["samples"] == 1
    state = {}
    h._record_prompt_budget_sample(
        state,
        {
            "tokens": 1200,
            "original_tokens": 2600,
            "budget": 2200,
            "compact": True,
            "budget_guard_applied": True,
            "hot_layer": {"name": "\u8bb0\u5fc6\u5c42", "tokens": 900, "share": 0.42},
        },
    )
    h._record_prompt_budget_sample(
        state,
        {
            "tokens": 1180,
            "original_tokens": 2550,
            "budget": 2200,
            "compact": True,
            "budget_guard_applied": True,
            "hot_layer": {"name": "\u8bb0\u5fc6\u5c42", "tokens": 880, "share": 0.41},
        },
    )
    stats = h._record_prompt_budget_sample(
        state,
        {
            "tokens": 1190,
            "original_tokens": 2520,
            "budget": 2200,
            "compact": True,
            "budget_guard_applied": True,
            "hot_layer": {"name": "\u8bb0\u5fc6\u5c42", "tokens": 870, "share": 0.40},
        },
    )
    assert stats["hits"] == 3
    assert stats["streak"] == 3
    assert stats["clear_streak"] == 0
    assert stats["hit_rate"] == 1.0
    assert stats["frequent_hot_layer"] == "\u8bb0\u5fc6\u5c42"
    assert len(state["Prompt\u9884\u7b97\u5386\u53f2"]) == 3
    assert "\u9884\u7b97\u4fdd\u62a4\u8f83\u9891\u7e41" in h._build_prompt_budget_advice(state)
    assert "\u8bb0\u5fc6\u5b57\u7b26\u9884\u7b97" in h._build_prompt_budget_advice(state)
    report = h._build_diagnostic_report(state)
    assert "\u9884\u7b97\u8d8b\u52bf\uff1a3/3" in report
    assert "lean" in report
    mode_report = h._build_memory_mode_report()
    assert "Prompt\u9884\u7b97" in mode_report
    assert "\u9884\u7b97\u5386\u53f2\u7a97\u53e3" in mode_report


def test_prompt_layer_cost_helpers() -> None:
    h = Harness()
    costs = h._estimate_prompt_layer_tokens(
        [
            ("\u4eba\u683c\u5c42", "\u4f18\u96c5\u800c\u514b\u5236"),
            ("\u8bb0\u5fc6\u5c42", "\u627f\u8bfa" * 120),
            ("\u4e3b\u52a8\u4e8b\u4ef6\u5c42", ""),
        ]
    )
    hot = h._select_prompt_budget_hot_layer(costs)
    assert hot["name"] == "\u8bb0\u5fc6\u5c42"
    assert hot["tokens"] == costs["\u8bb0\u5fc6\u5c42"]
    assert hot["share"] > 0.5


def test_prompt_budget_auto_throttle_policy() -> None:
    h = Harness()
    h.memory_prompt_limit = 5
    h.prompt_budget_auto_throttle_min_streak = "not-a-number"
    h.prompt_budget_auto_throttle_recovery_turns = "not-a-number"
    h.prompt_budget_throttle_escalation_hits = "not-a-number"
    h.prompt_budget_throttle_escalation_recovery_clear = "not-a-number"
    state = {"Prompt\u9884\u7b97\u7edf\u8ba1": {"streak": 2, "hit_rate": 0.67, "frequent_hot_layer": "\u8bb0\u5fc6\u5c42"}}
    policy = h._build_prompt_budget_auto_throttle_policy(state)
    assert policy["enabled"] is True
    assert policy["memory_limit"] == 2
    assert "\u8bb0\u5fc6" in policy["action"]

    state = {"Prompt\u9884\u7b97\u7edf\u8ba1": {"streak": 2, "hit_rate": 0.67, "frequent_hot_layer": "\u98ce\u683c\u6307\u7eb9\u5c42"}}
    policy = h._build_prompt_budget_auto_throttle_policy(state)
    assert policy["style_limit"] == 0

    state = {"Prompt\u9884\u7b97\u7edf\u8ba1": {"streak": 2, "hit_rate": 0.67, "frequent_hot_layer": "\u4e3b\u52a8\u4e8b\u4ef6\u5c42"}}
    policy = h._build_prompt_budget_auto_throttle_policy(state)
    assert policy["suppress_active_event"] is True

    state = {"Prompt\u9884\u7b97\u7edf\u8ba1": {"streak": 2, "hit_rate": 0.5, "frequent_hot_layer": ""}}
    policy = h._build_prompt_budget_auto_throttle_policy(state)
    assert policy["compression_tier"] == "light"
    assert policy["style_limit"] == 0
    assert not policy.get("suppress_active_event")

    state = {"Prompt\u9884\u7b97\u7edf\u8ba1": {"streak": 3, "hit_rate": 0.67, "frequent_hot_layer": ""}}
    policy = h._build_prompt_budget_auto_throttle_policy(state)
    assert policy["compression_tier"] == "medium"
    assert policy["suppress_active_event"] is True
    assert not policy.get("memory_limit")

    state = {"Prompt\u9884\u7b97\u7edf\u8ba1": {"streak": 4, "hit_rate": 0.75, "frequent_hot_layer": ""}}
    policy = h._build_prompt_budget_auto_throttle_policy(state)
    assert policy["compression_tier"] == "heavy"
    assert policy["memory_limit"] == 2
    assert not policy.get("force_compact")

    state = {"Prompt\u9884\u7b97\u7edf\u8ba1": {"streak": 5, "hit_rate": 0.8, "frequent_hot_layer": ""}}
    policy = h._build_prompt_budget_auto_throttle_policy(state)
    assert policy["compression_tier"] == "critical"
    assert policy["force_compact"] is True

    state = {"Prompt\u9884\u7b97\u7edf\u8ba1": {"streak": 0, "clear_streak": 2, "hit_rate": 0.4, "frequent_hot_layer": "\u8bb0\u5fc6\u5c42"}}
    policy = h._build_prompt_budget_auto_throttle_policy(state)
    assert policy["enabled"] is False
    assert policy["recovered"] is True


def test_prompt_budget_stats_clear_streak() -> None:
    h = Harness()
    state = {}
    h._record_prompt_budget_sample(
        state,
        {"tokens": 1200, "original_tokens": 2600, "budget": 2200, "compact": True, "budget_guard_applied": True},
    )
    h._record_prompt_budget_sample(
        state,
        {"tokens": 1100, "original_tokens": 1800, "budget": 2200, "compact": False, "budget_guard_applied": False},
    )
    stats = h._record_prompt_budget_sample(
        state,
        {"tokens": 1050, "original_tokens": 1700, "budget": 2200, "compact": False, "budget_guard_applied": False},
    )
    assert stats["streak"] == 0
    assert stats["clear_streak"] == 2
    report = h._build_diagnostic_report(state)
    assert "\u6062\u590d2" in report


def test_prompt_cost_profile_stats() -> None:
    h = Harness()
    h.prompt_cost_profile_window = "not-a-number"
    state = {}
    bad = h._record_prompt_cost_profile(
        state,
        {
            "tokens": "not-a-number",
            "original_tokens": "not-a-number",
            "budget": "not-a-number",
            "memory_injection_limit": "not-a-number",
            "memory_candidate_limit": "not-a-number",
            "memory_selection_trace": "bad",
            "memory_slot_dedup_trace": "bad",
            "memory_slot_dedup_saved_chars": "not-a-number",
        },
    )
    assert bad["samples"] == 1
    assert bad["memory_slot_dedup_rate"] == 0.0

    h.prompt_cost_profile_window = 2
    state = {}
    first = h._record_prompt_cost_profile(
        state,
        {
            "tokens": 800,
            "original_tokens": 1000,
            "budget": 900,
            "budget_guard_applied": True,
            "compact": True,
            "memory_injection_limit": 2,
            "memory_candidate_limit": 6,
            "memory_selection_trace": [{"id": "a"}, {"id": "b"}],
            "memory_slot_dedup_trace": [{"slot": "nickname", "saved_chars": 20}],
            "memory_slot_dedup_saved_chars": 20,
            "memory_value_priority": True,
        },
    )
    assert first["samples"] == 1
    assert first["budget_hit_rate"] == 1.0

    second = h._record_prompt_cost_profile(
        state,
        {
            "tokens": 600,
            "original_tokens": 600,
            "budget": 900,
            "budget_guard_applied": False,
            "compact": False,
            "memory_injection_limit": 1,
            "memory_candidate_limit": 1,
            "memory_selection_trace": [{"id": "c"}],
            "memory_slot_dedup_trace": [],
            "memory_slot_dedup_saved_chars": 0,
            "memory_value_priority": False,
        },
    )
    assert second["samples"] == 2
    assert second["avg_original_tokens"] == 800
    assert second["budget_hit_rate"] == 0.5
    assert second["avg_memory_selected"] == 1
    assert second["avg_memory_slot_dedup_saved_chars"] == 10
    assert second["memory_slot_dedup_rate"] == 0.5
    report = h._build_diagnostic_report({"Prompt成本画像": second})
    assert "\u6210\u672c\u753b\u50cf" in report
    assert "800 token" in report
    assert "槽位省10字" in report


def test_prompt_budget_advice_uses_slot_dedup_savings() -> None:
    h = Harness()
    state = {
        "Prompt成本画像": {
            "avg_memory_slot_dedup_saved_chars": 18,
            "memory_slot_dedup_rate": 0.5,
        }
    }
    assert "记忆槽位去重已有收益" in h._build_prompt_budget_advice(state)

    state["Prompt预算统计"] = {
        "streak": 3,
        "hit_rate": 0.67,
        "avg_original_tokens": 2600,
        "frequent_hot_layer": "记忆层",
    }
    advice = h._build_prompt_budget_advice(state)
    assert "槽位去重已平均省18字" in advice
    assert "降低记忆字符预算" in advice


def test_prompt_budget_auto_throttle_in_diagnostic() -> None:
    h = Harness()
    state = {
        "Prompt\u9884\u7b97\u7edf\u8ba1": {
            "streak": 2,
            "hit_rate": 0.67,
            "frequent_hot_layer": "\u4e3b\u52a8\u4e8b\u4ef6\u5c42",
        }
    }
    state["Prompt\u9884\u7b97\u81ea\u52a8\u964d\u6863"] = h._build_prompt_budget_auto_throttle_policy(state)
    report = h._build_diagnostic_report(state)
    assert "\u9884\u7b97\u81ea\u52a8\u964d\u6863" in report
    assert "\u4e3b\u52a8\u4e8b\u4ef6\u5c42" in report


def test_prompt_budget_throttle_log_summary() -> None:
    h = Harness()
    state = {}
    h._record_prompt_budget_throttle_event(
        state,
        {
            "enabled": True,
            "action": "\u4e34\u65f6\u964d\u4f4e\u8bb0\u5fc6\u6ce8\u5165\u6761\u6570",
            "hot_layer": "\u8bb0\u5fc6\u5c42",
            "streak": 2,
            "clear_streak": 0,
            "memory_limit": 2,
        },
    )
    h._record_prompt_budget_throttle_event(
        state,
        {
            "enabled": True,
            "action": "\u672c\u8f6e\u8df3\u8fc7\u6700\u8fd1\u98ce\u683c\u6307\u7eb9",
            "hot_layer": "\u98ce\u683c\u6307\u7eb9\u5c42",
            "streak": 3,
            "clear_streak": 0,
            "style_limit": 0,
        },
    )
    h._record_prompt_budget_throttle_event(
        state,
        {"enabled": False, "recovered": True, "clear_streak": 2},
    )
    assert len(state["Prompt\u9884\u7b97\u964d\u6863\u65e5\u5fd7"]) == 3
    summary = h._summarize_prompt_budget_throttle_log(state)
    assert "\u8bb0\u5fc6\u5c42" in summary
    assert "\u6062\u590d" in summary
    report = h._build_diagnostic_report(state)
    assert "\u964d\u6863\u65e5\u5fd7" in report


def test_prompt_budget_throttle_escalation() -> None:
    h = Harness()
    h.memory_prompt_limit = 5
    state = {
        "Prompt\u9884\u7b97\u7edf\u8ba1": {
            "streak": 2,
            "hit_rate": 0.67,
            "frequent_hot_layer": "\u8bb0\u5fc6\u5c42",
        },
        "Prompt\u9884\u7b97\u964d\u6863\u65e5\u5fd7": [
            {"hot_layer": "\u8bb0\u5fc6\u5c42", "action": "\u4e34\u65f6\u964d\u4f4e\u8bb0\u5fc6\u6ce8\u5165\u6761\u6570"},
            {"hot_layer": "\u8bb0\u5fc6\u5c42", "action": "\u4e34\u65f6\u964d\u4f4e\u8bb0\u5fc6\u6ce8\u5165\u6761\u6570"},
        ],
    }
    policy = h._build_prompt_budget_auto_throttle_policy(state)
    assert policy["escalated"] is True
    assert policy["memory_limit"] == 1
    h._record_prompt_budget_throttle_event(state, policy)
    summary = h._summarize_prompt_budget_throttle_log(state)
    assert "\u5f3a\u9000\u907f" in summary
    state["Prompt\u9884\u7b97\u81ea\u52a8\u964d\u6863"] = policy
    report = h._build_diagnostic_report(state)
    assert "\u5f3a\u9000\u907f" in report


def test_prompt_budget_throttle_escalation_recovery() -> None:
    h = Harness()
    h.memory_prompt_limit = 5
    state = {
        "Prompt\u9884\u7b97\u7edf\u8ba1": {
            "streak": 0,
            "clear_streak": 1,
            "hit_rate": 0.67,
            "frequent_hot_layer": "\u8bb0\u5fc6\u5c42",
        },
        "Prompt\u9884\u7b97\u964d\u6863\u65e5\u5fd7": [
            {"hot_layer": "\u8bb0\u5fc6\u5c42", "action": "\u4e34\u65f6\u964d\u4f4e\u8bb0\u5fc6\u6ce8\u5165\u6761\u6570"},
            {"hot_layer": "\u8bb0\u5fc6\u5c42", "action": "\u4e34\u65f6\u964d\u4f4e\u8bb0\u5fc6\u6ce8\u5165\u6761\u6570"},
        ],
    }
    policy = h._build_prompt_budget_auto_throttle_policy(state)
    assert policy["escalated"] is False
    assert policy["escalation_recovering"] is True
    assert policy["memory_limit"] == 2
    h._record_prompt_budget_throttle_event(state, policy)
    summary = h._summarize_prompt_budget_throttle_log(state)
    assert "\u9000\u907f\u6062\u590d" in summary
    state["Prompt\u9884\u7b97\u81ea\u52a8\u964d\u6863"] = policy
    report = h._build_diagnostic_report(state)
    assert "\u9000\u907f\u6062\u590d" in report


def test_prompt_budget_memory_mode_policy() -> None:
    h = Harness()
    h.memory_mode_preset = "lean"
    h.memory_prompt_limit = "not-a-number"
    h.prompt_budget_memory_mode_pressure_hit_rate = "not-a-number"
    h.prompt_budget_memory_mode_lean_limit = "not-a-number"
    h.prompt_budget_memory_mode_lean_chars = "not-a-number"
    state = {"Prompt\u9884\u7b97\u7edf\u8ba1": {"streak": 1, "hit_rate": 0.25, "clear_streak": 0}}
    policy = h._build_prompt_budget_memory_mode_policy(state)
    assert policy["enabled"] is True
    assert policy["mode"] == "lean"
    assert policy["memory_limit"] == 1
    assert policy["char_budget"] == 120

    h.memory_mode_preset = "rich"
    h.memory_prompt_limit = 5
    h.prompt_budget_memory_mode_pressure_hit_rate = 34
    h.prompt_budget_memory_mode_lean_limit = 1
    h.prompt_budget_memory_mode_lean_chars = 120
    policy = h._build_prompt_budget_memory_mode_policy(state)
    assert policy["memory_limit"] == 3
    assert policy["char_budget"] == 260

    state = {"Prompt\u9884\u7b97\u7edf\u8ba1": {"streak": 0, "hit_rate": 0.1, "clear_streak": 0}}
    assert h._build_prompt_budget_memory_mode_policy(state) == {}

    state = {"Prompt\u9884\u7b97\u7edf\u8ba1": {"streak": 0, "hit_rate": 0.5, "clear_streak": 2}}
    policy = h._build_prompt_budget_memory_mode_policy(state)
    assert policy["recovered"] is True
    report = h._build_diagnostic_report({"Prompt\u9884\u7b97\u8bb0\u5fc6\u6a21\u5f0f\u7b56\u7565": policy})
    assert "\u8bb0\u5fc6\u6a21\u5f0f\u8f6f\u4e0a\u9650" in report


def test_prompt_cost_auto_memory_mode_policy() -> None:
    h = Harness()
    h.memory_mode_preset = "rich"
    h.prompt_cost_auto_mode_sticky_turns = 0
    state = {
        "Prompt\u9884\u7b97\u7edf\u8ba1": {"streak": 1, "hit_rate": 0.4, "clear_streak": 0},
        "Prompt\u6210\u672c\u753b\u50cf": {
            "samples": 4,
            "budget_hit_rate": 0.5,
            "compact_rate": 0.25,
            "avg_original_tokens": 2600,
        },
    }
    policy = h._build_prompt_budget_memory_mode_policy(state)
    assert policy["mode"] == "lean"
    assert policy["configured_mode"] == "rich"
    assert policy["auto_mode_reason"] == "cost_high"
    assert policy["memory_limit"] == 1

    state["Prompt\u6210\u672c\u753b\u50cf"] = {
        "samples": 4,
        "budget_hit_rate": 0.22,
        "compact_rate": 0.1,
        "avg_original_tokens": 1800,
    }
    policy = h._build_prompt_budget_memory_mode_policy(state)
    assert policy["mode"] == "balanced"
    assert policy["auto_mode_reason"] == "cost_medium"
    report = h._build_diagnostic_report({"Prompt\u9884\u7b97\u8bb0\u5fc6\u6a21\u5f0f\u7b56\u7565": policy})
    assert "cost_medium" in report


def test_prompt_cost_auto_memory_mode_sticky_upgrade() -> None:
    h = Harness()
    h.memory_mode_preset = "lean"
    state = {
        "Prompt\u9884\u7b97\u7edf\u8ba1": {"streak": 1, "hit_rate": 0.4, "clear_streak": 0},
        "Prompt\u6210\u672c\u753b\u50cf": {
            "samples": 4,
            "budget_hit_rate": 0.5,
            "compact_rate": 0.25,
            "avg_original_tokens": 2600,
        },
    }
    policy = h._build_prompt_budget_memory_mode_policy(state)
    assert policy["mode"] == "lean"
    assert policy["auto_mode_reason"] == "cost_high"

    state["Prompt\u6210\u672c\u753b\u50cf"] = {
        "samples": 4,
        "budget_hit_rate": 0.0,
        "compact_rate": 0.0,
        "avg_original_tokens": 1000,
    }
    policy = h._build_prompt_budget_memory_mode_policy(state)
    assert policy["mode"] == "lean"
    assert policy["auto_mode_reason"] == "sticky:cost_recovered"
    assert state["Prompt\u6210\u672c\u81ea\u52a8\u8bb0\u5fc6\u6863\u4f4d"]["pending_mode"] == "balanced"

    policy = h._build_prompt_budget_memory_mode_policy(state)
    assert policy["mode"] == "balanced"
    assert policy["auto_mode_reason"] == "cost_recovered"


def test_context_injection_defaults_cache_friendly() -> None:
    h = Harness()
    h.config = {}
    h._apply_config()
    assert not h.context_injection_enabled
    assert not h.inject_history
    assert not h.inject_summary_in_context
    assert h.avoid_duplicate_context_injection
    assert h.max_context_messages == 6
    assert h.max_tokens_per_message == 220
    assert h.history_duplicate_window_seconds == 5
    assert h.history_max_entry_chars == 4000

    h.config = {
        "enable_context_injection": True,
        "inject_summary_as_context": True,
        "history_duplicate_window_seconds": 12,
        "history_max_entry_chars": 2048,
    }
    h._apply_config()
    assert h.context_injection_enabled
    assert h.inject_history
    assert h.inject_summary_in_context
    assert h.history_duplicate_window_seconds == 12
    assert h.history_max_entry_chars == 2048


def test_memory_mode_preset_respects_explicit_config_overrides() -> None:
    h = Harness()
    h.config = {
        "memory_mode_preset": "lean",
        "builtin_memory_prompt_char_budget": 500,
        "memory_recall_cooldown_seconds": 120,
        "active_event_cooldown_turns": 3,
        "active_event_idle_hours": 8,
        "enable_token_cost_optimization": False,
    }
    h._apply_config()
    assert h.builtin_memory_prompt_char_budget == 500
    assert h.memory_recall_cooldown_seconds == 120
    assert h.active_event_cooldown_turns == 3
    assert h.active_event_idle_hours == 8

    h.config = {
        "memory_mode_preset": "rich",
        "memory_prompt_limit": 2,
        "memory_prompt_event_limit": 1,
        "builtin_memory_prompt_char_budget": 220,
        "memory_recall_cooldown_seconds": 900,
        "enable_token_cost_optimization": False,
    }
    h._apply_config()
    assert h.memory_prompt_limit == 2
    assert h.memory_prompt_event_limit == 1
    assert h.builtin_memory_prompt_char_budget == 220
    assert h.memory_recall_cooldown_seconds == 900


def test_numeric_config_strings_are_safely_coerced() -> None:
    h = Harness()
    h.config = {
        "marianna_initial_favor": "21",
        "marianna_initial_yan": "bad",
        "marianna_initial_trust": "34",
        "marianna_initial_anxiety": "-5",
        "marianna_initial_elegance": "130",
        "marianna_favor_multiplier": "1.5",
        "marianna_yan_multiplier": "bad",
        "marianna_lock_threshold": "80",
        "auto_summary_interval": "9",
        "auto_summary_idle_time": "120",
        "history_max_entry_chars": "1200",
        "marianna_temperature": "1.25",
        "builtin_memory_vector_min_similarity": "0.42",
        "builtin_memory_vector_max_dimensions": "2048",
        "builtin_memory_import_max_content_chars": "1500",
        "builtin_memory_import_max_line_chars": "60000",
        "short_term_emotion_decay": "0.7",
        "short_term_decay_half_life_hours": "12.5",
    }
    h._apply_config()
    assert DEFAULT_STATE["\u597d\u611f\u5ea6"] == 21
    assert DEFAULT_STATE["\u75c5\u5a07\u503c"] == 0
    assert DEFAULT_STATE["\u4fe1\u4efb\u5ea6"] == 34
    assert DEFAULT_STATE["\u7126\u8651\u503c"] == 0
    assert DEFAULT_STATE["\u4f18\u96c5\u503c"] == 100
    assert h.favor_multiplier == 1.5
    assert h.yan_multiplier == 1.0
    assert h.lock_threshold == 80
    assert h.auto_summary_interval == 9
    assert h.auto_summary_idle == 120
    assert h.history_max_entry_chars == 1200
    assert h.temperature == 1.25
    assert h.builtin_memory_vector_min_similarity == 0.42
    assert h.builtin_memory_vector_max_dimensions == 2048
    assert h.builtin_memory_import_max_content_chars == 1500
    assert h.builtin_memory_import_max_line_chars == 60000
    assert h.short_term_emotion_decay == 0.7
    assert h.short_term_decay_half_life_hours == 12.5
    assert h.context_injection_enabled is False

    h.config = {
        "marianna_temperature": "nan",
        "builtin_memory_vector_min_similarity": "inf",
        "builtin_memory_vector_max_dimensions": "not-a-number",
        "short_term_emotion_decay": "-inf",
    }
    h._apply_config()
    assert h.temperature == 0.85
    assert h.builtin_memory_vector_min_similarity == 0.35
    assert h.builtin_memory_vector_max_dimensions == 4096
    assert h.short_term_emotion_decay == 0.65


def test_config_source_and_provider_ids_self_heal() -> None:
    h = Harness()
    h.config = ["bad"]
    h._apply_config()
    assert isinstance(h.config, dict)
    assert h.temperature == 0.85

    h.config = {
        "marianna_embedding_provider_id": 123,
        "marianna_analysis_provider_id": ["analysis"],
        "marianna_debug_mode": "true",
        "enable_context_injection": "true",
    }
    h._apply_config()
    assert h.embedding_provider_id == "123"
    assert h.analysis_provider_id == "['analysis']"
    assert h.default_debug_mode is True
    assert h.context_injection_enabled is True

    h.config = "bad"
    h._get_config_source()["memory_mode_preset"] = "lean"
    h._apply_config()
    assert h.memory_mode_preset == "lean"


def test_boolean_config_strings_are_safely_coerced() -> None:
    h = Harness()
    h.config = {
        "enable_context_injection": "false",
        "inject_summary_as_context": "true",
        "enable_token_cost_optimization": "0",
        "avoid_duplicate_context_injection": "no",
        "enable_scene_memory_mode": "off",
        "private_chat_context_injection": "yes",
        "group_chat_context_injection": "1",
        "enable_builtin_memory_vector": "enabled",
        "enable_memory_privacy_layer": "disabled",
        "marianna_debug_mode": "true",
        "enable_performance_logging": "off",
    }
    h._apply_config()
    assert h.context_injection_enabled is False
    assert h.inject_history is False
    assert h.inject_summary_in_context is False
    assert h.enable_token_cost_optimization is False
    assert h.avoid_duplicate_context_injection is False
    assert h.enable_scene_memory_mode is False
    assert h.private_chat_context_injection is True
    assert h.group_chat_context_injection is True
    assert h.enable_builtin_memory_vector is True
    assert h.enable_memory_privacy_layer is False
    assert h.default_debug_mode is True
    assert h.enable_performance_logging is False


def test_release_and_config_audit_report() -> None:
    h = Harness()
    h.config = {}
    h._apply_config()
    release = h._build_release_report()
    audit = h._build_config_audit_report()
    assert f"Marianna {PLUGIN_VERSION}" in release
    assert "context injection" in release
    assert "0 risk" in audit
    assert "[OK] context_injection" in audit
    assert "[OK] scene_memory_mode" in audit
    assert "[INFO] private_context_injection" in audit
    assert "[OK] group_context_injection" in audit


def test_model_probe_report_detects_provider_ids() -> None:
    class DummyMeta:
        def __init__(self, provider_id: str, name: str, provider_type: str = "llm") -> None:
            self.id = provider_id
            self.name = name
            self.type = provider_type

    class DummyProvider:
        def __init__(self, provider_id: str, name: str, provider_type: str = "llm") -> None:
            self._meta = DummyMeta(provider_id, name, provider_type)

        def meta(self):
            return self._meta

    class DummyContext:
        def __init__(self) -> None:
            self.providers = {
                "chat-main": DummyProvider("chat-main", "Chat Main"),
                "analysis-lite": DummyProvider("analysis-lite", "Analysis Lite"),
                "embed-main": DummyProvider("embed-main", "Embedding Main", "embedding"),
            }

        def get_using_provider(self):
            return self.providers["chat-main"]

        async def get_current_chat_provider_id(self, umo=None):
            return "chat-main"

        def get_provider_by_id(self, provider_id: str):
            return self.providers.get(provider_id)

    h = Harness()
    h.context = DummyContext()
    h.config = {
        "marianna_analysis_provider_id": "analysis-lite",
        "enable_builtin_memory_vector": True,
        "marianna_embedding_provider_id": "embed-main",
    }
    h._apply_config()

    report = asyncio.run(h._build_model_probe_report(object()))

    assert "Marianna model probe" in report
    assert "对话模型: [OK] chat-main" in report
    assert "分析模型: [OK] analysis-lite" in report
    assert "嵌入模型: [OK] embed-main" in report
    assert "不调用 LLM/embedding" in report


def test_runtime_bad_numeric_state_is_tolerated() -> None:
    h = Harness()
    h.temperature = "bad"
    h.lock_threshold = "bad"
    h.user_states["u_lock"] = _state(锁定进度=0)
    prepared_state, _old_name, old_lock = asyncio.run(h._prepare_turn_state("u_lock", "tester"))
    assert prepared_state["锁定进度"] == 0
    assert old_lock == 0

    state = _state(
        **{
            "\u7126\u8651\u503c": "bad",
            "\u4f18\u96c5\u503c": "bad",
            "\u75c5\u5a07\u503c": "bad",
        }
    )
    assert h._get_effective_temperature(state) == 0.66

    h._perf_stats = {
        "bad": {
            "samples": [],
            "count": "bad",
            "last": "bad",
            "max": "bad",
        }
    }
    h._record_perf_sample("bad", "bad")
    report = h._build_perf_report()
    assert "bad" in report

    h.config = {
        "enable_context_injection": True,
        "avoid_duplicate_context_injection": False,
        "enable_token_cost_optimization": False,
        "enable_memory_privacy_layer": False,
    }
    h._apply_config()
    h.memory_prompt_limit = "bad"
    h.builtin_memory_prompt_char_budget = "bad"
    h.max_context_messages = "bad"
    h.prompt_token_budget = "bad"
    risky = h._build_config_audit_report()
    assert "[RISK] context_injection" in risky
    assert "[RISK] token_cost_optimization" in risky
    assert "[RISK] group_privacy" in risky

    for attr_name in (
        "_pending_events",
        "_pending_debug_deltas",
        "_analysis_request_cache",
        "_session_alias_created_at",
        "_session_alias_queues",
    ):
        if hasattr(h, attr_name):
            delattr(h, attr_name)
    h._purge_stale_pending_records()
    assert isinstance(h._pending_events, dict)
    assert isinstance(h._analysis_request_cache, dict)

    h._pending_events["u1::turn"] = {"_created_at": time.monotonic()}
    h._pending_debug_deltas["other"] = {"_created_at": time.monotonic()}
    h._analysis_request_cache["session::u1"] = {"_created_at": time.monotonic()}
    h._pending_events["bad-inf"] = {"_created_at": float("inf")}
    h._session_alias_created_at["stale"] = "bad"
    h._session_alias_created_at["nan"] = float("nan")
    h._session_alias_queues["alias"] = ["stale"]
    h._session_alias_queues["bad"] = "not-a-list"
    h._purge_stale_pending_records()
    assert "bad-inf" not in h._pending_events
    assert "stale" not in h._session_alias_created_at
    assert "nan" not in h._session_alias_created_at
    assert "alias" not in h._session_alias_queues
    assert "bad" not in h._session_alias_queues

    h._session_counter = "bad"
    h._session_alias_queues = {"alias": "bad"}
    event = _FakeEvent("u1")
    alias_key = h._get_session_alias_key(event, "u1")
    session_key = h._get_session_key(event, "u1", create=True)
    assert session_key.endswith("::seq:1")
    assert isinstance(h._session_alias_queues.get(alias_key), list)
    assert h._get_session_key(event, "u1") == session_key

    h._pending_debug_deltas = {
        "other": {"_created_at": time.monotonic()},
        "request-key": {"message_key": "same-message", "user_id": "group:g1::u1", "debug_mode": True},
        "unrelated": {"message_key": "other-message"},
    }
    resolved_key, debug_record = h._pop_pending_debug_delta("response-key", "same-message")
    assert resolved_key == "request-key"
    assert debug_record["debug_mode"] is True
    assert debug_record["user_id"] == "group:g1::u1"
    assert "unrelated" in h._pending_debug_deltas

    h._session_alias_created_at["scene::u1::old"] = time.monotonic()
    h._session_alias_created_at["scene::u2::old"] = time.monotonic()
    h._session_alias_queues["scene::u1::alias"] = ["scene::u1::alias::seq:9"]
    h._session_alias_queues["shared"] = ["scene::u1::alias::seq:10", "scene::u2::alias::seq:11"]
    h._clear_pending_for_user("u1")
    assert "u1::turn" not in h._pending_events
    assert "session::u1" not in h._analysis_request_cache
    assert "other" in h._pending_debug_deltas
    assert "scene::u1::old" not in h._session_alias_created_at
    assert "scene::u2::old" in h._session_alias_created_at
    assert "scene::u1::alias" not in h._session_alias_queues
    assert h._session_alias_queues["shared"] == ["scene::u2::alias::seq:11"]


def test_scene_memory_mode_policy_defaults() -> None:
    h = Harness()
    h.config = {}
    h._apply_config()
    private_event = _FakeEvent("u1")
    group_event = _FakeEvent("u1", group_id="g1")

    private_policy = h._build_scene_memory_policy(private_event)
    group_policy = h._build_scene_memory_policy(group_event)
    assert private_policy["mode"] == "rich"
    assert private_policy["memory_limit"] == 5
    assert private_policy["char_budget"] == 520
    assert private_policy["context_injection_enabled"]
    assert private_policy["inject_summary_in_context"]

    assert group_policy["mode"] == "lean"
    assert group_policy["memory_limit"] == 2
    assert group_policy["char_budget"] == 180
    assert not group_policy["context_injection_enabled"]
    assert not group_policy["inject_summary_in_context"]

    h.config = {
        "private_chat_memory_mode_preset": "balanced",
        "group_chat_memory_mode_preset": "rich",
        "group_chat_context_injection": True,
        "group_chat_inject_summary_as_context": True,
    }
    h._apply_config()
    assert h._build_scene_memory_policy(private_event)["mode"] == "balanced"
    custom_group = h._build_scene_memory_policy(group_event)
    assert custom_group["mode"] == "rich"
    assert custom_group["context_injection_enabled"]
    assert custom_group["inject_summary_in_context"]

    h.config = {
        "private_chat_memory_mode_preset": "rich",
        "group_chat_memory_mode_preset": "lean",
        "memory_prompt_limit": 4,
        "memory_prompt_event_limit": 1,
        "builtin_memory_prompt_char_budget": 333,
        "memory_recall_cooldown_seconds": 123,
        "active_event_cooldown_turns": 4,
        "active_event_idle_hours": 9,
        "enable_token_cost_optimization": False,
    }
    h._apply_config()
    explicit_private = h._build_scene_memory_policy(private_event)
    explicit_group = h._build_scene_memory_policy(group_event)
    for policy in (explicit_private, explicit_group):
        assert policy["memory_limit"] == 4
        assert policy["event_limit"] == 1
        assert policy["char_budget"] == 333
        assert policy["recall_cooldown_seconds"] == 123
        assert policy["active_event_cooldown_turns"] == 4
        assert policy["active_event_idle_hours"] == 9

    h.config = {
        "enable_scene_memory_mode": False,
        "memory_mode_preset": "balanced",
        "memory_prompt_limit": 4,
        "builtin_memory_prompt_char_budget": 333,
        "enable_context_injection": True,
        "inject_summary_as_context": True,
    }
    h._apply_config()
    legacy_policy = h._build_scene_memory_policy(group_event)
    assert not legacy_policy["enabled"]
    assert legacy_policy["memory_limit"] == 3
    assert legacy_policy["char_budget"] == 333
    assert legacy_policy["context_injection_enabled"]
    assert legacy_policy["inject_summary_in_context"]


def test_scene_memory_recall_cooldown_is_cache_scoped() -> None:
    h = Harness()
    h.config = {}
    h._apply_config()
    h.memory_recall_cooldown_seconds = 300
    h.user_states["u1"] = {
        "\u6700\u8fd1\u53ec\u56de\u8bb0\u5fc6": [
            {"id": "m1", "time": time.time() - 400},
        ]
    }
    memories = [{"id": "m1", "content": "old"}, {"id": "m2", "content": "new"}]
    assert [m["id"] for m in h._filter_memory_recall_cooldown("u1", memories, 2)] == ["m1", "m2"]
    assert [m["id"] for m in h._filter_memory_recall_cooldown("u1", memories, 2, cooldown_seconds=600)] == ["m2"]

    key_300 = h._build_builtin_memory_query_cache_key("u1", ["old"], 2, 300)
    key_600 = h._build_builtin_memory_query_cache_key("u1", ["old"], 2, 600)
    assert key_300 != key_600
    h.user_states["u1"]["\u6700\u8fd1\u53ec\u56de\u8bb0\u5fc6"] = [{"id": "m1", "time": time.time()}]
    key_recent_recall = h._build_builtin_memory_query_cache_key("u1", ["old"], 2, 300)
    assert key_recent_recall != key_300
    assert h._build_memory_recall_cooldown_signature("u1", cooldown_seconds=300) == ["m1"]
    key_group_lean = h._build_builtin_memory_query_cache_key("u1", ["old"], 2, 300, {"event_limit": 1})
    key_private_rich = h._build_builtin_memory_query_cache_key("u1", ["old"], 2, 300, {"event_limit": 3})
    assert key_group_lean != key_private_rich
    mnemo_group_lean = h._build_mnemosyne_query_cache_key(Path("memory.json"), (1, 2), ["old"], 2, {"event_limit": 1})
    mnemo_private_rich = h._build_mnemosyne_query_cache_key(Path("memory.json"), (1, 2), ["old"], 2, {"event_limit": 3})
    assert mnemo_group_lean != mnemo_private_rich
    builtin_temp_key = h._build_builtin_memory_query_cache_key("u1", ["old"], 2, 300)
    mnemo_temp_key = h._build_mnemosyne_query_cache_key(Path("memory.json"), (1, 2), ["old"], 2)
    h.memory_hot_days = h.memory_hot_days + 1
    assert h._build_builtin_memory_query_cache_key("u1", ["old"], 2, 300) != builtin_temp_key
    assert h._build_mnemosyne_query_cache_key(Path("memory.json"), (1, 2), ["old"], 2) != mnemo_temp_key
    assert h._get_memory_layer_quotas({"event_limit": 1, "profile_limit": 0})["event"] == 1
    assert h._get_memory_layer_quotas({"event_limit": 1, "profile_limit": 0})["profile"] == 0


def test_scene_memory_layer_quotas_are_hard_caps() -> None:
    h = Harness()
    memories = [
        {"id": "e1", "fingerprint": "e1", "content": "alpha event high", "memory_layer": "event", "salience": 9},
        {"id": "e2", "fingerprint": "e2", "content": "alpha event second", "memory_layer": "event", "salience": 8},
        {"id": "i1", "fingerprint": "i1", "content": "alpha impression", "memory_layer": "impression", "salience": 4},
    ]
    selected = h._select_layered_mnemosyne_memories(
        memories,
        ["alpha"],
        2,
        layer_quotas={"event_limit": 1, "impression_limit": 1, "summary_limit": 0, "profile_limit": 0},
    )
    selected_layers = [item["memory_layer"] for item in selected]
    assert len(selected) == 2
    assert selected_layers.count("event") == 1
    assert selected_layers.count("impression") == 1


def test_builtin_memory_recall_overfetches_for_cooldown_backfill() -> None:
    h = Harness()
    h.memory_recall_cooldown_seconds = 300
    h.user_states["u1"] = {
        "\u6700\u8fd1\u53ec\u56de\u8bb0\u5fc6": [
            {"id": "m1", "time": time.time()},
        ]
    }
    assert h._build_memory_recall_candidate_limit(
        1,
        cooldown_seconds=300,
        layer_quotas={"event_limit": 1, "impression_limit": 1, "summary_limit": 0, "profile_limit": 0},
    ) >= 2
    memories = [
        {"id": "m1", "content": "old"},
        {"id": "m2", "content": "backup"},
    ]
    assert [m["id"] for m in h._filter_memory_recall_cooldown("u1", memories, 1, cooldown_seconds=300)] == ["m2"]


def test_builtin_memory_hits_are_marked_after_cooldown() -> None:
    h = Harness()
    h._local_memory_query_cache = {}
    h.user_states["u1"] = {
        "\u6700\u8fd1\u53ec\u56de\u8bb0\u5fc6": [
            {"id": "m1", "time": time.time()},
        ]
    }
    marked = []

    async def ready():
        return True

    async def feedback(_user_id, _query):
        return 0

    def retrieve(_user_id, _query, _limit, _layer_quotas=None):
        return [
            {"id": "m1", "fingerprint": "m1", "content": "alpha old", "memory_layer": "event", "salience": 8},
            {"id": "m2", "fingerprint": "m2", "content": "alpha backup", "memory_layer": "impression", "salience": 5},
        ]

    async def vector(_user_id, _query, _limit):
        return []

    def mark(selected):
        marked.extend(item["id"] for item in selected)

    h._ensure_builtin_memory_ready = ready
    h._apply_memory_recall_negative_feedback = feedback
    h._retrieve_builtin_memories_sync = retrieve
    h._retrieve_builtin_vector_memories = vector
    h._mark_builtin_memory_hits_sync = mark
    selected = asyncio.run(
        h._retrieve_from_builtin_memory(
            "u1",
            "alpha",
            limit=1,
            cooldown_seconds=300,
            layer_quotas={"event_limit": 1, "impression_limit": 1},
        )
    )
    assert [item["id"] for item in selected] == ["m2"]
    assert marked == ["m2"]


def test_memory_layer_passes_scene_policy_to_builtin_retrieval() -> None:
    h = Harness()
    h.enable_builtin_memory = True
    h.mnemosyne_available = False
    h.enable_emotional_memory = True
    captured = {}

    async def retrieve(_user_id, _user_msg, limit=3, cooldown_seconds=None, layer_quotas=None):
        captured["limit"] = limit
        captured["cooldown_seconds"] = cooldown_seconds
        captured["layer_quotas"] = dict(layer_quotas or {})
        return [
            {
                "id": "m1",
                "content": "\u7b2c\u4e00\u6b21\u8ba4\u771f\u627f\u8bfa\u4f1a\u8bb0\u5f97\u8fb9\u754c",
                "memory_layer": "event",
                "memory_type": "milestone",
                "salience": 7,
            }
        ]

    h._retrieve_from_builtin_memory = retrieve
    layer = asyncio.run(
        h._build_memory_layer(
            "u1",
            "hello",
            memory_limit=2,
            memory_char_budget=180,
            scene_policy={
                "recall_cooldown_seconds": 600,
                "event_limit": 1,
                "impression_limit": 1,
                "summary_limit": 0,
                "profile_limit": 0,
            },
        )
    )
    assert "\u627f\u8bfa" in layer
    assert captured["cooldown_seconds"] == 600
    assert captured["layer_quotas"]["event_limit"] == 1


def test_analysis_memory_passes_scene_policy_to_retrieval() -> None:
    h = Harness()
    h.enable_builtin_memory = True
    h.mnemosyne_available = True
    h.enable_emotional_memory = True
    h.analysis_mnemosyne_memory_limit = 2
    h._perf_stats = {}
    captured = {"builtin": {}, "mnemosyne": {}}

    async def history(_user_id, _latest_user_msg, limit=None):
        return []

    async def builtin(_user_id, _user_msg, limit=3, cooldown_seconds=None, layer_quotas=None):
        captured["builtin"] = {
            "limit": limit,
            "cooldown_seconds": cooldown_seconds,
            "layer_quotas": dict(layer_quotas or {}),
        }
        return []

    async def mnemosyne(_user_id, _user_msg, limit=3, layer_quotas=None):
        captured["mnemosyne"] = {
            "limit": limit,
            "layer_quotas": dict(layer_quotas or {}),
        }
        return []

    h._get_analysis_history_entries = history
    h._retrieve_from_builtin_memory = builtin
    h._retrieve_from_mnemosyne = mnemosyne
    asyncio.run(
        h._get_analysis_memory_entries(
            "u1",
            "hello",
            scene_policy={"recall_cooldown_seconds": 600, "event_limit": 1},
        )
    )
    assert captured["builtin"]["limit"] == 2
    assert captured["builtin"]["cooldown_seconds"] == 600
    assert captured["builtin"]["layer_quotas"]["event_limit"] == 1
    assert captured["mnemosyne"]["limit"] == 2
    assert captured["mnemosyne"]["layer_quotas"]["event_limit"] == 1


def test_analysis_fingerprint_includes_scene_policy() -> None:
    h = Harness()
    entries = [{"role": "memory", "content": "\u8bb0\u5f97\u627f\u8bfa"}]
    lean_key = h._build_analysis_request_fingerprint(
        "s1",
        "hello",
        entries,
        scene_policy={"scene": "group", "mode": "lean", "event_limit": 1, "memory_limit": 2},
    )
    rich_key = h._build_analysis_request_fingerprint(
        "s1",
        "hello",
        entries,
        scene_policy={"scene": "private", "mode": "rich", "event_limit": 3, "memory_limit": 5},
    )
    assert lean_key != rich_key


def test_default_inject_prompt_keeps_existing_contexts() -> None:
    h = Harness()
    h.config = {}
    h._apply_config()
    state = _state()
    h._perf_stats = {}
    existing_contexts = [{"role": "user", "content": "astrbot history"}]
    req = types.SimpleNamespace(system_prompt="astrbot system", contexts=list(existing_contexts))

    async def build_prompt(*args, **kwargs):
        return "plugin system"

    h._build_system_prompt = build_prompt
    asyncio.run(
        h._inject_prompt_and_context(
            req,
            "u1",
            state,
            "hello",
            {"\u7528\u6237\u610f\u56fe": "\u666e\u901a\u56de\u5e94", "\u5173\u7cfb\u4fe1\u53f7": "\u6682\u65e0\u660e\u663e\u5173\u7cfb\u63a8\u8fdb"},
            {},
            False,
        )
    )
    assert req.contexts == existing_contexts
    assert "plugin system" in req.system_prompt


def test_context_injection_respects_existing_astrbot_contexts() -> None:
    h = Harness()
    h.config = {"enable_context_injection": True, "inject_summary_as_context": True}
    h._apply_config()
    h._perf_stats = {}
    state = _state()
    existing_contexts = [{"role": "user", "content": "astrbot history"}]
    req = types.SimpleNamespace(system_prompt="astrbot system", contexts=list(existing_contexts))

    async def build_prompt(*args, **kwargs):
        return "plugin system"

    async def fail_history(*args, **kwargs):
        raise AssertionError("plugin history should not be loaded when AstrBot contexts exist")

    def fail_profile(*args, **kwargs):
        raise AssertionError("plugin summary should not be loaded when AstrBot contexts exist")

    h._build_system_prompt = build_prompt
    h._get_recent_history_async = fail_history
    h._get_profile = fail_profile
    asyncio.run(
        h._inject_prompt_and_context(
            req,
            "u1",
            state,
            "hello",
            {"\u7528\u6237\u610f\u56fe": "\u666e\u901a\u56de\u5e94", "\u5173\u7cfb\u4fe1\u53f7": "\u6682\u65e0\u660e\u663e\u5173\u7cfb\u63a8\u8fdb"},
            {},
            False,
        )
    )
    assert req.contexts == existing_contexts


def test_context_injection_coerces_dirty_limits() -> None:
    h = Harness()
    h.config = {
        "enable_context_injection": True,
        "avoid_duplicate_context_injection": False,
    }
    h._apply_config()
    h.max_context_messages = "bad"
    h.max_tokens_per_message = "bad"
    h._perf_stats = {}
    state = _state()
    req = types.SimpleNamespace(system_prompt="", contexts=[])
    requested_limits = []

    async def build_prompt(*args, **kwargs):
        return "plugin system"

    async def recent_history(*args, **kwargs):
        requested_limits.append(kwargs.get("limit"))
        return [{"role": "user", "content": "x" * 300}]

    h._build_system_prompt = build_prompt
    h._get_recent_history_async = recent_history
    asyncio.run(
        h._inject_prompt_and_context(
            req,
            "u1",
            state,
            "hello",
            {"\u7528\u6237\u610f\u56fe": "\u666e\u901a\u56de\u5e94", "\u5173\u7cfb\u4fe1\u53f7": "\u6682\u65e0\u660e\u663e\u5173\u7cfb\u63a8\u8fdb"},
            {},
            False,
        )
    )
    assert requested_limits == [TOKEN_OPT_CONTEXT_HISTORY_LIMIT]
    assert len(req.contexts) == 1
    assert len(req.contexts[0]["content"]) == TOKEN_OPT_CONTEXT_MAX_CHARS_PER_MSG


def test_live_observation_tracks_cache_context_and_group_privacy() -> None:
    h = Harness()
    state = _state()
    state["??Prompt??"] = {"tokens": 320, "compact": False}
    req = types.SimpleNamespace(
        system_prompt="stable prefix\nremember public info\nprivate note",
        contexts=[
            {"role": "user", "content": "same"},
            {"role": "assistant", "content": "same"},
            {"role": "user", "content": "private note"},
        ],
    )
    h._last_prompt_memory_selection_trace = [
        {"id": "public", "visibility": "public_profile", "memory_layer": "profile"},
        {"id": "private", "visibility": "private_only", "memory_layer": "event"},
    ]
    h._record_live_observation(
        user_id="group:g1::u1",
        event=_FakeEvent("u1", group_id="g1"),
        req=req,
        state=state,
        scene_policy={"scene": "group", "mode": "lean"},
        existing_context_count=1,
        skip_plugin_history=False,
    )
    req.system_prompt = "stable prefix\nchanged tail"
    req.contexts = []
    h._last_prompt_memory_selection_trace = []
    h._record_live_observation(
        user_id="group:g1::u1",
        event=_FakeEvent("u1", group_id="g1"),
        req=req,
        state=state,
        scene_policy={"scene": "group", "mode": "lean"},
        existing_context_count=0,
        skip_plugin_history=True,
    )
    samples = h._coerce_runtime_list_value(h._live_observation_samples)
    assert len(samples) == 2
    assert samples[0]["duplicate_context_count"] == 1
    assert samples[0]["system_overlap_count"] == 1
    assert samples[0]["group_privacy_risk"] == 1
    assert samples[1]["stable_prefix_ratio"] > 0
    report = h._build_live_observation_report(limit=2)
    assert "Context duplication risk: 1 sample(s)" in report
    assert "Group privacy risk: 1 suspicious" in report


def test_summary_context_skips_malformed_summary_entries() -> None:
    h = Harness()
    h._perf_stats = {}
    state = _state()
    state["_scene_memory_policy"] = {
        "context_injection_enabled": True,
        "inject_history": False,
        "inject_summary_in_context": True,
    }
    req = types.SimpleNamespace(system_prompt="", contexts=[])
    profile = h._get_profile("u1")
    profile["玛丽亚学习笔记"]["自动总结"] = [
        "legacy-bad-entry",
        {"bad": "missing summary"},
        {"summary": "最近聊过薄荷茶"},
    ]

    async def build_prompt(*args, **kwargs):
        return "plugin system"

    h._build_system_prompt = build_prompt
    asyncio.run(
        h._inject_prompt_and_context(
            req,
            "u1",
            state,
            "hello",
            {"用户意图": "普通回应", "关系信号": "暂无明显关系推进"},
            {},
            False,
        )
    )
    assert len(req.contexts) == 1
    assert "薄荷茶" in req.contexts[0]["content"]


def test_memory_layer_respects_prompt_budget_char_limit() -> None:
    h = Harness()
    h.enable_builtin_memory = False
    h.mnemosyne_available = True
    h.enable_emotional_memory = True
    h.enable_prompt_budget_memory_value_priority = False
    long_memory = "alpha" * 30
    short_memory = "beta"

    async def retrieve(_user_id, _user_msg, limit, **_kwargs):
        assert limit == 2
        return [
            {"content": long_memory, "memory_type": "event", "salience": 8},
            {"content": short_memory, "memory_type": "event", "salience": 7},
        ]

    h._retrieve_from_mnemosyne = retrieve
    layer = asyncio.run(
        h._build_memory_layer(
            "u1",
            "hello",
            memory_limit=2,
            memory_char_budget=20,
        )
    )
    assert "alphaalphaalpha" in layer
    assert short_memory not in layer


def test_memory_layer_prioritizes_high_value_under_budget() -> None:
    h = Harness()
    h.enable_builtin_memory = False
    h.mnemosyne_available = True
    h.enable_emotional_memory = True
    ordinary = "\u666e\u901a\u5370\u8c61" * 20
    promise = "\u7b2c\u4e00\u6b21\u8ba4\u771f\u627f\u8bfa\u4f1a\u8bb0\u5f97\u8fb9\u754c"

    async def retrieve(_user_id, _user_msg, limit, **_kwargs):
        assert limit == 3
        return [
            {"content": ordinary, "memory_layer": "impression", "memory_type": "interaction", "salience": 2},
            {"content": promise, "memory_layer": "event", "memory_type": "milestone", "salience": 6},
        ]

    h._retrieve_from_mnemosyne = retrieve
    layer = asyncio.run(
        h._build_memory_layer(
            "u1",
            "hello",
            memory_limit=1,
            memory_char_budget=120,
        )
    )
    assert "\u627f\u8bfa" in layer
    assert "\u666e\u901a\u5370\u8c61" not in layer


def test_memory_layer_candidate_expansion_under_budget() -> None:
    h = Harness()
    h.enable_builtin_memory = False
    h.mnemosyne_available = True
    h.enable_emotional_memory = True
    assert h._build_prompt_memory_candidate_limit(1, 120) == 3
    assert h._build_prompt_memory_candidate_limit(2, None) == 2

    async def retrieve(_user_id, _user_msg, limit, **_kwargs):
        assert limit == 3
        candidates = [
            {"content": "\u65e5\u5e38\u95ee\u5019", "memory_layer": "impression", "memory_type": "interaction", "salience": 1},
            {"content": "\u8bb0\u5f97\u4ed6\u7684\u8fb9\u754c\u548c\u79d8\u5bc6", "memory_layer": "event", "memory_type": "milestone", "salience": 7},
        ]
        return candidates[:limit]

    h._retrieve_from_mnemosyne = retrieve
    layer = asyncio.run(
        h._build_memory_layer(
            "u1",
            "hello",
            memory_limit=1,
            memory_char_budget=120,
        )
    )
    assert "\u8fb9\u754c" in layer
    assert "\u65e5\u5e38\u95ee\u5019" not in layer


def test_prompt_memory_slot_dedup() -> None:
    h = Harness()
    assert h._infer_prompt_memory_slot({"content": "\u7528\u6237\u751f\u65e5\u662f5\u670813\u65e5"}) == "birthday"
    assert h._infer_prompt_memory_slot({"content": "\u7528\u6237\u5e0c\u671b\u88ab\u79f0\u547c\u4e3a\u661f"}) == "nickname"
    assert h._infer_prompt_memory_slot({"content": "\u8bb0\u5f97\u4ed6\u4e0d\u559c\u6b22\u592a\u751c\u7684\u98df\u7269"}).startswith("preference")
    memories = [
        {
            "id": "old",
            "content": "\u7528\u6237\u5e0c\u671b\u88ab\u79f0\u547c\u4e3a\u65e7\u540d",
            "memory_layer": "profile",
            "memory_type": "profile",
            "salience": 4,
            "updated_at": "2026-01-01T00:00:00",
        },
        {
            "id": "new",
            "content": "\u7528\u6237\u5e0c\u671b\u88ab\u79f0\u547c\u4e3a\u661f",
            "memory_layer": "profile",
            "memory_type": "profile",
            "salience": 7,
            "updated_at": "2026-05-01T00:00:00",
        },
        {"id": "free", "content": "\u666e\u901a\u95ee\u5019", "memory_layer": "impression", "salience": 2},
    ]
    deduped = h._dedupe_prompt_memory_slots(memories)
    ids = [item["id"] for item in deduped]
    assert "new" in ids
    assert "old" not in ids
    assert "free" in ids
    trace = getattr(h, "_last_prompt_memory_slot_dedup_trace", [])
    assert trace
    assert trace[0]["slot"] == "nickname"
    assert trace[0]["dropped_id"] == "old"
    assert trace[0]["kept_id"] == "new"
    assert trace[0]["saved_chars"] > 0
    bad_trace = h._build_prompt_memory_selection_trace(
        {"id": "bad", "content": "\u627f\u8bfa", "memory_layer": "event", "salience": "bad"},
        slot="promise",
        value_priority=True,
        memory_char_budget="bad",
    )
    assert bad_trace["salience"] == 0
    bad_dedup_trace = h._build_prompt_memory_slot_dedup_trace(
        slot="nickname",
        kept={"id": "kept", "content": "\u661f", "salience": "bad"},
        dropped={"id": "drop", "content": "\u65e7\u540d", "salience": "bad"},
    )
    assert bad_dedup_trace["kept_salience"] == 0


def test_prompt_memory_selection_trace() -> None:
    h = Harness()
    memory = {
        "id": "promise123456",
        "content": "\u7528\u6237\u8ba4\u771f\u627f\u8bfa\u4f1a\u8bb0\u5f97\u8fb9\u754c",
        "memory_layer": "event",
        "memory_type": "milestone",
        "salience": 7,
        "temperature": "hot",
        "visibility": "private_only",
    }
    trace = h._build_prompt_memory_selection_trace(
        memory,
        slot=h._infer_prompt_memory_slot(memory),
        value_priority=True,
        memory_char_budget=120,
    )
    assert trace["slot"] in {"boundary", "promise"}
    assert trace["layer"] == "event"
    assert trace["salience"] == 7
    assert "value_priority" in trace["reason"]
    assert "high_salience" in trace["reason"]
    report = h._build_diagnostic_report(
        {
            "最近Prompt估算": {
                "memory_selection_trace": [trace],
                "memory_slot_dedup_saved_chars": 12,
                "memory_slot_dedup_trace": [
                    {"slot": "nickname", "dropped_id": "old", "kept_id": "new", "saved_chars": 12}
                ],
            }
        }
    )
    assert "\u8bb0\u5fc6\u5165\u9009\u539f\u56e0" in report
    assert "\u8bb0\u5fc6\u69fd\u4f4d\u53bb\u91cd" in report
    assert "\u7ea6\u770112\u5b57" in report
    assert "value_priority" in report


def test_builtin_memory_vector_helpers() -> None:
    h = Harness()
    assert h._normalize_embedding_vector({"data": [{"embedding": [1, "2", 3.0]}]}) == [1.0, 2.0, 3.0]
    assert h._normalize_embedding_vector(["bad"]) is None
    assert h._normalize_embedding_vector([1, float("nan")]) is None
    assert h._normalize_embedding_vector([1, float("inf")]) is None
    h.builtin_memory_vector_max_dimensions = 2
    assert h._normalize_embedding_vector([1, 2, 3]) is None
    h.builtin_memory_vector_max_dimensions = 4096
    assert round(h._cosine_similarity([1, 0], [1, 0]), 4) == 1.0
    assert round(h._cosine_similarity([1, 0], [0, 1]), 4) == 0.0
    assert h._cosine_similarity([1, float("nan")], [1, 0]) == 0.0
    payload = json.loads(
        h._memory_json_dumps(
            {"bad_float": float("nan"), "bad_set": {"b", "a"}, "bad_path": Path("memory.tmp")}
        )
    )
    assert payload == {"bad_float": 0.0, "bad_set": ["a", "b"], "bad_path": "memory.tmp"}

    keyword = [{"id": "a", "fingerprint": "a", "memory_layer": "event", "content": "alpha", "keywords": ["alpha"], "salience": 5}]
    vector = [{"id": "b", "fingerprint": "b", "memory_layer": "impression", "content": "beta", "keywords": ["beta"], "salience": 4, "vector_similarity": 0.91}]
    merged = h._merge_builtin_vector_memory_results(keyword, vector, [], 2)
    assert {item["id"] for item in merged} == {"a", "b"}
    assert any(item.get("vector_similarity") == 0.91 for item in merged)

    bad_vector = [{"id": "c", "fingerprint": "c", "memory_layer": "event", "content": "gamma", "keywords": [], "salience": "bad", "vector_similarity": "bad"}]
    assert h._merge_builtin_vector_memory_results([], bad_vector, [], 1)[0]["id"] == "c"
    h.builtin_memory_vector_weight = "bad"
    assert h._score_mnemosyne_entry({"content": "alpha", "normalized_content": "alpha", "keywords": ["alpha"], "salience": "bad", "hit_count": "bad", "vector_similarity": "bad"}, ["alpha"]) > 0
    assert h._get_memory_layer_quotas({"event": "bad", "impression_limit": "2"})["event"] == 0

    class BadNumericRow:
        def __init__(self):
            self.values = {
                "id": "m_bad",
                "user_id": "u",
                "layer": "event",
                "type": "milestone",
                "summary": "记住生日",
                "raw_content": "记住生日",
                "normalized_content": "记住生日",
                "keywords_json": None,
                "salience": "bad",
                "created_at": "",
                "updated_at": "",
                "last_hit_at": "",
                "hit_count": "bad",
                "reinforcement_count": "bad",
                "superseded_by": "",
                "superseded_at": "",
                "revision_of": "",
                "visibility": "",
                "evidence_json": 7,
                "temperature": "warm",
            }

        def __getitem__(self, key):
            return self.values[key]

        def keys(self):
            return self.values.keys()

    entry = h._row_to_memory_entry(BadNumericRow())
    assert entry["keywords"] == []
    assert entry["evidence"] == {}
    assert entry["salience"] == 0
    assert entry["hit_count"] == 0

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        h.local_memory_db_file = Path(tmp_dir) / "memory.db"
        h._init_builtin_memory_db_sync()
        h._upsert_builtin_memory_vector_sync("u1", "m_bad", [1, float("nan")])
        with h._connect_local_memory_db() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM memory_vectors").fetchone()["c"]
        assert count == 0
        h._upsert_builtin_memory_vector_sync("u1", "m_ok", [1, "2"])
        with h._connect_local_memory_db() as conn:
            row = conn.execute("SELECT dimensions, vector_json FROM memory_vectors WHERE memory_id = ?", ("m_ok",)).fetchone()
        assert row["dimensions"] == 2
        assert json.loads(row["vector_json"]) == [1.0, 2.0]

    class SparseMemoryRow:
        def __init__(self):
            self.values = {"id": "m_sparse", "summary": "only summary"}

        def __getitem__(self, key):
            if key not in self.values:
                raise IndexError(key)
            return self.values[key]

        def keys(self):
            return self.values.keys()

    sparse_entry = h._row_to_memory_entry(SparseMemoryRow())
    assert sparse_entry["id"] == "m_sparse"
    assert sparse_entry["content"] == "only summary"
    assert sparse_entry["memory_layer"] == "impression"
    assert sparse_entry["type"] == "interaction"
    assert sparse_entry["temperature"] == "warm"
    assert h._format_mnemosyne_memory_for_analysis(
        {"memory_layer": "event", "type": "milestone", "content": "承诺", "salience": "bad"}
    )


def test_mnemosyne_flush_done_consumes_exception_and_restarts() -> None:
    h = Harness()
    h._mnemosyne_flush_tasks = {}
    h._mnemosyne_write_buffers = {"memory.json": [{"fingerprint": "m1"}]}
    h._mnemosyne_write_waiters = {}
    restarted = []

    def restart(user_id, memory_file, cache_key, started_at):
        restarted.append((user_id, str(memory_file), cache_key, started_at))

    h._start_mnemosyne_flush_task = restart

    async def run_check():
        future = asyncio.get_running_loop().create_future()
        error = RuntimeError("flush failed before writer")
        future.set_exception(error)
        h._mnemosyne_flush_tasks["memory.json"] = future
        h._on_mnemosyne_flush_done("u1", Path("memory.json"), "memory.json", future)
        assert future.exception() is error

    asyncio.run(run_check())
    assert restarted
    assert restarted[0][0] == "u1"


def test_mnemosyne_runtime_caches_self_heal() -> None:
    h = Harness()
    for attr_name in ("_mnemosyne_flush_tasks", "_mnemosyne_write_buffers", "_mnemosyne_write_waiters"):
        if hasattr(h, attr_name):
            delattr(h, attr_name)

    started = []

    def start(_user_id, _memory_file, cache_key, _started_at):
        started.append(cache_key)
        h._get_mnemosyne_runtime_cache("_mnemosyne_flush_tasks")[cache_key] = type(
            "DoneTask",
            (),
            {"done": lambda self: True},
        )()

    h._start_mnemosyne_flush_task = start

    async def run_queue_check():
        h._mnemosyne_write_buffers = {"memory.json": "bad"}
        h._mnemosyne_write_waiters = {"memory.json": {"bad": True}}

        async def complete_later():
            await asyncio.sleep(0)
            waiters = h._get_mnemosyne_runtime_cache("_mnemosyne_write_waiters").get("memory.json", [])
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(True)

        waiter_task = asyncio.create_task(
            h._queue_mnemosyne_write(
                "u1",
                Path("memory.json"),
                {"fingerprint": "m1"},
                time.perf_counter(),
            )
        )
        await complete_later()
        assert await waiter_task is True

    asyncio.run(run_queue_check())
    assert started == ["memory.json"]
    assert h._mnemosyne_write_buffers["memory.json"][0]["fingerprint"] == "m1"

    async def run_dirty_flush_check():
        h._mnemosyne_write_buffers = {"dirty.json": "bad"}
        h._mnemosyne_write_waiters = {"dirty.json": "bad"}
        await h._flush_mnemosyne_writes(
            "u1",
            Path("dirty.json"),
            "dirty.json",
            time.perf_counter(),
        )
        assert "dirty.json" not in h._mnemosyne_write_buffers
        assert "dirty.json" not in h._mnemosyne_write_waiters

    asyncio.run(run_dirty_flush_check())

    h._mnemosyne_flush_tasks = {"dirty": "bad"}
    asyncio.run(h._drain_mnemosyne_flush_tasks())
    assert h._mnemosyne_flush_tasks == {}

    h._mnemosyne_flush_tasks = "bad"
    h._mnemosyne_write_buffers = "bad"
    h._mnemosyne_write_waiters = "bad"
    asyncio.run(h._drain_mnemosyne_flush_tasks())
    assert isinstance(h._mnemosyne_flush_tasks, dict)
    assert isinstance(h._mnemosyne_write_buffers, dict)
    assert isinstance(h._mnemosyne_write_waiters, dict)


def test_mnemosyne_flush_failure_notifies_waiters_and_logs() -> None:
    h = Harness()
    errors = []

    class CapturingLogger(_DummyLogger):
        def error(self, *args, **kwargs):
            errors.append((args, kwargs))

    async def fail_write(_memory_file, _entries):
        raise RuntimeError("disk full")

    async def run_check():
        waiter = asyncio.get_running_loop().create_future()
        h.logger = CapturingLogger()
        h._write_mnemosyne_entries = fail_write
        h._mnemosyne_write_buffers = {"memory.json": [{"fingerprint": "m1", "content": "alpha"}]}
        h._mnemosyne_write_waiters = {"memory.json": [waiter]}
        await h._flush_mnemosyne_writes(
            "u1",
            Path("memory.json"),
            "memory.json",
            time.perf_counter(),
        )
        assert waiter.done()
        assert waiter.result() is False

    asyncio.run(run_check())
    assert errors
    assert errors[0][1].get("exc_info") is True


def test_mnemosyne_entry_and_query_caches_self_heal_on_write() -> None:
    h = Harness()

    async def run_check():
        with tempfile.TemporaryDirectory() as tmpdir:
            memory_file = Path(tmpdir) / "memory.jsonl"
            h._mnemosyne_entries_cache = "bad"
            h._mnemosyne_query_cache = "bad"
            await h._write_mnemosyne_entries(
                memory_file,
                [{"fingerprint": "m1", "content": "alpha"}],
            )
            assert isinstance(h._mnemosyne_entries_cache, dict)
            assert isinstance(h._mnemosyne_query_cache, dict)
            assert h._mnemosyne_query_cache == {}
            loaded = h._load_mnemosyne_entries(memory_file)
            assert loaded[0]["fingerprint"] == "m1"

            h._mnemosyne_entries_cache = "bad"
            assert h._load_mnemosyne_entries(Path(tmpdir) / "missing.jsonl") == []
            assert isinstance(h._mnemosyne_entries_cache, dict)

    asyncio.run(run_check())


def test_memory_cache_returns_deep_copies() -> None:
    h = Harness()
    h._local_memory_query_cache = {}
    h._mnemosyne_query_cache = {}
    selected = [
        {
            "id": "m1",
            "content": "alpha",
            "keywords": ["alpha"],
            "evidence": {"reasons": ["seed"]},
        }
    ]

    h._cache_builtin_memory_query_result("builtin", selected)
    selected[0]["keywords"].append("source-mutated")
    first = h._get_cached_builtin_memory_query("builtin")
    assert first is not None
    first[0]["keywords"].append("returned-mutated")
    first[0]["evidence"]["reasons"].append("returned-mutated")
    second = h._get_cached_builtin_memory_query("builtin")
    assert second is not None
    assert second[0]["keywords"] == ["alpha"]
    assert second[0]["evidence"]["reasons"] == ["seed"]
    del h._local_memory_query_cache
    assert h._get_cached_builtin_memory_query("missing-cache") is None
    h._cache_builtin_memory_query_result("rebuilt-cache", selected)
    assert h._get_cached_builtin_memory_query("rebuilt-cache") is not None
    h._clear_local_memory_query_cache()
    assert h._get_cached_builtin_memory_query("rebuilt-cache") is None

    h._cache_mnemosyne_query_result("mnemo", selected)
    mnemo_first = h._get_cached_mnemosyne_query("mnemo")
    assert mnemo_first is not None
    mnemo_first[0]["keywords"].append("mnemo-mutated")
    mnemo_second = h._get_cached_mnemosyne_query("mnemo")
    assert mnemo_second is not None
    assert mnemo_second[0]["keywords"] == ["alpha", "source-mutated"]
    del h._mnemosyne_query_cache
    assert h._get_cached_mnemosyne_query("missing-cache") is None
    h._cache_mnemosyne_query_result("rebuilt-cache", selected)
    assert h._get_cached_mnemosyne_query("rebuilt-cache") is not None
    h._mnemosyne_query_cache["bad-ts"] = {"_created_at": "bad", "result": []}
    h._mnemosyne_query_cache["nan-ts"] = {"_created_at": float("nan"), "result": []}
    h._prune_mnemosyne_query_cache()
    assert "bad-ts" not in h._mnemosyne_query_cache
    assert "nan-ts" not in h._mnemosyne_query_cache
    h._mnemosyne_query_cache["inf-ts"] = {"_created_at": float("inf"), "result": []}
    assert h._get_cached_mnemosyne_query("inf-ts") is None
    assert "inf-ts" not in h._mnemosyne_query_cache


def test_mnemosyne_entry_numeric_helpers_tolerate_bad_values() -> None:
    h = Harness()
    merged = h._merge_duplicate_mnemosyne_entries(
        {"fingerprint": "a", "raw_content": "alpha", "content": "alpha", "keywords": ["a"], "salience": "bad", "hit_count": "bad", "reinforcement_count": "bad"},
        {"fingerprint": "b", "raw_content": "alpha beta", "content": "alpha beta", "keywords": ["b"], "salience": "bad", "hit_count": "bad", "reinforcement_count": "bad"},
    )
    assert merged["salience"] == 0
    assert merged["hit_count"] == 0
    assert merged["reinforcement_count"] == 0

    entry = {"fingerprint": "a", "raw_content": "alpha", "content": "alpha", "keywords": [], "salience": "bad", "hit_count": "bad", "reinforcement_count": "bad"}
    incoming = {"raw_content": "alpha beta", "content": "alpha beta", "keywords": ["beta"], "salience": "bad"}
    assert h._reinforce_existing_mnemosyne_entry(entry, incoming, "2026-05-15T00:00:00")
    assert entry["reinforcement_count"] == 1
    assert entry["salience"] == 0

    memories = [{"fingerprint": "hit", "raw_content": "alpha", "content": "alpha", "memory_layer": "impression", "salience": "bad", "hit_count": "bad"}]
    assert h._mark_mnemosyne_entries_hit(memories, [{"fingerprint": "hit"}])
    assert memories[0]["hit_count"] == 1
    state = h._get_state("cooldown-user")
    state["æœ€è¿‘å¬å›žè®°å¿†"] = [{"id": "hit", "time": float("inf")}]
    filtered = h._filter_memory_recall_cooldown(
        "cooldown-user",
        [{"id": "hit", "content": "alpha"}, {"id": "fresh", "content": "beta"}],
        limit=1,
        cooldown_seconds=3600,
    )
    assert [item["id"] for item in filtered] == ["hit"]

    hydrated = h._hydrate_mnemosyne_entry({"content": "alpha", "salience": "bad", "hit_count": "bad", "reinforcement_count": "bad"})
    assert hydrated is not None
    assert hydrated["salience"] >= 0
    assert hydrated["hit_count"] == 0

    h.memory_hard_cleanup_days = "bad"
    assert not h._should_prune_mnemosyne_entry(
        {"memory_layer": "impression", "salience": "bad", "hit_count": "bad", "timestamp": datetime.now().isoformat()}
    )
    updated = [{"fingerprint": "old", "raw_content": "alpha beta", "content": "alpha beta", "keywords": ["alpha"], "memory_layer": "impression", "type": "interaction", "salience": "bad"}]
    new_entry = {"fingerprint": "new", "raw_content": "alpha beta new", "content": "alpha beta new", "keywords": ["alpha"], "memory_layer": "impression", "type": "interaction", "salience": "bad"}
    assert h._apply_memory_update_layer(updated, new_entry, "2026-05-15T00:00:00")
    assert updated[0]["superseded_by"] == "new"
    assert asyncio.run(h._retrieve_from_mnemosyne("u1", "alpha", limit="bad")) == []

    async def write_bad_json_check():
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            target = Path(tmp_dir) / "memory.jsonl"
            await h._write_mnemosyne_entries(target, [{"content": "bad float", "bad": float("nan")}])
            payload = json.loads(target.read_text(encoding="utf-8"))
            assert payload["bad"] == 0.0

    asyncio.run(write_bad_json_check())


def test_metadata_yaml_has_required_fields() -> None:
    text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    fields = {}
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()

    for key in ("name", "display_name", "desc", "version", "author", "repo"):
        assert fields.get(key), f"metadata.yaml missing {key}"
    assert fields["name"] == "marianna"
    assert fields["version"] == f"v{PLUGIN_VERSION}"
    assert fields["repo"].startswith("https://")


def test_config_schema_exposes_vector_dimension_guard() -> None:
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    memory_limit = schema.get("memory_prompt_limit")
    assert isinstance(memory_limit, dict)
    assert memory_limit.get("default") == 5
    field = schema.get("builtin_memory_vector_max_dimensions")
    assert isinstance(field, dict)
    assert field.get("type") == "int"
    assert field.get("default") == 4096
    assert field.get("minimum") == 1
    assert field.get("maximum") == 32768
    temperature = schema.get("marianna_temperature")
    assert isinstance(temperature, dict)
    assert temperature.get("minimum") == 0
    assert temperature.get("maximum") == 2
    history_chars = schema.get("history_max_entry_chars")
    assert isinstance(history_chars, dict)
    assert history_chars.get("default") == 4000
    assert history_chars.get("minimum") == 200
    assert history_chars.get("maximum") == 20000


def main() -> None:
    tests = [
        test_state_delta_smoothing,
        test_relationship_cooldown,
        test_memory_quality_filter,
        test_memory_write_candidate_staging,
        test_memory_write_candidate_refresh_before_limit_trim,
        test_memory_write_candidate_tolerates_bad_counts,
        test_topic_resonance_gives_small_trust_delta,
        test_subtle_social_signals_prevent_frozen_opening_values,
        test_recent_memory_command_helpers,
        test_short_term_behavior_state,
        test_behavior_band_smoothing,
        test_behavior_continuity_bridge,
        test_behavior_action_budget_prompt,
        test_reply_variety_guard_prompt,
        test_reply_style_fingerprint,
        test_numeric_coercion_handles_infinite_values,
        test_reply_template_trim,
        test_reply_template_trim_categories,
        test_group_boundary_reply_dedup,
        test_time_decay_and_event_log,
        test_relationship_stage_uses_event_log,
        test_memory_recall_protection,
        test_missed_memory_penalty_uses_raw_content_and_protection,
        test_builtin_memory_reinforcement_preserves_manual_metadata,
        test_builtin_memory_write_coerces_dirty_numeric_fields,
        test_builtin_memory_export_uses_unique_files,
        test_builtin_memory_export_tolerates_bad_limit,
        test_builtin_memory_export_disabled_returns_none,
        test_builtin_memory_maintenance_wrappers_tolerate_failures,
        test_builtin_memory_import_caps_content_and_tolerates_bad_limit,
        test_builtin_memory_import_sanitizes_metadata,
        test_builtin_memory_paths_work_without_data_dir,
        test_builtin_memory_backfill_tolerates_bad_limit,
        test_builtin_memory_lookup_limits_are_safely_coerced,
        test_builtin_memory_cleanup_preserves_protected_rows,
        test_builtin_memory_bulk_delete_chunks_ids,
        test_builtin_memory_like_prefix_escapes_wildcards,
        test_builtin_memory_row_protection_tolerates_sparse_rows,
        test_lifelike_active_event,
        test_diagnostic_history_report,
        test_contextual_state_delta_rules,
        test_state_scope_mode,
        test_unstable_group_origin_reuses_recent_scoped_state,
        test_single_existing_group_state_is_reused_for_new_group_key,
        test_private_origin_alias_reuses_state_when_sender_id_changes,
        test_command_scope_falls_back_to_recent_group_state,
        test_debug_mode_propagates_across_user_scene_states,
        test_group_state_inherits_raw_debug_mode_on_first_turn,
        test_memory_privacy_bridge_and_temperature,
        test_memory_conflict_slot_and_cooldown,
        test_prompt_token_estimate_and_group_self_check,
        test_memory_mode_preset_and_profile_confidence,
        test_profile_update_tolerates_bad_interaction_count,
        test_scene_memory_policy_controls_active_event_cooldown,
        test_transient_scene_policy_is_not_persisted,
        test_dirty_state_markers_survive_failed_or_racing_save,
        test_dirty_profile_markers_survive_failed_save,
        test_runtime_task_and_state_save_caches_self_heal,
        test_runtime_config_and_perf_caches_self_heal,
        test_profile_save_and_update_caches_self_heal,
        test_global_state_and_destined_cache_self_heal,
        test_prompt_caches_self_heal,
        test_history_skips_recent_exact_duplicates,
        test_history_limits_are_safely_coerced,
        test_history_runtime_caches_self_heal,
        test_auto_summary_pass_self_heals_runtime_fields,
        test_auto_summary_pass_isolates_user_failures,
        test_auto_summary_loop_has_single_pass_entrypoint,
        test_memory_repair_mode_report_and_profile_prompt_confidence,
        test_adaptive_lightweight_prompt_and_diagnostic_estimate,
        test_prompt_budget_guard_falls_back_to_compact,
        test_group_lean_cache_first_prompt_is_small_and_stable,
        test_prompt_budget_memory_anchor_selection,
        test_prompt_budget_history_and_advice,
        test_prompt_layer_cost_helpers,
        test_prompt_budget_auto_throttle_policy,
        test_prompt_budget_stats_clear_streak,
        test_prompt_cost_profile_stats,
        test_prompt_budget_advice_uses_slot_dedup_savings,
        test_prompt_budget_auto_throttle_in_diagnostic,
        test_prompt_budget_throttle_log_summary,
        test_prompt_budget_throttle_escalation,
        test_prompt_budget_throttle_escalation_recovery,
        test_prompt_budget_memory_mode_policy,
        test_prompt_cost_auto_memory_mode_policy,
        test_prompt_cost_auto_memory_mode_sticky_upgrade,
        test_context_injection_defaults_cache_friendly,
        test_memory_mode_preset_respects_explicit_config_overrides,
        test_numeric_config_strings_are_safely_coerced,
        test_config_source_and_provider_ids_self_heal,
        test_boolean_config_strings_are_safely_coerced,
        test_release_and_config_audit_report,
        test_model_probe_report_detects_provider_ids,
        test_runtime_bad_numeric_state_is_tolerated,
        test_scene_memory_mode_policy_defaults,
        test_scene_memory_recall_cooldown_is_cache_scoped,
        test_scene_memory_layer_quotas_are_hard_caps,
        test_builtin_memory_recall_overfetches_for_cooldown_backfill,
        test_builtin_memory_hits_are_marked_after_cooldown,
        test_memory_layer_passes_scene_policy_to_builtin_retrieval,
        test_analysis_memory_passes_scene_policy_to_retrieval,
        test_analysis_fingerprint_includes_scene_policy,
        test_default_inject_prompt_keeps_existing_contexts,
        test_context_injection_respects_existing_astrbot_contexts,
        test_context_injection_coerces_dirty_limits,
        test_live_observation_tracks_cache_context_and_group_privacy,
        test_summary_context_skips_malformed_summary_entries,
        test_memory_layer_respects_prompt_budget_char_limit,
        test_memory_layer_prioritizes_high_value_under_budget,
        test_memory_layer_candidate_expansion_under_budget,
        test_prompt_memory_slot_dedup,
        test_prompt_memory_selection_trace,
        test_builtin_memory_vector_helpers,
        test_mnemosyne_flush_done_consumes_exception_and_restarts,
        test_mnemosyne_runtime_caches_self_heal,
        test_mnemosyne_flush_failure_notifies_waiters_and_logs,
        test_mnemosyne_entry_and_query_caches_self_heal_on_write,
        test_memory_cache_returns_deep_copies,
        test_mnemosyne_entry_numeric_helpers_tolerate_bad_values,
        test_metadata_yaml_has_required_fields,
        test_config_schema_exposes_vector_dimension_guard,
    ]
    for test in tests:
        test()
    print(f"{len(tests)} behavior checks passed.")


if __name__ == "__main__":
    main()
