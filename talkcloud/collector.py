"""톡클라우드 수집기 1차 버전 (AI 없음, 톡클라우드에는 읽기 전용 GET 만).

하는 일
  1) 방 목록·친구 이름 갱신
  2) 파일·사진 목록을 최신순으로 훑어 새 항목만 DB에 추가 (첫 실행은 최근 initial_days 일)
  3) 거래처로 등록된 방(vendor_rooms — Claude 가 사용자 확인 후 MCP 로 등록)의 항목만 원본 다운로드 → data/files/<업체명>/
  4) 채팅방 저장소(저장된 대화)의 메시지를 DB에 반영 (revision 이 바뀐 것만)

실행
  python -X utf8 collector.py once      # 한 번
  python -X utf8 collector.py loop      # interval_min 마다 반복 (상주)
설정: config.json (없으면 기본값으로 생성)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import photo_ocr
from export_import import collect_cutoff_s, import_exports, keep_cutoff_s
from tc_session import KST, kst, log, open_session

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
# 업무 자료는 이 PC 에만 둔다(2026-11-23 사용자 요구): 클라우드 동기화되지 않는 LOCALAPPDATA 가 기본 위치
LOCAL_DATA = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "KakaoAIAssistant" / "data"
DEFAULTS = {"data_dir": str(LOCAL_DATA), "initial_days": 90, "interval_min": 10, "max_pages_per_run": 30,
            # 원본을 받지 않을 형식(목록·메타데이터는 그대로 DB에 남음). 음성(2026-11-20)·동영상(2026-11-23) 제외, 사진은 받음
            "skip_download_mime_prefixes": ["audio/", "video/"],
            "max_download_mb": 50,           # 이보다 큰 파일(디자인 원본·묶음 zip)은 받지 않음 — 필요하면 따로 받기
            "auto_delete": False,            # 오래된 기록 자동 삭제(앱 설정 창에서 켜고 끔, 기본 꺼짐)
            "auto_delete_months": 12,        # 켰을 때 이보다 오래된 대화·원본·대화 원문 txt 를 지움
            "collect_months": 3,             # 수집 기간: 최근 N개월의 대화·파일·사진(같은 기간 — 2026-11-23 사용자). 앱이 처음 실행 때 묻고 설정 창에서 변경
            "idle_minutes": 10}              # 이만큼 입력이 없으면 '쉬는 시간' → 대화 수집(assistant_app 설정 창에서 변경)

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (room_id TEXT PRIMARY KEY, chat_id TEXT, title TEXT, chat_type TEXT,
  member_count INTEGER, joined INTEGER, updated_at TEXT);
CREATE TABLE IF NOT EXISTS vendor_rooms (room_id TEXT PRIMARY KEY, vendor TEXT, kind TEXT, contact TEXT, memo TEXT);
CREATE TABLE IF NOT EXISTS people (author_hash TEXT PRIMARY KEY, name TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS items (item_id TEXT PRIMARY KEY, drawer_id TEXT, vertical TEXT, room_id TEXT,
  author_hash TEXT, created_at INTEGER, file_name TEXT, mime TEXT, size INTEGER, content_type TEXT,
  content_log_id TEXT, first_seen_at TEXT, local_path TEXT, downloaded_at TEXT, download_error TEXT);
CREATE INDEX IF NOT EXISTS ix_items_room_time ON items(room_id, created_at);
CREATE TABLE IF NOT EXISTS snapshots (snapshot_id TEXT PRIMARY KEY, title TEXT, chat_type TEXT, revision INTEGER,
  updated_at INTEGER, synced_revision INTEGER, room_id TEXT);
CREATE TABLE IF NOT EXISTS messages (msg_key TEXT PRIMARY KEY, room_key TEXT, room_title TEXT, snapshot_id TEXT,
  author_hash TEXT, sent_at INTEGER, type INTEGER, message TEXT, has_attachment INTEGER, deleted INTEGER, first_seen_at TEXT);
CREATE INDEX IF NOT EXISTS ix_messages_room_time ON messages(room_key, sent_at);
CREATE INDEX IF NOT EXISTS ix_messages_time ON messages(sent_at);
CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT);
-- 개인 대화 보호(2026-11-23 사용자 요청): decision='excluded' 인 방은 어떤 경로로도 수집하지 않는다. 'kept' = 검토 후 수집 유지
CREATE TABLE IF NOT EXISTS privacy_rooms (room_key TEXT PRIMARY KEY, title TEXT, decision TEXT, reason TEXT, decided_at TEXT);
"""


