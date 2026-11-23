<#
.SYNOPSIS
사진 파일들의 글자를 Windows 내장 OCR(한국어)로 읽는다. 이 PC 안에서만 처리하고 아무것도 밖으로 보내지 않는다.

.DESCRIPTION
-ListFile: UTF-8, 한 줄에 이미지 경로 하나.  -OutFile: 결과를 한 줄에 하나씩 JSON 으로 덧붙인다
  {"path": ..., "text": ...}  또는  {"path": ..., "error": ...}
휴대폰 사진은 EXIF 방향대로 돌린 뒤 읽는다(옆으로 누운 문서 사진 대비).
#>
param([Parameter(Mandatory)][string]$ListFile, [Parameter(Mandatory)][string]$OutFile)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing, System.Runtime.WindowsRuntime
[void][Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
[void][Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
function Wait-WinRt($op, [Type]$t) { $asTask.MakeGenericMethod($t).Invoke($null, @($op)).GetAwaiter().GetResult() }
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('ko'))
if ($null -eq $engine) { throw 'Korean OCR language pack is not installed.' }
$maxDim = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
$utf8 = [Text.UTF8Encoding]::new($false)

function Read-Image([string]$path) {
    $img = [Drawing.Image]::FromFile($path)
    try {
        # EXIF 방향(0x0112) 반영
        if ($img.PropertyIdList -contains 0x0112) {
            $o = $img.GetPropertyItem(0x0112).Value[0]
            $flip = @{ 3 = 'Rotate180FlipNone'; 6 = 'Rotate90FlipNone'; 8 = 'Rotate270FlipNone' }[[int]$o]
            if ($flip) { $img.RotateFlip([Drawing.RotateFlipType]$flip) }
        }
        # 작은 사진은 키우고(글자 인식률), 큰 사진은 OCR 한도 안으로 줄인다
        $long = [Math]::Max($img.Width, $img.Height)
        $scale = if ($long -lt 1600) { [Math]::Min(2.0, 1600 / $long) } else { [Math]::Min(1.0, ($maxDim - 1) / $long) }
        $bmp = [Drawing.Bitmap]::new($img, [int]($img.Width * $scale), [int]($img.Height * $scale))
    } finally { $img.Dispose() }
    $stream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
    try {
        $s = [System.IO.WindowsRuntimeStreamExtensions]::AsStreamForWrite($stream)
        $bmp.Save($s, [Drawing.Imaging.ImageFormat]::Png); $s.Flush(); $stream.Seek(0)
        $dec = Wait-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $sb = Wait-WinRt ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        try { $res = Wait-WinRt ($engine.RecognizeAsync($sb)) ([Windows.Media.Ocr.OcrResult]) } finally { $sb.Dispose() }
        return (@($res.Lines | ForEach-Object { $_.Text }) -join "`n")
    } finally { $bmp.Dispose(); $stream.Dispose() }
}

foreach ($path in [IO.File]::ReadAllLines($ListFile, $utf8)) {
    if (-not $path.Trim()) { continue }
    try { $rec = [ordered]@{ path = $path; text = (Read-Image $path) } }
    catch { $rec = [ordered]@{ path = $path; error = $_.Exception.Message } }
    [IO.File]::AppendAllText($OutFile, (($rec | ConvertTo-Json -Compress) + "`n"), $utf8)
}
