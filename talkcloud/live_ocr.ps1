<#
.SYNOPSIS
사용 중 실시간 수집: 열려 있는 카카오톡 업무방 창을 화면을 뺏지 않고 캡처해, 내용이 바뀌었을 때만 OCR 결과를 내보낸다.

.DESCRIPTION
- WatchFile(UTF-8, 한 줄에 방 이름 하나)에 있는 방만 본다 — 수집 중인 업무방. 개인 대화로 제외된 방·미등록 방은 읽지 않는다.
- IntervalSec 마다 창 목록을 보고, 보이는(최소화 안 된) 방 창의 메시지 영역을 PrintWindow 로 캡처한다. 창을 앞으로 가져오거나 클릭하지 않는다.
- 화면이 바뀌었을 때만 Windows 내장 OCR(한국어, PC 안 처리)로 읽어 표준출력에 한 줄 JSON 으로 쓴다:
  {"title":..., "at":"ISO 시각", "w":..., "h":..., "lines":[{"t":글자, "x":왼쪽, "y":위, "r":오른쪽, "b":아래,
   "bt":말풍선 윗변 y(말풍선 밖 글자면 -1), "c":말풍선 색 'y'(노랑=내 메시지)/'w'}, ...]}
- ParentPid 프로세스(상주 앱)가 끝나면 함께 끝난다.
#>
param([Parameter(Mandatory)][string]$WatchFile, [int]$IntervalSec = 3, [int]$ParentPid = 0)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Drawing, System.Runtime.WindowsRuntime
if (-not ('LiveUi' -as [type])) {
Add-Type -ReferencedAssemblies System.Drawing @'
using System; using System.Collections.Generic; using System.Drawing; using System.Runtime.InteropServices; using System.Text;
public static class LiveUi {
  private delegate bool CB(IntPtr w, IntPtr s);
  [DllImport("user32.dll")] private static extern bool EnumWindows(CB cb, IntPtr s);
  [DllImport("user32.dll")] private static extern bool EnumChildWindows(IntPtr p, CB cb, IntPtr s);
  [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr w, out int pid);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetWindowText(IntPtr w, StringBuilder t, int c);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetClassName(IntPtr w, StringBuilder t, int c);
  [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr w);
  [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr w);
  [DllImport("user32.dll")] private static extern bool IsHungAppWindow(IntPtr w);
  [DllImport("user32.dll")] private static extern bool PrintWindow(IntPtr w, IntPtr hdc, uint f);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [StructLayout(LayoutKind.Sequential)] private struct R { public int L, T, Ri, B; }
  [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr w, out R r);
  private static string Cls(IntPtr w) { var b = new StringBuilder(256); GetClassName(w, b, 256); return b.ToString(); }
  public static string Title(IntPtr w) { var b = new StringBuilder(1024); GetWindowText(w, b, 1024); return b.ToString(); }
  // 보이는(최소화·응답 없음 아닌) 카카오톡 방 창
  public static List<IntPtr> Rooms(int[] pids) {
    var set = new HashSet<int>(pids); var o = new List<IntPtr>();
    EnumWindows((w, s) => { int p; GetWindowThreadProcessId(w, out p);
      if (set.Contains(p) && IsWindowVisible(w) && !IsIconic(w) && !IsHungAppWindow(w) && Cls(w) == "EVA_Window_Dblclk") o.Add(w); return true; }, IntPtr.Zero);
    return o;
  }
  public static IntPtr MessageList(IntPtr room) {
    IntPtr f = IntPtr.Zero;
    EnumChildWindows(room, (w, s) => { if (IsWindowVisible(w) && Cls(w) == "EVA_VH_ListControl_Dblclk") { f = w; return false; } return true; }, IntPtr.Zero);
    return f;
  }
  public static Bitmap Capture(IntPtr top, IntPtr child) {
    R b, c; GetWindowRect(top, out b); GetWindowRect(child, out c);
    int W = b.Ri - b.L, H = b.B - b.T;
    if (W <= 0 || H <= 0) return null;
    var frame = new Bitmap(W, H, System.Drawing.Imaging.PixelFormat.Format32bppRgb);
    using (var g = Graphics.FromImage(frame)) { var hdc = g.GetHdc(); try { PrintWindow(top, hdc, 2); } finally { g.ReleaseHdc(hdc); } }
    var crop = Rectangle.Intersect(Rectangle.FromLTRB(c.L - b.L, c.T - b.T, c.Ri - b.L, c.B - b.T), new Rectangle(0, 0, W, H));
    if (crop.Width <= 0 || crop.Height <= 0) { frame.Dispose(); return null; }
    var o = frame.Clone(crop, frame.PixelFormat); frame.Dispose(); return o;
  }
  // 배경색 = 맨 왼쪽 세로줄에서 가장 흔한 색(프로필 사진·말풍선이 닿지 않는 곳)
  public static int Background(Bitmap b) {
    var n = new Dictionary<int, int>(); int best = 0, bc = 0;
    for (int y = 0; y < b.Height; y += 2) { int c = b.GetPixel(2, y).ToArgb(); int k; n.TryGetValue(c, out k); n[c] = ++k; if (k > bc) { bc = k; best = c; } }
    return best;
  }
  private static bool Near(Color a, Color b) { return Math.Abs(a.R - b.R) + Math.Abs(a.G - b.G) + Math.Abs(a.B - b.B) < 30; }
  // 글자 줄이 말풍선 안이면 그 말풍선의 윗변 y 와 색('y' 노랑=내 메시지, 'w' 그 밖), 배경 위 글자(이름·시각·날짜)면 -1
  public static string Bubble(Bitmap b, int bgArgb, int x, int y, int bottom) {
    var bg = Color.FromArgb(bgArgb); int cy = Math.Min(b.Height - 1, Math.Max(0, (y + bottom) / 2)); int px = x - 3;
    if (px < 1 || Near(b.GetPixel(px, cy), bg)) return "-1";
    int lx = px; while (lx > 0 && !Near(b.GetPixel(lx - 1, cy), bg)) lx--;
    int col = Math.Min(b.Width - 1, lx + 6); var c = b.GetPixel(Math.Min(b.Width - 1, lx + 2), cy);
    int ty = cy; while (ty > 0 && !Near(b.GetPixel(col, ty - 1), bg)) ty--;
    bool yellow = c.R > 220 && c.G > 190 && c.B < 140;
    return ty + (yellow ? ",y" : ",w");
  }
  // 바뀜 감지용 지문(1/4 축소 픽셀의 해시) — OCR 은 바뀌었을 때만
  public static string Fingerprint(Bitmap bmp) {
    using (var small = new Bitmap(bmp, Math.Max(1, bmp.Width / 4), Math.Max(1, bmp.Height / 4))) {
      long h = 1469598103934665603;
      for (int y = 0; y < small.Height; y++) for (int x = 0; x < small.Width; x++) { h ^= small.GetPixel(x, y).ToArgb(); h *= 1099511628211; }
      return h.ToString("x");
    }
  }
}
'@
}
[void][LiveUi]::SetProcessDPIAware()

