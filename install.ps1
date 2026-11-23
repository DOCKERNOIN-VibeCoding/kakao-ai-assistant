<#
.SYNOPSIS
카카오톡 연동 AI Assistant 설치 — 설치.cmd 를 더블클릭하면 실행된다.

1. 프로그램 파일을 %LOCALAPPDATA%\KakaoAIAssistant\app 으로 복사(다시 설치하면 프로그램 파일만 바뀌고 설정·수집 자료는 그대로)
2. Python 3.12 이상이 없으면 winget 으로 설치(사용자 범위, 관리자 권한 불필요)
3. 필요한 Python 패키지 설치(브라우저는 PC 의 Microsoft Edge 를 쓰므로 따로 받지 않음)
4. Windows 한국어 OCR(글자 인식) 기능 확인 — 없으면 설치 여부를 묻는다(관리자 권한 필요)
5. 시작 메뉴·바탕화면 바로가기 만들고 앱 실행

-InstallDir : 설치 위치 변경(시험용), -NoStart : 바로가기·앱 실행 생략(시험용)
#>
param([string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'KakaoAIAssistant\app'), [switch]$NoStart)

$ErrorActionPreference = 'Continue'
$AppName = '카카오톡 연동 AI Assistant'
$Src = $PSScriptRoot
function Say($msg, $color = 'Gray') { Write-Host $msg -ForegroundColor $color }
function Fail($msg) { Say "`n설치 실패: $msg" Red; exit 1 }

Say "=== $AppName 설치 ===`n" Cyan

# ---------- 1. 프로그램 파일 복사 ----------
if (-not (Test-Path (Join-Path $Src 'talkcloud\assistant_app.py'))) { Fail '압축을 푼 폴더 안에서 설치.cmd 를 실행하세요(talkcloud 폴더가 옆에 있어야 합니다).' }
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
# 이 위치에서 이미 실행 중인 앱은 끝낸다(파일 교체 후 다시 실행)
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$InstallDir*assistant_app.py*" } |
    ForEach-Object { Say '실행 중인 앱을 잠시 종료합니다.'; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
if ([IO.Path]::GetFullPath($Src).TrimEnd('\') -ne $InstallDir.TrimEnd('\')) {
    New-Item -ItemType Directory -Force $InstallDir | Out-Null
    Get-ChildItem -LiteralPath $Src -Force | Where-Object { $_.Name -ne '.git' } |
        ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $InstallDir -Recurse -Force }
}
# 인터넷에서 받은 파일 표시(차단) 해제 — 앱이 PowerShell 스크립트를 실행할 수 있게
Get-ChildItem -LiteralPath $InstallDir -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
Say "[1/5] 프로그램 파일: $InstallDir" Green

# ---------- 2. Python ----------
function Find-Python {
    $cands = @()
    foreach ($c in 'py', 'python') { $cmd = Get-Command $c -ErrorAction SilentlyContinue; if ($cmd) { $cands += $cmd.Source } }
    $cands += @(Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
                Sort-Object FullName -Descending | ForEach-Object FullName)
    foreach ($c in $cands) {
        # py 런처는 인수 없이도 최신 Python 3 를 고른다. 표준입력은 닫아 둔다(어떤 경우에도 입력 대기로 멈추지 않게)
        $out = @('' | & $c -c "import sys;print(sys.version_info[0]*100+sys.version_info[1]);print(sys.executable)" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $out.Count -ge 2 -and [int]$out[0] -ge 312) { return $out[1] }
    }
    return $null
}
$py = Find-Python
if (-not $py) {
    Say 'Python 3.12 이상이 없어 설치합니다(몇 분 걸릴 수 있습니다)...' Yellow
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Start-Process 'https://www.python.org/downloads/windows/'
        Fail "winget 이 없어 Python 을 자동 설치할 수 없습니다. 열린 페이지에서 Python 3.12 이상을 설치한 뒤('Add python.exe to PATH' 체크) 설치.cmd 를 다시 실행하세요."
    }
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    $py = Find-Python
    if (-not $py) { Fail 'Python 설치를 확인하지 못했습니다. PC 를 다시 시작한 뒤 설치.cmd 를 다시 실행하세요.' }
}
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
Say "[2/5] Python: $py" Green

# ---------- 3. 패키지 ----------
Say '필요한 패키지를 설치합니다...'
& $py -m pip install --disable-pip-version-check -q -r (Join-Path $InstallDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Fail '패키지 설치에 실패했습니다. 인터넷 연결을 확인하고 다시 실행하세요.' }
Say '[3/5] 패키지 설치 완료' Green

# ---------- 4. 한국어 OCR ----------
try {
    [void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    $hasKo = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | Where-Object { $_.LanguageTag -like 'ko*' }).Count -gt 0
} catch { $hasKo = $false }
if ($hasKo) {
    Say '[4/5] 한국어 글자 인식(OCR) 사용 가능' Green
} else {
    Say '[4/5] Windows 한국어 글자 인식(OCR) 기능이 없습니다 — 대화 수집·사진 확인에 필요합니다.' Yellow
    $ans = Read-Host '지금 설치할까요? 관리자 권한 확인 창이 뜹니다 (Y/N)'
    if ($ans -match '^[Yy]') {
        Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile', '-Command', 'Add-WindowsCapability -Online -Name "Language.OCR~~~ko-KR~0.0.1.0"'
        Say '한국어 OCR 설치를 요청했습니다. 앱이 글자를 못 읽으면 PC 를 다시 시작하세요.' Yellow
    } else {
        Say '나중에 설정 > 시간 및 언어 > 언어 > 한국어 > 옵션에서 설치할 수 있습니다.' Yellow
    }
}

# ---------- 5. 바로가기·실행 ----------
$app = Join-Path $InstallDir 'talkcloud\assistant_app.py'
if ($NoStart) {
    Say '[5/5] 시험 설치 — 바로가기·실행 생략' Green
} else {
    $ws = New-Object -ComObject WScript.Shell
    foreach ($dir in [Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop')) {
        $lnk = $ws.CreateShortcut((Join-Path $dir "$AppName.lnk"))
        $lnk.TargetPath = $pyw
        $lnk.Arguments = "-X utf8 `"$app`""
        $lnk.WorkingDirectory = Split-Path $app
        $lnk.Description = $AppName
        $lnk.Save()
    }
    Start-Process $pyw -ArgumentList '-X', 'utf8', "`"$app`"" -WorkingDirectory (Split-Path $app)
    Say '[5/5] 시작 메뉴·바탕화면 바로가기를 만들고 앱을 실행했습니다' Green
}

Say "`n설치가 끝났습니다. 다음 순서로 진행하세요:" Cyan
Say ' 1. 앱이 처음 수집할 과거 기간(기본 3개월)을 묻습니다.'
Say ' 2. 앱 설정 창에서 [Claude Code MCP 연결]을 누릅니다.'
if (-not (Get-Command claude -ErrorAction SilentlyContinue) -and -not (Test-Path "$HOME\.local\bin\claude.exe")) {
    Say '    (Claude Code 가 설치되어 있지 않습니다 — https://claude.com/claude-code 에서 먼저 설치하세요)' Yellow
}
Say " 3. 이 폴더에서 Claude Code 를 열고 '카카오톡 업무방 등록해 줘'라고 말합니다:"
Say "    $InstallDir"
Say ' (PC 카카오톡에 로그인되어 있어야 합니다. 앱은 알림 영역(트레이)에서 계속 실행됩니다)'
