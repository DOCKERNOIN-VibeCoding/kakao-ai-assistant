"""GitHub 릴리스로 프로그램 업데이트 — 사용자가 '설치'를 눌러야만 설치한다(자동 설치 안 함).

- 최신 릴리스(태그 vX.Y.Z)가 이 PC 의 VERSION 보다 높으면 알린다.
- 설치하면 프로그램 파일(talkcloud/*.py·*.ps1, CLAUDE.md, README.md, requirements.txt, VERSION)만 바꾼다.
  설정(config.json)·수집 데이터는 건드리지 않고, 바꾸기 전 파일은 _backup/<이전 버전>/ 에 남긴다.
- 비공개 저장소면 이 PC 에 로그인된 GitHub CLI(gh) 인증을 쓴다. 토큰을 파일에 저장하지 않는다.
- Git 저장소 폴더(개발용)나 VERSION 이 없는 폴더(작업 폴더)에서는 꺼진다 — 개발 중인 코드를 덮어쓰지 않게.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = "DOCKERNOIN-VibeCoding/kakao-ai-assistant"
ROOT = Path(__file__).resolve().parent.parent   # 프로그램 폴더(VERSION·CLAUDE.md 가 있는 곳)
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def local_version() -> str | None:
    p = ROOT / "VERSION"
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


def enabled() -> tuple[bool, str]:
    if (ROOT / ".git").exists():
        return False, "개발용 Git 폴더라 업데이트를 쓰지 않습니다"
    if not local_version():
        return False, "배포본이 아니라(VERSION 없음) 업데이트를 쓰지 않습니다"
    return True, ""


def _ver(s: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", s or "")[:3])


def _updatable(rel: str) -> bool:
    return rel in {"CLAUDE.md", "README.md", "requirements.txt", "VERSION"} or \
        (rel.startswith("talkcloud/") and rel.count("/") == 1 and rel.endswith((".py", ".ps1")))


def _gh_token() -> str | None:
    gh = shutil.which("gh")
    if not gh:
        return None
    r = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, creationflags=FLAGS)
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def _get(url: str, token: str | None) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "kakao-ai-assistant-updater"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def check() -> dict | None:
    """새 버전이 있으면 {'version', 'notes', 'zip', 'token'}, 없으면 None. 확인 실패는 예외."""
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    token = None
    try:
        rel = json.loads(_get(url, None))
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403, 404):
            raise
        token = _gh_token()   # 비공개 저장소 — 이 PC 의 gh 로그인으로 다시 시도
        if not token:
            raise RuntimeError("비공개 저장소라 GitHub CLI 로그인(gh auth login)이 필요합니다") from e
        rel = json.loads(_get(url, token))
    latest = (rel.get("tag_name") or "").lstrip("v")
    if _ver(latest) <= _ver(local_version() or "0"):
        return None
    return {"version": latest, "notes": (rel.get("body") or "").strip()[:1500], "zip": rel["zipball_url"], "token": token}


def install(info: dict, log=print) -> str:
    """프로그램 파일만 교체. 반환: 요약. requirements.txt 가 바뀌면 pip 로 설치한다."""
    data = _get(info["zip"], info.get("token"))
    old = local_version() or "unknown"
    backup = ROOT / "_backup" / old
    old_req = (ROOT / "requirements.txt").read_text(encoding="utf-8") if (ROOT / "requirements.txt").exists() else ""
    changed = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for m in z.infolist():
            if m.is_dir():
                continue
            rel = m.filename.split("/", 1)[1] if "/" in m.filename else ""   # zip 맨 위 폴더(저장소-커밋) 제거
            if not _updatable(rel):
                continue
            target = ROOT / rel
            new = z.read(m)
            if target.exists() and target.read_bytes() == new:
                continue
            if target.exists():
                (backup / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup / rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(new)
            changed += 1
    msg = f"{old} → {info['version']}: 파일 {changed}개 교체(이전 파일은 {backup})"
    new_req = (ROOT / "requirements.txt").read_text(encoding="utf-8") if (ROOT / "requirements.txt").exists() else ""
    if new_req != old_req:
        python = Path(sys.executable).with_name("python.exe")
        r = subprocess.run([str(python), "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")],
                           capture_output=True, text=True, creationflags=FLAGS)
        msg += " · 필요한 패키지 설치 " + ("완료" if r.returncode == 0 else f"실패: {(r.stderr or r.stdout)[-300:]}")
    log(msg)
    return msg


def restart_app() -> None:
    """앱을 2초 뒤 다시 띄운다(지금 프로세스가 끝나 중복 실행 방지 잠금이 풀린 뒤)."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    app = Path(__file__).resolve().with_name("assistant_app.py")
    cmd = f"Start-Sleep 2; Start-Process -FilePath '{pythonw}' -ArgumentList '-X','utf8','\"{app}\"'"
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", cmd], creationflags=FLAGS)
