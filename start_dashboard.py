"""
Claude Code 대시보드 시작 스크립트
1. 로그 파서 실행 → usage_data.json 생성
2. HTTP 서버 시작
3. 브라우저 자동 열기
"""

import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from threading import Timer

PORT = 8080
DASHBOARD_FILE = "dashboard.html"


def run_parser(script_name: str) -> bool:
    """지정된 파서 스크립트를 실행합니다."""
    parser = Path(__file__).parent / script_name
    if not parser.exists():
        print(f"❌ {script_name} 를 찾을 수 없습니다: {parser}")
        return False

    print("=" * 50)
    result = subprocess.run([sys.executable, "-X", "utf8", str(parser)], check=False)
    print("=" * 50)
    return result.returncode == 0


def open_browser(url: str, delay: float = 1.5) -> None:
    """지정된 지연 후 브라우저를 엽니다."""
    def _open():
        webbrowser.open(url)
    Timer(delay, _open).start()


def main() -> None:
    script_dir = Path(__file__).parent
    os.chdir(script_dir)

    print("\n🚀 Claude Code 대시보드 시작\n")

    # Claude Code 로그 파싱
    print("📊 Step 1-A: Claude Code 로그 파싱 중...\n")
    ok1 = run_parser("parse_claude_logs.py")
    if not ok1:
        print("\n⚠ Claude Code 파서 오류. Copilot 파싱은 계속 진행합니다.\n")

    # Copilot Chat 로그 파싱
    print("\n✈️  Step 1-B: GitHub Copilot Chat 로그 파싱 중...\n")
    ok2 = run_parser("parse_copilot_logs.py")
    if not ok2:
        print("\n⚠ Copilot Chat 파서 오류. 대시보드는 계속 시작됩니다.\n")

    ok = ok1 or ok2
    if not ok:
        print("\n⚠ 두 파서 모두 오류가 있었습니다. 대시보드는 계속 시작됩니다.\n")

    # 서버 시작
    url = f"http://localhost:{PORT}/{DASHBOARD_FILE}"
    print(f"\n🌐 Step 2: 웹서버 시작 (포트 {PORT})")
    print(f"   대시보드 주소: {url}")
    print("\n종료하려면 Ctrl+C 를 누르세요.\n")

    open_browser(url)

    try:
        import http.server
        handler = http.server.SimpleHTTPRequestHandler
        # 로그 출력 억제를 원하면 아래 주석 해제
        # handler.log_message = lambda *args: None
        with http.server.HTTPServer(("", PORT), handler) as httpd:
            httpd.serve_forever()
    except OSError as e:
        if e.errno == 98 or "Address already in use" in str(e):
            print(f"⚠ 포트 {PORT}가 이미 사용 중입니다.")
            print(f"  브라우저에서 직접 열어주세요: {url}")
            open_browser(url, delay=0)
        else:
            raise
    except KeyboardInterrupt:
        print("\n\n✓ 대시보드 서버 종료.")


if __name__ == "__main__":
    main()
