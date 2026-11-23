"""PC 카카오톡 '대화 내보내기' txt → messages 표 반영 (대화 원문의 주 출처, 2026-11-23 결정).

kakao_exporter.ps1 이 data/exports/ 에 남긴 txt 를 읽어, MCP 도구(get_messages·search_messages·recent_updates)가
쓰는 messages 표에 같은 형식으로 넣는다.

- 메시지 키 = 방·분·보낸 사람·본문(+같은 내용 반복 순번) → 같은 방을 매일 다시 내보내도 새로 붙은 대화만 들어간다.
- 내보내기에는 시각이 분 단위뿐이라, 같은 분 안의 순서를 지키려고 sent_at 초 자리에 그 분 안의 순번(0~59)을 쓴다.
- 보낸 사람은 이름만 있으므로 people 에 author_hash='export:<이름>' 으로 등록한다.
- 같은 방·같은 기간의 채팅방 저장소(톡클라우드) 메시지는 중복이므로 지운다(내보내기가 우선).
  사용 중 화면 인식으로 넣은 임시 대화(snapshot_id 'live:%', live_capture.py)도 내보낸 시각 이전 것은 지운다.
- 수집 시작점 = 처음 수집한 날(collect_started) − collect_months(기본 3개월). 파일·사진과 같은 시작점이고, 그 뒤로는 계속 쌓인다.
  설정에서 개월 수를 줄이면 시작점보다 오래된 대화는 정리하고,
  늘리면 보관 중인 방별 최신 txt 를 다시 읽어 채운다(txt 에는 방 전체 대화가 들어 있음).
- 처리한 파일은 sync_state 에 기록하고, 방마다 가장 최근 파일만 남긴다.

실행: python -X utf8 export_import.py   (collector.py 도 매 회차 호출)
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from tc_session import KST, log

MSG = re.compile(r"^\[(.*?)\] \[(오전|오후) (\d{1,2}):(\d{2})\] ?(.*)$")
DAY = re.compile(r"^-{15} (\d{4})년 (\d{1,2})월 (\d{1,2})일 \S+ -{15}$")
HEADER = re.compile(r"^(.*) 님과 카카오톡 대화$")
SYSTEM = re.compile(r"(님을 초대했습니다\.?|님이 나갔습니다\.?|님을 내보냈습니다\.?|방장이 되었습니다\.?|"
                    r"방장이 되어 팀채팅을 시작했어요!?|님이 들어왔습니다\.?)$")
DELETED_NOTICE = "메시지가 삭제되었습니다."
ATTACHMENT = re.compile(r"^(사진( \d+장)?|동영상|이모티콘|음성메시지|파일: .+|삭제된 메시지입니다\.)$")


CONFIG_PATH = Path(__file__).resolve().with_name("config.json")


def collect_cutoff_s(cfg: dict) -> int:
    """수집 시작 시각(초) = 처음 수집한 날(collect_started) − collect_months 개월. 고정된 시작점이라
    그 뒤로는 대화·파일·사진이 지워지지 않고 계속 쌓인다(2026-11-23 사용자: '처음만 3개월, 이후 계속 쌓기').
    대화와 파일·사진에 같은 시작점을 쓴다(기간이 다르면 내용 파악이 어긋남). collect_started 가 없으면 오늘로 정해 저장."""
    started = cfg.get("collect_started")
    if not started:
        started = cfg["collect_started"] = datetime.now(KST).strftime("%Y-%m-%d")
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
            raw.setdefault("collect_started", started)
            CONFIG_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, ValueError):
            pass
    base = datetime.fromisoformat(started).replace(tzinfo=KST).timestamp()
    return int(base - float(cfg.get("collect_months", 3)) * 30.44 * 86400)


def keep_cutoff_s(cfg: dict) -> int:
    """이 시각(초)보다 오래된 수집 기록은 두지 않는다 = 수집 시작점, 그리고 '오래된 기록 자동 삭제'(auto_delete,
    기본 꺼짐)를 켰으면 지금부터 auto_delete_months(기본 12개월) 전 중 더 최근 쪽."""
    cut = collect_cutoff_s(cfg)
    if cfg.get("auto_delete"):
        cut = max(cut, int(datetime.now().timestamp() - float(cfg.get("auto_delete_months", 12)) * 30.44 * 86400))
    return cut


def trim_export(path: Path, cutoff_s: int) -> bool:
    """내보낸 대화 txt 에서 cutoff 이전 날짜 부분을 잘라낸다(자동 삭제가 txt 에 남은 원문까지 지우도록). 바뀌면 True."""
    lines = path.read_text(encoding="utf-8-sig").replace("\r", "").split("\n")
    days = [(i, DAY.match(l)) for i, l in enumerate(lines)]
    days = [(i, m) for i, m in days if m]
    if not days:
        return False
    cut_day = datetime.fromtimestamp(cutoff_s, KST).date()
    keep_at = next((i for i, m in days if datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date() >= cut_day), len(lines))
    if keep_at == days[0][0]:
        return False
    path.write_text("\n".join(lines[:days[0][0]] + lines[keep_at:]), encoding="utf-8")
    return True


def norm_title(t: str | None) -> str:
    return re.sub(r"\s+", " ", (t or "").replace("\xa0", " ")).strip()


def parse_export(path: Path) -> tuple[str, list[dict]]:
    """txt → (방 이름, [{t, sender, text, system}]) — t 는 분 단위 epoch 초."""
    lines = path.read_text(encoding="utf-8-sig").replace("\r", "").split("\n")
    m = HEADER.match(lines[0].strip()) if lines else None
    if not m:
        raise ValueError(f"내보내기 파일 형식이 아님: {path.name}")
    title = norm_title(m.group(1))
    out: list[dict] = []
    day = None
    cur = None
    for line in lines[1:]:
        d = DAY.match(line)
        if d:
            day = (int(d.group(1)), int(d.group(2)), int(d.group(3)))
            cur = None
            continue
        if day is None:          # 머리말(저장한 날짜 등)
            continue
        mm = MSG.match(line)
        if mm:
            h = int(mm.group(3)) % 12 + (12 if mm.group(2) == "오후" else 0)
            t = int(datetime(*day, h, int(mm.group(4)), tzinfo=KST).timestamp())
            cur = {"t": t, "sender": mm.group(1), "text": mm.group(5), "system": False}
            out.append(cur)
            continue
        if line.strip() == DELETED_NOTICE:
            cur = None
            continue
        if SYSTEM.search(line.strip()):    # 시각이 없으므로 같은 날 직전 메시지 시각(없으면 그날 0시)
            day_start = int(datetime(*day, tzinfo=KST).timestamp())
            t = out[-1]["t"] if out and out[-1]["t"] >= day_start else day_start
            out.append({"t": t, "sender": None, "text": line.strip(), "system": True})
            cur = None
            continue
        if cur is not None:      # 여러 줄 메시지의 다음 줄
            cur["text"] += "\n" + line
    for o in out:
        o["text"] = o["text"].rstrip()
    return title, out


def import_file(con: sqlite3.Connection, path: Path, keep_from: int) -> dict:
    title, msgs = parse_export(path)
    room_by_title = {norm_title(t): rid for rid, t in con.execute("SELECT room_id, title FROM rooms")}
    room_key = room_by_title.get(title) or f"title:{title}"
    excluded = con.execute("SELECT 1 FROM privacy_rooms WHERE decision='excluded' AND (room_key=? OR title=?)",
                           (room_key, title)).fetchone()
    if excluded:   # 개인 대화로 제외된 방: 반영하지 않고 내보낸 파일도 지운다
        path.unlink(missing_ok=True)
        return {"file": path.name, "room": title, "room_key": room_key, "parsed": len(msgs), "in_retention": 0,
                "added": 0, "removed_snapshot_dupes": 0, "excluded": True}
    source = f"export:{path.name}"
    seen: dict[str, int] = {}
    minute_seq: dict[int, int] = {}
    rows, people = [], set()
    for m in msgs:
        seq = minute_seq[m["t"]] = minute_seq.get(m["t"], -1) + 1
        if m["t"] < keep_from:
            continue
        author = f"export:{m['sender']}" if m["sender"] else None
        if m["sender"]:
            people.add((author, m["sender"]))
        base = f"{room_key}|{m['t']}|{m['sender']}|{m['text']}"
        n = seen[base] = seen.get(base, 0) + 1                 # 같은 분·같은 사람·같은 내용 반복 구분
        key = hashlib.sha1(f"export|{base}#{n}".encode("utf-8")).hexdigest()
        attach = 1 if ATTACHMENT.match(m["text"]) else 0
        rows.append((key, room_key, title, source, author, m["t"] + min(seq, 59), 0 if m["system"] else 1,
                     m["text"], attach, 0, datetime.now(KST).isoformat(timespec="seconds")))
    con.executemany("INSERT INTO people VALUES (?,?,'export') ON CONFLICT(author_hash) DO NOTHING", sorted(people))
    removed = 0
    if rows:
        lo, hi = min(r[5] for r in rows), max(r[5] for r in rows) + 60
        removed = con.execute("DELETE FROM messages WHERE room_key=? AND sent_at>=? AND sent_at<? "
                              "AND (snapshot_id IS NULL OR snapshot_id NOT LIKE 'export:%')", (room_key, lo, hi)).rowcount
    # 사용 중 화면 인식으로 넣은 임시 대화(live_capture)는 내보낸 시각까지 원문이 생겼으므로 지운다
    removed += con.execute("DELETE FROM messages WHERE room_key=? AND snapshot_id LIKE 'live:%' AND sent_at<?",
                           (room_key, int(path.stat().st_mtime) + 60)).rowcount
    added = con.executemany("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows).rowcount
    con.execute("INSERT INTO sync_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"export_at:{room_key}", datetime.fromtimestamp(path.stat().st_mtime, KST).isoformat(timespec="seconds")))
    return {"file": path.name, "room": title, "room_key": room_key, "parsed": len(msgs), "in_retention": len(rows),
            "added": added, "removed_snapshot_dupes": removed}


def import_exports(con: sqlite3.Connection, cfg: dict) -> list[dict]:
    exp_dir = Path(cfg["data_dir"]) / "exports"
    if not exp_dir.exists():
        return []
    keep_from = keep_cutoff_s(cfg)
    row = con.execute("SELECT value FROM sync_state WHERE key='messages_from'").fetchone()
    if row and keep_from < int(row[0]) - 86400:
        # 기간을 늘렸다 → 남아 있는 방별 최신 txt 를 다시 읽어 늘어난 기간을 채운다(같은 메시지는 키가 같아 중복 없음)
        con.execute("DELETE FROM sync_state WHERE key LIKE 'export_done:%'")
        log("수집 기간이 늘어 보관 중인 대화 파일을 다시 읽습니다")
    pruned = con.execute("DELETE FROM messages WHERE sent_at < ?", (keep_from,)).rowcount   # 보관 범위 밖 대화 정리
    if pruned:
        log(f"보관 범위(수집 시작점·자동 삭제) 밖의 오래된 대화 {pruned}건 정리")
    con.execute("INSERT INTO sync_state VALUES ('messages_from',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(keep_from),))
    con.commit()
    done = {k[len("export_done:"):] for (k,) in con.execute("SELECT key FROM sync_state WHERE key LIKE 'export_done:%'")}
    results = []
    files = sorted(exp_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime)
    for p in files:
        if p.name in done:
            continue
        try:
            r = import_file(con, p, keep_from)
        except Exception as e:    # 한 파일이 깨져도 나머지는 계속
            log(f"내보내기 반영 실패 {p.name}: {type(e).__name__}: {e}")
            continue
        con.execute("INSERT INTO sync_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"export_done:{p.name}", datetime.now(KST).isoformat(timespec="seconds")))
        con.commit()
        if r.get("excluded"):
            log(f"내보내기 '{r['room']}': 수집 제외된 방(개인 대화) — 반영하지 않고 파일 삭제")
            continue
        log(f"내보내기 '{r['room']}': 새 메시지 {r['added']}건 (파일 {r['parsed']}건 중 보관기간 내 {r['in_retention']}건, "
            f"중복 저장소 메시지 {r['removed_snapshot_dupes']}건 정리)")
        results.append(r)
    # 방마다 가장 최근 파일만 남김(반영 끝난 옛 파일 삭제)
    latest: dict[str, Path] = {}
    for p in sorted(exp_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime):
        if not p.exists():
            continue
        try:
            title = norm_title(HEADER.match(p.open(encoding="utf-8-sig").readline().strip()).group(1))
        except Exception:
            continue
        if title in latest and latest[title].name in done | {r["file"] for r in results}:
            latest[title].unlink()
        latest[title] = p
    if cfg.get("auto_delete"):   # 자동 삭제를 켰으면 남겨 둔 원문 txt 에서도 오래된 부분을 잘라낸다
        trimmed = sum(trim_export(p, keep_from) for p in latest.values() if p.exists())
        if trimmed:
            log(f"자동 삭제: 대화 원문 {trimmed}개 파일에서 오래된 부분을 잘라냄")
    return results


if __name__ == "__main__":
    from collector import db_open, load_config
    cfg = load_config()
    c = db_open(Path(cfg["data_dir"]))
    try:
        for r in import_exports(c, cfg):
            print(r)
    finally:
        c.close()
