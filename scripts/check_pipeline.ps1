<#
scripts/check_pipeline.ps1
Single cumulative pipeline check -- runs the entire timeline.

Checks (12):
   1. python
   2. leadforge CLI
   3. imports
   4. SMTP config
   5. .env.example present
   6. pytest
   7. ruff check
   8. mypy
   9. tracked-file hygiene
  10. offline replay end-to-end
  11. profiles schema
  12. profile CLI (list + show, list runs)

Usage:
  .\scripts\check_pipeline.ps1
  .\scripts\check_pipeline.ps1 -Quick
#>
param(
    [switch]$Quick
)

$ErrorActionPreference = "Continue"
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
$script:Checks = New-Object System.Collections.Generic.List[object]

function Add-Check {
    param([string]$Name, [bool]$Pass, [string]$Detail = "")
    $script:Checks.Add([pscustomobject]@{ Name = $Name; Pass = $Pass; Detail = $Detail })
    $color = if ($Pass) { "Green" } else { "Red" }
    $icon  = if ($Pass) { "PASS" } else { "FAIL" }
    Write-Host ("  [{0}] {1}" -f $icon, $Name) -ForegroundColor $color -NoNewline
    if ($Detail) { Write-Host (" - " + $Detail) -ForegroundColor DarkGray } else { Write-Host "" }
}

function Parse-Pytest {
    param([string]$Output)
    $res = @{ Passed = 0; Failed = 0; Errors = 0 }
    if (-not $Output) { return $res }
    $clean = $Output -replace "`e\[[0-9;]*m", ""
    if ($clean -match '(\d+)\s+passed')  { $res.Passed = [int]$Matches[1] }
    if ($clean -match '(\d+)\s+failed')  { $res.Failed = [int]$Matches[1] }
    if ($clean -match '(\d+)\s+errors?') { $res.Errors = [int]$Matches[1] }
    return $res
}

if (-not (Test-Path "pyproject.toml")) {
    Write-Host "ERROR: run from repo root (where pyproject.toml lives)" -ForegroundColor Red
    exit 1
}

$py = ".\.venv\Scripts\python.exe"
$lf = ".\.venv\Scripts\leadforge.exe"

Write-Host ""
Write-Host "=== konnekt pipeline check ===" -ForegroundColor Cyan
Write-Host "  repo: $(Get-Location)" -ForegroundColor DarkGray
Write-Host ""

New-Item -ItemType Directory -Force -Path ".diag" | Out-Null

# 1. Python
if (Test-Path $py) {
    $v = (& $py --version 2>&1 | Out-String).Trim()
    Add-Check "python" $true $v
} else {
    Add-Check "python" $false ".venv\Scripts\python.exe missing"
    exit 1
}

# 2. leadforge CLI
if (Test-Path $lf) { Add-Check "leadforge CLI" $true "" }
else              { Add-Check "leadforge CLI" $false "not installed" }

# 3. Imports
$imports = & $py -c "import sys; sys.path.insert(0,'src'); import importlib.util; mods=['leadforge','leadforge.cli','leadforge.config','leadforge.models','leadforge.db','leadforge.pipeline','leadforge.profiles','leadforge.web.fetcher','leadforge.web.policy','leadforge.tools.smtp_verify','leadforge.tools.email_patterns','leadforge.llm.client']; miss=[m for m in mods if importlib.util.find_spec(m) is None]; print('OK' if not miss else 'MISS:'+','.join(miss))" 2>&1
$importsStr = ($imports | Out-String).Trim()
if ($importsStr -like "OK*") { Add-Check "imports" $true "" }
else                         { Add-Check "imports" $false $importsStr }

# 4. SMTP config valid
$envOk = $false; $envDetail = "no .env"
if (Test-Path ".env") {
    $mode = ""
    foreach ($ln in [System.IO.File]::ReadAllLines((Join-Path $PWD ".env"))) {
        if ($ln -match '^\s*(LEADFORGE_)?SMTP_VERIFY_MODE\s*=\s*(.+)$') {
            $mode = $Matches[2].Trim().Trim('"').Trim("'")
        }
    }
    if ($mode -in @("off","null_sender","domain")) { $envOk = $true; $envDetail = "mode = $mode" }
    else                                          { $envDetail = "mode = '$mode' (run .\scripts\setup_free.ps1)" }
}
Add-Check "SMTP config" $envOk $envDetail

