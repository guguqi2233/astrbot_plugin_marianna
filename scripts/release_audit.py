"""Release readiness checks for the Marianna AstrBot plugin.

This script is intentionally lightweight and does not import AstrBot or the
plugin runtime. It checks packaging boundaries, high-risk cost defaults, and
basic documentation hygiene before a release.

Run with:
    python scripts/release_audit.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = (
    "main.py",
    "metadata.yaml",
    "_conf_schema.json",
    "README.md",
    "RELEASE_CHECKLIST.md",
    "requirements.txt",
    "marianna/__init__.py",
    "marianna/analysis.py",
    "marianna/constants.py",
    "marianna/history.py",
    "marianna/memory.py",
    "marianna/profile.py",
    "marianna/prompts.py",
    "marianna/runtime.py",
    "marianna/state_store.py",
    "marianna/turn.py",
    "scripts/test_behavior.py",
    "scripts/scenario_regression.py",
)

RUNTIME_DATA_PATHS = (
    "data/user_states.json",
    "data/user_profiles.json",
    "data/local_memory.db",
    "data/conversation_history",
    "data/memory_exports",
)

CONFIG_DEFAULTS = {
    "enable_context_injection": False,
    "avoid_duplicate_context_injection": True,
    "enable_token_cost_optimization": True,
    "enable_scene_memory_mode": True,
    "private_chat_memory_mode_preset": "rich",
    "group_chat_memory_mode_preset": "lean",
    "group_chat_context_injection": False,
    "group_chat_inject_summary_as_context": False,
    "enable_prompt_budget_guard": True,
    "enable_prompt_cost_auto_memory_mode": True,
}

DOC_MARKERS = (
    "enable_context_injection",
    "DeepSeek",
    "group_chat_memory_mode_preset",
    "private_chat_memory_mode_preset",
    "data/*",
)

MOJIBAKE_MARKERS = ("\ufffd", "é", "ç¼", "î†", "â‚¬")


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def load_schema() -> dict:
    schema_file = ROOT / "_conf_schema.json"
    with schema_file.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("_conf_schema.json must contain a JSON object")
    return data


def add_result(results: list[tuple[str, str, str]], level: str, name: str, detail: str) -> None:
    results.append((level, name, detail))


def check_required_files(results: list[tuple[str, str, str]]) -> None:
    for item in REQUIRED_FILES:
        path = ROOT / item
        if path.is_file():
            add_result(results, "OK", "required_file", item)
        else:
            add_result(results, "FAIL", "required_file", f"missing {item}")


def check_runtime_data(results: list[tuple[str, str, str]]) -> None:
    for item in RUNTIME_DATA_PATHS:
        path = ROOT / item
        if path.exists():
            add_result(
                results,
                "WARN",
                "runtime_data",
                f"{item} exists locally; keep it ignored and do not publish it",
            )
    gitignore = ROOT / ".gitignore"
    text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if "data/*" in text and "!data/.gitkeep" in text:
        add_result(results, "OK", "runtime_data_ignore", "data runtime files are ignored")
    else:
        add_result(results, "FAIL", "runtime_data_ignore", ".gitignore must ignore data/* and keep data/.gitkeep")

    tracked_runtime = list_tracked_runtime_files()
    if tracked_runtime:
        add_result(results, "FAIL", "tracked_runtime_data", "tracked runtime files: " + ", ".join(tracked_runtime[:8]))
    else:
        add_result(results, "OK", "tracked_runtime_data", "no tracked runtime data found")


def list_tracked_runtime_files() -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except Exception:
        return []
    tracked: list[str] = []
    for line in completed.stdout.splitlines():
        path = line.strip().replace("\\", "/")
        if not path or path == "data/.gitkeep":
            continue
        if path.startswith("data/") or path.endswith(".pyc") or "/__pycache__/" in path:
            tracked.append(path)
    return tracked


def check_schema_defaults(results: list[tuple[str, str, str]]) -> None:
    schema = load_schema()
    for key, expected in CONFIG_DEFAULTS.items():
        field = schema.get(key)
        if not isinstance(field, dict):
            add_result(results, "FAIL", "config_default", f"{key} missing from _conf_schema.json")
            continue
        actual = field.get("default")
        if actual == expected:
            add_result(results, "OK", "config_default", f"{key}={actual!r}")
        else:
            add_result(results, "FAIL", "config_default", f"{key} default is {actual!r}, expected {expected!r}")


def check_docs(results: list[tuple[str, str, str]]) -> None:
    for filename in ("README.md", "RELEASE_CHECKLIST.md"):
        path = ROOT / filename
        if not path.exists():
            add_result(results, "FAIL", "docs", f"{filename} missing")
            continue
        text = path.read_text(encoding="utf-8")
        bad_markers = [marker for marker in MOJIBAKE_MARKERS if marker in text]
        if bad_markers:
            add_result(results, "FAIL", "docs_encoding", f"{filename} contains mojibake markers: {bad_markers}")
        else:
            add_result(results, "OK", "docs_encoding", f"{filename} utf-8 text looks clean")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for marker in DOC_MARKERS:
        if marker in readme:
            add_result(results, "OK", "docs_marker", marker)
        else:
            add_result(results, "FAIL", "docs_marker", f"README.md missing {marker}")


def check_pycache(results: list[tuple[str, str, str]]) -> None:
    pycache_dirs = [path for path in ROOT.rglob("__pycache__") if path.is_dir()]
    pyc_files = [path for path in ROOT.rglob("*.pyc") if path.is_file()]
    if pycache_dirs or pyc_files:
        sample = [rel(path) for path in (pycache_dirs[:3] + pyc_files[:3])]
        add_result(results, "WARN", "python_cache", "local cache exists: " + ", ".join(sample))
    else:
        add_result(results, "OK", "python_cache", "no local Python cache found")


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    strict = "--strict" in argv
    if any(arg not in {"--strict"} for arg in argv):
        print("usage: python scripts/release_audit.py [--strict]", file=sys.stderr)
        return 2

    results: list[tuple[str, str, str]] = []
    check_required_files(results)
    check_runtime_data(results)
    check_schema_defaults(results)
    check_docs(results)
    check_pycache(results)

    for level, name, detail in results:
        print(f"[{level}] {name}: {detail}")

    fail_count = sum(1 for level, _, _ in results if level == "FAIL")
    warn_count = sum(1 for level, _, _ in results if level == "WARN")
    strict_fail_count = fail_count + (warn_count if strict else 0)
    suffix = " strict" if strict else ""
    print(f"release audit{suffix}: {fail_count} failure(s), {warn_count} warning(s)")
    return 1 if strict_fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
