"""업무 자료 폴더를 클라우드 동기화되지 않는 로컬 폴더로 옮긴다(2026-11-23 사용자 요구: 업무 기록은 이 PC 에만).

  python -X utf8 talkcloud/move_data_local.py          # 확인만(무엇을 어디로 옮길지)
  python -X utf8 talkcloud/move_data_local.py --apply  # 실제 이동 + config.json 의 data_dir 변경

옮기기 전에 수집기·상주 앱(카카오톡 연동 AI Assistant)·MCP 서버를 끈다(DB 가 열려 있으면 이동 실패).
옮긴 뒤 OneDrive 웹의 '휴지통'과 '버전 기록'에 남은 사본은 사용자가 비워야 완전히 지워진다.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

from collector import CONFIG, LOCAL_DATA, cloud_sync_warning, load_config

# DB 안의 원본 파일 경로(items.local_path)도 새 위치로 바꾼다


def move_item(src: Path, dst: Path):
    """이름 바꾸기가 안 되면(OneDrive 가 폴더를 잡고 있음 — 실측) 복사 → 크기 확인 → 원본 파일 삭제. 잡힌 빈 폴더는 남긴다."""
    try:
        if not dst.exists():
            os.rename(src, dst)
            return
    except OSError:
        pass
    files = [src] if src.is_file() else [f for f in src.rglob("*") if f.is_file()]
    for f in files:
        t = dst if src.is_file() else dst / f.relative_to(src)
        t.parent.mkdir(parents=True, exist_ok=True)
        if not (t.exists() and t.stat().st_size == f.stat().st_size):
            shutil.copy2(f, t)
        if t.stat().st_size != f.stat().st_size:
            raise OSError(f"복사 확인 실패: {f}")
        f.unlink()
    if src.is_dir():
        for d in sorted((d for d in src.rglob("*") if d.is_dir()), key=lambda d: len(d.parts), reverse=True) + [src]:
            try:
                d.rmdir()
            except OSError:
                pass


def main():
    cfg = load_config()
    src, dst = Path(cfg["data_dir"]).resolve(), LOCAL_DATA.resolve()
    print("현재:", src, "| 동기화 폴더 경고:", bool(cloud_sync_warning(src)))
    print("이동할 곳:", dst)
    if src == dst:
        print("이미 로컬 폴더입니다.")
        return
    items = list(src.iterdir()) if src.exists() else []
    def same(a: Path, b: Path) -> bool:
        return a.stat().st_size == b.stat().st_size and \
            hashlib.sha256(a.read_bytes()).digest() == hashlib.sha256(b.read_bytes()).digest()
    # 이전에 끊긴 이동을 이어서 할 때: 이미 똑같이 복사된 파일은 괜찮다
    clash = [p.name for p in items if (dst / p.name).is_file() and not same(p, dst / p.name)]
    if clash:
        sys.exit(f"이동할 곳에 같은 이름 파일이 이미 있습니다: {clash} — 확인 후 다시 실행")
    print("옮길 항목:", [p.name for p in items])
    if "--apply" not in sys.argv:
        print("(확인만 했습니다. 옮기려면 --apply)")
        return
    dst.mkdir(parents=True, exist_ok=True)
    for p in items:
        move_item(p, dst / p.name)
    con = sqlite3.connect(dst / "talkcloud.db")
    n = con.execute("UPDATE items SET local_path = ? || substr(local_path, ?) WHERE local_path LIKE ?",
                    (str(dst), len(str(src)) + 1, str(src) + "%")).rowcount
    con.commit()
    con.close()
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    raw["data_dir"] = str(dst)
    CONFIG.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        src.rmdir()
    except OSError:
        pass
    print(f"완료: {len(items)}개 항목 이동, 파일 경로 {n}건 갱신, config.json data_dir → {dst}")


if __name__ == "__main__":
    main()