def cloud_sync_warning(path) -> str | None:
    """데이터 폴더가 클라우드 동기화 폴더(OneDrive·Dropbox·Google Drive·iCloud) 안이면 경고 문구."""
    p = str(Path(path).resolve()).lower()
    roots = [os.environ.get(k) for k in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")]
    if any(r and p.startswith(str(Path(r).resolve()).lower()) for r in roots) or \
            any(w in p for w in ("\\onedrive", "\\dropbox", "\\google drive", "\\icloud")):
        return (f"업무 자료 폴더가 클라우드 동기화 폴더 안에 있습니다: {path}\n"
                f"이 PC 밖으로 올라갈 수 있습니다. move_data_local.py 로 로컬 폴더({LOCAL_DATA})로 옮기세요.")
    return None


def now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def load_config() -> dict:
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(DEFAULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg = dict(DEFAULTS)
    cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    return cfg


def db_open(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(data_dir / "talkcloud.db")
    cols = [r[1] for r in con.execute("PRAGMA table_info(messages)")]
    if cols and "msg_key" not in cols:   # 1차 버전(log_id 키) → 새 구조로 교체. 저장본에서 다시 받으면 됨
        con.execute("DROP TABLE messages")
        con.execute("UPDATE snapshots SET synced_revision=NULL")
    con.executescript(SCHEMA)
    photo_ocr.ensure_columns(con)
    return con


def excluded_rooms(con) -> tuple[set[str], set[str]]:
    """수집 제외 방 (room_key 집합, 방 이름 집합). 방 이름으로도 막는 이유: 내보내기는 이름으로만 방을 안다."""
    rows = con.execute("SELECT room_key, title FROM privacy_rooms WHERE decision='excluded'").fetchall()
    return {k for k, _ in rows}, {norm_title(t) for _, t in rows if t}


def norm_title(t: str | None) -> str:
    return re.sub(r"\s+", " ", (t or "").replace("\xa0", " ")).strip()


def safe_name(s: str, limit: int = 120) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", s or "").strip(" .")
    return (s or "_")[:limit]


def sync_rooms_people(tc, con):
    rooms = tc.rooms()
    con.executemany(
        "INSERT INTO rooms VALUES (?,?,?,?,?,?,?) ON CONFLICT(room_id) DO UPDATE SET "
        "title=excluded.title, member_count=excluded.member_count, joined=excluded.joined, updated_at=excluded.updated_at",
        [(rid, str(r.get("chatId")), r.get("title"), r.get("chatType"), int(r.get("memberCount") or 0),
          1 if r.get("joined") is not False else 0, now_iso()) for rid, r in rooms.items()])
    st, fr = tc.get("/friend/list")
    people = [(f.get("hashedTalkUserId"), f.get("displayName"), "friend") for f in (fr or {}).get("friends", [])]
    me = tc.logged_in() or {}
    people.append((me.get("hashedTalkUserId"), "(나)", "me"))
    con.executemany("INSERT INTO people VALUES (?,?,?) ON CONFLICT(author_hash) DO UPDATE SET name=excluded.name",
                    [p for p in people if p[0]])
    con.commit()
    log(f"방 {len(rooms)}개, 친구 {len(people) - 1}명 갱신")


def _fetch_items(tc, con, cfg, vt: str, frm: int, to: int, excluded_keys: set) -> tuple[int, int, int | None, bool]:
    """톡클라우드 파일·사진 목록 [frm, to] 구간을 최신순으로 읽어 items 에 추가. (새 건수, 페이지, 가장 최근 시각, 끝까지 읽었는지)"""
    pages, new, newest, offset = 0, 0, None, None
    while pages < cfg["max_pages_per_run"]:
        params = dict(verticalType=vt, fetchCount="100", **{"from": str(frm)}, to=str(to), joined="true", direction="DESC")
        if offset:
            params["offset"] = offset
        st, body = tc.get("/v2/mediaFile/list", **params)
        pages += 1
        if st != 200 or not isinstance(body, dict):
            log(f"{vt} 목록 응답 {st} — 이번 회차 중단")
            return new, pages, newest, False
        batch = body.get("items", [])
        for it in batch:
            ts = int(it.get("createdAt") or 0)
            newest = max(newest or 0, ts)
            if it.get("hashedChatId") in excluded_keys:   # 개인 대화로 제외된 방
                continue
            cur = con.execute(
                "INSERT OR IGNORE INTO items (item_id, drawer_id, vertical, room_id, author_hash, created_at, file_name,"
                " mime, size, content_type, content_log_id, first_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (it.get("id"), it.get("drawerId"), vt, it.get("hashedChatId"), it.get("hashedAuthorId"), ts,
                 it.get("originalFileName") or it.get("originalFilename"), it.get("mimeType"),
                 int(it.get("size") or 0), it.get("contentType"), it.get("contentLogId"), now_iso()))
            new += cur.rowcount
        if not body.get("hasMore") or not batch:
            return new, pages, newest, True
        offset = batch[-1].get("drawerId")
    return new, pages, newest, False


def sync_items(tc, con, cfg):
    cutoff = collect_cutoff_s(cfg) * 1000
    first_run_floor = min(int((time.time() - cfg["initial_days"] * 86400) * 1000), cutoff)
    total_new = 0
    excluded_keys, _ = excluded_rooms(con)

    def state(key, value=None):
        if value is None:
            row = con.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
            return int(row[0]) if row else None
        con.execute("INSERT INTO sync_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    for vt in ("FILE", "MEDIA"):
        wm = state(f"watermark_{vt}")
        floor = wm - 86400_000 if wm else first_run_floor   # 하루 겹쳐서 다시 확인
        new, pages, newest, _ = _fetch_items(tc, con, cfg, vt, floor, int(time.time() * 1000), excluded_keys)
        if newest:
            state(f"watermark_{vt}", newest)
        # 수집 기간(collect_months)을 늘렸으면 그만큼 예전 목록도 가져온다
        listed_from = state(f"listed_from_{vt}") or (wm and (con.execute(
            "SELECT MIN(created_at) FROM items WHERE vertical=?", (vt,)).fetchone()[0] or floor)) or first_run_floor
        if cutoff < listed_from - 86400_000:
            n2, p2, _, complete = _fetch_items(tc, con, cfg, vt, cutoff, listed_from, excluded_keys)
            new, pages = new + n2, pages + p2
            if complete:
                listed_from = cutoff
            log(f"{vt}: 수집 기간 확대로 예전 목록 {n2}건 추가")
        state(f"listed_from_{vt}", min(listed_from, floor))
        con.commit()
        total_new += new
        log(f"{vt}: 새 항목 {new}건 ({pages}페이지)")
    return total_new


def prune_old_files(con, data_dir: Path, cutoff_ms: int) -> None:
    """보관 범위(수집 시작점, 자동 삭제를 켰으면 그 기간) 밖의 받은 원본을 지운다 — 대화와 같은 범위 유지."""
    own = (Path(data_dir) / "files").resolve()
    n = 0
    for iid, p in con.execute("SELECT item_id, local_path FROM items WHERE local_path IS NOT NULL AND created_at < ?",
                              (cutoff_ms,)).fetchall():
        f = Path(p)
        if f.exists() and f.resolve().is_relative_to(own):
            f.unlink()
        con.execute("UPDATE items SET local_path=NULL, downloaded_at=NULL WHERE item_id=?", (iid,))
        n += 1
    if n:
        con.commit()
        log(f"수집 범위 밖의 오래된 원본 {n}개 정리")


SECRET_NAME = re.compile(r"키값|api[ _-]?key|apikey|비밀번호|password|passwd|credential|계정정보|\.env$|token", re.I)


def download_vendor_items(tc, con, files_dir: Path, skip_prefixes: list[str], max_mb: float = 50, since_ms: int = 0):
    """업무방 원본 받기. 음성·동영상은 받지 않고, max_mb 넘는 파일(디자인 원본·묶음 zip 등)도 받지 않는다(목록에는 남음).
    사진은 받는다 — 세금계산서·견적서를 사진으로 보내는 경우가 있어 OCR 로 확인해야 함(2026-11-23 사용자 요청)."""
    skip_sql = "".join(" AND COALESCE(i.mime,'') NOT LIKE ?" for _ in skip_prefixes)
    rows = con.execute(
        "SELECT i.item_id, i.vertical, i.created_at, i.file_name, i.mime, v.vendor FROM items i "
        "JOIN vendor_rooms v ON v.room_id = i.room_id WHERE i.local_path IS NULL AND i.download_error IS NULL"
        f" AND COALESCE(i.size, 0) <= ? AND i.created_at >= ?{skip_sql} ORDER BY i.created_at DESC",
        [int(max_mb * 1048576), since_ms] + [p + "%" for p in skip_prefixes]).fetchall()
    ok = 0
    for item_id, vt, created, fname, mime, vendor in rows:
        if fname and SECRET_NAME.search(fname):   # API 키·비밀번호로 보이는 파일은 받지 않는다(Claude 가 열지 않게)
            con.execute("UPDATE items SET download_error=? WHERE item_id=?", ("skipped: 비밀 정보로 보이는 파일", item_id))
            con.commit()
            continue
        try:
            st, d = tc.get(f"/v2/mediaFile/{item_id}")
            if st != 200 or not isinstance(d, dict):
                raise RuntimeError(f"상세 응답 {st}")
            url = next((d.get(k) for k in (("urlHq", "urlNormal", "url") if vt == "MEDIA" else ("url",))
                        if isinstance(d.get(k), str) and d.get(k)), None)
            if not url:
                raise RuntimeError("원본 주소 없음")
            resp = tc.ctx.request.get(url, headers={"Referer": "https://talkcloud.kakao.com/"}, timeout=120000)
            if resp.status != 200:
                raise RuntimeError(f"다운로드 응답 {resp.status}")
            stamp = datetime.fromtimestamp(created / 1000, KST).strftime("%Y%m%d_%H%M%S")
            ext = (mime or "").split("/")[-1].replace("jpeg", "jpg") if not fname else ""
            name = fname or f"사진_{item_id[:8]}.{ext or 'bin'}"
            folder = files_dir / safe_name(vendor)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{stamp}_{safe_name(name)}"
            path.write_bytes(resp.body())
            con.execute("UPDATE items SET local_path=?, downloaded_at=? WHERE item_id=?", (str(path), now_iso(), item_id))
            ok += 1
        except Exception as e:
            con.execute("UPDATE items SET download_error=? WHERE item_id=?", (f"{type(e).__name__}: {e}"[:300], item_id))
        con.commit()
    if rows:
        log(f"거래처 원본 다운로드 {ok}/{len(rows)}건")


def sync_snapshots(tc, con, cfg):
    """채팅방 저장소 → messages. 방(제목)별 최신 저장본만 읽고, 보관 기간(기본 180일) 안 메시지만 저장.
    같은 방을 여러 번 저장해도 메시지 키(방·시각·보낸 사람·내용)로 중복 없이 합친다."""
    st, body = tc.get("/chatSnapshots", sortType="DATE", direction="DESC", fetchCount="100")
    snaps = [s for s in (body.get("items", []) if isinstance(body, dict) else []) if s.get("status") == "COMPLETED"]
    room_by_title = {norm_title(t): rid for rid, t in con.execute("SELECT room_id, title FROM rooms")}
    keep_from = keep_cutoff_s(cfg)
    latest: dict[str, dict] = {}
    for s in snaps:   # 최신순 목록 → 제목별 첫 항목이 최신
        latest.setdefault(norm_title(s.get("chatTitle")), s)
    excluded_keys, excluded_titles = excluded_rooms(con)
    for s in snaps:
        sid, title = s.get("id"), norm_title(s.get("chatTitle"))
        room_key = room_by_title.get(title) or f"title:{title}"
        if room_key in excluded_keys or title in excluded_titles:   # 개인 대화로 제외된 방
            continue
        row = con.execute("SELECT synced_revision FROM snapshots WHERE snapshot_id=?", (sid,)).fetchone()
        is_latest = latest[title] is s
        con.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?) ON CONFLICT(snapshot_id) DO UPDATE SET "
                    "title=excluded.title, updated_at=excluded.updated_at, room_id=excluded.room_id",
                    (sid, title, s.get("chatType"), int(s.get("revision") or 0), int(s.get("createdAt") or 0),
                     row[0] if row else (None if is_latest else -1), room_key))   # 옛 저장본은 -1(건너뜀)
        if not is_latest or (row and row[0] is not None):
            continue
        st, au = tc.get(f"/chatSnapshots/{sid}/authors", pageNumber="0", pageSize="1000")
        con.executemany("INSERT INTO people VALUES (?,?,?) ON CONFLICT(author_hash) DO NOTHING",
                        [(a.get("hashedAuthorId"), a.get("nickname"), "snapshot") for a in (au or {}).get("items", [])])
        offset, pages, kept, seen = None, 0, 0, {}
        while pages < 500:   # 1000건씩 오래된 것부터 (최신순 정렬 미지원 — 2026-11-20 실측)
            params = {"fetchCount": "1000"}
            if offset:
                params["offsetLogId"] = offset
            st, logs = tc.get(f"/chatSnapshots/{sid}/chatLogs", pause=0.2, **params)
            pages += 1
            items = (logs or {}).get("items", []) if st == 200 else []
            rows = []
            for m in items:
                t = int(m.get("sendAt") or 0)
                if t < keep_from:
                    continue
                base = f"{room_key}|{t}|{m.get('hashedAuthorId')}|{m.get('type')}|{m.get('message')}"
                n = seen[base] = seen.get(base, 0) + 1          # 같은 초·같은 내용 연속 메시지 구분
                key = hashlib.sha1(f"{base}#{n}".encode("utf-8")).hexdigest()
                rows.append((key, room_key, title, sid, m.get("hashedAuthorId"), t, m.get("type"), m.get("message"),
                             0 if m.get("type") in (0, 1) else 1, 1 if m.get("deleted") else 0, now_iso()))
            cur = con.executemany("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            kept += cur.rowcount
            if st != 200 or not (logs or {}).get("hasMore") or not items:
                break
            offset = items[-1].get("logId")
        con.execute("UPDATE snapshots SET synced_revision=? WHERE snapshot_id=?", (int(s.get("revision") or 0), sid))
        con.commit()
        log(f"저장된 대화 '{title}': {pages}페이지 확인, 새 메시지 {kept}건 반영(최근 {cfg.get('collect_months', 3)}개월)")


def run_once(cfg):
    data_dir = Path(cfg["data_dir"])
    if warn := cloud_sync_warning(data_dir):
        log("경고: " + warn.replace("\n", " "))
    con = db_open(data_dir)
    n = con.execute("SELECT COUNT(*) FROM vendor_rooms").fetchone()[0]
    log(f"거래처 방 {n}곳 (등록은 Claude MCP 도구 register_vendor_rooms)")
    import_exports(con, cfg)   # 대화 원문의 주 출처: PC 카카오톡 내보내기 txt (톡클라우드 세션과 무관)
    try:
        session = open_session(headless=True)
        tc = session.__enter__()
    except PermissionError:
        log("세션 만료 — 로그인 창을 엽니다.")
        session = open_session(headless=False)
        tc = session.__enter__()
    try:
        sync_rooms_people(tc, con)
        sync_items(tc, con, cfg)
        download_vendor_items(tc, con, data_dir / "files", cfg.get("skip_download_mime_prefixes") or [],
                              float(cfg.get("max_download_mb", 50)), keep_cutoff_s(cfg) * 1000)
        prune_old_files(con, data_dir, keep_cutoff_s(cfg) * 1000)   # 수집 시작점·자동 삭제 범위 밖 원본 정리
        photo_ocr.run(con, log=log)   # 받은 사진 중 문서(세금계산서·견적서 등)로 보이는 것 표시 — PC 안 OCR
        sync_snapshots(tc, con, cfg)
        con.execute("INSERT INTO sync_state VALUES ('last_run',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (now_iso(),))
        con.commit()
    finally:
        session.__exit__(None, None, None)
        con.close()


def main():
    cfg = load_config()
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    if mode == "once":
        run_once(cfg)
        return
    while True:
        try:
            run_once(cfg)
        except Exception as e:
            log(f"회차 실패: {type(e).__name__}: {e}")
        time.sleep(cfg["interval_min"] * 60)


if __name__ == "__main__":
    main()
