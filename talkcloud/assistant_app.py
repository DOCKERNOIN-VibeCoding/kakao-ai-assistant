"""카카오톡 연동 AI Assistant — 설정 창 + 트레이 상주.

PC 를 쉬는 시간(설정한 분 이상 마우스·키보드 입력 없음)에, 지난 수집 이후 대화가 있었던 거래처 방만
PC 카카오톡 '대화 내보내기'로 받아 DB 에 반영한다(sync_conversations). 한 시간에 최대 한 번.
창을 닫으면 알림 영역(트레이)에서 계속 실행되고, 트레이 아이콘 → 종료로 끝낸다.
--background : PC 로그인 시 자동 실행용(창 없이 트레이로 시작)
"""
import ctypes, json, os, queue, shutil, subprocess, sys, threading, winreg
from datetime import datetime, timedelta
from pathlib import Path

if sys.stdout is None:  # pythonw 에는 콘솔이 없어 라이브러리 출력이 실패하지 않게 막는다
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")

import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog, ttk

from collector import CONFIG, cloud_sync_warning, db_open, load_config, norm_title

# 업무 자료는 이 PC 에만 둔다(2026-11-23 사용자 요구). 비서 업무는 대화 기록이 이 PC 에만 남는 Claude Code·Codex 에서만 쓰고,
# 대화 기록이 계정에 저장되는 데스크톱 채팅 앱(Claude 데스크톱·ChatGPT 데스크톱)에는 연결하지 않는다.
import updater
from live_capture import LiveCapture
from privacy import exclude_room
from sync_conversations import last_sync_date, run as sync_run, vendor_room_titles

