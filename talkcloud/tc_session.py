"""톡클라우드 세션 공용 모듈 (읽기 전용 GET 만 제공).

- 수집 전용 Edge 프로필: %LOCALAPPDATA%\\talkcloud-collector\\profile  (OneDrive 밖 — 세션 쿠키 동기화 방지)
- 첫 실행 때만 사람이 창에서 카카오 로그인. 이후에는 저장된 세션 재사용.
- drawer-api 호출은 브라우저 컨텍스트 쿠키로 직접 GET (페이지 안 fetch 는 CORS 로 막힘 — 2026-11-20 실측)
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

KST = timezone(timedelta(hours=9))
API = "https://drawer-api.kakao.com"
WEB = "https://talkcloud.kakao.com"
PROFILE = Path(os.environ["LOCALAPPDATA"]) / "talkcloud-collector" / "profile"
STATE = PROFILE.parent / "session_state.json"   # 세션 쿠키 보관 (OneDrive 밖)
HEADERS = {"Accept": "application/json+javascript", "Origin": WEB, "Referer": f"{WEB}/"}


def log(msg: str) -> None:
    print(f"[{datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


def kst(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, KST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


class TalkCloud:
    def __init__(self, ctx, page):
        self.ctx, self.page = ctx, page

    def get(self, path: str, pause: float = 0.4, **params) -> tuple[int, object]:
        """GET 만 한다. (status, json|None)"""
        url = f"{API}{path}" + (f"?{urlencode(params)}" if params else "")
        time.sleep(pause)
        resp = self.ctx.request.get(url, headers=HEADERS, timeout=30000)
        try:
            body = resp.json()
        except Exception:
            body = None
        return resp.status, body

    def logged_in(self) -> dict | None:
        try:
            st, body = self.get("/user/info", pause=0)
            if st == 200 and isinstance(body, dict) and body.get("fullHashedAccountId"):
                return body
        except Exception:
            pass
        return None

    def rooms(self) -> dict[str, dict]:
        """hashedChatId -> 방 정보 (FILE·MEDIA 목록 합침)"""
        out: dict[str, dict] = {}
        for vt in ("FILE", "MEDIA"):
            st, body = self.get("/chat/list", verticalType=vt)
            if st != 200 or not isinstance(body, dict):
                continue
            for it in body.get("items", []):
                rid = it.get("hashedChatId")
                if not rid:
                    continue
                r = out.setdefault(rid, dict(it, counts={}))
                r["counts"][vt] = it.get("count")
        return out


@contextmanager
def open_session(headless: bool = False, login_timeout_s: int = 15 * 60):
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE), channel="msedge", headless=headless,
                                                   viewport={"width": 1400, "height": 900}, locale="ko-KR")
        # 세션 쿠키(만료 없는 쿠키)는 브라우저를 닫으면 사라지므로 따로 저장했다가 복원한다
        if STATE.exists():
            try:
                cookies = json.loads(STATE.read_text(encoding="utf-8")).get("cookies", [])
                if cookies:
                    ctx.add_cookies(cookies)
            except Exception as e:
                log(f"저장된 세션 복원 실패: {type(e).__name__}")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        tc = TalkCloud(ctx, page)
        try:
            t0 = time.time()
            restored = tc.logged_in() is not None
            page.goto(f"{WEB}/", wait_until="domcontentloaded")
            deadline = time.time() + login_timeout_s
            asked = False
            while not tc.logged_in():
                if headless:
                    raise PermissionError("저장된 세션 만료 — 화면 있는 창으로 다시 로그인 필요")
                if time.time() > deadline:
                    raise TimeoutError("톡클라우드 로그인 시간 초과")
                if not asked:
                    log("로그인이 필요합니다. 열린 창에서 카카오 로그인(휴대폰 인증 포함)을 진행해 주세요.")
                    asked = True
                time.sleep(3)
            log(f"톡클라우드 로그인 확인 (저장 세션으로 바로 연결={restored}, 사람 로그인 필요={asked}, "
                f"{time.time() - t0:.0f}초)")
            ctx.storage_state(path=str(STATE))
            yield tc
            ctx.storage_state(path=str(STATE))
        finally:
            ctx.close()
