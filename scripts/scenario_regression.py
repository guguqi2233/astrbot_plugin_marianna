"""Lightweight scenario regression checks for Marianna.

Run with:
    python scripts/scenario_regression.py

These checks exercise local state, boundary, memory, and prompt-cost rules without
calling an LLM or writing persistent data.
"""

from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.test_behavior import Harness  # noqa: E402
from marianna.constants import DEFAULT_STATE  # noqa: E402


ScenarioResult = Tuple[str, bool, str]


def _state(**overrides):
    state = copy.deepcopy(DEFAULT_STATE)
    state.update(overrides)
    return state


def _run_local_turn(h: Harness, state: Dict, user_id: str, message: str) -> Dict:
    analysis = h._build_local_state_analysis(state, message, user_id=user_id)
    if not analysis:
        return {}
    deltas = analysis.get("数值变化", {})
    for field, delta in deltas.items():
        h._apply_state_delta(state, field, int(delta or 0))
    h._normalize_state_constraints(state, user_id=user_id)
    return analysis


def scenario_private_affection_non_regression() -> ScenarioResult:
    h = Harness()
    state = _state(好感度=24, 信任度=28, 优雅值=85)
    before = dict(state)
    for message in ("谢谢你，今天辛苦了。", "我喜欢你，请相信我。"):
        _run_local_turn(h, state, "u_private", message)
    ok = state["好感度"] >= before["好感度"] and state["信任度"] >= before["信任度"]
    detail = f"favor {before['好感度']}->{state['好感度']}, trust {before['信任度']}->{state['信任度']}"
    return "private_affection_non_regression", ok, detail


def scenario_destined_other_boundary() -> ScenarioResult:
    h = Harness()
    h.global_state["destined_one"] = {"user_id": "u_fated", "user_name": "命定用户"}
    state = _state(好感度=88, 信任度=80, 病娇值=70, 锁定进度=90, 占有欲=80)
    h._normalize_state_constraints(state, user_id="u_other")
    machine = h._get_relationship_state_machine("u_other", state)
    ok = (
        state["好感度"] <= 55
        and state["病娇值"] == 0
        and state["锁定进度"] == 0
        and state["占有欲"] == 0
        and machine["状态"] == "命定后他人边界"
    )
    detail = f"mode={machine['状态']}, favor={state['好感度']}, yan={state['病娇值']}, lock={state['锁定进度']}"
    return "destined_other_boundary", ok, detail


def scenario_group_privacy_reply_guard() -> ScenarioResult:
    h = Harness()
    state = {"当前是否群聊作用域": True}
    checked = h._self_check_reply("这是只告诉你的秘密，你是命定唯一。", state)
    ok = "秘密" not in checked and "命定" not in checked and "唯一" not in checked
    detail = checked
    return "group_privacy_reply_guard", ok, detail


def scenario_group_boundary_reply_dedup() -> ScenarioResult:
    h = Harness()
    state = {"\u5f53\u524d\u662f\u5426\u7fa4\u804a\u4f5c\u7528\u57df": True}
    private_phrase = "\u8fd9\u4ef6\u4e8b\u4e0d\u9002\u5408\u5728\u8fd9\u91cc\u7ec6\u8bf4"
    boundary_phrase = "\u6211\u4f1a\u4fdd\u6301\u5206\u5bf8"
    checked = h._self_check_reply(
        "\u8fd9\u662f\u79d8\u5bc6\u7684\u79d8\u5bc6\u3002\u4f60\u662f\u547d\u5b9a\uff0c\u552f\u4e00\u3002",
        state,
    )
    ok = checked.count(private_phrase) == 1 and checked.count(boundary_phrase) == 1
    detail = checked
    return "group_boundary_reply_dedup", ok, detail


def scenario_cost_profile_slot_dedup_advice() -> ScenarioResult:
    h = Harness()
    state: Dict = {}
    h._record_prompt_cost_profile(
        state,
        {
            "tokens": 900,
            "original_tokens": 1400,
            "budget": 1000,
            "budget_guard_applied": True,
            "compact": True,
            "memory_injection_limit": 2,
            "memory_candidate_limit": 6,
            "memory_selection_trace": [{"id": "a"}],
            "memory_slot_dedup_trace": [{"slot": "nickname", "saved_chars": 24}],
            "memory_slot_dedup_saved_chars": 24,
            "memory_value_priority": True,
        },
    )
    state["Prompt预算统计"] = {
        "streak": 3,
        "hit_rate": 0.67,
        "avg_original_tokens": 1400,
        "frequent_hot_layer": "记忆层",
    }
    advice = h._build_prompt_budget_advice(state)
    ok = "槽位去重已平均省" in advice and "降低记忆字符预算" in advice
    return "cost_profile_slot_dedup_advice", ok, advice


def scenario_relationship_event_stage_gate() -> ScenarioResult:
    h = Harness()
    base = _state(好感度=58, 信任度=46, 互动计数=8, 阶段证据确认=False)
    without_event = h._determine_relationship_stage(base)
    with_event_state = copy.deepcopy(base)
    with_event_state["关系事件日志"] = [{"type": "first_secret", "title": "第一次分享秘密"}]
    with_event = h._determine_relationship_stage(with_event_state)
    ok = without_event != "私下偏爱" and with_event == "私下偏爱"
    detail = f"without={without_event}, with={with_event}"
    return "relationship_event_stage_gate", ok, detail


