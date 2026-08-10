# Add a UTF-8 BOM to .ps1 files that contain non-ASCII characters.
#
# Why this is needed:
#   Windows PowerShell 5.1 assumes a .ps1 file is in the system ANSI code page
#   unless it starts with a BOM. A UTF-8 script with Chinese comments therefore
#   gets decoded as GBK, which corrupts string literals and breaks parsing with
#   errors like "unexpected token '}'".
#
# Pure-ASCII scripts are left alone: no BOM keeps them clean and they parse fine.
#
# Usage:
#   & .\tools\add_ps1_bom.ps1 -Root .
param(
    [string]$Root = "."
)

$ErrorActionPreference = 'Stop'

$strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
$withBom = New-Object System.Text.UTF8Encoding($true)

Get-ChildItem -Path $Root -Recurse -File -Filter *.ps1 | ForEach-Object {
    $bytes = [System.IO.File]::ReadAllBytes($_.FullName)

    $hasBom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and
              $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
    $nonAscii = $false
    foreach ($b in $bytes) { if ($b -gt 127) { $nonAscii = $true; break } }

    if (-not $nonAscii) {
        Write-Host ("ascii    {0}" -f $_.Name)
        return
    }
    if ($hasBom) {
        Write-Host ("has bom  {0}" -f $_.Name)
        return
    }

    try { $text = $strictUtf8.GetString($bytes) }
    catch {
        Write-Warning ("not valid UTF-8, skipped: {0}" -f $_.Name)
        return
    }
    [System.IO.File]::WriteAllText($_.FullName, $text, $withBom)
    Write-Host ("bom added {0}" -f $_.Name)
}