[void][Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
[void][Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
function Wait-WinRt($op, [Type]$t) { $asTask.MakeGenericMethod($t).Invoke($null, @($op)).GetAwaiter().GetResult() }
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('ko'))
if ($null -eq $engine) { throw 'Korean OCR language pack is not installed.' }

# 3배 확대 + 흑백(실측: 2배보다 정확, 컬러 고화질 확대와 같은 결과를 절반 시간에)
$gray = [Drawing.Imaging.ColorMatrix]::new([single[][]]@(
    [single[]]@(0.299, 0.299, 0.299, 0, 0), [single[]]@(0.587, 0.587, 0.587, 0, 0),
    [single[]]@(0.114, 0.114, 0.114, 0, 0), [single[]]@(0, 0, 0, 1, 0), [single[]]@(0, 0, 0, 0, 1)))
function Read-Lines([Drawing.Bitmap]$bmp, [double]$scale = 3.0) {
    $bg = [LiveUi]::Background($bmp)
    $scaled = [Drawing.Bitmap]::new([int]($bmp.Width * $scale), [int]($bmp.Height * $scale))
    $g = [Drawing.Graphics]::FromImage($scaled)
    $ia = [Drawing.Imaging.ImageAttributes]::new(); $ia.SetColorMatrix($gray)
    $g.DrawImage($bmp, [Drawing.Rectangle]::new(0, 0, $scaled.Width, $scaled.Height), 0, 0, $bmp.Width, $bmp.Height, [Drawing.GraphicsUnit]::Pixel, $ia)
    $g.Dispose(); $ia.Dispose()
    $stream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
    try {
        $s = [System.IO.WindowsRuntimeStreamExtensions]::AsStreamForWrite($stream)
        $scaled.Save($s, [Drawing.Imaging.ImageFormat]::Png); $s.Flush(); $stream.Seek(0)
        $dec = Wait-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $sb = Wait-WinRt ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        try { $res = Wait-WinRt ($engine.RecognizeAsync($sb)) ([Windows.Media.Ocr.OcrResult]) } finally { $sb.Dispose() }
        foreach ($l in $res.Lines) {
            $ws = @($l.Words | ForEach-Object { $_.BoundingRect })
            $x = [int](($ws | ForEach-Object X | Measure-Object -Minimum).Minimum / $scale)
            $y = [int](($ws | ForEach-Object Y | Measure-Object -Minimum).Minimum / $scale)
            $b = [int](($ws | ForEach-Object { $_.Y + $_.Height } | Measure-Object -Maximum).Maximum / $scale)
            $bub = ([LiveUi]::Bubble($bmp, $bg, $x, $y, $b)).Split(',')
            [ordered]@{ t = $l.Text; x = $x; y = $y
                        r = [int](($ws | ForEach-Object { $_.X + $_.Width } | Measure-Object -Maximum).Maximum / $scale)
                        b = $b; bt = [int]$bub[0]; c = $(if ($bub.Count -gt 1) { $bub[1] } else { '' }) }
        }
    } finally { $scaled.Dispose(); $stream.Dispose() }
}

$utf8 = [Text.UTF8Encoding]::new($false)
$watch = @{}; $watchStamp = $null
$last = @{}
while ($true) {
    if ($ParentPid -and -not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) { break }
    try {
        $stamp = (Get-Item -LiteralPath $WatchFile -ErrorAction SilentlyContinue).LastWriteTimeUtc
        if ($stamp -ne $watchStamp) {
            $watch = @{}
            if ($stamp) { foreach ($t in [IO.File]::ReadAllLines($WatchFile, $utf8)) { if ($t.Trim()) { $watch[$t.Trim()] = $true } } }
            $watchStamp = $stamp
        }
        $pids = [int[]]@(Get-Process -Name KakaoTalk -ErrorAction SilentlyContinue | ForEach-Object Id)
        if ($pids.Count -and $watch.Count) {
            foreach ($w in [LiveUi]::Rooms($pids)) {
                $title = [LiveUi]::Title($w)
                if (-not $watch.ContainsKey($title)) { continue }
                $list = [LiveUi]::MessageList($w)
                if ($list -eq [IntPtr]::Zero) { continue }
                $bmp = [LiveUi]::Capture($w, $list)
                if ($null -eq $bmp) { continue }
                try {
                    $fp = [LiveUi]::Fingerprint($bmp)
                    if ($last[$title] -eq $fp) { continue }
                    $last[$title] = $fp
                    $frame = [ordered]@{ title = $title; at = (Get-Date).ToString('s'); w = $bmp.Width; h = $bmp.Height; lines = @(Read-Lines $bmp) }
                    [Console]::Out.WriteLine(($frame | ConvertTo-Json -Depth 4 -Compress))
                    [Console]::Out.Flush()
                } finally { $bmp.Dispose() }
            }
        }
    } catch {
        [Console]::Error.WriteLine("live_ocr: $($_.Exception.Message)")
    }
    Start-Sleep -Seconds $IntervalSec
}