# 5. .env.example present
if (Test-Path ".env.example") { Add-Check ".env.example present" $true "" }
else                          { Add-Check ".env.example present" $false "missing (git restore .env.example)" }

if (-not $Quick) {
    # 6. pytest
    $pytestLog = ".diag\pytest_last.log"
    & $py -m pytest -q --tb=line -p no:cacheprovider 2>&1 | Out-File -FilePath $pytestLog -Encoding utf8
    $pytestExit = $LASTEXITCODE
    $pytestOut = ""
    if (Test-Path $pytestLog) { $pytestOut = (Get-Content $pytestLog -Raw) }
    $p = Parse-Pytest -Output $pytestOut

    if ($pytestExit -eq 0) {
        $detail = if ($p.Passed -gt 0) { "$($p.Passed) passed" } else { "all passed (exit 0)" }
        Add-Check "pytest" $true $detail
    } elseif ($pytestExit -eq 5) {
        Add-Check "pytest" $false "no tests collected (exit 5)"
    } else {
        $detail = if ($p.Failed -gt 0 -or $p.Errors -gt 0) {
            "passed=$($p.Passed) failed=$($p.Failed) errors=$($p.Errors) (exit $pytestExit)"
        } else {
            "exit $pytestExit (see $pytestLog)"
        }
        Add-Check "pytest" $false $detail
        ($pytestOut -split "`r?`n") | Where-Object { $_ -match '(FAILED|ERROR|Traceback)' } | Select-Object -First 10 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkRed }
    }

    # 7. ruff
    $ruffOut = & $py -m ruff check src tests 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) { Add-Check "ruff check" $true "" }
    else {
        Add-Check "ruff check" $false "issues"
        ($ruffOut -split "`r?`n") | Select-Object -First 8 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkRed }
    }

    # 8. mypy
    $mypyOut = & $py -m mypy src/leadforge 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) { Add-Check "mypy" $true "" }
    else                     { Add-Check "mypy" $false "type issues (non-blocking)" }

    # 9. tracked-file hygiene
    $hygiene = "scripts\check_tracked_files.py"
    if (Test-Path $hygiene) {
        $hOut = & $py $hygiene 2>&1 | Out-String
        if ($LASTEXITCODE -eq 0) { Add-Check "tracked files" $true "" }
        else {
            Add-Check "tracked files" $false "issues"
            ($hOut -split "`r?`n") | Select-Object -First 5 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkRed }
        }
    }

    # 10. Offline replay end-to-end
    $replayFixtures = "data/fixtures/sample"
    $replayContacts = "tests/fixtures/sample_contacts.csv"
    if ((Test-Path $replayFixtures) -and (Test-Path $replayContacts)) {
        $replayOut = & $lf enrich $replayContacts --replay $replayFixtures --force 2>&1 | Out-String
        if ($LASTEXITCODE -eq 0) {
            Add-Check "offline replay" $true "pipeline ran end-to-end"
        } else {
            Add-Check "offline replay" $false "exit $LASTEXITCODE"
            ($replayOut -split "`r?`n") | Select-Object -Last 10 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkRed }
        }
    } else {
        Add-Check "offline replay" $false "fixtures missing"
    }

    # 11. Profiles schema
    $checkProfiles = "scripts\check_profiles.py"
    if (Test-Path $checkProfiles) {
        $cpOut = (& $py $checkProfiles 2>&1 | Out-String).Trim()
        if ($cpOut -like "SKIP:*") {
            Add-Check "profiles schema" $true ($cpOut -replace "^SKIP:\s*", "")
        } elseif ($cpOut -like "OK*") {
            Add-Check "profiles schema" $true "OK"
        } else {
            Add-Check "profiles schema" $false "see detail"
            ($cpOut -split "`r?`n") | Select-Object -First 8 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkRed }
        }
    }

    # 12. Profile CLI
    if (Test-Path $lf) {
        $helpOut = & $lf profile --help 2>&1 | Out-String
        $hasList = $helpOut -match '\blist\b'
        $hasShow = $helpOut -match '\bshow\b'
        if ($hasList -and $hasShow) {
            & $lf profile list 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Add-Check "profile CLI" $true "list + show available, list runs"
            } else {
                Add-Check "profile CLI" $false "list runs with exit $LASTEXITCODE"
            }
        } else {
            Add-Check "profile CLI" $false "missing (list=$hasList, show=$hasShow)"
        }
    }
} else {
    Write-Host "  (skipping pytest/ruff/mypy/replay/profiles - Quick mode)" -ForegroundColor DarkGray
}

    # 13. profile ingest --track flag
    if (Test-Path $lf) {
        $ingestHelp = & $lf profile ingest --help 2>&1 | Out-String
        if ($ingestHelp -match '--track') {
            Add-Check "profile ingest --track" $true "flag available"
        } else {
            Add-Check "profile ingest --track" $false "flag missing from help"
        }
    }

    # 14. discover --profile flag
    if (Test-Path $lf) {
        $discoverHelp = & $lf discover --help 2>&1 | Out-String
        if ($discoverHelp -match '--profile') {
            Add-Check "discover --profile" $true "flag available"
        } else {
            Add-Check "discover --profile" $false "flag missing from help"
        }
    }

    # 15. job intel (extraction + schema)
    $checkJobIntel = "scripts\check_job_intel.py"
    if (Test-Path $checkJobIntel) {
        $jiOut = (& $py $checkJobIntel 2>&1 | Out-String).Trim()
        if ($jiOut -like "SKIP:*") {
            Add-Check "job intel" $true ($jiOut -replace "^SKIP:\s*", "")
        } elseif ($jiOut -like "OK*") {
            Add-Check "job intel" $true "OK"
        } else {
            Add-Check "job intel" $false "see detail"
            ($jiOut -split "`r?`n") | Select-Object -First 8 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkRed }
        }
    }

    # 16. migrations framework
    $checkMig = "scripts\check_migrations.py"
    if (Test-Path $checkMig) {
        $migOut = (& $py $checkMig 2>&1 | Out-String).Trim()
        if ($migOut -like "OK*") {
            Add-Check "migrations" $true $migOut
        } else {
            Add-Check "migrations" $false "see detail"
            ($migOut -split "`r?`n") | Select-Object -First 8 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkRed }
        }
    }

