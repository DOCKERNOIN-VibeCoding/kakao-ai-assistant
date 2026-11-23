<#
.SYNOPSIS
PC 카카오톡 '대화 내보내기'(Ctrl+S)를 방 목록에 대해 자동 실행한다. (Windows PowerShell 5.1, 추가 설치 없음)

.DESCRIPTION
사용자가 기다리기로 한 최초 일괄 수집, 또는 PC 무입력 시간에만 실행한다(CLAUDE.md 원칙).
방마다:
  1) 방 창이 이미 열려 있으면 그 창을 쓰고, 없으면 메인 창 검색(Ctrl+F)으로 찾는다.
     검색 결과 첫 줄 이름이 방 이름과 맞지 않으면 열지 않는다(not_found).
     첫 줄의 마지막 대화 날짜가 SkipInactiveDays(30일)보다 오래되면 열지 않는다(skipped_inactive).
     ActiveSince 를 주면 마지막 대화가 그 날짜보다 앞선 방도 열지 않는다(skipped_no_new).
     첫 줄에 안 읽은 표시가 있으면 열지 않는다(skipped_unread) — 방을 열면 읽음 처리되므로.
  2) 메시지 목록으로 포커스를 옮기고 Ctrl+S → '다른 이름으로 저장'에 경로 입력 → 저장.
     (새로 연 방은 입력칸에 포커스가 있어 Ctrl+S 가 무시된다 — 2026-11-23 실측)
  3) '대화 내보내기 … 완료되었습니다' 창이 뜨면 닫고, 직접 연 방 창도 닫는다.
