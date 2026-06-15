"""
Claude Code Usage Log Parser
~/.claude/projects/ 하위의 JSONL 세션 로그를 파싱하여 usage_data.json 으로 저장합니다.
"""

import io
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Windows 콘솔 UTF-8 출력 보장
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ──────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────
PROJECTS_DIR = Path.home() / ".claude" / "projects"
OUTPUT_JSON = Path(__file__).parent / "usage_data.json"
OUTPUT_DB = Path.home() / ".claude" / "usage.db"

# 모델별 단가 (USD / 1M tokens)
MODEL_PRICING: dict[str, dict[str, float]] = {
    "default": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
}

# 모델 이름 → 가격 매핑 (prefix 매칭)
MODEL_PRICE_MAP = [
    ("claude-opus-4",     {"input": 15.0,  "output": 75.0,  "cache_read": 1.5,  "cache_write": 18.75}),
    ("claude-opus",       {"input": 15.0,  "output": 75.0,  "cache_read": 1.5,  "cache_write": 18.75}),
    ("claude-sonnet-4",   {"input": 3.0,   "output": 15.0,  "cache_read": 0.3,  "cache_write": 3.75}),
    ("claude-sonnet",     {"input": 3.0,   "output": 15.0,  "cache_read": 0.3,  "cache_write": 3.75}),
    ("claude-haiku-4",    {"input": 0.8,   "output": 4.0,   "cache_read": 0.08, "cache_write": 1.0}),
    ("claude-haiku",      {"input": 0.8,   "output": 4.0,   "cache_read": 0.08, "cache_write": 1.0}),
]

# 작업 유형 분류 키워드
TASK_KEYWORDS: dict[str, list[str]] = {
    "code_write": [
        "write", "create", "implement", "build", "generate", "add feature",
        "작성", "만들어", "구현", "생성", "새로",
    ],
    "debugging": [
        "fix", "debug", "error", "bug", "issue", "broken", "not working",
        "수정", "에러", "버그", "오류", "안 돼", "안됨",
    ],
    "refactoring": [
        "refactor", "improve", "optimize", "clean", "restructure",
        "리팩토링", "개선", "최적화", "정리",
    ],
    "testing": [
        "test", "unit test", "e2e", "spec", "coverage",
        "테스트", "검증",
    ],
    "documentation": [
        "document", "comment", "readme", "docs", "explain",
        "문서", "주석", "설명",
    ],
    "planning": [
        "plan", "roadmap", "milestone", "next step", "prioritize",
        "기획", "계획", "로드맵", "우선순위", "다음 작업",
    ],
    "ui_ux": [
        "ui", "ux", "layout", "design", "screen", "responsive",
        "화면", "레이아웃", "디자인", "반응형", "스타일",
    ],
    "deployment": [
        "deploy", "release", "ship", "production", "publish", "ci/cd",
        "배포", "릴리스", "출시", "운영", "파이프라인",
    ],
    "performance": [
        "performance", "latency", "optimize", "memory", "fps", "benchmark",
        "성능", "지연", "메모리", "프레임", "벤치마크",
    ],
    "review": [
        "review", "check", "analyze", "audit", "inspect",
        "검토", "분석", "확인", "리뷰",
    ],
}

# 도구 사용 → 작업 유형 힌트
TOOL_TASK_HINTS: dict[str, str] = {
    "write": "code_write",
    "create": "code_write",
    "edit": "refactoring",
    "str_replace": "refactoring",
    "patch": "refactoring",
    "test": "testing",
    "pytest": "testing",
    "unittest": "testing",
    "lint": "review",
    "format": "refactoring",
    "build": "deployment",
    "deploy": "deployment",
    "bash": "debugging",
    "computer": "debugging",
    "read": "review",
}

WORK_ITEM_GAP_MIN = 20
KOREAN_RE = re.compile(r"[가-힣]")


