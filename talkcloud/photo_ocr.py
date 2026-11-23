"""받은 사진에서 문서(세금계산서·견적서 등)로 보이는 것을 골라낸다 — Windows 내장 OCR, 이 PC 안에서만 처리.

사진으로 세금계산서·견적서를 보내는 경우가 있어 사진도 확인해야 하지만(2026-11-23 사용자 요청), 사진이 수천 장이라
Claude 가 다 볼 수는 없다. 그래서 OCR 로 글자를 읽어 문서 단어가 보이는 사진에만 items.ocr_hint 를 붙이고,
MCP list_unreviewed_items 는 사진 중 ocr_hint 가 있는 것만 올린다.

실행: python -X utf8 talkcloud/photo_ocr.py   (collector.py 도 다운로드 뒤 호출)
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "ocr_images.ps1"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff"}
# 문서 사진 판정 단어(공백 제거 후 비교). 강한 단어 하나 또는 약한 단어 셋 이상이면 '문서로 보임'
STRONG = ["세금계산서", "계산서", "공급가액", "공급받는자", "사업자등록번호", "견적서", "견적금액", "거래명세", "청구서",
          "영수증", "입금확인", "이체확인", "송금확인", "계약서", "발주서", "납품서", "인보이스", "invoice", "합계금액"]
WEAK = ["부가세", "세액", "단가", "수량", "공급자", "합계", "금액", "대표자", "상호", "품목", "vat", "원정", "총액", "결제", "₩"]


def doc_hint(text: str) -> str | None:
    t = re.sub(r"\s+", "", text or "").lower()
    strong = [w for w in STRONG if w.lower() in t]
    weak = [w for w in WEAK if w.lower() in t]
    if strong or len(weak) >= 3:
        return ", ".join(strong + weak)[:200]
    return None


def ensure_columns(con) -> None:
    cols = {r[1] for r in con.execute("PRAGMA table_info(items)")}
    for col, typ in (("ocr_text", "TEXT"), ("ocr_hint", "TEXT"), ("ocr_at", "TEXT")):
        if col not in cols:
            con.execute(f"ALTER TABLE items ADD COLUMN {col} {typ}")
    con.commit()


def pending(con, limit: int) -> list[tuple[str, str]]:
    rows = con.execute("SELECT item_id, local_path FROM items WHERE local_path IS NOT NULL AND ocr_at IS NULL "
                       "ORDER BY created_at DESC").fetchall()
    out = []
    for iid, p in rows:
        if Path(p).suffix.lower() in IMAGE_EXT and Path(p).exists():
            out.append((iid, p))
            if len(out) >= limit:
                break
    return out


def run(con, batch: int = 100, max_items: int = 5000, log=print) -> dict:
    """아직 OCR 안 한 사진을 batch 장씩 읽어 DB 에 기록. 반환: {done, doc_like, errors}."""
    ensure_columns(con)
    done = doc_like = errors = 0
    while done + errors < max_items:
        todo = pending(con, batch)
        if not todo:
            break
        with tempfile.TemporaryDirectory() as tmp:
            lst, out = Path(tmp) / "list.txt", Path(tmp) / "out.jsonl"
            lst.write_text("\n".join(p for _, p in todo), encoding="utf-8")
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
                            "-ListFile", str(lst), "-OutFile", str(out)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            results = {}
            if out.exists():
                for line in out.read_text(encoding="utf-8").splitlines():
                    try:
                        r = json.loads(line)
                        results[r["path"]] = r
                    except (ValueError, KeyError):
                        pass
        now = datetime.now().isoformat(timespec="seconds")
        for iid, p in todo:
            r = results.get(p)
            if r is None or "error" in r:
                errors += 1
                text, hint = f"(OCR 실패: {(r or {}).get('error', '결과 없음')})"[:300], None
            else:
                done += 1
                text, hint = (r.get("text") or "")[:4000], doc_hint(r.get("text"))
                doc_like += bool(hint)
            con.execute("UPDATE items SET ocr_text=?, ocr_hint=?, ocr_at=? WHERE item_id=?", (text, hint, now, iid))
        con.commit()
        log(f"사진 OCR {done + errors}장 처리(문서로 보임 {doc_like}, 실패 {errors})")
    return {"done": done, "doc_like": doc_like, "errors": errors}


if __name__ == "__main__":
    from collector import db_open, load_config
    c = db_open(Path(load_config()["data_dir"]))
    try:
        print(run(c))
    finally:
        c.close()
