"""
GitHub Copilot Chat Log Parser
%APPDATA%/Code/logs/ 하위의 VS Code 확장 로그에서 Copilot Chat 요청을 파싱합니다.
토큰 수는 제공되지 않으므로 요청 수·응답 시간·모델 분포를 집계합니다.
"""

import io
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ──────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────
VSCODE_LOGS_DIR = Path(os.environ.get("APPDATA", "")) / "Code" / "logs"
OUTPUT_JSON = Path(__file__).parent / "copilot_data.json"
LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc

# 사용자에게 표시할 모델 그룹 이름
MODEL_GROUP_MAP = [
    ("claude-opus",     "Claude Opus"),
    ("claude-sonnet",   "Claude Sonnet"),
    ("claude-haiku",    "Claude Haiku"),
    ("gpt-5.4",         "GPT-5.4"),
    ("gpt-5.3",         "GPT-5.3 Codex"),
    ("gpt-5-mini",      "GPT-5 Mini"),
    ("gpt-4.1",         "GPT-4.1"),
    ("gpt-4o-mini",     "GPT-4o Mini"),
    ("gpt-4o",          "GPT-4o"),
    ("o3",              "o3"),
    ("o1",              "o1"),
    ("copilot-nes",     "Copilot NES"),
    ("copilot-suggestions", "Copilot Suggestions"),
    ("vscode-agentic",  "Agentic Router"),
]

# 모드 표시명
MODE_LABELS = {
    "panel/editAgent":               "Chat 패널",
    "copilotLanguageModelWrapper":   "내부 도구",
    "progressMessages":              "진행 메시지",
    "searchSubagentTool":            "검색 도구",
    "title":                         "제목 생성",
    "healApplyPatch":                "패치 적용",
    "retry-server-error-panel/editAgent": "재시도",
    "tool/runSubagent":              "서브 에이전트",
}

# ccreq 로그 라인 파싱 정규식
# 형태: 2026-04-29 15:13:24.020 [info] ccreq:ae618c82.copilotmd | success | model -> resolved | 4922ms | [mode]
# 또는:                                                              | success | model | 4922ms | [mode]
LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)"   # timestamp
    r" \[(?:info|warning|error)\] "
    r"ccreq:([0-9a-f]+)\.copilotmd"                     # request_id
    r" \| (success|failure|error|markdown)"             # status
    r"(?: \| ([^|]+?)"                                  # model (optional)
    r" \| (\d+)ms"                                      # duration_ms
    r" \| \[([^\]]+)\])?",                              # mode (optional)
    re.IGNORECASE,
)


def group_model(raw: str) -> str:
    raw_lower = raw.lower()
    for prefix, label in MODEL_GROUP_MAP:
        if raw_lower.startswith(prefix):
            return label
    return raw.split("-")[0].capitalize() if raw else "Unknown"


def label_mode(raw: str) -> str:
    return MODE_LABELS.get(raw, raw.split("/")[-1][:20])


def parse_log_file(log_path: Path, session_ts: datetime) -> list[dict]:
    """단일 Copilot Chat 로그 파일에서 ccreq 이벤트를 파싱합니다."""
    events: list[dict] = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = LOG_RE.match(line.rstrip())
                if not m:
                    continue

                ts_str, req_id, status, model_raw, dur_str, mode_raw = m.groups()

                # 타임스탬프 파싱
                try:
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f").replace(
                        tzinfo=LOCAL_TIMEZONE
                    )
                except ValueError:
                    ts = session_ts

                # 모델 정규화 ('alias -> resolved' 형태이면 alias 기준)
                model_raw = (model_raw or "").strip()
                if " -> " in model_raw:
                    model_alias = model_raw.split(" -> ")[0].strip()
                else:
                    model_alias = model_raw

                model_group = group_model(model_alias) if model_alias else "Unknown"
                duration_ms = int(dur_str) if dur_str else None
                mode = label_mode((mode_raw or "").strip()) if mode_raw else None

                events.append(
                    {
                        "request_id": req_id,
                        "timestamp": ts.isoformat(),
                        "status": status.lower(),
                        "model_raw": model_alias,
                        "model": model_group,
                        "duration_ms": duration_ms,
                        "mode": mode,
                    }
                )
    except OSError as e:
        print(f"  ⚠ 파일 읽기 실패: {log_path} — {e}")
    return events