도중에 사용자 입력이 감지되면 그 방을 중단·원상복구하고, 다시 IdleSec 동안 입력이 없을 때 재시도한다.
결과는 표준출력과 <OutDir>\last_run.json 에 JSON 으로 남는다.

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File kakao_exporter.ps1 -RoomsFile rooms.txt
#>
[CmdletBinding()]
param(
    [string[]]$Rooms = @(),
    [string]$RoomsFile,                 # UTF-8, 한 줄에 방 이름 하나
    [string]$OutDir,                    # 기본: config.json 의 data_dir\exports
    [int]$IdleSec = 5,                  # 이 시간 동안 입력이 없어야 시작/재시도
    [int]$MaxMinutes = 30,              # 전체 제한 시간(무입력 대기 포함)
    [int]$RetriesPerRoom = 2,           # 사용자 입력으로 중단된 방을 다시 시도할 횟수
    [int]$SkipInactiveDays = 30,        # 마지막 대화가 이보다 오래된 방은 열지 않음(skipped_inactive). 0 = 끔
    [string]$ActiveSince                # 'yyyy-MM-dd': 마지막 대화가 이 날짜보다 앞선 방은 열지 않음(skipped_no_new) — 쉬는 시간 수집용
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Drawing, System.Runtime.WindowsRuntime

if ($RoomsFile) { $Rooms += @(Get-Content -LiteralPath $RoomsFile -Encoding UTF8 | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
if (-not $Rooms.Count) { throw 'No rooms given (-Rooms or -RoomsFile).' }
if (-not $OutDir) {
    $cfg = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $OutDir = Join-Path $cfg.data_dir 'exports'
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$LogFile = Join-Path $OutDir 'exporter.log'
function Write-Log([string]$m) { Add-Content -LiteralPath $LogFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') $m" -Encoding UTF8 }

if (-not ('KakaoUi' -as [type])) {
Add-Type -ReferencedAssemblies System.Drawing @'
using System; using System.Collections.Generic; using System.Drawing; using System.Runtime.InteropServices; using System.Text;
public static class KakaoUi {
  private delegate bool CB(IntPtr w, IntPtr s);
  [DllImport("user32.dll")] private static extern bool EnumWindows(CB cb, IntPtr s);
  [DllImport("user32.dll")] private static extern bool EnumChildWindows(IntPtr p, CB cb, IntPtr s);
  [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr w, out int pid);
  [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr w, IntPtr pid);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetWindowText(IntPtr w, StringBuilder t, int c);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetClassName(IntPtr w, StringBuilder t, int c);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr w);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr w);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr w, int cmd);
  [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr w);
  [DllImport("user32.dll")] private static extern bool BringWindowToTop(IntPtr w);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] private static extern IntPtr GetWindow(IntPtr w, uint cmd);
  [DllImport("user32.dll")] private static extern int GetDlgCtrlID(IntPtr w);
  [DllImport("user32.dll")] private static extern IntPtr GetFocus();
  [DllImport("user32.dll")] private static extern IntPtr SetFocus(IntPtr w);
  [DllImport("user32.dll")] private static extern bool AttachThreadInput(uint a, uint b, bool f);
  [DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] private static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
  // 카카오톡이 응답하지 않을 때 무한정 기다리지 않도록 SendMessageTimeout(5초, 응답 없음이면 즉시 포기)만 쓴다.
  // (2026-11-23 실측: 내보내기 도중 카카오톡이 3분 멈춘 뒤 DUI70.dll 오류로 종료됨)
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern IntPtr SendMessageTimeout(IntPtr w, uint m, IntPtr wp, StringBuilder lp, uint flags, uint ms, out IntPtr res);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern IntPtr SendMessageTimeout(IntPtr w, uint m, IntPtr wp, string lp, uint flags, uint ms, out IntPtr res);
  [DllImport("user32.dll")] private static extern IntPtr SendMessageTimeout(IntPtr w, uint m, IntPtr wp, IntPtr lp, uint flags, uint ms, out IntPtr res);
  [DllImport("user32.dll")] private static extern bool PostMessage(IntPtr w, uint m, IntPtr wp, IntPtr lp);
  [DllImport("user32.dll")] public static extern bool IsHungAppWindow(IntPtr w);
  private const uint SMTO = 0x0002; // SMTO_ABORTIFHUNG
  private const uint WAIT_MS = 5000;
  private static void Ok(IntPtr r) { if (r == IntPtr.Zero) throw new InvalidOperationException("KAKAO_HUNG: KakaoTalk did not respond within 5s"); }
  [DllImport("user32.dll")] private static extern bool PrintWindow(IntPtr w, IntPtr hdc, uint f);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern uint GetDpiForWindow(IntPtr w);
  [StructLayout(LayoutKind.Sequential)] private struct LII { public uint cbSize; public uint dwTime; }
  [DllImport("user32.dll")] private static extern bool GetLastInputInfo(ref LII p);
  [StructLayout(LayoutKind.Sequential)] private struct R { public int L, T, Ri, B; }
  [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr w, out R r);

  public static uint LastInputTick() { var l = new LII(); l.cbSize = 8; GetLastInputInfo(ref l); return l.dwTime; }
  public static uint IdleMs() { return (uint)Environment.TickCount - LastInputTick(); }
  public static string Cls(IntPtr w) { var b = new StringBuilder(256); GetClassName(w, b, 256); return b.ToString(); }
  public static string Title(IntPtr w) { var b = new StringBuilder(1024); GetWindowText(w, b, 1024); return b.ToString(); }
  public static IntPtr Owner(IntPtr w) { return GetWindow(w, 4); }
  [DllImport("user32.dll")] private static extern IntPtr GetParent(IntPtr w);
  public static string ParentTitle(IntPtr w) { var p = GetParent(w); return p == IntPtr.Zero ? "" : Title(p); }
  [StructLayout(LayoutKind.Sequential)] private struct P { public int X, Y; }
  [DllImport("user32.dll")] private static extern IntPtr WindowFromPoint(P p);
  [DllImport("user32.dll")] private static extern bool ScreenToClient(IntPtr w, ref P p);
  // 메인 창 안 (x,y)[96dpi 기준 창 좌표] 에 클릭 '신호'만 보낸다(실제 마우스는 움직이지 않음).
  // 그 위치가 메인 창의 OnlineMainView 가 아니면(다른 창이 덮음 등) 보내지 않고 false.
  public static bool PostClick(IntPtr main, int x, int y, double scale) {
    R r; GetWindowRect(main, out r);
    var p = new P { X = r.L + (int)(x * scale), Y = r.T + (int)(y * scale) };
    var target = WindowFromPoint(p);
    if (target == IntPtr.Zero || !Title(target).StartsWith("OnlineMainView") || GetParent(target) != main) return false;
    var c = p; ScreenToClient(target, ref c);
    var lp = (IntPtr)((c.Y << 16) | (c.X & 0xFFFF));
    PostMessage(target, 0x200, IntPtr.Zero, lp); PostMessage(target, 0x201, (IntPtr)1, lp);
    System.Threading.Thread.Sleep(60); PostMessage(target, 0x202, IntPtr.Zero, lp);
    return true;
  }
  public static void Key(byte vk, bool up) { keybd_event(vk, 0, up ? 2u : 0u, UIntPtr.Zero); }
  public static void Press(byte vk) { Key(vk, false); Key(vk, true); }
  public static void Chord(byte mod, byte vk) { Key(mod, false); Key(vk, false); Key(vk, true); Key(mod, true); }
  public static bool Activate(IntPtr w) {
    // Alt 탭 방식은 쓰지 않는다: 이미 맨 앞인 카카오톡 창은 메뉴 모드가 되어 Ctrl+S 를 삼키고(2026-11-23 실측),
    // 사용자 프로그램이 맨 앞이면 그 프로그램에 Alt 가 들어간다. 대신 맨 앞 창의 입력 스레드에 붙어서 넘겨받는다.
    if (GetForegroundWindow() == w) return true;
    uint fgThread = GetWindowThreadProcessId(GetForegroundWindow(), IntPtr.Zero), me = GetCurrentThreadId();
    bool attached = fgThread != 0 && fgThread != me && AttachThreadInput(me, fgThread, true);
    try { BringWindowToTop(w); SetForegroundWindow(w); }
    finally { if (attached) AttachThreadInput(me, fgThread, false); }
    System.Threading.Thread.Sleep(200);
    return GetForegroundWindow() == w;
  }
  public static List<IntPtr> Tops(int[] pids, bool visibleOnly) {
    var set = new HashSet<int>(pids); var o = new List<IntPtr>();
    EnumWindows((w, s) => { int p; GetWindowThreadProcessId(w, out p); if (set.Contains(p) && (!visibleOnly || IsWindowVisible(w))) o.Add(w); return true; }, IntPtr.Zero);
    return o;
  }
  public static IntPtr FindTop(int[] pids, string title, bool visibleOnly) {
    foreach (var w in Tops(pids, visibleOnly)) if (Cls(w) == "EVA_Window_Dblclk" && Title(w) == title) return w;
    return IntPtr.Zero;
  }
  public static IntPtr Child(IntPtr h, string cls, string titlePrefix, int id) {
    IntPtr f = IntPtr.Zero;
    EnumChildWindows(h, (w, s) => {
      // PowerShell 은 $null 을 "" 로 넘기므로 빈 값 = 조건 없음
      if (IsWindowVisible(w) && (string.IsNullOrEmpty(cls) || Cls(w) == cls) && (string.IsNullOrEmpty(titlePrefix) || Title(w).StartsWith(titlePrefix)) && (id < 0 || GetDlgCtrlID(w) == id)) { f = w; return false; }
      return true; }, IntPtr.Zero);
    return f;
  }
  public static IntPtr FocusOf(IntPtr top) {
    uint t = GetWindowThreadProcessId(top, IntPtr.Zero), me = GetCurrentThreadId();
    AttachThreadInput(me, t, true); var f = GetFocus(); AttachThreadInput(me, t, false); return f;
  }
  public static void FocusIn(IntPtr top, IntPtr target) {
    uint t = GetWindowThreadProcessId(top, IntPtr.Zero), me = GetCurrentThreadId();
    AttachThreadInput(me, t, true); SetFocus(target); AttachThreadInput(me, t, false);
  }
  public static string GetText(IntPtr w) { var b = new StringBuilder(2048); IntPtr r; Ok(SendMessageTimeout(w, 0x0D, (IntPtr)2048, b, SMTO, WAIT_MS, out r)); return b.ToString(); }
  public static void SetText(IntPtr w, string s) { IntPtr r; Ok(SendMessageTimeout(w, 0x0C, IntPtr.Zero, s, SMTO, WAIT_MS, out r)); }
  public static void Close(IntPtr w) { PostMessage(w, 0x10, IntPtr.Zero, IntPtr.Zero); }   // 기다리지 않음
  public static void Click(IntPtr btn) { IntPtr r; Ok(SendMessageTimeout(btn, 0xF5, IntPtr.Zero, IntPtr.Zero, SMTO, WAIT_MS, out r)); }
  public static Bitmap Capture(IntPtr top, IntPtr child) {
    R b, c; GetWindowRect(top, out b); if (child == IntPtr.Zero) c = b; else GetWindowRect(child, out c);
    int W = b.Ri - b.L, H = b.B - b.T;
    if (W <= 0 || H <= 0) throw new InvalidOperationException("window has no area");
    var frame = new Bitmap(W, H, System.Drawing.Imaging.PixelFormat.Format32bppRgb);
    using (var g = Graphics.FromImage(frame)) { var hdc = g.GetHdc(); try { if (!PrintWindow(top, hdc, 2)) throw new InvalidOperationException("PrintWindow failed"); } finally { g.ReleaseHdc(hdc); } }
    var crop = Rectangle.FromLTRB(c.L - b.L, c.T - b.T, c.Ri - b.L, c.B - b.T);
    var o = frame.Clone(crop, frame.PixelFormat); frame.Dispose(); return o;
  }
}
'@
}
[void][KakaoUi]::SetProcessDPIAware()

# ---------- OCR (Windows 내장, 한국어) ----------
[void][Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
[void][Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$script:AsTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
$script:Ocr = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('ko'))
if ($null -eq $script:Ocr) { throw 'Korean OCR language pack is not installed.' }
function Wait-WinRt($op, [Type]$t) { $script:AsTask.MakeGenericMethod($t).Invoke($null, @($op)).GetAwaiter().GetResult() }
function Read-Text([Drawing.Bitmap]$bmp, [double]$scale = 3.0) {
    $scaled = [Drawing.Bitmap]::new($bmp, [int]($bmp.Width * $scale), [int]($bmp.Height * $scale))
    $stream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
    try {
        $s = [System.IO.WindowsRuntimeStreamExtensions]::AsStreamForWrite($stream)
        $scaled.Save($s, [Drawing.Imaging.ImageFormat]::Png); $s.Flush(); $stream.Seek(0)
        $dec = Wait-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $sb = Wait-WinRt ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $res = Wait-WinRt ($script:Ocr.RecognizeAsync($sb)) ([Windows.Media.Ocr.OcrResult])
        $sb.Dispose()
        foreach ($l in $res.Lines) {
            $words = @($l.Words | ForEach-Object { [pscustomobject]@{ x = [int]($_.BoundingRect.X / $scale); text = $_.Text } })
            $w0 = (@($l.Words | ForEach-Object { $_ }))[0].BoundingRect
            [pscustomobject]@{ y = [int]($w0.Y / $scale); x = [int]($w0.X / $scale); text = $l.Text; words = $words }
        }
    } finally { $scaled.Dispose(); $stream.Dispose() }
}

# ---------- 판정 도우미 ----------
function Get-Norm([string]$s) { ($s -replace '…|\.\.\.|["“”\s]', '' -replace '\d+$', '') }
function Get-Lcs([string]$a, [string]$b) {
    $w = $b.Length + 1
    $dp = New-Object 'int[]' (($a.Length + 1) * $w)
    for ($i = 1; $i -le $a.Length; $i++) { for ($j = 1; $j -le $b.Length; $j++) {
        $diag = $dp[($i - 1) * $w + $j - 1]; $up = $dp[($i - 1) * $w + $j]; $left = $dp[$i * $w + $j - 1]
        $dp[$i * $w + $j] = if ($a[$i - 1] -eq $b[$j - 1]) { $diag + 1 } else { [Math]::Max($up, $left) } } }
    return $dp[$a.Length * $w + $b.Length]
}
function Get-Similarity([string]$ocrName, [string]$title) {
    # ① 말줄임 대비: 방 이름의 같은 길이 앞부분과 비교  ② OCR 이 앞 단어를 빠뜨린 경우 대비('유튜브 가나다_라마바'
    #    → '가나타-라마바', 실측 유형): OCR 글자가 방 이름 안에 순서대로 들어 있는 비율(OCR 6자 이상일 때만)
    $a = Get-Norm $ocrName; $b = Get-Norm $title
    if (-not $a -or -not $b) { return 0.0 }
    $p = if ($b.Length -gt $a.Length) { $b.Substring(0, $a.Length) } else { $b }
    $prefix = (Get-Lcs $a $p) / [double][Math]::Max($a.Length, $p.Length)
    # 말줄임 없이 온전히 보이는 이름이 방 이름보다 3자 이상 길면 다른 방(실측 유형: '영업팀' ↔ '영업팀 실무방')
    if ($ocrName -notmatch '…|\.\.\.' -and $a.Length -gt $b.Length + 2) { $prefix *= 0.5 }
    # 짧은 다른 방 이름이 긴 방 이름 안에 통째로 들어가는 경우는 제외(실측 유형: '영업팀' ⊂ '과장 홍길동 -영업팀')
    $cover = if ($a.Length -ge 6 -and $a.Length -ge $b.Length * 0.6) { (Get-Lcs $a $b) / [double]$a.Length } else { 0.0 }
    if ($cover -lt 0.7) { $cover = 0.0 }   # 실측: 앞 단어 빠진 같은 방 0.75, 그 방 멤버의 1:1 방('홍길동 -업체명') 0.57
    return [Math]::Max($prefix, $cover)
}
function Get-Rows([Drawing.Bitmap]$bmp, [double]$dpiScale, [int]$maxRows = 4) {
    # 검색 결과 위쪽 몇 줄: 이름(왼쪽 위), 마지막 대화 날짜(오른쪽 위), 안 읽은 표시(오른쪽 아래 연한 다홍 원 ≈ R252 G144 B132)
    $rowH = [int](70 * $dpiScale)
    $all = @(Read-Text $bmp)
    # 선택된 줄(회색 배경)에서는 작은 날짜를 놓치므로 날짜 칸만 따로 4배로 읽는다(실측: 첫 줄 날짜를 놓친 사례)
    $rx = [int]($bmp.Width * 0.7)
    $right = $bmp.Clone([Drawing.Rectangle]::FromLTRB($rx, 0, $bmp.Width, $bmp.Height), $bmp.PixelFormat)
    try { $rightLines = @(Read-Text $right 4.0) } finally { $right.Dispose() }
    $rows = @()
    for ($k = 0; $k -lt $maxRows -and $k * $rowH + $rowH * 0.45 -le $bmp.Height; $k++) {   # 아래가 잘린 줄도 이름·날짜가 보이면 읽음
        $y0 = $k * $rowH
        # 이름 = 프로필 사진 오른쪽~날짜 왼쪽의 단어들. 사진 잡음이 줄 앞에 붙는 경우가 있어 줄이 아니라 단어 위치로 거른다
        # (실측 유형: '13 [가나다1 홍길동…', '수4鬱 가나다 라마점…')
        $nameLines = foreach ($l in @($all | Where-Object { $_.y -ge $y0 -and $_.y -lt $y0 + $rowH * 0.35 })) {
            $ws = @($l.words | Where-Object { $_.x -gt $bmp.Width * 0.22 -and $_.x -lt $bmp.Width * 0.75 })
            if ($ws.Count) { [pscustomobject]@{ x = $ws[0].x; text = ($ws | ForEach-Object text) -join ' ' } }
        }
        $name = (@($nameLines) | Sort-Object x | Select-Object -First 1).text
        if (-not $name) { break }
        # 방 이름이 길면 인원수(예: '4')도 날짜 칸에 걸리므로 날짜로 읽히는 것만 고른다.
        # 숫자 오인식(실측: 08-26 → 03-26)으로 활동 중인 방을 건너뛰지 않도록, 두 배율 판독 중 더 최근 날짜를 쓴다
        $cands = @(@($rightLines | ForEach-Object { [pscustomobject]@{ y = $_.y; text = $_.text } }) +
                   @($all | Where-Object { $_.x -ge $rx } | ForEach-Object { [pscustomobject]@{ y = $_.y; text = $_.text } }) |
            Where-Object { $_.y -ge $y0 -and $_.y -lt $y0 + $rowH * 0.45 -and (Get-ListDate $_.text) })
        $dateText = ($cands | Sort-Object { (Get-ListDate $_.text) } -Descending | Select-Object -First 1).text
        $red = 0
        for ($x = [int]($bmp.Width * 0.8); $x -lt $bmp.Width; $x++) {
            for ($y = $y0 + [int]($rowH * 0.4); $y -lt [Math]::Min($y0 + $rowH, $bmp.Height); $y++) {
                $c = $bmp.GetPixel($x, $y)
                if ($c.R -gt 220 -and ($c.R - $c.G) -gt 80 -and ($c.R - $c.B) -gt 90) { $red++ } } }
        $rows += [pscustomobject]@{ index = $k; name = $name; unread = ($red -gt [int](15 * $dpiScale * $dpiScale)); redPixels = $red
                                    dateText = $dateText; lastDate = (Get-ListDate $dateText) }
    }
    return $rows
}
function Get-ListDate([string]$t) {
    # 목록의 마지막 대화 표시 → 날짜. 오늘='오전 10:46', 이전='2026-11-20' (실측), '어제'·'11월 20일'도 처리. 못 읽으면 $null
    if (-not $t) { return $null }
    $today = (Get-Date).Date
    if ($t -match '오전|오후') { return $today }
    if ($t -match '어제') { return $today.AddDays(-1) }
    $n = @([regex]::Matches($t, '\d+') | ForEach-Object { [int]$_.Value })
    try {
        if ($n.Count -ge 3 -and $n[0] -ge 2000) { return [datetime]::new($n[0], $n[1], $n[2]) }
        if ($n.Count -ge 2 -and $t -match '월') {
            $d = [datetime]::new($today.Year, $n[0], $n[1]); if ($d -gt $today) { $d = $d.AddYears(-1) }; return $d
        }
    } catch {}
    return $null
}

# ---------- 사용자 입력 감지 ----------
$script:OurTick = [KakaoUi]::LastInputTick()
function Set-OurInputMark { Start-Sleep -Milliseconds 120; $script:OurTick = [KakaoUi]::LastInputTick() }
function Assert-NoUserInput([string]$where) {
    if ([KakaoUi]::LastInputTick() -gt $script:OurTick + 30) { throw "USER_INPUT: $where" }
}
$Deadline = (Get-Date).AddMinutes($MaxMinutes)
function Wait-Idle {
    while ([KakaoUi]::IdleMs() -lt $IdleSec * 1000) {
        if ((Get-Date) -gt $Deadline) { return $false }
        Start-Sleep -Milliseconds 250
    }
    $script:OurTick = [KakaoUi]::LastInputTick(); return $true
}

# ---------- 카카오톡 창 ----------
$Pids = [int[]]@(Get-Process -Name KakaoTalk -ErrorAction SilentlyContinue | ForEach-Object Id)
if (-not $Pids.Count) { throw 'KakaoTalk is not running.' }
function Get-MainWindow {
    $m = [KakaoUi]::FindTop($Pids, '카카오톡', $false)
    if ($m -eq [IntPtr]::Zero) { throw 'KakaoTalk main window not found.' }
    return $m
}
function Assert-KakaoAlive([IntPtr]$w) {
    # 카카오톡이 꺼졌거나 '응답 없음'이면 더 아무것도 보내지 않고 회차를 끝낸다(캡처·포커스 이동도 멈출 수 있으므로)
    if (-not (Get-Process -Id $Pids[0] -ErrorAction SilentlyContinue)) { throw 'KAKAO_GONE: KakaoTalk is not running' }
    if ($w -ne [IntPtr]::Zero -and [KakaoUi]::IsHungAppWindow($w)) { throw 'KAKAO_HUNG: KakaoTalk is not responding' }
}
function Show-Tab([IntPtr]$main, [string]$tab) {
    # 사이드바 아이콘 위치(96dpi 창 좌표, 실측): 친구 (33,56), 채팅 (33,118)
    # 탭 판단은 목록을 담는 View 로 한다(검색 중에는 ChatRoomListCtrl 이 숨고 SearchListCtrl 이 보임 — 실측)
    $list = @{ friends = 'ContactListView'; chat = 'ChatRoomListView' }[$tab]
    if ([KakaoUi]::Child($main, $null, $list, -1) -ne [IntPtr]::Zero) { return }
    $y = @{ friends = 56; chat = 118 }[$tab]
    if (-not [KakaoUi]::PostClick($main, 33, $y, (Get-Dpi $main))) { throw "could not click the $tab tab icon (covered or layout changed)" }
    for ($i = 0; $i -lt 10 -and [KakaoUi]::Child($main, $null, $list, -1) -eq [IntPtr]::Zero; $i++) { Start-Sleep -Milliseconds 100 }
    if ([KakaoUi]::Child($main, $null, $list, -1) -eq [IntPtr]::Zero) { throw "could not switch to the $tab tab" }
}
function Get-Dpi([IntPtr]$w) { try { $d = [KakaoUi]::GetDpiForWindow($w); if ($d -gt 0) { return $d / 96.0 } } catch {}; return 1.0 }
function Get-SafeName([string]$s) { ($s -replace '[\\/:*?"<>|\r\n\t]', '_').Trim(' .') }

function Export-OneRoom([string]$title) {
    $t0 = Get-Date
    $r = [ordered]@{ room = $title; status = ''; file = $null; bytes = 0; seconds = 0; last_activity = $null; note = '' }
    $room = [KakaoUi]::FindTop($Pids, $title, $true)
    $weOpened = $false; $roomWasMin = $false; $searchEdit = [IntPtr]::Zero
    $before = @([KakaoUi]::Tops($Pids, $true) | Where-Object { [KakaoUi]::Cls($_) -eq '#32770' })
    try {
        if ($room -eq [IntPtr]::Zero) {
            # --- 메인 창 검색으로 찾기 ---
            $main = Get-MainWindow
            Assert-KakaoAlive $main
            if ([KakaoUi]::IsIconic($main) -or -not [KakaoUi]::IsWindowVisible($main)) { [void][KakaoUi]::ShowWindow($main, 9); Start-Sleep -Milliseconds 400 }
            if (-not [KakaoUi]::Activate($main)) { throw 'could not activate main window' }
            Set-OurInputMark
            # Ctrl+F 는 지금 보이는 탭의 검색을 연다(재시작 직후엔 '친구' 탭 — 실측). Ctrl+2 는 탭을 바꾸지 못함(실측)
            # → 채팅 탭이 아니면 사이드바 채팅 아이콘에 클릭 신호를 보낸 뒤, 채팅 목록에 포커스를 두고 Ctrl+F
            Show-Tab $main 'chat'
            $chatList = [KakaoUi]::Child($main, $null, 'ChatRoomListCtrl', -1)
            if ($chatList -ne [IntPtr]::Zero) { [KakaoUi]::FocusIn($main, $chatList) }
            [KakaoUi]::Chord(0x11, 0x46); Set-OurInputMark          # Ctrl+F
            Start-Sleep -Milliseconds 300
            $searchEdit = [KakaoUi]::FocusOf($main)
            if ([KakaoUi]::Cls($searchEdit) -ne 'Edit') { throw 'search box not focused after Ctrl+F' }
            [KakaoUi]::SetText($searchEdit, $title)
            Start-Sleep -Milliseconds 900
            Assert-NoUserInput 'search'
            $list = [KakaoUi]::Child($main, $null, 'SearchListCtrl', -1)
            if ($list -eq [IntPtr]::Zero) { throw 'search result list not found' }
            # 친구 검색 결과면(부모가 ContactListView) 엉뚱한 1:1 방을 열 수 있으므로 중단
            $listParent = [KakaoUi]::ParentTitle($list)
            if (-not $listParent.StartsWith('ChatRoomListView')) { throw "search is not in the chat tab (parent '$listParent')" }
            Assert-KakaoAlive $main
            $bmp = [KakaoUi]::Capture($main, $list)
            $dpi = Get-Dpi $main
            try { $rows = @(Get-Rows $bmp $dpi 6) } finally { $bmp.Dispose() }
            # 검색 결과 위쪽 줄 중 이름이 가장 비슷한 줄 (사람 이름으로 찾으면 그 사람이 있는 단체방이 먼저, 1:1 방은 아래 — 실측)
            $row = $null; $sim = 0.0
            foreach ($cand in $rows) { $s = Get-Similarity $cand.name $title; if ($s -gt $sim) { $sim = $s; $row = $cand } }
            $r.note = "rows: " + (($rows | ForEach-Object { "#$($_.index) '$($_.name)' last='$($_.dateText)' red=$($_.redPixels)" }) -join ' | ') +
                      " -> best sim=$([Math]::Round($sim, 2))"
            if ($null -eq $row -or $sim -lt 0.55) {
                # 화면 밖 결과: 사람 이름으로 찾으면 그 사람이 있는 단체방들이 먼저 나와 1:1 방이 아래로 밀린다(실측).
                # ↓ 로 한 줄씩 내려가며 '선택된 줄'(회색 배경 237)만 읽는다. 찾으면 그 줄이 이미 선택된 상태다.
                $rowH = [int](70 * $dpi); $prevName = $null; $same = 0
                for ($step = 1; $step -le 25; $step++) {
                    $cand = $null
                    [KakaoUi]::Press(0x28); Set-OurInputMark; Start-Sleep -Milliseconds 150
                    Assert-NoUserInput 'scan search results'
                    Assert-KakaoAlive $main
                    $bmp = [KakaoUi]::Capture($main, $list)
                    try {
                        # 목록은 픽셀 단위로 스크롤되므로(실측) 선택된 줄의 위치를 찾는다:
                        # 프로필 사진과 이름 사이 빈 세로줄(폭의 22%)에서 회색(≈237) 구간의 시작
                        $gx = [int]($bmp.Width * 0.22); $y0 = -1
                        for ($y = 0; $y -lt $bmp.Height; $y++) {
                            $c = $bmp.GetPixel($gx, $y)
                            if ($c.R -eq $c.G -and $c.G -eq $c.B -and $c.R -ge 225 -and $c.R -le 245) { $y0 = $y; break }
                        }
                        if ($y0 -lt 0) { continue }
                        $bh = [Math]::Min($rowH, $bmp.Height - $y0)
                        if ($bh -lt [int]($rowH * 0.45)) { continue }   # 이름이 다 안 보이는 조각
                        $band = $bmp.Clone([Drawing.Rectangle]::new(0, $y0, $bmp.Width, $bh), $bmp.PixelFormat)
                        try { $cand = @(Get-Rows $band $dpi 1) | Select-Object -First 1 } finally { $band.Dispose() }
                    } finally { $bmp.Dispose() }
                    if (-not $cand) { continue }
                    $s = Get-Similarity $cand.name $title
                    $r.note += " | scan#$step '$($cand.name)' sim=$([Math]::Round($s, 2))"
                    if ($s -ge 0.55) { $row = $cand; $row.index = 0; $sim = $s; break }   # 이미 선택됨 → ↓ 추가 없이 Enter
                    if ($cand.name -eq $prevName) { if (++$same -ge 2) { break } } else { $same = 0 }   # 목록 끝
                    $prevName = $cand.name
                }
            }
            if ($null -eq $row -or $sim -lt 0.55) { $r.status = 'not_found'; return $r }   # 실측: 흐린 OCR 0.6, 다른 방끼리 0~0.4
            if ($row.lastDate) { $r.last_activity = $row.lastDate.ToString('yyyy-MM-dd') }
            if ($SkipInactiveDays -gt 0 -and $row.lastDate -and $row.lastDate -lt (Get-Date).Date.AddDays(-$SkipInactiveDays)) {
                $r.status = 'skipped_inactive'; return $r            # 한 달 넘게 대화 없는 방은 열지 않음(2026-11-23 사용자 요청)
            }
            if ($ActiveSince -and $row.lastDate -and $row.lastDate -lt [datetime]::ParseExact($ActiveSince, 'yyyy-MM-dd', $null)) {
                $r.status = 'skipped_no_new'; return $r              # 지난 수집 이후 대화 없음
            }
            if ($row.unread) { $r.status = 'skipped_unread'; return $r }
            $roomsBefore = @([KakaoUi]::Tops($Pids, $true) | Where-Object { [KakaoUi]::Cls($_) -eq 'EVA_Window_Dblclk' })
            for ($k = 0; $k -lt $row.index; $k++) { [KakaoUi]::Press(0x28); Start-Sleep -Milliseconds 120 }   # ↓ 로 그 줄 선택
            [KakaoUi]::Press(0x0D); Set-OurInputMark                 # Enter → 선택한 결과 열기
            for ($i = 0; $i -lt 30 -and $room -eq [IntPtr]::Zero; $i++) { Start-Sleep -Milliseconds 150; $room = [KakaoUi]::FindTop($Pids, $title, $true) }
            if ($room -eq [IntPtr]::Zero) {
                # 엉뚱한 방이 열렸으면 바로 닫는다
                $wrong = @([KakaoUi]::Tops($Pids, $true) | Where-Object { [KakaoUi]::Cls($_) -eq 'EVA_Window_Dblclk' -and [KakaoUi]::Title($_) -and $roomsBefore -notcontains $_ })
                $wrongTitles = ($wrong | ForEach-Object { [KakaoUi]::Title($_) }) -join ', '
                foreach ($w in $wrong) { [KakaoUi]::Close($w) }
                throw "room window did not open$(if ($wrong.Count) { " (closed wrong room: $wrongTitles)" })"
            }
            $weOpened = $true
            Start-Sleep -Milliseconds 1500   # 막 연 방은 잠시 Ctrl+S 를 받지 않음
        } else {
            $roomWasMin = [KakaoUi]::IsIconic($room)
            if ($roomWasMin) { [void][KakaoUi]::ShowWindow($room, 9); Start-Sleep -Milliseconds 400 }
        }
        # --- 내보내기 ---
        Assert-KakaoAlive $room
        if (-not [KakaoUi]::Activate($room)) { throw 'could not activate room window' }
        Set-OurInputMark; Assert-NoUserInput 'activate room'
        $msgList = [KakaoUi]::Child($room, 'EVA_VH_ListControl_Dblclk', $null, -1)
        Assert-KakaoAlive $room
        if ($msgList -ne [IntPtr]::Zero) { [KakaoUi]::FocusIn($room, $msgList) }
        [KakaoUi]::Chord(0x11, 0x53); Set-OurInputMark              # Ctrl+S
        $dlg = [IntPtr]::Zero
        for ($i = 0; $i -lt 40 -and $dlg -eq [IntPtr]::Zero; $i++) {
            Start-Sleep -Milliseconds 150
            $dlg = @([KakaoUi]::Tops($Pids, $true) | Where-Object { [KakaoUi]::Cls($_) -eq '#32770' -and $before -notcontains $_ }) | Select-Object -First 1
            if ($null -eq $dlg) { $dlg = [IntPtr]::Zero }
            Assert-NoUserInput 'waiting save dialog'
            Assert-KakaoAlive $room
            if ($i -eq 20 -and $dlg -eq [IntPtr]::Zero) { [void][KakaoUi]::Activate($room); [KakaoUi]::FocusIn($room, $msgList); [KakaoUi]::Chord(0x11, 0x53); Set-OurInputMark }
        }
        if ($dlg -eq [IntPtr]::Zero) { throw 'save dialog did not appear' }
        $nameBox = [IntPtr]::Zero
        for ($i = 0; $i -lt 10 -and $nameBox -eq [IntPtr]::Zero; $i++) {   # 저장 창이 늦게 그려질 수 있음
            Assert-KakaoAlive $dlg
            $nameBox = [KakaoUi]::Child($dlg, 'Edit', $null, 1001)
            if ($nameBox -eq [IntPtr]::Zero) { Start-Sleep -Milliseconds 200 }
        }
        if ($nameBox -eq [IntPtr]::Zero) { throw 'file name box not found in save dialog' }
        $target = Join-Path $OutDir ("{0}_{1}.txt" -f (Get-SafeName $title), (Get-Date -Format 'yyyyMMdd_HHmmss'))
        [KakaoUi]::SetText($nameBox, $target)
        Assert-NoUserInput 'save dialog'
        [KakaoUi]::Click([KakaoUi]::Child($dlg, 'Button', $null, 1))
        # --- 완료 창 대기 → 닫기 ---
        $pop = [IntPtr]::Zero
        for ($i = 0; $i -lt 100 -and $pop -eq [IntPtr]::Zero; $i++) {
            Start-Sleep -Milliseconds 200
            foreach ($w in [KakaoUi]::Tops($Pids, $true)) { if ([KakaoUi]::Title($w) -eq '' -and [KakaoUi]::Owner($w) -eq $room) { $pop = $w } }
        }
        if ($pop -eq [IntPtr]::Zero) { throw 'export progress window not seen' }
        $txt = ''; $hungSec = 0.0
        for ($j = 0; $j -lt 360 -and $txt -notmatch '완료'; $j++) {
            if ([KakaoUi]::IsHungAppWindow($pop)) {   # 큰 방은 잠깐 바쁠 수 있음 — 캡처(멈출 수 있음)는 건너뛰고 30초까지 기다림
                $hungSec += 0.5; if ($hungSec -gt 30) { throw 'KAKAO_HUNG: not responding during export' }
                Start-Sleep -Milliseconds 500; continue
            }
            Assert-KakaoAlive ([IntPtr]::Zero)
            $pb = [KakaoUi]::Capture($pop, [IntPtr]::Zero)
            try { $txt = (Read-Text $pb | ForEach-Object text) -join ' ' } finally { $pb.Dispose() }
            if ($txt -notmatch '완료') { Start-Sleep -Milliseconds 500 }
        }
        if ($txt -notmatch '완료') { throw "export did not complete: $txt" }
        [KakaoUi]::Close($pop)
        if (-not (Test-Path -LiteralPath $target)) { throw 'exported file not found' }
        $r.status = 'exported'; $r.file = $target; $r.bytes = (Get-Item -LiteralPath $target).Length
        return $r
    } finally {
        foreach ($w in [KakaoUi]::Tops($Pids, $true)) { if ([KakaoUi]::Cls($w) -eq '#32770' -and $before -notcontains $w) { [KakaoUi]::Close($w) } }
        if ($room -ne [IntPtr]::Zero) {
            foreach ($w in [KakaoUi]::Tops($Pids, $true)) { if ([KakaoUi]::Title($w) -eq '' -and [KakaoUi]::Owner($w) -eq $room) { [KakaoUi]::Close($w) } }
            if ($weOpened) { [KakaoUi]::Close($room) } elseif ($roomWasMin) { [void][KakaoUi]::ShowWindow($room, 6) }
        }
        if ($searchEdit -ne [IntPtr]::Zero) { try { [KakaoUi]::SetText($searchEdit, '') } catch {} }
        $r.seconds = [Math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
    }
}

# ---------- 실행 ----------
$prevFg = [KakaoUi]::GetForegroundWindow()
$main0 = Get-MainWindow
$mainWasMin = [KakaoUi]::IsIconic($main0) -or -not [KakaoUi]::IsWindowVisible($main0)
$wasFriendsTab = [KakaoUi]::Child($main0, $null, 'ContactListCtrl', -1) -ne [IntPtr]::Zero   # 끝나면 원래 탭으로
$results = New-Object System.Collections.Generic.List[object]
$usedSearch = $false
$abortAll = $null
Write-Log "start: $($Rooms.Count) rooms -> $OutDir"
foreach ($title in $Rooms) {
    if ($abortAll) {   # 카카오톡이 멈췄거나 꺼지면 남은 방은 손대지 않는다
        $results.Add([pscustomobject][ordered]@{ room = $title; status = 'aborted'; file = $null; bytes = 0; seconds = 0; note = $abortAll })
        continue
    }
    $res = $null
    for ($attempt = 0; $attempt -le $RetriesPerRoom -and $null -eq $res; $attempt++) {
        if (-not (Wait-Idle)) { $res = [ordered]@{ room = $title; status = 'timeout'; file = $null; bytes = 0; seconds = 0; note = 'no idle period before deadline' }; break }
        try {
            if ([KakaoUi]::FindTop($Pids, $title, $true) -eq [IntPtr]::Zero) { $usedSearch = $true }
            $res = Export-OneRoom $title
        } catch {
            $msg = $_.Exception.Message
            if ($msg -notlike 'USER_INPUT*') { $res = [ordered]@{ room = $title; status = 'error'; file = $null; bytes = 0; seconds = 0; note = $msg }; break }
            Write-Log "interrupted: $title ($msg) attempt $($attempt + 1)"
            if ($attempt -eq $RetriesPerRoom) { $res = [ordered]@{ room = $title; status = 'interrupted'; file = $null; bytes = 0; seconds = 0; note = $msg } }
        }
    }
    Write-Log ("{0}: {1} {2}B {3}s {4}" -f $title, $res.status, $res.bytes, $res.seconds, $res.note)
    $results.Add([pscustomobject]$res)
    if ($res.note -match '^KAKAO_(HUNG|GONE)') { $abortAll = "stopped: $($res.note)"; Write-Log "ABORT remaining rooms: $($res.note)" }
}
# 마무리: 검색 모드 해제, 메인 창·포커스 원래대로 (카카오톡이 멈췄거나 꺼졌으면 건드리지 않음)
if (-not $abortAll) { try {
    $main = Get-MainWindow
    $userTouched = [KakaoUi]::LastInputTick() -gt $script:OurTick + 30
    if ($usedSearch -and -not $userTouched -and [KakaoUi]::Activate($main)) {
        # 검색 모드 닫기: 검색칸에 포커스를 준 뒤 Esc (메인 창 자체에 Esc 를 보내면 창이 숨을 수 있음)
        [KakaoUi]::Chord(0x11, 0x46); Start-Sleep -Milliseconds 250
        if ([KakaoUi]::Cls([KakaoUi]::FocusOf($main)) -eq 'Edit') { [KakaoUi]::Press(0x1B) }
        if ($wasFriendsTab) { Start-Sleep -Milliseconds 200; Show-Tab $main 'friends' }
    }
    if ($mainWasMin) { [void][KakaoUi]::ShowWindow($main, 6) }
    if ($prevFg -ne [IntPtr]::Zero) { [void][KakaoUi]::Activate($prevFg) }
} catch { Write-Log "restore failed: $($_.Exception.Message)" } }

$summary = [ordered]@{ finished_at = (Get-Date).ToString('s'); out_dir = $OutDir; results = $results }
$json = $summary | ConvertTo-Json -Depth 4
[IO.File]::WriteAllText((Join-Path $OutDir 'last_run.json'), $json, [Text.UTF8Encoding]::new($false))
$json
