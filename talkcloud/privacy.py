"""방을 '개인 대화로 수집 제외'할 때의 공용 처리 — MCP(decide_privacy)와 상주 앱(방 목록 탭)이 같이 쓴다."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


def exclude_room(con: sqlite3.Connection, data_dir: Path, rid: str, title: str | None, reason: str) -> dict:
    """수집 제외 목록에 올리고, 이미 모은 그 방의 대화·파일 목록·받은 원본·내보낸 txt 를 지우고 업무방 등록도 푼다.
    이후 내보내기·톡클라우드 어느 경로로도 다시 수집하지 않는다(collector.excluded_rooms 가 막음)."""
    con.execute("INSERT INTO privacy_rooms VALUES (?,?,'excluded',?,?) ON CONFLICT(room_key) DO UPDATE SET "
                "decision='excluded', title=excluded.title, reason=excluded.reason, decided_at=excluded.decided_at",
                (rid, title, reason, datetime.now().isoformat(timespec="seconds")))
    removed_files = 0
    own = (Path(data_dir) / "files").resolve()
    for (p,) in con.execute("SELECT local_path FROM items WHERE room_id=? AND local_path IS NOT NULL", (rid,)).fetchall():
        f = Path(p)
        # 이 data 폴더 안 파일만 지운다(DB 사본으로 시험할 때 원본 파일을 지운 사고 방지 — 2026-11-23 실제로 발생)
        if f.exists() and f.resolve().is_relative_to(own):
            f.unlink()
            removed_files += 1
    removed_txt = 0
    exports = Path(data_dir) / "exports"
    if title and exports.exists():
        for t in exports.glob("*.txt"):
            with t.open(encoding="utf-8-sig") as fh:
                first = fh.readline().strip()
            if first == f"{title} 님과 카카오톡 대화":
                t.unlink()
                removed_txt += 1
    return {"room": title,
            "messages_deleted": con.execute("DELETE FROM messages WHERE room_key=? OR room_title=?", (rid, title)).rowcount,
            "file_list_deleted": con.execute("DELETE FROM items WHERE room_id=?", (rid,)).rowcount,
            "downloaded_files_deleted": removed_files, "exported_txt_deleted": removed_txt,
            "vendor_unregistered": bool(con.execute("DELETE FROM vendor_rooms WHERE room_id=?", (rid,)).rowcount)}