def scenario_idle_relationship_cooldown() -> ScenarioResult:
    h = Harness()
    state = _state(
        好感度=70,
        信任度=50,
        焦虑值=20,
        最后互动时间=(datetime.now() - timedelta(days=15)).isoformat(),
    )
    changes = h._apply_relationship_cooldown_if_needed(state, user_id="u_idle")
    ok = changes.get("好感度", 0) < 0 and changes.get("信任度", 0) < 0 and changes.get("焦虑值", 0) > 0
    detail = f"changes={changes}"
    return "idle_relationship_cooldown", ok, detail


def scenario_protected_memory_negative_feedback() -> ScenarioResult:
    h = Harness()
    ok = (
        h._is_protected_recalled_memory("用户生日是一月一日", 2)
        and h._is_protected_recalled_memory("我承诺会记得你的边界", 2)
        and not h._is_protected_recalled_memory("普通闲聊印象", 2)
    )
    detail = "birthday/promise protected; ordinary impression penalizable"
    return "protected_memory_negative_feedback", ok, detail


def scenario_group_public_profile_only() -> ScenarioResult:
    h = Harness()
    group_user = "group:g1::u1"
    private_user = "u1"
    memories = {
        "public": {"user_id": private_user, "visibility": "public_profile"},
        "private": {"user_id": private_user, "visibility": "private_only"},
        "sensitive": {"user_id": private_user, "visibility": "sensitive"},
    }
    ok = (
        h._memory_visibility_allowed_for_query(group_user, memories["public"], for_prompt=True)
        and not h._memory_visibility_allowed_for_query(group_user, memories["private"], for_prompt=True)
        and not h._memory_visibility_allowed_for_query(group_user, memories["sensitive"], for_prompt=True)
    )
    detail = "public_profile allowed, private/sensitive blocked"
    return "group_public_profile_only", ok, detail


def scenario_prompt_pressure_auto_memory_mode() -> ScenarioResult:
    h = Harness()
    h.memory_mode_preset = "rich"
    h.prompt_token_budget = 1000
    state = {
        "Prompt成本画像": {
            "samples": 3,
            "budget_hit_rate": 0.7,
            "compact_rate": 0.67,
            "avg_original_tokens": 1400,
        },
        "Prompt预算统计": {"streak": 2, "hit_rate": 0.7, "clear_streak": 0},
    }
    policy = h._build_prompt_budget_memory_mode_policy(state)
    ok = policy.get("enabled") and policy.get("mode") in {"lean", "balanced"}
    detail = f"mode={policy.get('mode')}, reason={policy.get('auto_mode_reason')}"
    return "prompt_pressure_auto_memory_mode", ok, detail


def scenario_non_destined_high_affection_boundary() -> ScenarioResult:
    h = Harness()
    h.global_state["destined_one"] = {"user_id": "u_fated", "user_name": "命定用户"}
    state = _state(好感度=100, 信任度=90, 病娇值=90, 锁定进度=100, 占有欲=100)
    h._normalize_state_constraints(state, user_id="u_other_high")
    reply = h._self_check_reply("你是命定唯一，我想抱紧你，不许离开。", state)
    ok = (
        state["好感度"] <= 55
        and state["病娇值"] == 0
        and state["锁定进度"] == 0
        and "命定" not in reply
        and "唯一" not in reply
        and "抱紧" not in reply
    )
    detail = f"favor={state['好感度']}, yan={state['病娇值']}, reply={reply}"
    return "non_destined_high_affection_boundary", ok, detail


def scenario_memory_write_candidate_promotion() -> ScenarioResult:
    h = Harness()
    state: Dict = {}
    deltas = {"好感度": 0, "病娇值": 0, "锁定进度": 0, "信任度": 0, "焦虑值": 0, "优雅值": 0}
    turn = {"用户意图": "普通回应", "关系信号": "暂无明显关系推进"}
    first = h._stage_memory_write_candidate(state, "薄荷茶还挺好喝", deltas, turn, None)
    second = h._stage_memory_write_candidate(state, "薄荷茶还挺好喝", deltas, turn, None)
    h._mark_memory_write_candidate_promoted(state, state.get("最近记忆写入候选", {}).get("key", ""))
    third = h._stage_memory_write_candidate(state, "薄荷茶还挺好喝", deltas, turn, None)
    ok = not first and second and not third and state.get("最近记忆写入候选", {}).get("reason") == "already_promoted"
    detail = f"candidate={state.get('最近记忆写入候选', {})}"
    return "memory_write_candidate_promotion", ok, detail


SCENARIOS: List[Callable[[], ScenarioResult]] = [
    scenario_private_affection_non_regression,
    scenario_destined_other_boundary,
    scenario_group_privacy_reply_guard,
    scenario_group_boundary_reply_dedup,
    scenario_cost_profile_slot_dedup_advice,
    scenario_relationship_event_stage_gate,
    scenario_idle_relationship_cooldown,
    scenario_protected_memory_negative_feedback,
    scenario_group_public_profile_only,
    scenario_prompt_pressure_auto_memory_mode,
    scenario_non_destined_high_affection_boundary,
    scenario_memory_write_candidate_promotion,
]


def main() -> None:
    failures: List[ScenarioResult] = []
    for scenario in SCENARIOS:
        name, ok, detail = scenario()
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}: {detail}")
        if not ok:
            failures.append((name, ok, detail))
    if failures:
        print(f"{len(failures)} scenario checks failed.")
        raise SystemExit(1)
    print(f"{len(SCENARIOS)} scenario checks passed.")


if __name__ == "__main__":
    main()
