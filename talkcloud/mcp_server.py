"""카카오톡(톡클라우드) 업무자료 MCP 서버 — Claude 와 수집 DB 사이의 창구.

- 수집기(collector.py)가 채운 DB·파일을 조회하고, Claude 의 분석 결과(문서 분류·추출값)를 기록한다.
- 톡클라우드/카카오톡에는 접속하지 않는다. 삭제는 사용자가 '개인 대화라 수집하지 말라'고 확인한 방의 수집 자료뿐(decide_privacy).
- 거래처에 보낼 메시지는 '승인 대기열'에 넣기만 한다. 승인·발송 도구는 일부러 없다(사람 승인 원칙).

실행(stdio): python -X utf8 talkcloud/mcp_server.py
"""
from __future__ import annotations

import calendar
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from photo_ocr import ensure_columns as ensure_ocr_columns
from privacy import exclude_room

KST = timezone(timedelta(hours=9))
HERE = Path(__file__).resolve().parent
_cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8")) if (HERE / "config.json").exists() else {}
# 설정 파일이 없을 때도 collector 와 같은 로컬 폴더(동기화 안 됨)를 쓴다
DB = Path(_cfg.get("data_dir") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "KakaoAIAssistant" / "data") / "talkcloud.db"

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviewed (item_id TEXT PRIMARY KEY, reviewed_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, vendor TEXT, doc_type TEXT,
  period TEXT, title TEXT, amount_total REAL, fields_json TEXT, summary TEXT, recorded_at TEXT);
CREATE INDEX IF NOT EXISTS ix_docs_vendor ON documents(vendor, doc_type, period);
CREATE TABLE IF NOT EXISTS expectations (vendor TEXT, doc_type TEXT, due_day INTEGER, grace_days INTEGER, note TEXT,
  PRIMARY KEY (vendor, doc_type));
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, room_or_vendor TEXT, kind TEXT, content TEXT,
  source_time TEXT, people TEXT, due TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS drafts (id INTEGER PRIMARY KEY AUTOINCREMENT, vendor TEXT, room_id TEXT, text TEXT,
  reason TEXT, status TEXT DEFAULT 'pending', created_at TEXT, decided_at TEXT, decided_note TEXT);
