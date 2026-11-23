"""사용 중 실시간 수집 — 열려 있는 업무방 창의 화면 인식(live_ocr.ps1) 결과를 '임시 대화'로 저장한다.

- live_ocr.ps1 이 3초마다 보이는 업무방 창을 캡처해 화면이 바뀌었을 때만 OCR 결과(글자 줄·위치·말풍선)를 한 줄 JSON 으로 낸다.
  창을 앞으로 가져오거나 클릭하지 않는다(화면을 뺏지 않음). 수집 중인 업무방만 본다 — 개인 대화로 제외된 방·미등록 방은 읽지 않음.
- 여기서 말풍선을 날짜·보낸 사람·시각·내용으로 나눠 messages 에 snapshot_id='live:ocr' 로 넣는다(임시 대화).
  보낸 사람은 그 방에서 이미 확인된 이름과 맞춰 고치고, 노란 말풍선은 사용자 본인. 같은 내용을 다시 읽으면 걸러낸다.
- 화면 인식이라 글자가 틀릴 수 있다(실측 글자 정확도 약 85~90%). 쉬는 시간 내보내기 원문이 들어오면(export_import)
  그 방의 내보낸 시각 이전 임시 대화는 지우고 원문으로 바꾼다. Claude 에게는 '화면 인식(임시)'으로 표시된다.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from collector import db_open, excluded_rooms, norm_title
from export_import import keep_cutoff_s
from tc_session import KST

HERE = Path(__file__).resolve().parent
WORKER = HERE / "live_ocr.ps1"
SOURCE = "live:ocr"

DATE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
TIME = re.compile(r"(?:\d{1,3}\s+)?(오\S)?\s*(\d{1,2})\s*[:.;,·'`]?\s*(\d{2})")
FILE_CARD = re.compile(r"다른\s*이름으로\s*저장|유효\s*기\s*간|용\s*량\s*[:;]")


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def similar(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _clock(t: str):
    """'오후 3:55'·'오후 355'(쌍점을 못 읽음)·'4.06'(오전/오후 못 읽음) → (시, 분, 'am'|'pm'|None). 아니면 None."""
    m = TIME.fullmatch(t.strip())
    if not m:
        return None
    h, mi = int(m.group(2)), int(m.group(3))
    if not (1 <= h <= 12 and mi < 60):
        return None
    ampm = None
    if m.group(1):
        ampm = "am" if "전" in m.group(1) else "pm"
    return h, mi, ampm


def _to_ts(day: date, clock, at: datetime) -> int | None:
    h, mi, ampm = clock
    if ampm:
        cands = [h % 12 + (12 if ampm == "pm" else 0)]
    else:                       # 오전/오후를 못 읽으면 캡처 시각 이전 중 가장 늦은 쪽
        cands = [h % 12, h % 12 + 12]
    best = None
    for hh in cands:
        ts = datetime(day.year, day.month, day.day, hh, mi, tzinfo=KST)
        if ts <= at + timedelta(minutes=1) and (best is None or ts > best):
            best = ts
    if best is None and ampm:
        return None             # 미래 시각 = 날짜 추정이 틀림
    return int(best.timestamp()) if best else None


def parse_frame(fr: dict, names: list[str], me: str | None) -> list[dict]:
    """OCR 한 화면 → [{t(분 단위 epoch 초), sender, text, attach}]. 확실하지 않은 말풍선(잘린 것·보낸 사람 모름·시각 없음)은 버린다."""
    W = max(1, int(fr["w"]))
    at = datetime.fromisoformat(fr["at"]).replace(tzinfo=KST)
    lines = sorted(fr.get("lines") or [], key=lambda l: (l["y"], l["x"]))
    hs = sorted(l["b"] - l["y"] for l in lines if l.get("bt", -1) >= 0) or [13]
    lh = max(8, hs[len(hs) // 2])

    bubbles: dict[tuple, dict] = {}
    events: list[tuple] = []
    for l in lines:
        t = (l.get("t") or "").strip()
        if not t:
            continue
        if l.get("bt", -1) >= 0:
            key = (l["bt"], l.get("c"))
            b = bubbles.get(key)
            if b is None:
                b = bubbles[key] = {"top": l["bt"], "mine": l.get("c") == "y", "lines": [], "bottom": l["b"]}
                events.append((l["bt"], "bubble", b))
            if b["lines"] and l["y"] - b["bottom"] > 1.6 * lh:
                b["lines"].append("\n")
            b["lines"].append(t)
            b["bottom"] = max(b["bottom"], l["b"])
            continue
        if d := DATE.search(t):
            try:
                events.append((l["y"], "date", date(int(d.group(1)), int(d.group(2)), int(d.group(3)))))
            except ValueError:
                pass
            continue
        if c := _clock(t):
            events.append((l["y"], "time", (c, l["b"])))
            continue
        if l["x"] < 0.14 * W or (l["x"] + l["r"]) / 2 > 0.45 * W:
            continue            # 프로필 사진 자리의 잡음, 가운데·오른쪽(안내 문구 등)
        events.append((l["y"], "name", t))
    events.sort(key=lambda e: (e[0], 0 if e[1] == "date" else 1))

    has_date = any(e[1] == "date" for e in events)
    day = at.date() if not has_date else None   # 날짜 구분선이 안 보이면 오늘로 본다(이전 날이면 아래 중복 검사가 걸러냄)
    sender, pending, out = None, [], []
    for i, (y, kind, v) in enumerate(events):
        if kind == "date":
            day, sender, pending = v, None, []
        elif kind == "name":
            nxt = next((e for e in events[i + 1:] if e[1] == "bubble"), None)
            if not nxt or nxt[2]["mine"] or nxt[0] - y > 3 * lh:
                continue        # 바로 아래 남의 말풍선이 없으면 이름 줄이 아님
            best = max(names, key=lambda n: similar(n, v), default=None)
            sender = best if best and similar(best, v) >= 0.55 else re.sub(r"\s+", " ", v)
        elif kind == "bubble":
            who = me if v["mine"] else sender
            if v["mine"]:
                sender = None   # 내 말 다음의 남의 말에는 이름 줄이 다시 나온다
            pending.append({**v, "who": who, "day": day, "cut": v["top"] <= 1})
        elif kind == "time":
            clock, tb = v
            for b in [p for p in pending if p["bottom"] <= tb + lh]:
                pending.remove(b)
                if b["cut"] or not b["who"] or not b["day"]:
                    continue
                ts = _to_ts(b["day"], clock, at)
                if ts is None:
                    continue
                text = "".join(x if x == "\n" else (" " + x) for x in b["lines"]).replace(" \n", "\n").replace("\n ", "\n").strip()
                attach = 0
                if FILE_CARD.search(text):
                    text, attach = "파일: " + b["lines"][0], 1
                out.append({"t": ts, "sender": b["who"], "text": text, "attach": attach})
    return out


class Store:
    """임시 대화를 DB 에 넣는다(한 스레드에서만 사용)."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.con = db_open(Path(cfg["data_dir"]))
        self._names: dict[str, tuple[float, list[str]]] = {}
        self._me: str | None = None

    def close(self):
        self.con.close()

    def me(self) -> str | None:
        """사용자 본인 표시 이름 = 내보낸 대화에서 가장 많은 방에 나오는 사람."""
        if self._me is None:
            r = self.con.execute("SELECT p.name FROM messages m JOIN people p ON p.author_hash=m.author_hash "
                                 "WHERE m.snapshot_id LIKE 'export:%' GROUP BY p.name "
                                 "ORDER BY COUNT(DISTINCT m.room_key) DESC LIMIT 1").fetchone()
            self._me = r[0] if r else None
        return self._me

    def names(self, room_key: str) -> list[str]:
        hit = self._names.get(room_key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        rows = [r[0] for r in self.con.execute(
            "SELECT DISTINCT p.name FROM messages m JOIN people p ON p.author_hash=m.author_hash "
            "WHERE m.room_key=? AND p.name IS NOT NULL", (room_key,))]
        self._names[room_key] = (time.time(), rows)
        return rows

    def room_key(self, title: str) -> str | None:
        """수집 중인 업무방이면 room_key, 아니면 None(제외·미등록 방은 저장하지 않음)."""
        t = norm_title(title)
        keys = [rid for rid, rt in self.con.execute("SELECT room_id, title FROM rooms") if norm_title(rt) == t]
        key = keys[0] if keys else f"title:{t}"
        ex_keys, ex_titles = excluded_rooms(self.con)
        if key in ex_keys or t in ex_titles:
            return None
        if not self.con.execute("SELECT 1 FROM vendor_rooms WHERE room_id=?", (key,)).fetchone():
            return None
        return key

    def ingest(self, fr: dict) -> int:
        title = norm_title(fr.get("title"))
        key = self.room_key(title)
        if not key:
            return 0
        msgs = parse_frame(fr, self.names(key), self.me())
        if not msgs:
            return 0
        c = self.con
        keep_from = keep_cutoff_s(self.cfg)
        last_export = c.execute("SELECT MAX(sent_at) FROM messages WHERE room_key=? AND snapshot_id LIKE 'export:%'",
                                (key,)).fetchone()[0] or 0
        history = [r[0] for r in c.execute(
            "SELECT message FROM messages WHERE room_key=? AND snapshot_id LIKE 'export:%' AND sent_at>=? "
            "ORDER BY sent_at DESC LIMIT 400", (key, last_export - 14 * 86400))]
        now = datetime.now(KST).isoformat(timespec="seconds")
        added = 0
        for m in msgs:
            if m["t"] < keep_from or m["t"] < last_export // 60 * 60:
                continue        # 원문이 이미 있는 구간
            near = [r[0] for r in c.execute("SELECT message FROM messages WHERE room_key=? AND sent_at>=? AND sent_at<?",
                                            (key, m["t"] - 120, m["t"] + 180))]
            if any(similar(m["text"], x) >= 0.7 for x in near):
                continue        # 이미 저장(같은 말풍선을 다시 읽음)
            if len(_norm(m["text"])) >= 8 and any(similar(m["text"], x) >= 0.85 for x in history):
                continue        # 날짜 구분선이 안 보여 날짜를 잘못 짐작한 옛 대화
            author = f"export:{m['sender']}"
            if not c.execute("SELECT 1 FROM people WHERE author_hash=?", (author,)).fetchone():
                author = f"live:{m['sender']}"
                c.execute("INSERT INTO people VALUES (?,?,'live') ON CONFLICT(author_hash) DO NOTHING", (author, m["sender"]))
            seq = c.execute("SELECT COUNT(*) FROM messages WHERE room_key=? AND sent_at>=? AND sent_at<?",
                            (key, m["t"], m["t"] + 60)).fetchone()[0]
            mk = "live:" + hashlib.sha1(f"{key}|{m['t']}|{m['sender']}|{_norm(m['text'])}".encode("utf-8")).hexdigest()
            added += c.execute("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (mk, key, title, SOURCE, author, m["t"] + min(seq, 59), 1, m["text"], m["attach"], 0, now)).rowcount
        c.commit()
        return added


def watch_titles(cfg: dict) -> list[str]:
    from sync_conversations import vendor_room_titles
    con = db_open(Path(cfg["data_dir"]))
    try:
        return vendor_room_titles(con)    # 수집 중인 업무방(개인 대화로 제외된 방 빼고)
    finally:
        con.close()


class LiveCapture:
    """상주 앱이 쓰는 실행기: live_ocr.ps1 을 띄우고 결과를 받아 저장. paused 동안(쉬는 시간 내보내기 중)은 버린다."""

    def __init__(self, cfg: dict, log):
        self.cfg, self.log = cfg, log
        self.proc: subprocess.Popen | None = None
        self.paused = False
        self.watch_file = Path(cfg["data_dir"]) / "live_watch.txt"

    def refresh_watch(self):
        titles = watch_titles(self.cfg)
        self.watch_file.write_text("\n".join(titles) + "\n", encoding="utf-8")
        return len(titles)

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.running():
            return
        n = self.refresh_watch()
        import os
        self.proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(WORKER),
             "-WatchFile", str(self.watch_file), "-IntervalSec", "3", "-ParentPid", str(os.getpid())],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._errors, args=(self.proc,), daemon=True).start()
        self.log(f"사용 중 화면 인식 시작 — 업무방 {n}곳 중 열려 있는 창만 읽음(화면을 뺏지 않음)")

    def stop(self):
        if self.running():
            self.proc.terminate()
            self.log("사용 중 화면 인식 중지")
        self.proc = None

    def _read(self, proc):
        store = Store(self.cfg)
        try:
            for raw in proc.stdout:
                if self.paused:
                    continue
                try:
                    fr = json.loads(raw.decode("utf-8", errors="replace"))
                    n = store.ingest(fr)
                    if n:
                        self.log(f"화면 인식: '{fr['title']}' 새 메시지 {n}건(임시 — 쉬는 시간에 원문으로 바뀜)")
                except Exception as e:
                    self.log(f"화면 인식 처리 오류: {type(e).__name__}: {e}")
        finally:
            store.close()

    def _errors(self, proc):
        last = 0.0
        for raw in proc.stderr:
            if time.time() - last > 600:     # 같은 오류가 3초마다 쌓이지 않게 10분에 한 번만
                last = time.time()
                self.log("화면 인식 작업 경고: " + raw.decode("utf-8", errors="replace").strip()[:200])


if __name__ == "__main__":   # 시험: python -X utf8 live_capture.py frames.jsonl  (저장 없이 해석 결과만 출력)
    import sys
    from collector import load_config
    cfg = load_config()
    st = Store(cfg)
    for line in Path(sys.argv[1]).read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            fr = json.loads(line)
            key = st.room_key(fr["title"])
            print(fr["title"], "→", key)
            for m in parse_frame(fr, st.names(key) if key else [], st.me()):
                print(" ", datetime.fromtimestamp(m["t"], KST).strftime("%m-%d %H:%M"), m["sender"], "|", m["text"][:60].replace("\n", " / "))
