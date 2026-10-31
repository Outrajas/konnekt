#requires -Version 5.1
<#
.SYNOPSIS
    Live end-to-end run of leadforge with the Tier-3 headed browser.

.DESCRIPTION
    Turns the browser tier on for this PowerShell session only, then runs the
    full chain per board: discover -> people -> email -> draft.

    The first navigation opens a real Chrome window using the persistent profile
    in data/browser-profile. If a platform asks you to sign in or shows a
    challenge, do it in that window: the fetcher waits (browser_manual_challenge_wait_s)
    and continues once the page clears. Nothing is typed or stored by the code.

    No command sends email. Drafts and review.csv are written for you to send.

.PARAMETER Boards
    Boards to run. Defaults to the three browser-heavy platforms plus two that
    already work over plain HTTP.

.PARAMETER Profile
    Profile track (ai_ml / swe / data_analyst).

.PARAMETER DryRun
    Switches on -DryRun for every stage: no network, no LLM, no browser.

.EXAMPLE
    .\scripts\live_run.ps1
    .\scripts\live_run.ps1 -Boards linkedin,indeed -Profile swe
    .\scripts\live_run.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [string[]]$Boards = @("linkedin", "indeed", "glassdoor", "wellfound", "instahyre"),
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
if (-not (Test-Path $exe)) {
    throw "leadforge.exe not found at $exe - create .venv and 'pip install -e .[dev]' first."
}

# Tier 3 is opt-in per process; this scopes it to this window only.
$env:LEADFORGE_BROWSER_ENABLED = "true"

Write-Host "=== leadforge live run ===" -ForegroundColor Cyan
Write-Host "repo:    $repo"
Write-Host "profile: $Profile"
Write-Host "boards:  $($Boards -join ', ')"
Write-Host "browser: $env:LEADFORGE_BROWSER_ENABLED (headed profile in data/browser-profile)"
Write-Host "dry-run: $([bool]$DryRun)"
Write-Host ""

& $exe doctor
if ($LASTEXITCODE -ne 0) { throw "doctor failed" }

$common = @("--profile", $Profile, "--max-companies", $MaxCompanies,
            "--max-people", $MaxPeople, "--max-drafts", $MaxDrafts)
if ($DryRun) { $common += "--dry-run" }

foreach ($board in $Boards) {
    Write-Host ""
    Write-Host "=== board: $board ===" -ForegroundColor Cyan
    & $exe e2e @common --board $board
    if ($LASTEXITCODE -ne 0) {
        Write-Host "board '$board' exited $LASTEXITCODE - continuing with the next board" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "=== summary ===" -ForegroundColor Cyan
& $exe report --since 7
& $exe export-run-summary
Write-Host ""
Write-Host "Drafts: data/output/drafts/  |  Review: data/output/review.csv" -ForegroundColor Green
Write-Host "Nothing was sent. Approve and send manually." -ForegroundColor Green