# Summaryost ""
$pass  = @($script:Checks | Where-Object { $_.Pass }).Count
$fail  = @($script:Checks | Where-Object { -not $_.Pass }).Count
$total = $pass + $fail

$sumLines = @()
$sumLines += "konnekt pipeline check " + (Get-Date -Format "yyyyMMdd_HHmmss")
$sumLines += "repo: $(Get-Location)"
$sumLines += ""
foreach ($c in $script:Checks) {
    $status = if ($c.Pass) { "PASS" } else { "FAIL" }
    $sumLines += ("[{0}] {1} {2}" -f $status, $c.Name, $c.Detail)
}
$sumLines += ""
$sumLines += ("Result: {0}/{1} passed, {2} failed" -f $pass, $total, $fail)
[System.IO.File]::WriteAllText("$PWD\.diag\last_check.txt", ($sumLines -join "`r`n"), $utf8NoBom)

if ($fail -eq 0) {
    Write-Host "=== ALL $total CHECKS PASSED ===" -ForegroundColor Green
    Write-Host "Summary: .diag\last_check.txt" -ForegroundColor DarkGray
    exit 0
} else {
    Write-Host "=== $pass/$total PASSED, $fail FAILED ===" -ForegroundColor Red
    Write-Host ""
    $script:Checks | Where-Object { -not $_.Pass } | ForEach-Object {
        Write-Host "  - $($_.Name): $($_.Detail)" -ForegroundColor Red
    }
    Write-Host ""
    Write-Host "Summary: .diag\last_check.txt" -ForegroundColor DarkGray
    exit 1
}