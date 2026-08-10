# Normalize text files to UTF-8 (no BOM) + CRLF line endings.
#
# Why this exists:
#   1. On this machine some editing tools write files using the system ANSI code
#      page (GBK). Python 3 reads source as UTF-8, so a GBK-encoded file with
#      Chinese comments fails to parse. This script detects and repairs that.
#   2. The existing repository uses CRLF. New files should match so diffs stay
#      readable.
#
# Usage:
#   & .\tools\normalize_text.ps1 -Paths "src\a.py","docs\b.md"
#   & .\tools\normalize_text.ps1 -Root "app" -Filter "*.py"
param(
    [string[]]$Paths,
    [string]$Root,
    [string]$Filter = "*.py"
)

$ErrorActionPreference = 'Stop'

# Strict decoder: throws on invalid UTF-8 rather than substituting U+FFFD.
$strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
$gbk = [System.Text.Encoding]::GetEncoding(936)
$outUtf8 = New-Object System.Text.UTF8Encoding($false)

$targets = @()
if ($Paths) { $targets += $Paths }
if ($Root) {
    $targets += Get-ChildItem -Path $Root -Recurse -File -Filter $Filter |
        ForEach-Object { $_.FullName }
}
if (-not $targets) { Write-Warning "no files given"; return }

foreach ($p in $targets) {
    $full = (Resolve-Path -LiteralPath $p).Path
    $bytes = [System.IO.File]::ReadAllBytes($full)

    $isUtf8 = $true
    try { [void]$strictUtf8.GetString($bytes) } catch { $isUtf8 = $false }

    if ($isUtf8) {
        $text = $strictUtf8.GetString($bytes)
        $encNote = "utf-8"
    }
    else {
        $text = $gbk.GetString($bytes)
        if ($text.IndexOf([char]0xFFFD) -ge 0) {
            Write-Warning "undecodable as UTF-8 or GBK, skipped: $p"
            continue
        }
        $encNote = "gbk->utf-8"
    }

    # Collapse to LF first so mixed endings are handled, then expand to CRLF.
    $normalized = ($text -replace "`r`n", "`n") -replace "`n", "`r`n"

    $changed = ($normalized -ne $text) -or (-not $isUtf8)
    if ($changed) {
        [System.IO.File]::WriteAllText($full, $normalized, $outUtf8)
        Write-Host ("fixed   [{0,-10}] {1}" -f $encNote, $p)
    }
    else {
        Write-Host ("ok      [{0,-10}] {1}" -f $encNote, $p)
    }
}