def clean_prompt_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_meaningful_prompt(text: str) -> bool:
    if not text:
        return False

    cleaned = clean_prompt_text(text)
    if len(cleaned) < 8:
        return False

    lower = cleaned.lower()
    ignore_markers = (
        "tool_result",
        "tool_use_error",
        "has been updated successfully",
        "is now visible in the launch preview panel",
        "the user opened the file",
    )
    if any(m in lower for m in ignore_markers):
        return False

    return True


def pick_summary_prompt(prompts: list[str]) -> str:
    if not prompts:
        return ""

    cleaned = [clean_prompt_text(p) for p in prompts if clean_prompt_text(p)]
    if not cleaned:
        return ""

    korean = [p for p in cleaned if KOREAN_RE.search(p)]
    return (korean[-1] if korean else cleaned[-1])[:200]


# ──────────────────────────────────────────────
# 유틸리티
# ──────────────────────────────────────────────

def get_model_price(model: str) -> dict[str, float]:
    model_lower = (model or "").lower()
    for prefix, price in MODEL_PRICE_MAP:
        if model_lower.startswith(prefix):
            return price
    return MODEL_PRICING["default"]


def calc_cost(usage: dict[str, int], model: str) -> float:
    price = get_model_price(model)
    cost = (
        usage.get("input_tokens", 0) * price["input"] / 1_000_000
        + usage.get("output_tokens", 0) * price["output"] / 1_000_000
        + usage.get("cache_read_input_tokens", 0) * price["cache_read"] / 1_000_000
        + usage.get("cache_creation_input_tokens", 0) * price["cache_write"] / 1_000_000
    )
    return round(cost, 6)


def classify_task(text: str, tool_counts: dict[str, int]) -> str:
    """텍스트와 도구 사용 빈도로 작업 유형을 추론합니다."""
    text_lower = text.lower()
    scores: dict[str, int] = defaultdict(int)

    for task_type, keywords in TASK_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                scores[task_type] += 1

    for tool, hint in TOOL_TASK_HINTS.items():
        count = tool_counts.get(tool, 0)
        if count > 0:
            scores[hint] += count

    if not scores:
        return "other"
    return max(scores, key=lambda k: scores[k])


def normalize_model_name(model: str) -> str:
    model_lower = (model or "unknown").lower()
    if "opus" in model_lower:
        return "Opus"
    if "sonnet" in model_lower:
        return "Sonnet"
    if "haiku" in model_lower:
        return "Haiku"
    return model or "Unknown"


def decode_project_path(folder_name: str) -> str:
    """C--Users-username-MyProj → MyProj 형태로 변환합니다."""
    # 폴더명에서 마지막 의미있는 세그먼트 추출
    parts = folder_name.replace("--", "/").split("/")
    meaningful = [p for p in parts if p and p.lower() not in ("c", "users", "username", "onedrive")]
    return "/".join(meaningful) if meaningful else folder_name


def extract_first_text(content: Any) -> str:
    """메시지 content에서 첫 번째 텍스트를 추출합니다."""
    if isinstance(content, str):
        return content[:300]
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")[:300]
    return ""


# ──────────────────────────────────────────────
# 파서 핵심 로직
# ──────────────────────────────────────────────

