"""거래처 방 대화 일괄 수집: PC 카카오톡 내보내기(kakao_exporter.ps1) → DB 반영(export_import).

설치 직후(거래처 방 등록 직후) 흐름에서 Claude 가 쓴다.
  python -X utf8 talkcloud/sync_conversations.py --estimate   # 방 수·예상 시간만 (JSON)
  python -X utf8 talkcloud/sync_conversations.py              # 실행: 사용자는 그동안 PC 를 쓰지 않고 기다린다
  python -X utf8 talkcloud/sync_conversations.py --since-last # 지난 수집일 이후 대화가 있었던 방만 (쉬는 시간 수집, assistant_app 이 사용)

내보내기는 사용자가 마우스·키보드를 건드리면 그 방을 원상복구하고, 다시 입력이 멈추면 이어서 한다.
한 달 넘게 대화가 없는 방(skipped_inactive)과 안 읽은 메시지가 있는 방(skipped_unread)은 열지 않는다.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from collector import db_open, excluded_rooms, load_config, norm_title, prune_old_files
from export_import import import_exports, keep_cutoff_s

HERE = Path(__file__).resolve().parent
EXPORTER = HERE / "kakao_exporter.ps1"
SECONDS_PER_ROOM = 7   # 실측(2026-11-23): 닫힌 방 열기·내보내기·닫기 약 5초, 건너뛰는 방 약 2초 + 여유


def vendor_room_titles(con) -> list[str]:
    rows = con.execute("SELECT v.room_id, COALESCE(r.title, (SELECT room_title FROM messages m WHERE m.room_key=v.room_id LIMIT 1)) "
                       "FROM vendor_rooms v LEFT JOIN rooms r ON r.room_id=v.room_id ORDER BY v.vendor").fetchall()
    excluded_keys, excluded_titles = excluded_rooms(con)
    titles = []
    for rid, title in rows:
        title = title or (rid[len("title:"):] if rid.startswith("title:") else None)
        if rid in excluded_keys or norm_title(title) in excluded_titles:   # 개인 대화로 제외된 방은 열지도 않는다
            continue
        if title and title not in titles:
            titles.append(title)
    return titles


def estimate(n_rooms: int) -> dict:
    sec = n_rooms * SECONDS_PER_ROOM + 10
    return {"rooms": n_rooms, "estimated_seconds": sec, "estimated_minutes": max(1, math.ceil(sec / 60))}


def last_sync_date(con) -> str | None:
    row = con.execute("SELECT value FROM sync_state WHERE key='conversation_sync_at'").fetchone()
    return row[0][:10] if row else None


def never_exported_titles(con) -> set[str]:
    """한 번도 내보낸 적 없는 등록 방(새로 등록한 방) — 날짜 조건 없이 첫 수집을 받아야 한다."""
    done = {k[len("export_at:"):] for (k,) in con.execute("SELECT key FROM sync_state WHERE key LIKE 'export_at:%'")}
    titles = {}
    for rid, t in con.execute("SELECT v.room_id, COALESCE(r.title, (SELECT room_title FROM messages m WHERE m.room_key=v.room_id LIMIT 1)) "
                              "FROM vendor_rooms v LEFT JOIN rooms r ON r.room_id=v.room_id"):
        if rid not in done:
            titles[rid] = t or rid.removeprefix("title:")
    return set(titles.values())


def _export(cfg: dict, titles: list[str], active_since: str | None, idle_sec: int,
            skip_inactive_days: int | None = None) -> list[dict] | dict:
    """kakao_exporter.ps1 실행 → 방별 결과 목록(실패 시 {"ok": False, ...})."""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as f:
        f.write("\n".join(titles))
        rooms_file = f.name
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(EXPORTER),
            "-RoomsFile", rooms_file, "-IdleSec", str(idle_sec),
            "-MaxMinutes", str(max(10, estimate(len(titles))["estimated_minutes"] * 3))]
    if active_since:
        args += ["-ActiveSince", active_since]
    if skip_inactive_days is not None:   # 0 = 한 달 넘게 조용한 방도 연다(사용자가 직접 고른 방)
        args += ["-SkipInactiveDays", str(skip_inactive_days)]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        Path(rooms_file).unlink(missing_ok=True)
    last_run = Path(cfg["data_dir"]) / "exports" / "last_run.json"
    if proc.returncode != 0 or not last_run.exists():
        return {"ok": False, "error": (proc.stderr or proc.stdout).strip()[-2000:]}
    return json.loads(last_run.read_text(encoding="utf-8-sig"))["results"]


def run(cfg: dict, titles: list[str], active_since: str | None = None, idle_sec: int = 3,
        skip_inactive_days: int | None = None) -> dict:
    """active_since('YYYY-MM-DD')를 주면 마지막 대화가 그 날짜보다 앞선 방은 열지 않는다(skipped_no_new).
    단, 한 번도 내보낸 적 없는 방(새로 등록한 방)은 날짜와 상관없이 먼저 첫 수집을 한다.
    날짜 단위라 같은 날 다시 돌면 그날 대화가 있었던 방은 다시 내보낸다(새 메시지만 DB에 추가됨)."""
    est = estimate(len(titles))
    started = datetime.now().isoformat(timespec="seconds")
    batches = [(titles, active_since)]
    if active_since:
        con = db_open(Path(cfg["data_dir"]))
        try:
            fresh = never_exported_titles(con)
        finally:
            con.close()
        first = [t for t in titles if t in fresh]
        batches = [(first, None), ([t for t in titles if t not in fresh], active_since)]
    results = []
    for batch, since in batches:
        if not batch:
            continue
        got = _export(cfg, batch, since, idle_sec, skip_inactive_days)
        if isinstance(got, dict):
            return got
        results += got
        if any(r["status"] == "aborted" for r in got):   # 카카오톡이 멈췄거나 꺼졌으면 다음 묶음은 시작하지 않음
            break
    if results and all(r["status"] == "not_found" for r in results):
        # 모든 방을 못 찾았으면 개별 방 문제가 아니라 검색 자체가 실패한 것(2026-11-23 실측: 친구 탭 검색) — 수집일 갱신 안 함
        return {"ok": False, "error": "검색 결과를 읽지 못함(모든 방 not_found) — 카카오톡 메인 창 상태 확인 필요",
                "notes": [r.get("note") for r in results[:3]]}
    con = db_open(Path(cfg["data_dir"]))
    try:
        # 방별 최근 수집 결과(앱의 '방 목록' 탭에서 보여 줌)
        for r in results:
            con.execute("INSERT INTO sync_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (f"room_status:{norm_title(r['room'])}", json.dumps(
                            {"status": r["status"], "last_activity": r.get("last_activity"), "at": started}, ensure_ascii=False)))
        con.commit()
        imported = import_exports(con, cfg)
        prune_old_files(con, Path(cfg["data_dir"]), keep_cutoff_s(cfg) * 1000)   # 자동 삭제 등 보관 범위 밖 원본 정리
        # 중단·오류 없이 끝났을 때만 수집일을 갱신(아니면 다음 회차에 같은 기준으로 다시 본다)
        if not any(r["status"] in ("interrupted", "error", "timeout", "aborted") for r in results):
            con.execute("INSERT INTO sync_state VALUES ('conversation_sync_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (started,))
            con.commit()
    finally:
        con.close()
    added = {r["room"]: r["added"] for r in imported}
    by_status: dict[str, list] = {}
    for r in results:
        item = {"room": r["room"], "last_activity": r.get("last_activity")}
        if r["status"] == "exported":
            item["new_messages"] = added.get(r["room"], 0)
        elif r["status"] in ("error", "interrupted", "not_found", "aborted"):
            item["note"] = r.get("note")
        by_status.setdefault(r["status"], []).append(item)
    return {"ok": True, "estimate": est, "seconds": round(sum(r.get("seconds") or 0 for r in results), 1), "by_status": by_status}


def main():
    cfg = load_config()
    con = db_open(Path(cfg["data_dir"]))
    try:
        titles = vendor_room_titles(con)
    finally:
        con.close()
    if not titles:
        print(json.dumps({"ok": False, "error": "등록된 거래처 방이 없음 — 먼저 register_vendor_rooms"}, ensure_ascii=False))
        return
    if "--estimate" in sys.argv:
        print(json.dumps(dict(estimate(len(titles)), room_titles=titles), ensure_ascii=False, indent=1))
        return
    since = None
    if "--since-last" in sys.argv:
        con = db_open(Path(cfg["data_dir"]))
        try:
            since = last_sync_date(con)
        finally:
            con.close()
    print(json.dumps(run(cfg, titles, active_since=since), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
