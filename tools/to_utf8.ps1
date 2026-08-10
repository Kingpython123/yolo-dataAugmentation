# Convert GBK/ANSI encoded text files to UTF-8 (no BOM).
# Files that are already valid UTF-8 are left untouched.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\to_utf8.ps1 -Paths "a.md","src\b.py"
param(
    [Parameter(Mandatory = $true)]
    [string[]]$Paths
)

$ErrorActionPreference = 'Stop'

# Strict UTF-8 decoder: throws on invalid byte sequences instead of substituting.
$strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
$gbk = [System.Text.Encoding]::GetEncoding(936)
$outUtf8 = New-Object System.Text.UTF8Encoding($false)

foreach ($p in $Paths) {
    $full = (Resolve-Path -LiteralPath $p).Path
    $bytes = [System.IO.File]::ReadAllBytes($full)

    $isUtf8 = $true
    try { [void]$strictUtf8.GetString($bytes) } catch { $isUtf8 = $false }

    if ($isUtf8) {
        Write-Host "utf-8 already : $p"
        continue
    }

    $text = $gbk.GetString($bytes)
    if ($text.IndexOf([char]0xFFFD) -ge 0) {
        Write-Warning "undecodable as GBK, skipped: $p"
        continue
    }

    [System.IO.File]::WriteAllText($full, $text, $outUtf8)
    Write-Host "converted     : $p"
}