def parse_session_file(jsonl_path: Path) -> dict[str, Any] | None:
    """
    단일 JSONL 파일(= 하나의 세션)을 파싱합니다.
    반환값: 세션 정보 dict 또는 None
    """
    session_id = jsonl_path.stem
    project_folder = jsonl_path.parent.name
    project_name = decode_project_path(project_folder)

    messages: list[dict] = []
    assistant_records: list[dict] = []
    meaningful_prompts: list[str] = []
    tool_counts: dict[str, int] = defaultdict(int)
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    model_used = "unknown"
    model_full = "unknown"
    work_items_raw: list[dict[str, Any]] = []
    current_work: dict[str, Any] | None = None
    pending_user_ts: datetime | None = None
    session_response_secs: list[float] = []

    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                record_type = record.get("type", "")

                # 타임스탬프 수집
                ts_str = record.get("timestamp")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if first_ts is None or ts < first_ts:
                            first_ts = ts
                        if last_ts is None or ts > last_ts:
                            last_ts = ts
                    except ValueError:
                        pass

                # 사용자 메시지 수집
                if record_type == "user":
                    msg = record.get("message", {})
                    text = extract_first_text(msg.get("content", ""))
                    messages.append({"role": "user", "text": text})

                    if is_meaningful_prompt(text):
                        meaningful_prompts.append(text)

                        if ts_str:
                            try:
                                user_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            except ValueError:
                                user_ts = None
                        else:
                            user_ts = None

                        if user_ts:
                            need_new_work = (
                                current_work is None
                                or (user_ts - current_work["last_ts"]).total_seconds() > WORK_ITEM_GAP_MIN * 60
                            )
                            if need_new_work:
                                current_work = {
                                    "start_ts": user_ts,
                                    "last_ts": user_ts,
                                    "end_ts": user_ts,
                                    "prompts": [],
                                    "model": model_used,
                                    "response_secs": [],
                                    "input_tokens": 0,
                                    "output_tokens": 0,
                                    "cache_read_tokens": 0,
                                    "cache_write_tokens": 0,
                                    "cost_usd": 0.0,
                                }
                                work_items_raw.append(current_work)

                            if current_work is not None:
                                current_work["last_ts"] = user_ts
                                current_work["prompts"].append(text)
                            pending_user_ts = user_ts

                # 어시스턴트 메시지 수집 (usage 포함)
                elif record_type == "assistant":
                    msg = record.get("message", {})
                    usage = msg.get("usage", {})
                    if usage:
                        m = msg.get("model", "")
                        if m:
                            model_used = m
                            model_full = m
                        # 도구 사용 카운트
                        for block in msg.get("content", []):
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tool_name = block.get("name", "").lower()
                                for key in TOOL_TASK_HINTS:
                                    if key in tool_name:
                                        tool_counts[key] += 1
                                        break
                        assistant_records.append({
                            "model": model_used,
                            "usage": usage,
                        })

                        if ts_str:
                            try:
                                assistant_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            except ValueError:
                                assistant_ts = None
                        else:
                            assistant_ts = None

                        if current_work is not None:
                            if assistant_ts:
                                current_work["last_ts"] = max(current_work["last_ts"], assistant_ts)
                                current_work["end_ts"] = max(current_work["end_ts"], assistant_ts)

                                if pending_user_ts and assistant_ts >= pending_user_ts:
                                    response_sec = (assistant_ts - pending_user_ts).total_seconds()
                                    if 0 <= response_sec <= 3600:
                                        session_response_secs.append(response_sec)
                                        current_work["response_secs"].append(response_sec)
                                    pending_user_ts = None

                            current_work["model"] = model_used or current_work["model"]
                            current_work["input_tokens"] += usage.get("input_tokens", 0)
                            current_work["output_tokens"] += usage.get("output_tokens", 0)
                            current_work["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
                            current_work["cache_write_tokens"] += usage.get("cache_creation_input_tokens", 0)
                            current_work["cost_usd"] += calc_cost(usage, model_used)

    except OSError as e:
        print(f"  ⚠ 파일 읽기 실패: {jsonl_path} — {e}")
        return None

    # 집계
    total_input = total_output = total_cache_read = total_cache_write = 0
    for rec in assistant_records:
        u = rec["usage"]
        total_input += u.get("input_tokens", 0)
        total_output += u.get("output_tokens", 0)
        total_cache_read += u.get("cache_read_input_tokens", 0)
        total_cache_write += u.get("cache_creation_input_tokens", 0)

    total_tokens = total_input + total_output + total_cache_read + total_cache_write

    if total_tokens <= 0:
        return None

    cost = calc_cost(
        {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_read_input_tokens": total_cache_read,
            "cache_creation_input_tokens": total_cache_write,
        },
        model_used,
    )

    # 소요 시간 (분)
    duration_min = 0
    if first_ts and last_ts:
        duration_min = round((last_ts - first_ts).total_seconds() / 60, 1)

    first_user_text = pick_summary_prompt(meaningful_prompts)
    task_type = classify_task(first_user_text, tool_counts)
    avg_response_sec = round(sum(session_response_secs) / len(session_response_secs), 1) if session_response_secs else None

    active_duration_min = 0.0
    work_items: list[dict[str, Any]] = []
    for idx, w in enumerate(work_items_raw, start=1):
        start_w = w["start_ts"]
        end_w = w["end_ts"]
        duration_w = max(0.0, round((end_w - start_w).total_seconds() / 60, 1))
        active_duration_min += duration_w

        w_input = w["input_tokens"]
        w_output = w["output_tokens"]
        w_cache_read = w["cache_read_tokens"]
        w_cache_write = w["cache_write_tokens"]
        w_total = w_input + w_output + w_cache_read + w_cache_write
        if w_total <= 0:
            continue

        w_model = w["model"] or model_used
        w_summary = pick_summary_prompt(w["prompts"])
        w_response = w.get("response_secs", [])
        avg_w_response_sec = round(sum(w_response) / len(w_response), 1) if w_response else None

        work_items.append({
            "work_id": f"{session_id}#{idx}",
            "session_id": session_id,
            "project_folder": project_folder,
            "project_name": project_name,
            "timestamp": start_w.isoformat(),
            "last_timestamp": end_w.isoformat(),
            "duration_min": duration_w,
            "avg_response_sec": avg_w_response_sec,
            "model": w_model,
            "model_detail": w_model,
            "model_display": normalize_model_name(w_model),
            "task_type": classify_task(w_summary, {}),
            "summary_prompt": w_summary,
            "input_tokens": w_input,
            "output_tokens": w_output,
            "cache_read_tokens": w_cache_read,
            "cache_write_tokens": w_cache_write,
            "total_tokens": w_total,
            "cost_usd": round(w["cost_usd"], 6),
        })

    if not work_items and first_ts:
        work_items.append({
            "work_id": f"{session_id}#1",
            "session_id": session_id,
            "project_folder": project_folder,
            "project_name": project_name,
            "timestamp": first_ts.isoformat(),
            "last_timestamp": last_ts.isoformat() if last_ts else first_ts.isoformat(),
            "duration_min": duration_min,
            "avg_response_sec": avg_response_sec,
            "model": model_used,
            "model_detail": model_full,
            "model_display": normalize_model_name(model_used),
            "task_type": task_type,
            "summary_prompt": first_user_text,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_read_tokens": total_cache_read,
            "cache_write_tokens": total_cache_write,
            "total_tokens": total_tokens,
            "cost_usd": cost,
        })

    # 세션 소요 시간은 전체 span보다 활동 구간의 합을 우선 사용
    duration_min = round(active_duration_min, 1) if active_duration_min > 0 else duration_min

    return {
        "session_id": session_id,
        "project_folder": project_folder,
        "project_name": project_name,
        "timestamp": first_ts.isoformat() if first_ts else None,
        "last_timestamp": last_ts.isoformat() if last_ts else None,
        "duration_min": duration_min,
        "avg_response_sec": avg_response_sec,
        "model": model_used,
        "model_detail": model_full,
        "model_display": normalize_model_name(model_used),
        "task_type": task_type,
        "first_prompt": first_user_text[:200],
        "message_count": len(messages),
        "assistant_turns": len(assistant_records),
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cache_read_tokens": total_cache_read,
        "cache_write_tokens": total_cache_write,
        "total_tokens": total_tokens,
        "cost_usd": cost,
        "work_items": work_items,
    }


# ──────────────────────────────────────────────
# 집계 및 저장
# ──────────────────────────────────────────────

def aggregate(sessions: list[dict]) -> dict[str, Any]:
    """세션 목록을 대시보드용 집계 구조로 변환합니다."""

    # 일별 집계
    daily: dict[str, dict] = defaultdict(lambda: {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
        "cost": 0.0, "sessions": 0,
    })
    # 시간대별
    hourly: dict[int, int] = defaultdict(int)
    # 요일×시간 히트맵 (weekday 0=월, hour 0~23)
    heatmap: dict[str, int] = defaultdict(int)
    # 모델별
    by_model: dict[str, dict] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "sessions": 0})
    # 작업 유형별
    by_task: dict[str, dict] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "sessions": 0})
    # 프로젝트별
    by_project: dict[str, dict] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "sessions": 0})

    for s in sessions:
        if not s["timestamp"]:
            continue

        try:
            ts = datetime.fromisoformat(s["timestamp"])
        except ValueError:
            continue

        day = ts.strftime("%Y-%m-%d")
        hour = ts.hour
        weekday = ts.weekday()  # 0=월요일
        hk = f"{weekday}_{hour}"

        daily[day]["input"] += s["input_tokens"]
        daily[day]["output"] += s["output_tokens"]
        daily[day]["cache_read"] += s["cache_read_tokens"]
        daily[day]["cache_write"] += s["cache_write_tokens"]
        daily[day]["cost"] += s["cost_usd"]
        daily[day]["sessions"] += 1

        hourly[hour] += s["total_tokens"]
        heatmap[hk] += s["total_tokens"]

        mdl = s.get("model_detail") or s.get("model") or s["model_display"]
        by_model[mdl]["tokens"] += s["total_tokens"]
        by_model[mdl]["cost"] += s["cost_usd"]
        by_model[mdl]["sessions"] += 1

        tt = s["task_type"]
        by_task[tt]["tokens"] += s["total_tokens"]
        by_task[tt]["cost"] += s["cost_usd"]
        by_task[tt]["sessions"] += 1

        pj = s["project_name"]
        by_project[pj]["tokens"] += s["total_tokens"]
        by_project[pj]["cost"] += s["cost_usd"]
        by_project[pj]["sessions"] += 1

    # 총계
    total_tokens = sum(s["total_tokens"] for s in sessions)
    total_cost = sum(s["cost_usd"] for s in sessions)
    total_sessions = len(sessions)
    avg_duration = (
        sum(s["duration_min"] for s in sessions) / total_sessions
        if total_sessions else 0
    )
    peak_hour = max(hourly, key=lambda h: hourly[h]) if hourly else 0

    return {
        "summary": {
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 4),
            "total_sessions": total_sessions,
            "avg_duration_min": round(avg_duration, 1),
            "peak_hour": peak_hour,
            "peak_hour_tokens": hourly.get(peak_hour, 0),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "sessions": sessions,
        "daily": {
            day: {**v, "cost": round(v["cost"], 6)}
            for day, v in sorted(daily.items())
        },
        "hourly": {str(h): hourly[h] for h in range(24)},
        "heatmap": dict(heatmap),
        "by_model": dict(by_model),
        "by_task": dict(by_task),
        "by_project": dict(
            sorted(by_project.items(), key=lambda x: x[1]["tokens"], reverse=True)[:20]
        ),
    }