CREATE TABLE IF NOT EXISTS privacy_rooms (room_key TEXT PRIMARY KEY, title TEXT, decision TEXT, reason TEXT, decided_at TEXT);
"""

mcp = MCPServer(
    "kakao-talkcloud",
    instructions=(
        "카카오톡 거래처 업무자료(톡클라우드 수집분) 조회·정리용. 거래처 방이 비어 있으면 list_rooms_overview 로 방을 파악해 "
        "거래처 후보를 사용자에게 확인받고 register_vendor_rooms 로 일괄 등록. 새 자료 처리 순서: list_unreviewed_items → 파일(local_path)을 "
        "직접 열어 내용 확인 → 거래처 규칙(vendors/<업체명>.md)에 따라 record_document 로 기록(또는 mark_reviewed). "
        "정기 문서 점검은 check_overdue. 거래처에 보낼 메시지는 queue_draft 로 승인 대기열에만 넣는다(직접 발송 불가)."),
)


def con() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript(EXTRA_SCHEMA)
    ensure_ocr_columns(c)   # items.ocr_text/ocr_hint/ocr_at (photo_ocr 가 채움)
    return c


def ts(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, KST).strftime("%Y-%m-%d %H:%M:%S") if ms else ""


def now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def _vendor_room(c, vendor: str):
    r = c.execute("SELECT room_id, vendor FROM vendor_rooms WHERE vendor = ?", (vendor,)).fetchone()
    return r or c.execute("SELECT room_id, vendor FROM vendor_rooms WHERE vendor LIKE ?", (f"%{vendor}%",)).fetchone()


@mcp.tool()
def collector_status() -> dict:
    """수집기 마지막 실행 시각, DB 항목 수, 거래처 방 수."""
    with con() as c:
        last = c.execute("SELECT value FROM sync_state WHERE key='last_run'").fetchone()
        return {"last_run": last[0] if last else None,
                "items": {r["vertical"]: r["n"] for r in c.execute("SELECT vertical, COUNT(*) n FROM items GROUP BY vertical")},
                "vendor_rooms": c.execute("SELECT COUNT(*) FROM vendor_rooms").fetchone()[0],
                "saved_conversations": c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]}


@mcp.tool()
def list_vendors() -> list[dict]:
    """거래처로 등록된 방 목록과 미확인 자료 수, 정기 문서 규칙. 비어 있으면 list_rooms_overview 로 등록부터."""
    with con() as c:
        rows = c.execute(
            "SELECT v.vendor, v.kind, v.contact, v.memo, r.title room_title, "
            " (SELECT COUNT(*) FROM items i WHERE i.room_id=v.room_id AND i.item_id NOT IN (SELECT item_id FROM reviewed)) unreviewed, "
            " (SELECT MAX(created_at) FROM items i WHERE i.room_id=v.room_id) last_item "
            "FROM vendor_rooms v LEFT JOIN rooms r ON r.room_id=v.room_id ORDER BY v.vendor").fetchall()
        exp = {}
        for e in c.execute("SELECT * FROM expectations"):
            exp.setdefault(e["vendor"], []).append(dict(e))
        return [dict(r, last_item=ts(r["last_item"]), expectations=exp.get(r["vendor"], [])) for r in rows]


DOC_WORDS = ("견적", "계산서", "명세", "청구", "발주", "계약", "제안", "단가", "정산", "invoice", "quote")


@mcp.tool()
def list_rooms_overview(mode: str = "recent", recent_days: int = 30, include_inactive: bool = False) -> list[dict]:
    """거래처 방 등록용 방 활동 요약(파일·사진 수, 마지막 활동, 최근 파일명 3개, 문서성 파일 수, 자주 보낸 사람).
    진행 순서(사용자 지정 방식):
      1) mode="recent" — 최근 recent_days(기본 30일) 안에 교류가 있는 방만. 여기서 거래처 후보를 골라 확인받고 등록.
      2) 그다음 "한 달 이상 교류가 없었던 업무방도 연결할까요?" 라고 물어보고, 원하면 mode="dormant"
         — 마지막 교류가 recent_days 보다 오래된 방(DB 보관 기간 안). 처음엔 보여주지 않는다.
    '교류'는 톡클라우드에 쌓인 파일·사진 기준(글만 오간 방은 활동으로 잡히지 않음).
    사용자 확인 후 register_vendor_rooms 로 등록. 이미 등록된 방은 vendor 값이 있다."""
    cut = int((datetime.now().timestamp() - recent_days * 86400) * 1000)
    since = 0 if mode == "dormant" else cut
    with con() as c:
        rooms = {r["room_id"]: dict(r) for r in c.execute(
            "SELECT r.room_id, r.title, r.member_count, v.vendor FROM rooms r "
            "LEFT JOIN vendor_rooms v ON v.room_id=r.room_id")}
        stats = {s["room_id"]: dict(s) for s in c.execute(
            "SELECT room_id, SUM(vertical='FILE') files, SUM(vertical='MEDIA') photos, MAX(created_at) last "
            "FROM items WHERE created_at >= ? GROUP BY room_id", (since,))}
        # 저장된 대화(글)도 활동으로 친다 — 파일 없이 글만 오가는 방 포함
        for m in c.execute("SELECT room_key, MAX(room_title) title, COUNT(*) n, MAX(sent_at)*1000 last FROM messages "
                           "WHERE sent_at*1000 >= ? GROUP BY room_key", (since,)):
            s = stats.setdefault(m["room_key"], {"room_id": m["room_key"], "files": 0, "photos": 0, "last": 0})
            s["messages"] = m["n"]
            s["last"] = max(s["last"] or 0, m["last"] or 0)
            rooms.setdefault(m["room_key"], {"room_id": m["room_key"], "title": m["title"], "member_count": None,
                                             "vendor": (c.execute("SELECT vendor FROM vendor_rooms WHERE room_id=?",
                                                                  (m["room_key"],)).fetchone() or [None])[0]})
        out = []
        excluded = _excluded(c)
        for s in stats.values():
            r = rooms.get(s["room_id"])
            if not r or s["room_id"] in excluded or (mode == "dormant" and (s["last"] or 0) >= cut):
                continue
            names = [x[0] for x in c.execute(
                "SELECT file_name FROM items WHERE room_id=? AND vertical='FILE' AND created_at>=? "
                "ORDER BY created_at DESC", (s["room_id"], since))]
            senders = [x[0] for x in c.execute(
                "SELECT COALESCE(p.name,'(알 수 없음)') n FROM items i LEFT JOIN people p ON p.author_hash=i.author_hash "
                "WHERE i.room_id=? AND i.created_at>=? GROUP BY n ORDER BY COUNT(*) DESC LIMIT 3", (s["room_id"], since))]
            out.append(dict(r, files=s["files"], photos=s["photos"], saved_messages=s.get("messages", 0),
                            last_activity=ts(s["last"]),
                            doc_like_files=sum(any(w in (n or "").lower() for w in DOC_WORDS) for n in names),
                            recent_files=[(n or "")[:50] for n in names[:3]], top_senders=senders))
        if include_inactive and mode == "dormant":   # DB 보관 기간 안에 파일·사진이 하나도 없는 방
            seen = {x[0] for x in c.execute("SELECT DISTINCT room_id FROM items")}
            out += [dict(r, files=0, photos=0, last_activity="(보관 기간 내 없음)", doc_like_files=0,
                         recent_files=[], top_senders=[]) for rid, r in rooms.items() if rid not in seen and rid not in excluded]
        return sorted(out, key=lambda x: (-x["doc_like_files"], -(x["files"] or 0)))


@mcp.tool()
def register_vendor_rooms(rooms: list[dict]) -> dict:
    """사용자가 '업무(거래처) 방이 맞다'고 확인한 방들을 일괄 등록/수정한다. 사용자 확인 없이 호출하지 말 것.
    글만 오가는 방(파일 없음)은 room_id 가 'title:<방 이름>' 형태다(list_rooms_overview 결과 그대로 사용).
    각 항목: {"room_id": 필수, "vendor": 업체명 필수, "kind": 유형(예: 원재료 납품, 영상제작, 인테리어),
    "contact": 담당자, "memo": 정기문서·규칙 메모}. 등록된 방은 수집기 다음 회차부터 원본 파일을 받는다
    (DB에 있는 최근 기간 자료까지 소급)."""
    done, errors = [], []
    with con() as c:
        for r in rooms:
            rid, vendor = r.get("room_id"), (r.get("vendor") or "").strip()
            title = c.execute("SELECT title FROM rooms WHERE room_id=? UNION ALL "
                              "SELECT title FROM snapshots WHERE room_id=? UNION ALL "
                              "SELECT room_title FROM messages WHERE room_key=? LIMIT 1", (rid, rid, rid)).fetchone()
            if not title or not vendor:
                errors.append({"room_id": rid, "error": "없는 방이거나 업체명 누락"})
                continue
            if rid in _excluded(c):
                errors.append({"room_id": rid, "error": "개인 대화로 수집 제외된 방 — 사용자가 원하면 decide_privacy(keep=[...]) 후 등록"})
                continue
            c.execute("INSERT INTO vendor_rooms VALUES (?,?,?,?,?) ON CONFLICT(room_id) DO UPDATE SET "
                      "vendor=excluded.vendor, kind=excluded.kind, contact=excluded.contact, memo=excluded.memo",
                      (rid, vendor, r.get("kind"), r.get("contact"), r.get("memo")))
            done.append({"room": title[0], "vendor": vendor})
    return {"registered": done, "errors": errors}


def _room_title(c, rid: str) -> str | None:
    row = c.execute("SELECT title FROM rooms WHERE room_id=? UNION ALL SELECT room_title FROM messages WHERE room_key=? "
                    "UNION ALL SELECT title FROM snapshots WHERE room_id=? LIMIT 1", (rid, rid, rid)).fetchone()
    return row[0] if row else (rid[len("title:"):] if rid.startswith("title:") else None)


def _excluded(c) -> set[str]:
    return {r[0] for r in c.execute("SELECT room_key FROM privacy_rooms WHERE decision='excluded'")}


@mcp.tool()
def privacy_review(sample: int = 6, include_decided: bool = False) -> dict:
    """개인 대화 점검용: 대화가 수집된 방(과 거래처로 등록된 방)마다 참여자·대화량·최근 대화 몇 줄을 보여 준다.
    최초 대화 수집 직후(그리고 새 방이 수집될 때) 쓴다. 각 방을 세 가지로 나눈다(2026-11-23 사용자 지정 방식):
      ① 확실히 업무용 — 거래·견적·일정·작업 이야기가 오가고 사적 내용이 없음 → 수집 유지
      ② 불확실 — 업무와 사적 대화가 섞였거나 판단 근거가 부족 → 사용자에게 업무용/개인용을 고르게 함
      ③ 확실히 업무용 아님 — 가족·연인·친구의 사적 대화, 업무와 무관한 1:1 개인 용무, 건강·금전·연애 등 사생활
         → "더 이상 수집하지 않고, 기존에 수집한 내용은 삭제합니다"라고 목록과 이유를 표시
    세 목록을 한 번에 보여 주고 ②에 대한 답을 받은 뒤 decide_privacy 를 한 번 호출한다
    (①·②의 업무용 → keep, ②의 개인용·③ → exclude). 사용자가 ③에 이의를 달면 그 방은 빼고 처리한다.
    이미 결정한 방은 기본으로 빠진다."""
    with con() as c:
        decided = {r[0] for r in c.execute("SELECT room_key FROM privacy_rooms")}
        keys = [r[0] for r in c.execute("SELECT room_key FROM messages GROUP BY room_key UNION SELECT room_id FROM vendor_rooms")]
        vend = {r["room_id"]: r["vendor"] for r in c.execute("SELECT room_id, vendor FROM vendor_rooms")}
        out = []
        for k in keys:
            if k in decided and not include_decided:
                continue
            senders = [(r[0], r[1]) for r in c.execute(
                "SELECT COALESCE(p.name,'(알 수 없음)'), COUNT(*) FROM messages m LEFT JOIN people p ON p.author_hash=m.author_hash "
                "WHERE m.room_key=? AND m.type<>0 GROUP BY 1 ORDER BY 2 DESC", (k,))]
            n, last = c.execute("SELECT COUNT(*), MAX(sent_at) FROM messages WHERE room_key=?", (k,)).fetchone()
            recent = _msg_rows(c, "m.room_key=? AND m.type<>0", [k], sample, order="DESC")[::-1]
            mc = c.execute("SELECT member_count FROM rooms WHERE room_id=?", (k,)).fetchone()
            out.append({"room_id": k, "title": _room_title(c, k), "registered_vendor": vend.get(k),
                        "member_count": mc[0] if mc else None, "messages": n,
                        "last_message": datetime.fromtimestamp(last, KST).strftime("%Y-%m-%d") if last else None,
                        "senders": [f"{s}({cnt})" for s, cnt in senders[:5]], "distinct_senders": len(senders),
                        "recent": [f"{m['sender']}: {(m['text'] or '')[:60]}" for m in recent],
                        "decision": (c.execute("SELECT decision FROM privacy_rooms WHERE room_key=?", (k,)).fetchone() or [None])[0]})
        return {"rooms": out, "already_decided": len(decided)}


@mcp.tool()
def decide_privacy(exclude: list[str] | None = None, keep: list[str] | None = None, reason: str = "") -> dict:
    """privacy_review 의 세 목록을 사용자에게 보여 주고 불확실한 방에 대한 답을 받은 뒤 기록한다(room_id 목록).
    목록을 보여 주기 전에 호출하지 말 것.
    exclude: 개인 대화라 수집하지 않을 방 — 수집 제외 목록에 올리고, 이미 모은 그 방의 대화·파일 목록·받은 원본·내보낸 txt 를
             지우고 거래처 등록도 해제한다. 이후 내보내기·톡클라우드 어느 경로로도 다시 수집하지 않는다.
    keep:    검토했지만 계속 수집할 방(다음 점검 때 다시 묻지 않음). 제외했던 방을 keep 으로 바꾸면 수집이 다시 시작된다.
    Claude 가 만든 업무 메모(notes)·문서 기록(documents)은 지우지 않는다 — 필요하면 사용자에게 따로 확인."""
    now = now_iso()
    done = {"excluded": [], "kept": []}
    with con() as c:
        for rid in exclude or []:
            done["excluded"].append(exclude_room(c, DB.parent, rid, _room_title(c, rid), reason))
        for rid in keep or []:
            title = _room_title(c, rid)
            c.execute("INSERT INTO privacy_rooms VALUES (?,?,'kept',?,?) ON CONFLICT(room_key) DO UPDATE SET "
                      "decision='kept', title=excluded.title, reason=excluded.reason, decided_at=excluded.decided_at",
                      (rid, title, reason, now))
            done["kept"].append(title)
    return done


@mcp.tool()
def list_privacy_decisions() -> list[dict]:
    """개인 대화 점검 결과(수집 제외 / 수집 유지) 목록."""
    with con() as c:
        return [dict(r) for r in c.execute("SELECT room_key, title, decision, reason, decided_at FROM privacy_rooms ORDER BY decided_at DESC")]


@mcp.tool()
def unregister_vendor_room(room_id: str) -> dict:
    """거래처 방 지정 해제(사용자 요청 시). 이미 받은 파일·기록은 지우지 않는다."""
    with con() as c:
        n = c.execute("DELETE FROM vendor_rooms WHERE room_id=?", (room_id,)).rowcount
    return {"ok": bool(n)}


@mcp.tool()
def search_rooms(keyword: str) -> list[dict]:
    """방 이름으로 검색(거래처 지정 여부와 무관하게 전체 방)."""
    with con() as c:
        return [dict(r) for r in c.execute(
            "SELECT r.room_id, r.title, r.member_count, v.vendor FROM rooms r LEFT JOIN vendor_rooms v ON v.room_id=r.room_id "
            "WHERE r.title LIKE ? ORDER BY r.title LIMIT 50", (f"%{keyword}%",))]


def _items_query(where: str, params: list, limit: int):
    with con() as c:
        rows = c.execute(
            "SELECT i.item_id, i.vertical, i.created_at, i.file_name, i.mime, i.size, i.local_path, i.download_error, "
            " i.ocr_hint, substr(i.ocr_text, 1, 300) ocr_excerpt, "
            " r.title room_title, v.vendor, COALESCE(p.name, '(알 수 없음)') sender, "
            " (SELECT 1 FROM reviewed x WHERE x.item_id=i.item_id) reviewed "
            "FROM items i LEFT JOIN rooms r ON r.room_id=i.room_id LEFT JOIN vendor_rooms v ON v.room_id=i.room_id "
            f"LEFT JOIN people p ON p.author_hash=i.author_hash WHERE {where} ORDER BY i.created_at DESC LIMIT ?",
            params + [limit]).fetchall()
    return [dict(r, created_at=ts(r["created_at"]), reviewed=bool(r["reviewed"])) for r in rows]


@mcp.tool()
def list_unreviewed_items(vendor: str | None = None, photos: str = "doc_like", limit: int = 50) -> list[dict]:
    """업무방의 아직 확인(기록)하지 않은 파일·사진. local_path 의 파일을 직접 열어 내용을 확인할 것(사진도 직접 봄).
    photos: "doc_like"(기본) = 사진은 PC 안 OCR 로 세금계산서·견적서 등 문서 단어가 보인 것만(ocr_hint·ocr_excerpt 참고),
            "all" = 사진 전부, "none" = 파일만. 사진이 수천 장이라 기본은 doc_like.
    local_path 가 비어 있으면 아직 다운로드 전 또는 download_error 참고('skipped: 비밀 정보…' 파일은 열지 말 것)."""
    # 수집 시작점(처음 수집한 날 collect_started − collect_months) 이후 자료만 — 대화와 같은 시작점. 설정이 바뀔 수 있어 매번 읽는다
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8")) if (HERE / "config.json").exists() else {}
    started = datetime.fromisoformat(cfg.get("collect_started") or date.today().isoformat()).replace(tzinfo=KST)
    cutoff = int((started.timestamp() - float(cfg.get("collect_months", 3)) * 30.44 * 86400) * 1000)
    if cfg.get("auto_delete"):   # 오래된 기록 자동 삭제를 켰으면 그 범위 밖도 제외
        cutoff = max(cutoff, int((datetime.now().timestamp() - float(cfg.get("auto_delete_months", 12)) * 30.44 * 86400) * 1000))
    where, params = "v.vendor IS NOT NULL AND i.item_id NOT IN (SELECT item_id FROM reviewed) AND i.created_at >= ?", [cutoff]
    if vendor:
        where += " AND v.vendor LIKE ?"
        params.append(f"%{vendor}%")
    if photos == "none":
        where += " AND i.vertical='FILE'"
    elif photos != "all":
        where += " AND (i.vertical='FILE' OR i.ocr_hint IS NOT NULL)"
    return _items_query(where, params, limit)


@mcp.tool()
def list_room_items(room_or_vendor: str, days: int = 30, files_only: bool = False, limit: int = 100) -> list[dict]:
    """특정 방(이름 일부) 또는 거래처의 최근 파일·사진 목록. 거래처가 아닌 방은 원본 파일 없이 목록만 있다."""
    since = int((datetime.now().timestamp() - days * 86400) * 1000)
    where = "(r.title LIKE ? OR v.vendor LIKE ?) AND i.created_at >= ?"
    params: list = [f"%{room_or_vendor}%", f"%{room_or_vendor}%", since]
    if files_only:
        where += " AND i.vertical='FILE'"
    return _items_query(where, params, limit)


def _day(s: str, end: bool = False) -> int:
    d = datetime.fromisoformat(s).replace(tzinfo=KST)
    return int((d + timedelta(days=1) if end and len(s) <= 10 else d).timestamp())


def _msg_rows(c, where: str, params: list, limit: int, order: str = "ASC"):
    rows = c.execute(
        f"SELECT m.room_key, m.room_title, m.sent_at, COALESCE(p.name,'(알 수 없음)') sender, m.message, m.has_attachment "
        f"FROM messages m LEFT JOIN people p ON p.author_hash=m.author_hash WHERE {where} "
        f"ORDER BY m.sent_at {order} LIMIT ?", params + [limit]).fetchall()
    return [{"room": r["room_title"], "time": datetime.fromtimestamp(r["sent_at"], KST).strftime("%Y-%m-%d %H:%M"),
             "sender": r["sender"], "text": r["message"] if not r["has_attachment"] or r["message"] else "(첨부)",
             "_t": r["sent_at"], "_room": r["room_key"]} for r in rows]


def _clean(msgs):
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in msgs]


@mcp.tool()
def get_messages(room_keyword: str, since: str | None = None, until: str | None = None, limit: int = 300) -> dict:
    """저장된 대화의 메시지(보낸 사람·시각·본문)를 시간순으로. since/until 'YYYY-MM-DD' 또는 'YYYY-MM-DD HH:MM'.
    대화가 없으면 그 방이 아직 내보내기(kakao_exporter.ps1)되지 않은 것(최근 180일만 보관)."""
    with con() as c:
        where, params = "(m.room_title LIKE ? OR m.room_key IN (SELECT room_id FROM vendor_rooms WHERE vendor LIKE ?))", \
            [f"%{room_keyword}%", f"%{room_keyword}%"]
        if since:
            where += " AND m.sent_at >= ?"
            params.append(_day(since))
        if until:
            where += " AND m.sent_at < ?"
            params.append(_day(until, end=True))
        msgs = _msg_rows(c, where, params, limit)
        if not msgs:
            return {"found": False, "note": "저장된 대화 없음 — 이 방을 PC 카카오톡에서 내보내기(kakao_exporter.ps1)해야 읽을 수 있음"}
        return {"found": True, "count": len(msgs), "messages": _clean(msgs)}


@mcp.tool()
def search_messages(query: str, room_or_vendor: str | None = None, since: str | None = None,
                    until: str | None = None, context: int = 2, limit: int = 30) -> dict:
    """저장된 대화 원문 검색(사용자가 기억 못하는 내용 찾기). query 는 공백으로 나눈 단어가 모두 들어간 메시지를 찾는다
    (대소문자 무시). 각 결과에 앞뒤 context 개 메시지를 붙여 맥락을 보여 준다. 최신 결과부터."""
    words = [w for w in query.split() if w]
    if not words:
        return {"hits": []}
    where = " AND ".join("LOWER(m.message) LIKE ?" for _ in words)
    params: list = [f"%{w.lower()}%" for w in words]
    if room_or_vendor:
        where += " AND (m.room_title LIKE ? OR m.room_key IN (SELECT room_id FROM vendor_rooms WHERE vendor LIKE ?))"
        params += [f"%{room_or_vendor}%"] * 2
    if since:
        where += " AND m.sent_at >= ?"
        params.append(_day(since))
    if until:
        where += " AND m.sent_at < ?"
        params.append(_day(until, end=True))
    with con() as c:
        hits = _msg_rows(c, where, params, limit, order="DESC")
        out = []
        for h in hits:
            before = _msg_rows(c, "m.room_key=? AND m.sent_at<?", [h["_room"], h["_t"]], context, order="DESC")[::-1]
            after = _msg_rows(c, "m.room_key=? AND m.sent_at>?", [h["_room"], h["_t"]], context)
            out.append({"hit": _clean([h])[0], "before": _clean(before), "after": _clean(after)})
    return {"count": len(out), "hits": out}


@mcp.tool()
def recent_updates(since: str | None = None, hours: int = 24, work_rooms_only: bool = True) -> dict:
    """업무방의 최근 갱신 내용(새 메시지 + 새 파일)을 방별로 묶어 준다. since 'YYYY-MM-DD HH:MM'(없으면 최근 hours 시간).
    이걸 읽고 방(업체)별로 '갱신한 건' — 무엇이 바뀌었는지(가격·요청·회신·약속·일정) + 날짜·사람·원문 인용 +
    '→ 다음 할 일' 형식으로 요약하고, 중요한 것은 record_note 로 업무 메모에 남긴다."""
    start = _day(since) if since else int(datetime.now().timestamp()) - hours * 3600
    with con() as c:
        where, params = "m.sent_at >= ?", [start]
        if work_rooms_only:
            where += " AND m.room_key IN (SELECT room_id FROM vendor_rooms)"
        msgs = _msg_rows(c, where, params, 2000)
        rooms: dict[str, dict] = {}
        vend = {r["room_id"]: r["vendor"] for r in c.execute("SELECT room_id, vendor FROM vendor_rooms")}
        for m in msgs:
            rooms.setdefault(m["_room"], {"vendor": vend.get(m["_room"]), "room": m["room"], "messages": [], "files": []})
            rooms[m["_room"]]["messages"].append(_clean([m])[0])
        fwhere = "i.created_at >= ? AND i.vertical='FILE'" + (" AND v.vendor IS NOT NULL" if work_rooms_only else "")
        for f in _items_query(fwhere, [start * 1000], 200):
            rid = c.execute("SELECT room_id FROM items WHERE item_id=?", (f["item_id"],)).fetchone()[0]
            rooms.setdefault(rid, {"vendor": f["vendor"], "room": f["room_title"], "messages": [], "files": []})
            rooms[rid]["files"].append({k: f[k] for k in ("item_id", "created_at", "file_name", "sender", "local_path")})
        exported = {k[len("export_at:"):]: v[:16].replace("T", " ") for k, v in
                    c.execute("SELECT key, value FROM sync_state WHERE key LIKE 'export_at:%'")}
        for rid, r in rooms.items():
            r["conversation_as_of"] = exported.get(rid)
        never = [t for rid, t in c.execute("SELECT v.room_id, COALESCE(r.title, v.room_id) FROM vendor_rooms v "
                                           "LEFT JOIN rooms r ON r.room_id=v.room_id") if rid not in exported]
    return {"since": datetime.fromtimestamp(start, KST).strftime("%Y-%m-%d %H:%M"),
            "note": "대화는 방마다 PC 카카오톡에서 내보낸 시점(rooms[].conversation_as_of)까지만 반영됨",
            "work_rooms_never_exported": never,
            "rooms": list(rooms.values())}


@mcp.tool()
def record_note(room_or_vendor: str, kind: str, content: str, source_time: str | None = None,
                people: str | None = None, due: str | None = None) -> dict:
    """대화에서 뽑은 업무 메모 저장. kind: 결정, 요청, 약속, 가격, 일정, 할일, 이슈, 정보 중 하나.
    content 는 나중에 읽어도 이해되게 한 문장 이상(누가·무엇을·얼마·언제). source_time 은 근거 메시지 시각
    'YYYY-MM-DD HH:MM', due 는 기한 'YYYY-MM-DD'. 같은 내용을 중복 저장하지 말 것(search_notes 로 먼저 확인)."""
    with con() as c:
        cur = c.execute("INSERT INTO notes (room_or_vendor, kind, content, source_time, people, due, status, created_at) "
                        "VALUES (?,?,?,?,?,?, 'open', ?)", (room_or_vendor, kind, content, source_time, people, due, now_iso()))
    return {"ok": True, "note_id": cur.lastrowid}


@mcp.tool()
def search_notes(query: str | None = None, room_or_vendor: str | None = None, kind: str | None = None,
                 status: str | None = None, limit: int = 50) -> list[dict]:
    """업무 메모 검색. 기억 안 나는 내용을 물으면 이것부터 찾고, 근거는 search_messages 로 원문 확인.
    status: open(진행 중)/done(완료). 최신 근거 시각 순."""
    where, params = "1=1", []
    for w in (query or "").split():
        where += " AND LOWER(content) LIKE ?"
        params.append(f"%{w.lower()}%")
    if room_or_vendor:
        where += " AND room_or_vendor LIKE ?"
        params.append(f"%{room_or_vendor}%")
    if kind:
        where += " AND kind = ?"
        params.append(kind)
    if status:
        where += " AND status = ?"
        params.append(status)
    with con() as c:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM notes WHERE {where} ORDER BY COALESCE(source_time, created_at) DESC LIMIT ?", params + [limit])]


@mcp.tool()
def update_note(note_id: int, status: str | None = None, content: str | None = None) -> dict:
    """업무 메모 상태 변경(open→done 등) 또는 내용 보완."""
    with con() as c:
        if status:
            c.execute("UPDATE notes SET status=? WHERE id=?", (status, note_id))
        if content:
            c.execute("UPDATE notes SET content=? WHERE id=?", (content, note_id))
    return {"ok": True}


@mcp.tool()
def record_document(item_id: str, vendor: str, doc_type: str, period: str, title: str,
                    amount_total: float | None = None, fields: dict | None = None, summary: str = "") -> dict:
    """확인한 파일을 업무 문서로 기록하고 확인 완료 처리.
    doc_type 예: 견적서, 세금계산서, 거래명세서, 제안서, 청구서, 계약서, 기타.
    period: 문서가 해당하는 기간 'YYYY-MM' (월 견적서면 적용 월).
    fields: 추출값 자유 형식. 견적서는 {"items":[{"품목":..,"규격":..,"단위":..,"단가":..,"수량":..,"금액":..}], "유효기간":..} 권장."""
    with con() as c:
        if not c.execute("SELECT 1 FROM items WHERE item_id=?", (item_id,)).fetchone():
            return {"ok": False, "error": "item_id 없음"}
        cur = c.execute("INSERT INTO documents (item_id, vendor, doc_type, period, title, amount_total, fields_json, summary, "
                        "recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (item_id, vendor, doc_type, period, title, amount_total,
                         json.dumps(fields or {}, ensure_ascii=False), summary, now_iso()))
        c.execute("INSERT OR REPLACE INTO reviewed VALUES (?,?,?)", (item_id, now_iso(), f"document #{cur.lastrowid}"))
        return {"ok": True, "document_id": cur.lastrowid}


@mcp.tool()
def mark_reviewed(item_ids: list[str], note: str = "") -> dict:
    """업무 문서가 아닌 자료(사진·잡담 파일 등)를 확인 완료로 표시."""
    with con() as c:
        c.executemany("INSERT OR REPLACE INTO reviewed VALUES (?,?,?)", [(i, now_iso(), note) for i in item_ids])
    return {"ok": True, "count": len(item_ids)}


@mcp.tool()
def list_documents(vendor: str | None = None, doc_type: str | None = None, since_period: str | None = None,
                   include_fields: bool = False, limit: int = 100) -> list[dict]:
    """기록된 업무 문서 조회(단가 비교·이력 확인용). since_period 'YYYY-MM' 이후."""
    where, params = "1=1", []
    for col, val in (("d.vendor LIKE ?", vendor), ("d.doc_type = ?", doc_type), ("d.period >= ?", since_period)):
        if val:
            where += f" AND {col}"
            params.append(f"%{val}%" if "LIKE" in col else val)
    with con() as c:
        rows = c.execute(f"SELECT d.*, i.local_path, i.created_at received_at FROM documents d "
                         f"LEFT JOIN items i ON i.item_id=d.item_id WHERE {where} ORDER BY d.period DESC, d.id DESC LIMIT ?",
                         params + [limit]).fetchall()
    out = []
    for r in rows:
        d = dict(r, received_at=ts(r["received_at"]))
        d["fields"] = json.loads(d.pop("fields_json") or "{}") if include_fields else None
        out.append(d)
    return out


@mcp.tool()
def set_expectation(vendor: str, doc_type: str, due_day: int, grace_days: int = 3, note: str = "") -> dict:
    """정기 문서 규칙 등록/수정. 예: 매달 말일 월 견적서 → due_day=31(말일로 처리), grace_days=3."""
    with con() as c:
        if not _vendor_room(c, vendor):
            return {"ok": False, "error": "거래처로 등록되지 않은 업체 — register_vendor_rooms 로 먼저 등록"}
        c.execute("INSERT OR REPLACE INTO expectations VALUES (?,?,?,?,?)", (vendor, doc_type, due_day, grace_days, note))
    return {"ok": True}


@mcp.tool()
def check_overdue(as_of: str | None = None) -> list[dict]:
    """정기 문서 규칙 대비 미수신 점검(이번 달·지난달). as_of 'YYYY-MM-DD' (기본 오늘).
    기한+유예가 지났는데 해당 period 문서 기록이 없으면 overdue. 기록 전 미확인 자료가 있으면 먼저 확인할 것."""
    today = date.fromisoformat(as_of) if as_of else datetime.now(KST).date()
    out = []
    with con() as c:
        for e in c.execute("SELECT * FROM expectations"):
            for back in (1, 0):
                y, m = (today.year, today.month - back) if today.month - back >= 1 else (today.year - 1, 12)
                due = date(y, m, min(e["due_day"], calendar.monthrange(y, m)[1]))
                limit_day = due + timedelta(days=e["grace_days"])
                period = f"{y:04d}-{m:02d}"
                have = c.execute("SELECT id FROM documents WHERE vendor=? AND doc_type=? AND period=?",
                                 (e["vendor"], e["doc_type"], period)).fetchone()
                vr = _vendor_room(c, e["vendor"])
                pending = c.execute("SELECT COUNT(*) FROM items WHERE room_id=? AND item_id NOT IN (SELECT item_id FROM reviewed)",
                                    (vr["room_id"] if vr else "",)).fetchone()[0]
                if not have and today > limit_day:
                    out.append({"vendor": e["vendor"], "doc_type": e["doc_type"], "period": period,
                                "due": due.isoformat(), "grace_until": limit_day.isoformat(),
                                "days_late": (today - limit_day).days, "unreviewed_items_in_room": pending})
    return out


@mcp.tool()
def queue_draft(vendor: str, text: str, reason: str) -> dict:
    """거래처에 보낼 메시지 초안을 승인 대기열에 넣는다. 실제 발송은 사람이 승인한 뒤에만 이뤄진다."""
    with con() as c:
        vr = _vendor_room(c, vendor)
        if not vr:
            return {"ok": False, "error": "거래처 방을 찾지 못함"}
        dup = c.execute("SELECT id FROM drafts WHERE vendor=? AND status='pending' AND reason=?", (vr["vendor"], reason)).fetchone()
        if dup:
            return {"ok": False, "error": f"같은 사유의 대기 중 초안 #{dup['id']} 이 이미 있음"}
        cur = c.execute("INSERT INTO drafts (vendor, room_id, text, reason, created_at) VALUES (?,?,?,?,?)",
                        (vr["vendor"], vr["room_id"], text, reason, now_iso()))
    return {"ok": True, "draft_id": cur.lastrowid, "status": "pending"}


@mcp.tool()
def list_drafts(status: str = "pending") -> list[dict]:
    """메시지 초안 목록(pending/approved/rejected/sent)."""
    with con() as c:
        return [dict(r) for r in c.execute("SELECT * FROM drafts WHERE status=? ORDER BY id DESC", (status,))]


if __name__ == "__main__":
    mcp.run()