APP_NAME = "카카오톡 연동 AI Assistant"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
CHECK_SECONDS = 60
MIN_INTERVAL = timedelta(hours=1)   # 쉬는 시간 수집은 한 시간에 최대 한 번


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds() -> float:
    li = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li))
    return ((ctypes.windll.kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000.0


def autostart_command() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw}" -X utf8 "{Path(__file__).resolve()}" --background'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on: bool):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


def save_settings(**values):
    raw = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    raw.update(values)
    CONFIG.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


def collect_months_set() -> bool:
    """수집 기간(대화·파일·사진 공통)을 사용자가 정한 적이 있는지(없으면 처음 실행 때 물어본다)."""
    try:
        return "collect_months" in json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False


MCP_NAME = "kakao-talkcloud"
MCP_SERVER = Path(__file__).resolve().with_name("mcp_server.py")


def mcp_spec() -> dict:
    python = Path(sys.executable).with_name("python.exe")   # 앱은 pythonw 로 돌지만 MCP 서버는 콘솔 python(표준입출력) 필요
    return {"command": str(python), "args": ["-X", "utf8", str(MCP_SERVER)]}


def desktop_config_path() -> Path:
    """Claude 데스크톱 앱 설정 파일. Microsoft Store 판은 패키지 전용 폴더에 저장된다."""
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    for pkg in sorted(local.glob("Packages/Claude_*")):
        roaming = pkg / "LocalCache" / "Roaming" / "Claude"
        if roaming.is_dir():
            return roaming / "claude_desktop_config.json"
    return Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json"


def remove_from_desktop() -> str | None:
    """Claude 데스크톱 앱에 이 비서가 등록돼 있으면 빼낸다(대화 기록이 계정에 저장되므로 연결하지 않음 — 2026-11-23 사용자 결정)."""
    path = desktop_config_path()
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    cfg = json.loads(raw) if raw.strip() else {}
    if MCP_NAME not in cfg.get("mcpServers", {}):
        return None
    path.with_name(path.name + f".bak_{datetime.now():%Y%m%d_%H%M%S}").write_text(raw, encoding="utf-8")
    del cfg["mcpServers"][MCP_NAME]
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return "Claude 데스크톱 앱: 예전에 등록된 연결을 해제했습니다(데스크톱 앱은 대화 기록이 계정에 저장되어 쓰지 않음)."


def chatgpt_desktop_installed() -> bool:
    """ChatGPT 데스크톱 앱은 Codex 와 같은 config.toml 을 읽는다 — 설치돼 있으면 Codex 등록이 그 앱에도 적용된다."""
    return any(Path(os.environ.get("LOCALAPPDATA", "")).glob("Packages/OpenAI.ChatGPT*"))


def connect_claude_code() -> str:
    exe = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
    if not Path(exe).exists():
        return "Claude Code: 설치되어 있지 않아 건너뜀."
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run([exe, "mcp", "remove", MCP_NAME, "-s", "user"], capture_output=True, text=True, creationflags=flags)
    spec = mcp_spec()
    r = subprocess.run([exe, "mcp", "add", "-s", "user", MCP_NAME, "--", spec["command"], *spec["args"]],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=flags)
    if r.returncode != 0:
        return f"Claude Code: 등록 실패 — {(r.stderr or r.stdout).strip()[:300]}"
    return "Claude Code: 등록 완료(사용자 범위 — 어느 폴더에서 열어도 사용) — 열려 있는 Claude Code 는 다시 시작하세요."


def codex_config_path() -> Path:
    """ChatGPT 데스크톱 앱(MCP 서버 설정)과 Codex 앱·CLI 가 함께 쓰는 설정 파일."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"


def with_codex_server(text: str) -> str:
    """config.toml 텍스트에서 [mcp_servers.kakao-talkcloud](와 하위 표)만 교체하고 나머지는 그대로 둔다."""
    head = f"[mcp_servers.{MCP_NAME}"
    out, skipping = [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            skipping = s.startswith(head + "]") or s.startswith(head + ".")
        if not skipping:
            out.append(line)
    spec = mcp_spec()   # TOML 기본 문자열은 JSON 문자열 표기와 호환(역슬래시 이스케이프)
    block = [f"[mcp_servers.{MCP_NAME}]",
             f"command = {json.dumps(spec['command'], ensure_ascii=False)}",
             "args = [" + ", ".join(json.dumps(a, ensure_ascii=False) for a in spec["args"]) + "]"]
    body = "\n".join(out).rstrip()
    return (body + "\n\n" if body else "") + "\n".join(block) + "\n"


def connect_codex() -> str:
    import tomllib
    path = codex_config_path()
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    new = with_codex_server(raw)
    servers = tomllib.loads(new).get("mcp_servers", {})   # 쓰기 전에 문법·결과 검증
    if servers.get(MCP_NAME, {}).get("command") != mcp_spec()["command"]:
        raise ValueError("config.toml 갱신 결과 검증 실패 — 파일을 바꾸지 않음")
    if raw:
        path.with_name(path.name + f".bak_{datetime.now():%Y%m%d_%H%M%S}").write_text(raw, encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    return f"Codex: 등록 완료 ({path}) — 열려 있는 Codex 는 완전히 종료한 뒤 다시 실행하세요."


STATUS_NAMES = {"exported": "수집", "skipped_no_new": "새 대화 없음", "skipped_inactive": "한 달 넘게 조용함",
                "skipped_unread": "안 읽은 메시지라 보류", "not_found": "방 못 찾음(이름 바뀜?)", "interrupted": "사용자 입력으로 중단",
                "error": "오류", "timeout": "시간 초과", "aborted": "카카오톡 응답 없음·종료로 중단"}


def room_lists(cfg: dict) -> tuple[list[tuple], list[tuple]]:
    """(수집 중, 수집 안 함) 방 목록. 수집 중 = 거래처로 등록된 방(개인 대화 제외·한 달 넘게 조용한 방 빼고)."""
    def day(ts):
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
    con = db_open(Path(cfg["data_dir"]))
    try:
        titles = {rid: t for rid, t in con.execute("SELECT room_id, title FROM rooms")}
        for k, t in con.execute("SELECT room_key, MAX(room_title) FROM messages GROUP BY room_key"):
            titles.setdefault(k, t)
        state = dict(con.execute("SELECT key, value FROM sync_state WHERE key LIKE 'room_status:%' OR key LIKE 'export_at:%'"))
        last_msg = dict(con.execute("SELECT room_key, MAX(sent_at) FROM messages GROUP BY room_key"))
        last_item = {k: v // 1000 for k, v in con.execute("SELECT room_id, MAX(created_at) FROM items GROUP BY room_id")}
        excluded = {k: (t, d) for k, t, d in con.execute("SELECT room_key, title, decided_at FROM privacy_rooms WHERE decision='excluded'")}
        vendors = dict(con.execute("SELECT room_id, vendor FROM vendor_rooms"))
    finally:
        con.close()
    collecting, idle = [], []
    for rid, vendor in vendors.items():
        if rid in excluded:
            continue
        title = titles.get(rid) or rid.removeprefix("title:")
        st = json.loads(state.get(f"room_status:{norm_title(title)}", "null") or "null") or {}
        last = st.get("last_activity") or day(max(last_msg.get(rid) or 0, last_item.get(rid) or 0))
        if st.get("status") == "skipped_inactive":
            idle.append((rid, title, "한 달 넘게 대화 없음 — 대화가 생기면 자동으로 다시 수집", last))
        else:
            collecting.append((rid, title, vendor, last, (state.get(f"export_at:{rid}") or "")[:16].replace("T", " "),
                               STATUS_NAMES.get(st.get("status"), "아직 수집 전")))
    for rid, (title, decided) in excluded.items():
        idle.append((rid, title or titles.get(rid, rid), "개인 대화로 제외 — 수집 안 함, 모았던 자료 삭제됨", (decided or "")[:10]))
    others = []
    for rid, title in titles.items():
        if rid in vendors or rid in excluded or not title:
            continue
        reason = "업무방으로 등록 안 됨" + (" (예전에 모은 대화 있음)" if rid in last_msg else "")
        others.append((rid, title, reason, day(max(last_msg.get(rid) or 0, last_item.get(rid) or 0))))
    others.sort(key=lambda r: r[3], reverse=True)
    # 각 줄의 첫 값은 room_id(화면에는 안 보임 — 선택한 방을 추가·수집할 때 씀)
    return sorted(collecting, key=lambda r: r[3], reverse=True), idle + others


def add_work_rooms(cfg: dict, rooms: list[tuple[str, str]]) -> None:
    """방 목록 탭에서 사용자가 고른 방을 업무방으로 등록. 업체 정보는 나중에 Claude Code 로 정리(업체명 = 방 이름).
    사용자가 직접 업무방이라고 고른 것이므로 개인 대화 점검은 '수집 유지'로 기록한다."""
    now = datetime.now().isoformat(timespec="seconds")
    con = db_open(Path(cfg["data_dir"]))
    try:
        for rid, title in rooms:
            con.execute("INSERT OR IGNORE INTO vendor_rooms VALUES (?,?,?,?,?)",
                        (rid, title, "앱에서 추가(업체 정보 미정)", None, None))
            con.execute("INSERT INTO privacy_rooms VALUES (?,?,'kept',?,?) ON CONFLICT(room_key) DO UPDATE SET "
                        "decision='kept', reason=excluded.reason, decided_at=excluded.decided_at",
                        (rid, title, "사용자가 앱 방 목록에서 업무방으로 추가", now))
        con.commit()
    finally:
        con.close()


def summarize(res: dict) -> str:
    if not res.get("ok"):
        return f"수집 실패: {res.get('error')}"
    names = STATUS_NAMES
    parts = [f"{names.get(k, k)} {len(v)}" for k, v in res["by_status"].items()]
    new = [f"{r['room']}(+{r['new_messages']})" for r in res["by_status"].get("exported", []) if r.get("new_messages")]
    return " · ".join(parts) + (f" — 새 메시지: {', '.join(new)}" if new else "")


class App:
    def __init__(self, background: bool):
        self.msgs: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.run_now = threading.Event()
        self.job: list[str] | None = None   # 방 목록에서 고른 방만 수집할 때(없으면 전체)
        self.running = False
        self.cfg = load_config()
        self.log_file = Path(self.cfg["data_dir"]) / "assistant.log"
        con = db_open(Path(self.cfg["data_dir"]))
        try:
            last = con.execute("SELECT value FROM sync_state WHERE key='conversation_sync_at'").fetchone()
        finally:
            con.close()
        self.last_attempt = datetime.fromisoformat(last[0]) if last else None

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("760x600")
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)
        self._build()
        if background:
            self.root.withdraw()
        if warn := cloud_sync_warning(self.cfg["data_dir"]):
            self.log("경고: " + warn.replace("\n", " "))
            if not background:
                messagebox.showwarning(APP_NAME, warn)
        if not collect_months_set():
            if background:
                self.root.deiconify()   # 설정을 물어야 하므로 창을 보여 준다
            self.root.after(500, self.ask_collect_months)
        self._start_tray()
        self.live = LiveCapture(self.cfg, self.log)
        self.root.after(2000, self._apply_live)
        self.root.after(15000, self.check_update)   # 시작 15초 뒤 새 버전 확인(있으면 설치 여부를 묻는다)
        threading.Thread(target=self._worker, daemon=True).start()
        self.root.after(300, self._drain)

    # ---------- 화면 ----------
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, **pad)
        main, rooms_tab = ttk.Frame(nb), ttk.Frame(nb)
        nb.add(main, text="설정·기록")
        nb.add(rooms_tab, text="방 목록")
        nb.bind("<<NotebookTabChanged>>", lambda e: nb.index("current") == 1 and self.refresh_rooms())
        self._build_rooms(rooms_tab, pad)
        box = ttk.LabelFrame(main, text="설정")
        box.pack(fill="x", **pad)
        self.idle_var = tk.IntVar(value=int(self.cfg.get("idle_minutes", 10)))
        self.auto_var = tk.BooleanVar(value=autostart_enabled())
        ttk.Label(box, text="쉬는 시간 기준: 마우스·키보드 입력이").grid(row=0, column=0, sticky="w", **pad)
        ttk.Spinbox(box, from_=1, to=120, width=5, textvariable=self.idle_var).grid(row=0, column=1, sticky="w")
        ttk.Label(box, text="분 이상 없으면 대화 수집 (기본 10분, 한 시간에 최대 한 번)").grid(row=0, column=2, sticky="w", **pad)
        self.months_var = tk.IntVar(value=int(self.cfg.get("collect_months", 3)))
        ttk.Label(box, text="처음 가져올 과거 범위: 최근").grid(row=1, column=0, sticky="w", **pad)
        ttk.Spinbox(box, from_=1, to=24, width=5, textvariable=self.months_var).grid(row=1, column=1, sticky="w")
        ttk.Label(box, text=f"개월 — 처음 수집일({self.cfg.get('collect_started', '오늘')}) 기준. 이후 대화·파일·사진은 계속 쌓임").grid(
            row=1, column=2, sticky="w", **pad)
        self.del_var = tk.BooleanVar(value=bool(self.cfg.get("auto_delete", False)))
        self.del_months_var = tk.IntVar(value=int(self.cfg.get("auto_delete_months", 12)))
        ttk.Checkbutton(box, text="오래된 기록 자동 삭제:", variable=self.del_var).grid(row=2, column=0, sticky="w", **pad)
        ttk.Spinbox(box, from_=3, to=120, width=5, textvariable=self.del_months_var).grid(row=2, column=1, sticky="w")
        ttk.Label(box, text="개월 지난 대화·파일·사진을 이 PC 에서 지움 (기본 꺼짐·12개월, 카카오톡 원본은 그대로)").grid(
            row=2, column=2, sticky="w", **pad)
        self.live_var = tk.BooleanVar(value=bool(self.cfg.get("live_ocr", True)))
        ttk.Checkbutton(box, text="사용 중 화면 인식:", variable=self.live_var).grid(row=3, column=0, sticky="w", **pad)
        ttk.Label(box, text="열어 둔 업무방 창을 화면을 뺏지 않고 읽어 임시 대화로 저장 (쉬는 시간에 원문으로 바뀜)").grid(
            row=3, column=1, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(box, text="PC 로그인 시 자동 실행", variable=self.auto_var).grid(row=4, column=0, columnspan=2, sticky="w", **pad)
        ttk.Button(box, text="저장", command=self.save).grid(row=4, column=2, sticky="e", **pad)
        ttk.Label(box, text="Claude Code 에서 거래처 대화·자료를 묻고 정리하려면 →").grid(row=5, column=0, columnspan=2, sticky="w", **pad)
        ttk.Button(box, text="Claude Code MCP 연결", command=self.connect_claude).grid(row=5, column=2, sticky="e", **pad)
        ttk.Label(box, text="Codex 에서 쓰려면 →").grid(row=6, column=0, columnspan=2, sticky="w", **pad)
        ttk.Button(box, text="Codex MCP 연결", command=self.connect_codex_btn).grid(row=6, column=2, sticky="e", **pad)
        ttk.Label(box, text=f"프로그램 버전: {updater.local_version() or '개발용'}").grid(row=8, column=0, columnspan=2, sticky="w", **pad)
        ttk.Button(box, text="업데이트 확인", command=lambda: self.check_update(manual=True)).grid(row=8, column=2, sticky="e", **pad)
        ttk.Label(box, text="데스크톱 채팅 앱(Claude·ChatGPT)은 대화 기록이 계정에 저장되어 연결하지 않습니다.",
                  foreground="gray").grid(row=7, column=0, columnspan=3, sticky="w", **pad)

        self.status = tk.StringVar()
        ttk.Label(main, textvariable=self.status, foreground="#1F4E79").pack(anchor="w", **pad)
        self._refresh_status()
        self.logbox = scrolledtext.ScrolledText(main, height=16, state="disabled")
        self.logbox.pack(fill="both", expand=True, **pad)
        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", **pad)
        ttk.Label(bottom, text="창을 닫아도 알림 영역(트레이)에서 계속 실행됩니다.", foreground="gray").pack(side="left")
        ttk.Button(bottom, text="종료", command=self.quit).pack(side="right")
        ttk.Button(bottom, text="전체 지금 수집", command=self.collect_now).pack(side="right", padx=6)

    def _build_rooms(self, tab, pad):
        def table(parent, cols, widths, height):
            frame = ttk.Frame(parent)
            frame.pack(fill="both", expand=True, **pad)
            tv = ttk.Treeview(frame, columns=cols, show="headings", height=height)
            for c, w in zip(cols, widths):
                tv.heading(c, text=c)
                tv.column(c, width=w, anchor="w", stretch=(c == cols[0] or c == "이유"))
            sb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
            tv.configure(yscrollcommand=sb.set)
            tv.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            return tv
        top = ttk.Frame(tab)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="방을 골라(Ctrl·Shift 로 여러 개) 아래 버튼을 누르세요. 업체 정보 정리·개인 대화 제외는 Claude Code 에 요청.",
                  foreground="gray").pack(side="left")
        ttk.Button(top, text="새로 고침", command=self.refresh_rooms).pack(side="right")
        self.on_label = tk.StringVar(value="수집 중")
        ttk.Label(tab, textvariable=self.on_label, font=("", 10, "bold")).pack(anchor="w", padx=8)
        self.on_tv = table(tab, ("방", "업체", "마지막 대화", "마지막 수집", "최근 결과"), (230, 110, 85, 110, 140), 7)
        on_btns = ttk.Frame(tab)
        on_btns.pack(fill="x", padx=8)
        ttk.Button(on_btns, text="선택한 방만 지금 수집", command=self.collect_selected).pack(side="right")
        ttk.Button(on_btns, text="선택한 방 수집 중단", command=self.stop_selected).pack(side="right", padx=6)
        self.off_label = tk.StringVar(value="수집 안 함")
        ttk.Label(tab, textvariable=self.off_label, font=("", 10, "bold")).pack(anchor="w", padx=8)
        self.off_tv = table(tab, ("방", "이유", "마지막 활동"), (230, 360, 85), 9)
        ttk.Button(tab, text="선택한 방을 업무방으로 추가하고 수집", command=self.add_selected).pack(anchor="e", padx=8, pady=(0, 6))

    def refresh_rooms(self):
        try:
            on, off = room_lists(self.cfg)
        except Exception as e:
            self.log(f"방 목록을 읽지 못함: {type(e).__name__}: {e}")
            return
        for tv, rows in ((self.on_tv, on), (self.off_tv, off)):
            tv.delete(*tv.get_children())
            for r in rows:
                if not tv.exists(r[0]):
                    tv.insert("", "end", iid=r[0], values=r[1:])
        excluded = sum(1 for r in off if r[2].startswith("개인 대화"))
        idle = sum(1 for r in off if r[2].startswith("한 달"))
        self.on_label.set(f"수집 중 — 거래처 방 {len(on)}곳")
        self.off_label.set(f"수집 안 함 — {len(off)}곳 (개인 대화 제외 {excluded}, 한 달 넘게 조용함 {idle}, 등록 안 된 방 {len(off) - excluded - idle})")

    def _refresh_status(self):
        last = self.last_attempt.strftime("%m-%d %H:%M") if self.last_attempt else "없음"
        state = "수집 중…" if self.running else f"대기 중 — 입력이 {self.idle_var.get()}분 이상 없으면 수집"
        self.status.set(f"{state} (마지막 수집 시도: {last})")

    def save(self):
        try:
            minutes, months = int(self.idle_var.get()), int(self.months_var.get())
            del_on, del_months = bool(self.del_var.get()), int(self.del_months_var.get())
            if not 1 <= minutes <= 120 or not 1 <= months <= 24 or not 3 <= del_months <= 120:
                raise ValueError
        except (ValueError, tk.TclError):
            messagebox.showwarning(APP_NAME, "쉬는 시간 기준은 1~120분, 수집 범위는 1~24개월, 자동 삭제는 3~120개월 사이로 입력하세요.")
            return
        newly_on = del_on and (not self.cfg.get("auto_delete") or del_months < int(self.cfg.get("auto_delete_months", 12)))
        if newly_on and not messagebox.askokcancel(
                APP_NAME, f"오래된 기록 자동 삭제를 켭니다.\n\n수집할 때마다 {del_months}개월이 지난 대화·받은 파일·사진·대화 원문이 "
                          "이 PC 에서 지워지며 되돌릴 수 없습니다.\n(카카오톡·톡클라우드의 원본과 Claude 가 만든 업무 메모·문서 기록은 그대로입니다)"):
            return
        live_on = bool(self.live_var.get())
        try:
            save_settings(idle_minutes=minutes, collect_months=months, auto_delete=del_on, auto_delete_months=del_months,
                          live_ocr=live_on)
            set_autostart(self.auto_var.get())
        except OSError as e:
            messagebox.showerror(APP_NAME, f"설정 저장 실패: {e}")
            return
        self.cfg.update(idle_minutes=minutes, collect_months=months, auto_delete=del_on, auto_delete_months=del_months,
                        live_ocr=live_on)
        self._refresh_status()
        self.log(f"설정 저장 — 쉬는 시간 기준 {minutes}분, 처음 가져올 과거 범위 {months}개월(이후 계속 쌓임), "
                 f"오래된 기록 자동 삭제 {f'켬({del_months}개월)' if del_on else '끔'}, 사용 중 화면 인식 {'켬' if live_on else '끔'}, "
                 f"자동 실행 {'켬' if self.auto_var.get() else '끔'}")
        self._apply_live()

    def _apply_live(self):
        """설정에 따라 사용 중 화면 인식을 켜고 끈다. 켜져 있으면 읽을 업무방 목록도 새로 쓴다(방 추가·중단 반영)."""
        try:
            if self.cfg.get("live_ocr", True):
                self.live.refresh_watch() if self.live.running() else self.live.start()
            else:
                self.live.stop()
        except Exception as e:
            self.log(f"사용 중 화면 인식 설정 실패: {type(e).__name__}: {e}")

    def ask_collect_months(self):
        """처음 실행 때: 최근 몇 개월의 대화·파일·사진까지 모을지 묻는다(대화와 파일은 같은 기간)."""
        months = simpledialog.askinteger(
            APP_NAME, "처음 수집할 때 업무방의 대화와 파일·사진을 과거 몇 개월치까지 가져올까요?\n"
                      "(대화와 파일은 같은 범위로 가져오고, 그 뒤로 새로 오는 것은 계속 쌓입니다. 나중에 설정 창에서 바꿀 수 있습니다)",
            initialvalue=3, minvalue=1, maxvalue=24, parent=self.root)
        months = months or 3
        started = self.cfg.get("collect_started") or datetime.now().strftime("%Y-%m-%d")   # 처음 수집일 = 기준일
        save_settings(collect_months=months, collect_started=started)
        self.cfg["collect_started"] = started
        self.cfg["collect_months"] = months
        self.months_var.set(months)
        self.log(f"처음 가져올 과거 범위: {months}개월 (기준일 {started}) — 이후 계속 쌓임")

    def connect_claude(self):
        if not messagebox.askokcancel(APP_NAME, "카카오톡 연동 AI Assistant 를 Claude Code 에 연결합니다(사용자 범위 — 어느 폴더에서 열어도 사용).\n"
                                                "Claude Code 의 대화 기록은 이 PC 에만 저장됩니다.\n"
                                                "Claude 데스크톱 앱에는 연결하지 않으며, 예전에 등록된 것이 있으면 해제합니다."):
            return
        lines = []
        for fn in (connect_claude_code, remove_from_desktop):
            try:
                if msg := fn():
                    lines.append(msg)
            except Exception as e:
                lines.append(f"{fn.__name__}: 실패 — {type(e).__name__}: {e}")
        for l in lines:
            self.log(l)
        messagebox.showinfo(APP_NAME, "\n\n".join(lines))

    def connect_codex_btn(self):
        text = ("카카오톡 연동 AI Assistant 를 Codex 에 연결합니다.\n"
                "(설정 파일 ~/.codex/config.toml 을 백업한 뒤 이 항목만 추가합니다)")
        if chatgpt_desktop_installed():
            text += ("\n\n주의: 이 PC 에 ChatGPT 데스크톱 앱이 설치돼 있습니다. 그 앱도 같은 설정 파일을 읽어 이 비서에 연결되고,\n"
                     "그 앱의 대화 기록은 계정에 저장됩니다. 계정을 다른 사람과 같이 쓰면 업무 요약이 보일 수 있습니다.")
        if not messagebox.askokcancel(APP_NAME, text):
            return
        try:
            msg = connect_codex()
        except Exception as e:
            msg = f"Codex 연결 실패 — {type(e).__name__}: {e}"
        self.log(msg)
        messagebox.showinfo(APP_NAME, msg)

    def collect_now(self):
        if self.running:
            messagebox.showinfo(APP_NAME, "이미 수집 중입니다. 끝난 뒤 다시 눌러 주세요.")
            return
        if messagebox.askokcancel(APP_NAME, "등록된 업무방 전체를 지금 확인합니다(지난 수집 이후 대화가 있는 방만 엽니다).\n"
                                            "몇 분 동안 마우스·키보드를 쓰지 말고 기다려 주세요."):
            self.job = None
            self.run_now.set()

    def _start_job(self, titles: list[str], what: str):
        if self.running:
            messagebox.showinfo(APP_NAME, "이미 수집 중입니다. 끝난 뒤 다시 눌러 주세요.")
            return
        names = "\n".join(f"· {t}" for t in titles[:12]) + (f"\n… 외 {len(titles) - 12}곳" if len(titles) > 12 else "")
        if not messagebox.askokcancel(APP_NAME, f"{what}\n\n{names}\n\n"
                                                f"약 {max(1, round(len(titles) * 7 / 60))}분 동안 마우스·키보드를 쓰지 말고 기다려 주세요."):
            return False
        self.job = titles
        self.run_now.set()
        return True

    def collect_selected(self):
        sel = self.on_tv.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "'수집 중' 목록에서 수집할 방을 먼저 고르세요.")
            return
        self._start_job([self.on_tv.item(i, "values")[0] for i in sel], f"선택한 {len(sel)}곳만 지금 수집합니다.")

    def stop_selected(self):
        sel = self.on_tv.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "'수집 중' 목록에서 수집을 멈출 방을 먼저 고르세요.")
            return
        if self.running:
            messagebox.showinfo(APP_NAME, "수집 중에는 바꿀 수 없습니다. 끝난 뒤 다시 눌러 주세요.")
            return
        rooms = [(rid, self.on_tv.item(rid, "values")[0]) for rid in sel]
        names = "\n".join(f"· {t}" for _, t in rooms[:12]) + (f"\n… 외 {len(rooms) - 12}곳" if len(rooms) > 12 else "")
        ans = messagebox.askyesnocancel(APP_NAME, f"아래 {len(rooms)}곳의 수집을 멈춥니다.\n\n{names}\n\n"
                                                  "이미 모은 대화·파일도 지울까요?\n"
                                                  "  예 — 개인 대화로 제외: 모은 자료를 지우고, 앞으로 어떤 경로로도 다시 모으지 않음\n"
                                                  "  아니요 — 수집만 중단: 모은 자료는 남겨 검색에 계속 사용\n"
                                                  "  취소 — 그대로 둠")
        if ans is None:
            return
        try:
            con = db_open(Path(self.cfg["data_dir"]))
            try:
                for rid, title in rooms:
                    if ans:
                        r = exclude_room(con, Path(self.cfg["data_dir"]), rid, title, "사용자가 앱 방 목록에서 제외(자료 삭제)")
                        self.log(f"수집 중단·자료 삭제: {title} — 대화 {r['messages_deleted']}건, 파일 {r['downloaded_files_deleted']}개 삭제")
                    else:
                        con.execute("DELETE FROM vendor_rooms WHERE room_id=?", (rid,))
                        self.log(f"수집 중단(자료 유지): {title}")
                con.commit()
            finally:
                con.close()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"처리 실패: {e}")
        self.refresh_rooms()
        self._apply_live()

    def add_selected(self):
        sel = self.off_tv.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "'수집 안 함' 목록에서 업무방으로 넣을 방을 먼저 고르세요.")
            return
        rows = [(rid, self.off_tv.item(rid, "values")) for rid in sel]
        private = [v[0] for _, v in rows if str(v[1]).startswith("개인 대화")]
        if private and not messagebox.askyesno(APP_NAME, "아래 방은 개인 대화로 제외했던 방입니다. 다시 업무방으로 수집할까요?\n\n"
                                                         + "\n".join(f"· {t}" for t in private)):
            rows = [(rid, v) for rid, v in rows if v[0] not in private]
            if not rows:
                return
        titles = [v[0] for _, v in rows]
        if self.running:
            messagebox.showinfo(APP_NAME, "이미 수집 중입니다. 끝난 뒤 다시 눌러 주세요.")
            return
        try:
            add_work_rooms(self.cfg, [(rid, v[0]) for rid, v in rows])
        except Exception as e:
            messagebox.showerror(APP_NAME, f"업무방 등록 실패: {e}")
            return
        self.log(f"업무방으로 추가 {len(rows)}곳: {', '.join(titles)} (업체 정보는 Claude Code 에서 정리)")
        self.refresh_rooms()
        self._apply_live()
        self._start_job(titles, f"{len(titles)}곳을 업무방으로 추가했습니다. 이 방들만 지금 수집합니다.")

    # ---------- 쉬는 시간 감시 ----------
    def _worker(self):
        while not self.stop.is_set():
            manual = self.run_now.wait(CHECK_SECONDS)   # '지금 수집'이면 바로 깨어남
            self.run_now.clear()
            if self.stop.is_set():
                break
            due = self.last_attempt is None or datetime.now() - self.last_attempt >= MIN_INTERVAL
            if manual or (idle_seconds() / 60 >= int(self.cfg.get("idle_minutes", 10)) and due):
                job, self.job = self.job, None
                self._collect(manual, job)

    def _collect(self, manual: bool, job: list[str] | None = None):
        self.running = True
        self.live.paused = True   # 내보내기가 여는 방 창은 화면 인식하지 않는다(곧 원문이 들어옴)
        self.msgs.put(("status", None))
        try:
            if job:   # 방 목록에서 고른 방만 — 한 달 넘게 조용한 방도 연다
                self.log(f"선택한 방 수집 시작 — {len(job)}곳")
                self.log(summarize(sync_run(self.cfg, job, active_since=None, idle_sec=3, skip_inactive_days=0)))
                return
            self.last_attempt = datetime.now()
            con = db_open(Path(self.cfg["data_dir"]))
            try:
                titles, since = vendor_room_titles(con), last_sync_date(con)
            finally:
                con.close()
            if not titles:
                self.log("등록된 거래처 방이 없습니다 — Claude 에게 거래처 방 등록을 먼저 요청하세요.")
                return
            self.log(f"{'수동' if manual else '쉬는 시간'} 수집 시작 — 거래처 방 {len(titles)}곳"
                     + (f", {since} 이후 대화가 있었던 방만" if since else ""))
            self.log(summarize(sync_run(self.cfg, titles, active_since=since, idle_sec=3 if manual else 5)))
        except Exception as e:
            self.log(f"오류: {type(e).__name__}: {e}")
        finally:
            self.running = False
            self.live.paused = False
            self.msgs.put(("status", None))
            self.msgs.put(("rooms", None))

    def log(self, msg: str):
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        self.msgs.put(("log", line))

    def _drain(self):
        while not self.msgs.empty():
            kind, payload = self.msgs.get()
            if kind == "log":
                self.logbox.configure(state="normal")
                self.logbox.insert("end", payload + "\n")
                self.logbox.see("end")
                self.logbox.configure(state="disabled")
            elif kind == "status":
                self._refresh_status()
            elif kind == "rooms":
                self.refresh_rooms()
            elif kind == "show":
                self.root.deiconify()
                self.root.lift()
            elif kind == "update":
                self.offer_update(payload)
            elif kind == "info":
                messagebox.showinfo(APP_NAME, payload)
            elif kind == "collect":
                self.collect_now()
            elif kind == "quit":
                self.quit()
                return
        self.root.after(300, self._drain)

    # ---------- 트레이 ----------
    # ---------- 업데이트(GitHub 릴리스, 사용자 확인 후 설치) ----------
    def check_update(self, manual: bool = False):
        ok, why = updater.enabled()
        if not ok:
            if manual:
                messagebox.showinfo(APP_NAME, why)
            return

        def work():
            try:
                info = updater.check()
            except Exception as e:
                self.log(f"업데이트 확인 실패: {e}")
                if manual:
                    self.msgs.put(("info", f"업데이트 확인 실패: {e}"))
                return
            if info:
                self.msgs.put(("update", info))
            elif manual:
                self.msgs.put(("info", f"최신 버전입니다({updater.local_version()})."))
        threading.Thread(target=work, daemon=True).start()

    def offer_update(self, info: dict):
        if self.running:
            self.log(f"새 버전 {info['version']} 이 있습니다 — 수집이 끝난 뒤 [업데이트 확인]을 눌러 설치하세요.")
            return
        notes = info["notes"] or "(변경 내용 없음)"
        if not messagebox.askyesno(APP_NAME, f"새 버전 {info['version']} 이 있습니다(지금 {updater.local_version()}).\n\n"
                                             f"{notes}\n\n지금 설치할까요? 프로그램 파일만 바뀌고 설정·수집 자료는 그대로입니다."):
            return
        try:
            msg = updater.install(info, log=self.log)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"업데이트 설치 실패: {e}")
            return
        messagebox.showinfo(APP_NAME, f"{msg}\n\n앱을 다시 시작합니다. 열려 있는 Claude Code 도 다시 시작해야 새 기능이 반영됩니다.")
        updater.restart_app()
        self.quit()

    def _start_tray(self):
        import pystray
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(250, 225, 0))
        d.ellipse((14, 16, 50, 44), fill=(60, 30, 30))
        menu = pystray.Menu(pystray.MenuItem("열기", lambda: self.msgs.put(("show", None)), default=True),
                            pystray.MenuItem("전체 지금 수집", lambda: self.msgs.put(("collect", None))),
                            pystray.MenuItem("종료", lambda: self.msgs.put(("quit", None))))
        self.tray = pystray.Icon(APP_NAME, img, APP_NAME, menu)
        self.tray.run_detached()

    def quit(self):
        self.live.stop()
        self.stop.set()
        self.run_now.set()
        self.tray.stop()
        self.root.destroy()


def main():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW(None, False, "Local\\KakaoBizAssistant")  # 두 개가 돌면 같은 방을 동시에 조작하므로 하나만 허용
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        if "--background" not in sys.argv:
            ctypes.windll.user32.MessageBoxW(None, "이미 실행 중입니다. 알림 영역(트레이) 아이콘을 확인하세요.", APP_NAME, 0x40)
        return
    App(background="--background" in sys.argv).root.mainloop()


if __name__ == "__main__":
    main()