def scan_all_logs() -> list[dict]:
    """VSCODE_LOGS_DIR 하위의 모든 Copilot Chat 로그 파일을 스캔합니다."""
    all_events: list[dict] = []
    log_files: list[Path] = []

    for root, _dirs, files in os.walk(VSCODE_LOGS_DIR):
        for fname in files:
            if fname == "GitHub Copilot Chat.log":
                log_files.append(Path(root) / fname)

    for lf in log_files:
        # 폴더명(세션 타임스탬프)에서 기준 시각 추출
        session_folder = lf.parts[-5]  # e.g. 20260429T145459
        try:
            session_ts = datetime.strptime(session_folder, "%Y%m%dT%H%M%S").replace(
                tzinfo=LOCAL_TIMEZONE
            )
        except ValueError:
            session_ts = datetime.now(timezone.utc)

        events = parse_log_file(lf, session_ts)
        all_events.extend(events)

    # 중복 request_id 제거 (여러 세션에 같은 캐시된 줄이 복사되는 경우 대비)
    seen: set[str] = set()
    unique: list[dict] = []
    for e in all_events:
        key = e["request_id"]
        if key not in seen:
            seen.add(key)
            unique.append(e)

    unique.sort(key=lambda x: x["timestamp"])
    return unique


def aggregate(events: list[dict]) -> dict[str, Any]:
    """이벤트 목록을 대시보드용 집계 구조로 변환합니다."""

    success_events = [e for e in events if e["status"] == "success"]

    # 일별 집계
    daily: dict[str, dict] = defaultdict(lambda: {
        "requests": 0, "success": 0, "failure": 0, "total_ms": 0,
    })
    hourly: dict[int, int] = defaultdict(int)
    heatmap: dict[str, int] = defaultdict(int)
    by_model: dict[str, dict] = defaultdict(lambda: {"requests": 0, "total_ms": 0})
    by_mode: dict[str, int] = defaultdict(int)
    durations: list[int] = []

    for e in events:
        try:
            ts = datetime.fromisoformat(e["timestamp"])
        except ValueError:
            continue

        day = ts.strftime("%Y-%m-%d")
        hour = ts.hour
        wd = ts.weekday()

        daily[day]["requests"] += 1
        if e["status"] == "success":
            daily[day]["success"] += 1
        else:
            daily[day]["failure"] += 1

        hourly[hour] += 1
        heatmap[f"{wd}_{hour}"] += 1

        if e["model"]:
            by_model[e["model"]]["requests"] += 1
            if e["duration_ms"]:
                by_model[e["model"]]["total_ms"] += e["duration_ms"]

        if e["mode"]:
            by_mode[e["mode"]] += 1

        if e["duration_ms"]:
            durations.append(e["duration_ms"])
            daily[day]["total_ms"] += e["duration_ms"]

    total_req = len(events)
    total_success = len(success_events)
    avg_dur_ms = round(sum(durations) / len(durations)) if durations else 0
    peak_hour = max(hourly, key=lambda h: hourly[h]) if hourly else 0

    # 일별 평균 응답시간
    for d in daily.values():
        cnt = d["success"]
        d["avg_ms"] = round(d["total_ms"] / cnt) if cnt else 0
        del d["total_ms"]

    # 모델별 평균 응답시간
    for m in by_model.values():
        cnt = m["requests"]
        m["avg_ms"] = round(m["total_ms"] / cnt) if cnt else 0
        del m["total_ms"]

    return {
        "summary": {
            "total_requests": total_req,
            "total_success": total_success,
            "success_rate": round(total_success / total_req * 100, 1) if total_req else 0,
            "avg_duration_ms": avg_dur_ms,
            "peak_hour": peak_hour,
            "peak_hour_requests": hourly.get(peak_hour, 0),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "events": events,
        "daily": {
            day: v for day, v in sorted(daily.items())
        },
        "hourly": {str(h): hourly[h] for h in range(24)},
        "heatmap": dict(heatmap),
        "by_model": dict(
            sorted(by_model.items(), key=lambda x: x[1]["requests"], reverse=True)
        ),
        "by_mode": dict(
            sorted(by_mode.items(), key=lambda x: x[1], reverse=True)
        ),
    }


def main() -> None:
    print("🤖 GitHub Copilot Chat 로그 분석 중...\n")

    if not VSCODE_LOGS_DIR.exists():
        print(f"❌ VS Code 로그 디렉토리를 찾을 수 없습니다: {VSCODE_LOGS_DIR}")
        return

    events = scan_all_logs()
    success_count = sum(1 for e in events if e["status"] == "success")

    print(f"✓ {len(events)}개 요청 파싱 완료 (성공: {success_count}개)\n")

    if not events:
        print("⚠ 분석할 Copilot Chat 요청이 없습니다.")
        return

    # 모델별 요약
    from collections import Counter
    model_counts = Counter(e["model"] for e in events if e["status"] == "success")
    print("모델별 성공 요청:")
    for model, cnt in model_counts.most_common():
        print(f"  - {model}: {cnt}건")

    data = aggregate(events)

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n💾 JSON 저장: {OUTPUT_JSON}")
    print(f"🌐 대시보드에서 Copilot 탭을 확인하세요.")


if __name__ == "__main__":
    main()