def save_to_db(sessions: list[dict], db_path: Path) -> None:
    """SQLite 데이터베이스에 세션 데이터를 저장합니다."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            project_folder TEXT,
            project_name TEXT,
            timestamp TEXT,
            last_timestamp TEXT,
            duration_min REAL,
            model TEXT,
            model_display TEXT,
            task_type TEXT,
            first_prompt TEXT,
            message_count INTEGER,
            assistant_turns INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            total_tokens INTEGER,
            cost_usd REAL
        )
    """)
    for s in sessions:
        cur.execute("""
            INSERT OR REPLACE INTO sessions VALUES (
                :session_id, :project_folder, :project_name,
                :timestamp, :last_timestamp, :duration_min,
                :model, :model_display, :task_type, :first_prompt,
                :message_count, :assistant_turns,
                :input_tokens, :output_tokens, :cache_read_tokens,
                :cache_write_tokens, :total_tokens, :cost_usd
            )
        """, s)
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────

def main() -> None:
    print("📊 Claude Code 사용 분석 중...\n")

    if not PROJECTS_DIR.exists():
        print(f"❌ 프로젝트 디렉토리를 찾을 수 없습니다: {PROJECTS_DIR}")
        return

    # JSONL 파일 스캔
    jsonl_files: list[Path] = []
    for root, _dirs, files in os.walk(PROJECTS_DIR):
        for fname in files:
            if fname.endswith(".jsonl"):
                jsonl_files.append(Path(root) / fname)

    project_dirs = {f.parent for f in jsonl_files}
    print(f"✓ {len(project_dirs)}개 프로젝트 스캔 완료")
    print(f"✓ {len(jsonl_files)}개 세션 파일 발견\n")

    # 파싱
    sessions: list[dict] = []
    all_work_items: list[dict] = []
    for fpath in jsonl_files:
        result = parse_session_file(fpath)
        if result:
            all_work_items.extend(result.get("work_items", []))
            sessions.append(result)

    valid = len(sessions)
    skipped = len(jsonl_files) - valid
    print(f"✓ {valid}개 세션 파싱 성공 ({skipped}개 데이터 없음으로 제외)\n")

    if not sessions:
        print("⚠ 분석할 세션 데이터가 없습니다.")
        return

    # 작업 유형별 요약 출력
    task_labels = {
        "code_write": "코드 작성",
        "debugging":  "디버깅",
        "refactoring":"리팩토링",
        "testing":    "테스트",
        "documentation": "문서화",
        "planning": "기획/계획",
        "ui_ux": "UI/UX",
        "deployment": "배포",
        "performance": "성능",
        "review":     "리뷰",
        "other":      "기타",
    }
    task_stats: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "tokens": 0})
    for s in sessions:
        tt = s["task_type"]
        task_stats[tt]["sessions"] += 1
        task_stats[tt]["tokens"] += s["total_tokens"]

    print("작업 유형별 분류:")
    for tt, label in task_labels.items():
        st = task_stats.get(tt)
        if st:
            print(f"  - {label}: {st['sessions']} 세션 ({st['tokens']:,} 토큰)")

    total_tokens = sum(s["total_tokens"] for s in sessions)
    total_cost = sum(s["cost_usd"] for s in sessions)
    print(f"\n✓ 총 {total_tokens:,} 토큰 사용")
    print(f"✓ 예상 비용: ${total_cost:.4f} USD\n")

    # 집계
    sessions_for_aggregate = []
    for s in sessions:
        sx = dict(s)
        sx.pop("work_items", None)
        sessions_for_aggregate.append(sx)

    data = aggregate(sessions_for_aggregate)
    data["work_items"] = sorted(
        all_work_items,
        key=lambda x: x.get("timestamp", ""),
        reverse=True,
    )
    data["summary"]["total_work_items"] = len(all_work_items)

    # JSON 저장
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"💾 JSON 저장: {OUTPUT_JSON}")

    # SQLite 저장
    try:
        save_to_db(sessions, OUTPUT_DB)
        print(f"💾 DB 저장: {OUTPUT_DB}")
    except Exception as e:
        print(f"⚠ DB 저장 실패 (JSON은 정상 저장됨): {e}")

    print(f"\n🌐 대시보드 시작 준비 완료")
    print(f"   python start_dashboard.py  또는")
    print(f"   python -m http.server 8080  후 http://localhost:8080/dashboard.html")


if __name__ == "__main__":
    main()
