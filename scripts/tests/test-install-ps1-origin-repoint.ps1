# Behavioral tests for install.ps1's origin-repoint logic (Install-Repository).
#
# A managed Windows checkout still pointed at the upstream repository (any
# install that predates the release-repo switch, or one a customer cloned by
# hand) must self-heal: Install-Repository's update path detects the known
# upstream identity, repoints origin to the release repository (SSH then
# HTTPS), widens the single-branch refspec `git clone --branch` leaves
# behind, and fails loudly -- not silently -- if the release repo still
# cannot be reached. See tests/test_install_origin_repoint_mechanics.py for
# the underlying git mechanism proven in isolation (why set-url alone is not
# enough), and tests/test_install_sh_origin_repoint.py for the same
# guarantees on the shell installer.
#
# Run from a PowerShell prompt:
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/tests/test-install-ps1-origin-repoint.ps1
#
# These run install.ps1's REAL repository stage as a subprocess
# (`-Stage repository -NonInteractive -InstallDir ... -HermesHome ...`),
# against LOCAL bare git repositories standing in for both "upstream" and
# "the release repository" -- no network to the real xdataplusx/trix-agent
# repo needed, and no dependency on whether it exists yet or has a "release"
# branch. $RepoUrlSsh/$RepoUrlHttps are hardcoded script-scope constants in
# install.ps1 (not CLI parameters), so the scenarios that need to control
# what origin gets repointed TO run against a TEMP COPY of install.ps1 with
# just those two lines substituted -- same spirit as
# tests/test_install_sh_origin_repoint.py overriding install.sh's
# REPO_URL_SSH/REPO_URL_HTTPS shell variables before sourcing clone_repo(),
# adapted for a script that doesn't expose them as parameters. Every other
# line is untouched, so this exercises the real, shipped Install-Repository
# logic end to end. Scenarios that never touch those two constants (origin
# already correct, a third-party origin) run against the real, unmodified
# install.ps1 directly.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$installScript = Join-Path $repoRoot "scripts\install.ps1"

if (-not (Test-Path $installScript)) {
    throw "Could not locate install.ps1 at $installScript"
}

# Run the installer under WHICHEVER host is running this harness (pwsh 7 or
# Windows PowerShell 5.1) -- a hardcoded `powershell` here would make the CI
# workflow's "pwsh 7" step exercise install.ps1 under 5.1 too, silently
# proving nothing about the other host. Same idiom
# test-install-ps1-longpath.ps1's Invoke-Normalization uses.
$psExe = (Get-Process -Id $PID).Path

$failures = 0
function Assert-Equal {
    param($Expected, $Actual, [string]$Label)
    if ($Expected -ne $Actual) {
        Write-Host "FAIL: $Label" -ForegroundColor Red
        Write-Host "  expected: $Expected"
        Write-Host "  actual:   $Actual"
        $script:failures++
    } else {
        Write-Host "OK: $Label" -ForegroundColor Green
    }
}
function Assert-True {
    param($Condition, [string]$Label)
    if (-not $Condition) {
        Write-Host "FAIL: $Label" -ForegroundColor Red
        $script:failures++
    } else {
        Write-Host "OK: $Label" -ForegroundColor Green
    }
}
function Assert-Contains {
    param([string]$Haystack, [string]$Needle, [string]$Label)
    Assert-True (($Haystack -replace "`r", "") -like "*$Needle*") $Label
}
function Assert-NotContains {
    param([string]$Haystack, [string]$Needle, [string]$Label)
    Assert-True (-not (($Haystack -replace "`r", "") -like "*$Needle*")) $Label
}
function Normalize-RepoPath {
    # git may echo a `remote get-url` path back with different separators
    # than the string we built it from (backslash vs. forward slash) --
    # normalize both sides before comparing so this isn't a spurious
    # Windows-only failure unrelated to what's actually under test.
    param([string]$Path)
    return ($Path -replace '\\', '/')
}
function Assert-SamePath {
    param([string]$Expected, [string]$Actual, [string]$Label)
    Assert-Equal (Normalize-RepoPath $Expected) (Normalize-RepoPath $Actual) $Label
}

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

$scratchRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("hermes-ps1-origin-repoint-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $scratchRoot | Out-Null

function Invoke-Git {
    # Windows PowerShell 5.1 wraps ANY stderr from a native command in a
    # NativeCommandError record under EAP=Stop, and git routinely writes
    # routine progress ("Cloning into...", branch-tracking notes, push
    # summaries) to stderr -- so this would throw spuriously on every other
    # call without relaxing EAP first. Same pattern install.ps1 itself uses
    # (Invoke-NativeWithRelaxedErrorAction) and
    # scripts/tests/test-install-ps1-longpath.ps1's Invoke-Normalization.
    param([string]$WorkDir, [string[]]$GitArgs)
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $global:LASTEXITCODE = 0
    try {
        $out = & git -C $WorkDir -c user.email=t@t -c user.name=t @GitArgs 2>&1
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    if ($LASTEXITCODE -ne 0) {
        throw "git $($GitArgs -join ' ') failed in $WorkDir (exit $LASTEXITCODE): $out"
    }
    return $out
}

function New-BareRepo {
    # A bare repo with one commit on $Branch, for use as a fake remote.
    param([string]$Path, [string]$Branch, [string]$Content)
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
    Invoke-Git -WorkDir $scratchRoot -GitArgs @("init", "-q", "--bare", $Path) | Out-Null

    $seed = Join-Path $scratchRoot ("seed-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $seed | Out-Null
    Invoke-Git -WorkDir $seed -GitArgs @("init", "-q") | Out-Null
    Set-Content -LiteralPath (Join-Path $seed "f.txt") -Value $Content -NoNewline
    Invoke-Git -WorkDir $seed -GitArgs @("add", "f.txt") | Out-Null
    Invoke-Git -WorkDir $seed -GitArgs @("commit", "-qm", "seed") | Out-Null
    Invoke-Git -WorkDir $seed -GitArgs @("branch", "-M", $Branch) | Out-Null
    Invoke-Git -WorkDir $seed -GitArgs @("remote", "add", "origin", $Path) | Out-Null
    Invoke-Git -WorkDir $seed -GitArgs @("push", "-q", "-u", "origin", $Branch) | Out-Null
    return $Path
}

function New-FakeUpstreamPath {
    # A path that literally contains the forward-slash substring
    # "NousResearch/hermes-agent", so install.ps1's `-like
    # "*NousResearch/hermes-agent*"` repoint condition matches it -- without
    # touching a real host.
    param([string]$CaseName)
    $base = Join-Path $scratchRoot $CaseName
    New-Item -ItemType Directory -Force -Path $base | Out-Null
    return (Join-Path $base "NousResearch") + "/hermes-agent"
}

$installScriptText = Get-Content -Raw -LiteralPath $installScript
$originalSshLine = '$RepoUrlSsh = "git@github.com:xdataplusx/trix-agent.git"'
$originalHttpsLine = '$RepoUrlHttps = "https://github.com/xdataplusx/trix-agent.git"'
if (($installScriptText.IndexOf($originalSshLine) -lt 0) -or ($installScriptText.IndexOf($originalHttpsLine) -lt 0)) {
    throw "install.ps1's `$RepoUrlSsh/`$RepoUrlHttps constant lines have changed shape; update this test's substitution strings to match."
}

function New-PatchedInstallScript {
    # A temp copy of the REAL install.ps1 with only $RepoUrlSsh/$RepoUrlHttps
    # replaced, so the scenario below controls where a repoint lands without
    # touching a real host. Every other line -- including the repoint logic
    # itself -- is the actual shipped source.
    param([string]$SshUrl, [string]$HttpsUrl)
    $patchedSshLine = '$RepoUrlSsh = "' + $SshUrl + '"'
    $patchedHttpsLine = '$RepoUrlHttps = "' + $HttpsUrl + '"'
    $patched = $installScriptText.Replace($originalSshLine, $patchedSshLine).Replace($originalHttpsLine, $patchedHttpsLine)
    $path = Join-Path $scratchRoot ("install-" + [Guid]::NewGuid().ToString("N") + ".ps1")
    Set-Content -LiteralPath $path -Value $patched -NoNewline -Encoding ascii
    return $path
}

function Invoke-RepositoryStage {
    # Same EAP=Continue guard as Invoke-Git, and for the same reason: the
    # child install.ps1 process's own stderr diagnostics (Write-Warn/
    # Write-Err use Write-Host, which is fine, but native git calls inside
    # it can still surface here) must not become a terminating error in
    # THIS script under PS 5.1.
    param([string]$ScriptPath, [string]$InstallDir, [string]$HermesHome)
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $global:LASTEXITCODE = 0
    try {
        $out = & $psExe -NoProfile -ExecutionPolicy Bypass -File $ScriptPath `
            -Stage repository -NonInteractive -InstallDir $InstallDir -HermesHome $HermesHome 2>&1
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = ($out -join "`n") }
}

# -----------------------------------------------------------------------------
# Scenario A: wrong origin (upstream) is repointed and the update succeeds
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "-- Wrong origin is repointed and update succeeds --"
$releaseA = New-BareRepo -Path (Join-Path $scratchRoot "release-a.git") -Branch "release" -Content "release content`n"
$upstreamA = New-BareRepo -Path (New-FakeUpstreamPath "case-a") -Branch "main" -Content "upstream content`n"
$managedA = Join-Path $scratchRoot "managed-a"
Invoke-Git -WorkDir $scratchRoot -GitArgs @("clone", "-q", "--depth", "1", "--branch", "main", $upstreamA, $managedA) | Out-Null

$patchedA = New-PatchedInstallScript -SshUrl $releaseA -HttpsUrl $releaseA
$resultA = Invoke-RepositoryStage -ScriptPath $patchedA -InstallDir $managedA -HermesHome (Join-Path $scratchRoot "hermes-home-a")

Assert-Equal 0 $resultA.ExitCode "wrong-origin: repository stage exits 0"
Assert-Contains $resultA.Output "repointed to" "wrong-origin: prints repoint confirmation"
$originA = (Invoke-Git -WorkDir $managedA -GitArgs @("remote", "get-url", "origin")) -join ""
Assert-SamePath $releaseA $originA "wrong-origin: origin now points at the release repo"
$contentA = Get-Content -Raw -LiteralPath (Join-Path $managedA "f.txt")
Assert-Equal "release content`n" $contentA "wrong-origin: working tree now has release content"

# -----------------------------------------------------------------------------
# Scenario B: SSH release URL unreachable -> falls back to the HTTPS one
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "-- SSH unreachable falls back to the HTTPS release URL --"
$releaseB = New-BareRepo -Path (Join-Path $scratchRoot "release-b.git") -Branch "release" -Content "release content b`n"
$upstreamB = New-BareRepo -Path (New-FakeUpstreamPath "case-b") -Branch "main" -Content "upstream b`n"
$managedB = Join-Path $scratchRoot "managed-b"
Invoke-Git -WorkDir $scratchRoot -GitArgs @("clone", "-q", "--depth", "1", "--branch", "main", $upstreamB, $managedB) | Out-Null

$bogusSsh = Join-Path $scratchRoot "does-not-exist-ssh-target"
$patchedB = New-PatchedInstallScript -SshUrl $bogusSsh -HttpsUrl $releaseB
$resultB = Invoke-RepositoryStage -ScriptPath $patchedB -InstallDir $managedB -HermesHome (Join-Path $scratchRoot "hermes-home-b")

Assert-Equal 0 $resultB.ExitCode "ssh-unreachable: repository stage exits 0"
Assert-Contains $resultB.Output "SSH unreachable, using HTTPS instead" "ssh-unreachable: prints fallback message"
$originB = (Invoke-Git -WorkDir $managedB -GitArgs @("remote", "get-url", "origin")) -join ""
Assert-SamePath $releaseB $originB "ssh-unreachable: origin ends up at the HTTPS release URL"

# -----------------------------------------------------------------------------
# Scenario C: neither release URL is reachable -> fail loud, not silent
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "-- Unreachable release repo fails loud with a clear message --"
$upstreamC = New-BareRepo -Path (New-FakeUpstreamPath "case-c") -Branch "main" -Content "upstream c`n"
$managedC = Join-Path $scratchRoot "managed-c"
Invoke-Git -WorkDir $scratchRoot -GitArgs @("clone", "-q", "--depth", "1", "--branch", "main", $upstreamC, $managedC) | Out-Null

$bogus1 = Join-Path $scratchRoot "does-not-exist-anywhere-1"
$bogus2 = Join-Path $scratchRoot "does-not-exist-anywhere-2"
$patchedC = New-PatchedInstallScript -SshUrl $bogus1 -HttpsUrl $bogus2
$resultC = Invoke-RepositoryStage -ScriptPath $patchedC -InstallDir $managedC -HermesHome (Join-Path $scratchRoot "hermes-home-c")

Assert-True ($resultC.ExitCode -ne 0) "unreachable-release: repository stage exits non-zero"
Assert-Contains $resultC.Output "Could not fetch" "unreachable-release: prints the branded failure message"

# -----------------------------------------------------------------------------
# Scenario D: origin already the release repo -> left alone, no repoint noise
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "-- Origin already at the release repo is left alone --"
$releaseD = New-BareRepo -Path (Join-Path $scratchRoot "release-d.git") -Branch "release" -Content "v1`n"
$managedD = Join-Path $scratchRoot "managed-d"
Invoke-Git -WorkDir $scratchRoot -GitArgs @("clone", "-q", "--depth", "1", "--branch", "release", $releaseD, $managedD) | Out-Null

# Advance the release repo so there is something real to fetch.
$seedD = Join-Path $scratchRoot "seed-d-advance"
Invoke-Git -WorkDir $scratchRoot -GitArgs @("clone", "-q", "--branch", "release", $releaseD, $seedD) | Out-Null
Set-Content -LiteralPath (Join-Path $seedD "f.txt") -Value "v2`n" -NoNewline
Invoke-Git -WorkDir $seedD -GitArgs @("commit", "-qam", "v2") | Out-Null
Invoke-Git -WorkDir $seedD -GitArgs @("push", "-q", "origin", "release") | Out-Null

# Runs the REAL, unmodified install.ps1 -- this scenario never touches
# $RepoUrlSsh/$RepoUrlHttps, so no substitution is needed.
$resultD = Invoke-RepositoryStage -ScriptPath $installScript -InstallDir $managedD -HermesHome (Join-Path $scratchRoot "hermes-home-d")

Assert-Equal 0 $resultD.ExitCode "origin-already-correct: repository stage exits 0"
Assert-NotContains $resultD.Output "repointed to" "origin-already-correct: no repoint noise printed"
$contentD = Get-Content -Raw -LiteralPath (Join-Path $managedD "f.txt")
Assert-Equal "v2`n" $contentD "origin-already-correct: working tree updated to the new release commit"

# -----------------------------------------------------------------------------
# Scenario E: a third-party origin (customer mirror) is never hijacked
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "-- A third-party origin (customer mirror) is never hijacked --"
$customerMirror = New-BareRepo -Path (Join-Path $scratchRoot "customer-mirror.git") -Branch "release" -Content "customer content`n"
$managedE = Join-Path $scratchRoot "managed-e"
Invoke-Git -WorkDir $scratchRoot -GitArgs @("clone", "-q", "--depth", "1", "--branch", "release", $customerMirror, $managedE) | Out-Null

# Runs the REAL, unmodified install.ps1 too -- the repoint condition must
# never match a customer's own mirror, regardless of what $RepoUrlSsh/
# $RepoUrlHttps are hardcoded to.
$resultE = Invoke-RepositoryStage -ScriptPath $installScript -InstallDir $managedE -HermesHome (Join-Path $scratchRoot "hermes-home-e")

Assert-Equal 0 $resultE.ExitCode "third-party-origin: repository stage exits 0"
Assert-NotContains $resultE.Output "repointed to" "third-party-origin: no repoint noise printed"
$originE = (Invoke-Git -WorkDir $managedE -GitArgs @("remote", "get-url", "origin")) -join ""
Assert-SamePath $customerMirror $originE "third-party-origin: origin left exactly as the customer set it"

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
Write-Host ""
Remove-Item -Recurse -Force $scratchRoot -ErrorAction SilentlyContinue
if ($failures -gt 0) {
    Write-Host "FAILED: $failures assertion(s) failed" -ForegroundColor Red
    exit 1
} else {
    Write-Host "All origin-repoint tests passed." -ForegroundColor Green
    exit 0
}
