# Claude Code Usage Dashboard

A zero-dependency web dashboard that visualizes your **Claude Code** (and optional **GitHub Copilot Chat**) usage by parsing local session logs.

> 한국어 설명은 아래 [한국어](#한국어) 섹션을 참고하세요.

![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green) ![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

## Why

Claude Code stores rich JSONL session logs under `~/.claude/projects/`, but there's no built-in way to see how many tokens you burn, what they cost, or when you work most. This tool parses those logs locally and renders an interactive dashboard — **no data ever leaves your machine**, and there are **no external dependencies** (Python standard library only; charts via the Chart.js CDN).

## Features

- **Summary cards** — total tokens, sessions, estimated cost, peak hour
- **Daily token chart** — stacked Input / Output / Cache bars
- **Hourly pattern** — 0–23h line chart
- **Model distribution** — Sonnet / Opus / Haiku doughnut
- **Work-type classification** — auto-tags sessions (coding, debugging, refactoring, review, ...)
- **Per-project breakdown** — top 10 projects
- **Weekly heatmap** — day-of-week × hour token intensity
- **Session table** — click a row for a detail modal (token breakdown, first prompt)
- **Filters** — by period / model / project / work type
- **Export** — download as JSON or CSV
- (Optional) **GitHub Copilot Chat** request analytics

## Quick start

### Option 1 — one click (recommended)

```bash
python start_dashboard.py
```

Runs the parser, starts the server, and opens your browser automatically.

### Option 2 — step by step

```bash
# 1. Parse logs (generates usage_data.json)
python parse_claude_logs.py

# 2. Serve on port 8080
python -m http.server 8080

# 3. Open http://localhost:8080/dashboard.html
```

## Cost basis

| Model  | Input     | Output    | Cache Read  | Cache Write  |
| ------ | --------- | --------- | ----------- | ------------ |
| Opus   | $15 / 1M  | $75 / 1M  | $1.5 / 1M   | $18.75 / 1M  |
| Sonnet | $3 / 1M   | $15 / 1M  | $0.3 / 1M   | $3.75 / 1M   |
| Haiku  | $0.8 / 1M | $4 / 1M   | $0.08 / 1M  | $1.0 / 1M    |

> Pricing is configurable at the top of `parse_claude_logs.py`. Update it if Anthropic's rates change.

## Requirements

- Python 3.9+ (standard library only — no `pip install` needed)
- Internet access for the Chart.js CDN in the browser

## Privacy

All parsing happens locally. The generated `usage_data.json` / `copilot_data.json` contain your own prompts and usage, so they are **git-ignored by default** — do not commit them.

## Contributing

Issues and PRs are welcome. Ideas: more model pricing presets, offline Chart.js bundling, additional export formats.

## License

[MIT](./LICENSE)

---

## 한국어

`~/.claude/projects/` 의 Claude Code 세션 로그(JSONL)를 분석해 토큰 사용량·비용·작업 패턴을 시각화하는 **무설치 웹 대시보드**입니다. 모든 분석은 로컬에서 이뤄지며 데이터는 외부로 전송되지 않습니다. (Python 표준 라이브러리만 사용, 차트는 Chart.js CDN)

### 실행

```bash
python start_dashboard.py   # 파싱 + 서버 + 브라우저 자동 실행
```

자세한 기능과 비용 기준은 위 영어 설명을 참고하세요. 생성되는 `usage_data.json` 등에는 개인 프롬프트가 담기므로 기본적으로 `.gitignore` 처리되어 있습니다 — 커밋하지 마세요.
