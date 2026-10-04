"""JYsubTools 자체 업데이트 (UI 없음, 표준 라이브러리만 사용).

비공개 GitHub 저장소의 Releases 를 gh(GitHub CLI)로 확인/다운로드한다.
  - 최신 버전 확인: gh release view
  - gh 가 없으면 winget 으로 설치, 로그인이 안 돼 있으면 콘솔 창에서 gh auth login
  - setup 을 받아 /SILENT 로 실행하면 같은 AppId 로 덮어 설치(선택했던 툴/설치 폴더 유지) 후 /LAUNCH 로 다시 실행
설치 폴더 구조: <설치폴더>\\version.txt, <설치폴더>\\<툴id>\\<툴>.exe
"""
import os
import shutil
import subprocess
import sys
import tempfile

REPO = "lovslave-netizen/JYsubTools"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEWCON = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


def install_root():
    """exe 가 설치형일 때 설치 폴더(= exe 폴더의 상위). 개발 중(스크립트 실행)이면 None."""
    if not getattr(sys, "frozen", False):
        return None
    return os.path.dirname(os.path.dirname(sys.executable))


def installed_version():
    root = install_root()
    if not root:
        return None
    try:
        with open(os.path.join(root, "version.txt"), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def parse_version(v):
    out = []
    for p in v.strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def is_newer(latest, current):
    return parse_version(latest) > parse_version(current)


def find_gh():
    p = shutil.which("gh")
    if p:
        return p
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base:
            c = os.path.join(base, "GitHub CLI", "gh.exe")
            if os.path.exists(c):
                return c
    return None


def _run(args, timeout=60):
    try:
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=NOWIN)
    except (OSError, subprocess.TimeoutExpired):
        return None


def logged_in(gh):
    r = _run([gh, "auth", "status", "--hostname", "github.com"], timeout=30)
    return bool(r and r.returncode == 0)


def latest_version(gh):
    """최신 릴리스 태그에서 'v' 를 뺀 버전 문자열. 실패하면 None."""
    r = _run([gh, "release", "view", "--repo", REPO, "--json", "tagName", "--jq", ".tagName"], timeout=30)
    if not r or r.returncode != 0:
        return None
    return r.stdout.strip().lstrip("vV") or None


def run_in_console(cmd_line):
    """새 콘솔 창에서 명령을 실행하고 끝날 때까지 기다린다 (사용자가 코드/선택을 입력할 수 있게)."""
    return subprocess.call(["cmd", "/c", cmd_line + " & echo. & pause"], creationflags=NEWCON)


def install_gh_in_console():
    return run_in_console("winget install --id GitHub.cli -e --accept-package-agreements --accept-source-agreements")


def login_in_console(gh):
    return run_in_console('"%s" auth login --hostname github.com --git-protocol https --web' % gh)


def download_setup(gh, version):
    """setup 을 임시 폴더로 받아 경로를 돌려준다. 실패하면 None."""
    dest = tempfile.mkdtemp(prefix="jysub_update_")
    r = _run([gh, "release", "download", "v" + version, "--repo", REPO,
              "--pattern", "JYsubTools-Setup-*.exe", "--dir", dest, "--clobber"], timeout=900)
    if not r or r.returncode != 0:
        return None
    for n in os.listdir(dest):
        if n.lower().endswith(".exe"):
            return os.path.join(dest, n)
    return None


def launch_setup(path, relaunch_id):
    """setup 을 분리된 프로세스로 실행 (호출한 프로그램은 곧 종료해야 한다). relaunch_id: 설치 후 다시 실행할 툴 id."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/LAUNCH=" + relaunch_id],
                     creationflags=flags, close_fds=True)
