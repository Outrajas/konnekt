#requires -Version 5.1
<#
.SYNOPSIS
    Run the complete enrichment pipeline with live terminal output and a
    headed browser, without sending email.

.DESCRIPTION
    Runs setup -> domain resolution -> India job discovery -> job enrichment ->
    public-web research -> people -> email evidence -> drafts -> review CSV.
    The browser is visible and uses data/browser-profile. Challenges remain
    manual. No send command is invoked.

.EXAMPLE
    .\scripts\live_enrichment.ps1
    .\scripts\live_enrichment.ps1 -Profile ai_ml -MaxCompanies 5
    .\scripts\live_enrichment.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [string]$Profile = "ai_ml",
    [int]$MaxCompanies = 3,
    [int]$MaxPeople = 5,
    [int]$MaxDrafts = 5,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$exe = Join-Path $repo ".venv\Scripts\leadforge.exe"
if (Test-Path $exe) {
    $leadforge = $exe
    $leadforgePrefix = @()
} else {
    $leadforge = "py"
    $leadforgePrefix = @("-3.12", "-m", "leadforge.cli")
    Write-Host "No local .venv found; using the active Python 3.12 installation." -ForegroundColor Yellow
}

$oldBrowserEnabled = $env:LEADFORGE_BROWSER_ENABLED
$oldBrowserHeaded = $env:LEADFORGE_BROWSER_HEADED
$env:LEADFORGE_BROWSER_ENABLED = "true"
$env:LEADFORGE_BROWSER_HEADED = "true"

try {
    Write-Host "=== LeadForge live enrichment ===" -ForegroundColor Cyan
    Write-Host "repo:        $repo"
    Write-Host "profile:     $Profile"
    Write-Host "browser:     headed, data/browser-profile"
    Write-Host "mail:        disabled; no send command is invoked"
    Write-Host "trajectory:  data/cache/leadforge.db"
    Write-Host ""

    if ($DryRun) {
        & $leadforge @leadforgePrefix doctor
    } else {
        & $leadforge @leadforgePrefix doctor --require-browser
    }
    if ($LASTEXITCODE -ne 0) {
        throw "leadforge doctor failed"
    }

    $args = @(
        "run-all",
        "--profile", $Profile,
        "--max-companies", $MaxCompanies,
        "--max-people", $MaxPeople,
        "--max-drafts", $MaxDrafts,
        "--live"
    )
    if ($DryRun) {
        $args += "--dry-run"
    }

    Write-Host "Starting live pipeline. Watch this terminal and the browser window." -ForegroundColor Green
    & $leadforge @leadforgePrefix @args
    if ($LASTEXITCODE -ne 0) {
        throw "live enrichment failed with exit code $LASTEXITCODE"
    }

    Write-Host ""
    Write-Host "=== persisted evidence ===" -ForegroundColor Cyan
    & $leadforge @leadforgePrefix report --since 7
    if ($LASTEXITCODE -ne 0) {
        Write-Host "report unavailable; pipeline output remains above." -ForegroundColor Yellow
    }

    Write-Host ""
    Write-Host "Review CSV and drafts are under data/output." -ForegroundColor Green
    Write-Host "Nothing was sent. Approval and sending remain manual." -ForegroundColor Green
}
finally {
    if ($null -eq $oldBrowserEnabled) {
        Remove-Item Env:LEADFORGE_BROWSER_ENABLED -ErrorAction SilentlyContinue
    } else {
        $env:LEADFORGE_BROWSER_ENABLED = $oldBrowserEnabled
    }
    if ($null -eq $oldBrowserHeaded) {
        Remove-Item Env:LEADFORGE_BROWSER_HEADED -ErrorAction SilentlyContinue
    } else {
        $env:LEADFORGE_BROWSER_HEADED = $oldBrowserHeaded
    }
}
