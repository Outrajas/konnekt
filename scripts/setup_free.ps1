<#
scripts/setup_free.ps1
Configure .env for free / personal-email / zero-cost operation.
Idempotent. Creates .env.bak.<YYYYMMDD> once per day.

Usage:
  .\scripts\setup_free.ps1
  .\scripts\setup_free.ps1 -Mode off          # disable SMTP entirely
  .\scripts\setup_free.ps1 -Mode null_sender  # default: null-sender SMTP
  .\scripts\setup_free.ps1 -Force             # rewrite even if unchanged
#>
param(
    [string]$EnvFile = ".env",
    [ValidateSet("null_sender","off","domain")]
    [string]$Mode = "null_sender",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$utf8NoBom = New-Object System.Text.UTF8Encoding $false

if (-not (Test-Path "pyproject.toml")) {
    Write-Host "ERROR: run from repo root (where pyproject.toml lives)" -ForegroundColor Red
    Write-Host "  current: $(Get-Location)" -ForegroundColor DarkGray
    exit 1
}

if (-not (Test-Path $EnvFile)) {
    Write-Host "ERROR: $EnvFile not found" -ForegroundColor Red
    exit 1
}

# Daily backup
$stamp  = Get-Date -Format "yyyyMMdd"
$backup = "$EnvFile.bak.$stamp"
if (-not (Test-Path $backup)) {
    Copy-Item $EnvFile $backup
    Write-Host "Backed up $EnvFile -> $backup" -ForegroundColor Cyan
    Write-Host "  (this backup holds your secrets; delete when no longer needed)" -ForegroundColor DarkGray
}

$desired = [ordered]@{
    "LEADFORGE_SMTP_VERIFY_MODE"    = $Mode
    "LEADFORGE_SMTP_TIMEOUT"        = "10.0"
    "LEADFORGE_SMTP_MAX_CONCURRENT" = "3"
}

$lines = [System.IO.File]::ReadAllLines((Join-Path $PWD $EnvFile))
$seen  = @{}
$out   = New-Object System.Collections.Generic.List[string]

foreach ($line in $lines) {
    $m = [regex]::Match($line, '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')
    if (-not $m.Success) { $out.Add($line); continue }
    $key = $m.Groups[1].Value
    if (-not $desired.Contains($key)) { $out.Add($line); continue }
    $old = $m.Groups[2].Value.Trim().Trim('"').Trim("'")
    $new = $desired[$key]
    if ($old -ne $new -or $Force) {
        $out.Add("$key=$new")
        if ($old) { Write-Host "  updated $key : '$old' -> '$new'" -ForegroundColor Yellow }
        else      { Write-Host "  updated $key : (empty) -> '$new'" -ForegroundColor Yellow }
    } else {
        $out.Add($line)
    }
    $seen[$key] = $true
}
foreach ($key in $desired.Keys) {
    if (-not $seen.ContainsKey($key)) {
        $out.Add("$key=$($desired[$key])")
        Write-Host "  added $key = $($desired[$key])" -ForegroundColor Yellow
    }
}

[System.IO.File]::WriteAllText((Join-Path $PWD $EnvFile), (($out -join "`r`n") + "`r`n"), $utf8NoBom)

# Mirror documentation into .env.example (no secrets there)
$exampleFile = ".env.example"
if (Test-Path $exampleFile) {
    $exampleLines = [System.IO.File]::ReadAllLines((Join-Path $PWD $exampleFile))
    $hasMode = $false
    foreach ($ln in $exampleLines) { if ($ln -match '^\s*(LEADFORGE_)?SMTP_VERIFY_MODE\s*=') { $hasMode = $true } }
    if (-not $hasMode) {
        $extra = @(
            "",
            "# SMTP verification mode:",
            "#   null_sender = free, no domain needed (MAIL FROM:<>)",
            "#   off         = skip SMTP entirely (pattern-only confidence)",
            "#   domain      = requires SMTP_MAIL_FROM on a domain you own",
            "LEADFORGE_SMTP_VERIFY_MODE=null_sender"
        )
        [System.IO.File]::AppendAllText((Join-Path $PWD $exampleFile), ("`r`n" + ($extra -join "`r`n") + "`r`n"), $utf8NoBom)
        Write-Host "  documented SMTP_VERIFY_MODE in $exampleFile" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "SMTP config (names only):" -ForegroundColor Cyan
[System.IO.File]::ReadAllLines((Join-Path $PWD $EnvFile)) | ForEach-Object {
    if ($_ -match '^\s*((LEADFORGE_)?SMTP_[A-Z_]+)\s*=') { Write-Host "  $($Matches[1])" }
}

# Sanity
$hasMailFrom = $false
[System.IO.File]::ReadAllLines((Join-Path $PWD $EnvFile)) | ForEach-Object {
    if ($_ -match '^\s*(LEADFORGE_)?SMTP_MAIL_FROM\s*=\s*.+$') { $hasMailFrom = $true }
}
if ($Mode -eq "domain" -and -not $hasMailFrom) {
    Write-Host ""
    Write-Host "WARNING: mode 'domain' but SMTP_MAIL_FROM is not set" -ForegroundColor Yellow
    Write-Host "  enrich will refuse until a real owned-domain address is set" -ForegroundColor Yellow
}
if ($Mode -eq "null_sender" -and $hasMailFrom) {
    Write-Host ""
    Write-Host "INFO: SMTP_MAIL_FROM is set but mode is null_sender; MAIL FROM will be ignored" -ForegroundColor DarkGray
}
Write-Host ""
Write-Host "Done. Mode = $Mode" -ForegroundColor Green