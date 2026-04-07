param(
    [ValidateSet("draft", "canary", "full_auto", "live")]
    [string]$RunMode = "draft",
    [int]$MaxRecords = 0,
    [string]$ProfileRef = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvFile = Join-Path $ScriptDir "skill.local.env"
$BrowserTargetHelper = Join-Path $ScriptDir "resolve_browser_target.py"

function Fail([string]$Message) {
    throw "[batch-sync] $Message"
}

function Read-SkillEnv([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        Fail "Missing $Path. Copy skill.local.env.example and fill it first."
    }

    function Normalize-EnvEntry([string]$Value) {
        $normalized = $Value.Trim()
        if ($normalized.StartsWith([char]0xFEFF)) {
            $normalized = $normalized.TrimStart([char]0xFEFF)
        }
        if ($normalized.StartsWith("export ")) {
            $normalized = $normalized.Substring(7).Trim()
        }
        if (
            $normalized.Length -ge 2 -and
            (
                ($normalized.StartsWith('"') -and $normalized.EndsWith('"')) -or
                ($normalized.StartsWith("'") -and $normalized.EndsWith("'"))
            )
        ) {
            $normalized = $normalized.Substring(1, $normalized.Length - 2)
        }
        return $normalized
    }

    $map = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed)) { continue }
        if ($trimmed.StartsWith("#")) { continue }
        $parts = $line.Split("=", 2)
        if ($parts.Count -ne 2) { continue }
        $key = Normalize-EnvEntry $parts[0]
        $value = Normalize-EnvEntry $parts[1]
        if ([string]::IsNullOrWhiteSpace($key)) { continue }
        $map[$key] = $value
    }
    return $map
}

function Test-CdpReady([string]$DebugHttp) {
    try {
        $baseUrl = $DebugHttp.TrimEnd("/")
        $response = Invoke-WebRequest -Uri "$baseUrl/json/version" -UseBasicParsing -TimeoutSec 2
        return ($response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Resolve-BrowserTarget {
    param(
        [string]$PythonPath,
        [string]$InstallDir,
        [string]$RequestedProfileRef,
        [string]$FallbackProfileRef
    )

    $args = @($BrowserTargetHelper, "resolve", "--install-dir", $InstallDir)
    if (-not [string]::IsNullOrWhiteSpace($RequestedProfileRef)) {
        $args += @("--profile-ref", $RequestedProfileRef)
    }
    if (-not [string]::IsNullOrWhiteSpace($FallbackProfileRef)) {
        $args += @("--fallback-profile-ref", $FallbackProfileRef)
    }

    $json = & $PythonPath @args
    if ($LASTEXITCODE -ne 0) {
        Fail "Failed to resolve browser target."
    }
    return ($json | ConvertFrom-Json)
}

function Ensure-BrowserReady($BrowserTarget) {
    $provider = [string]$BrowserTarget.provider
    $resolvedProfileRef = [string]$BrowserTarget.profile_ref
    $debugHttp = ""
    if ($null -ne $BrowserTarget.metadata -and $BrowserTarget.metadata.PSObject.Properties.Name -contains "debug_http") {
        $debugHttp = [string]$BrowserTarget.metadata.debug_http
    }
    if ([string]::IsNullOrWhiteSpace($debugHttp)) {
        $debugHttp = "http://127.0.0.1:9222"
    }

    switch ($provider) {
        "roxy" {
            Write-Host "[batch-sync] Using browser profile_ref=$resolvedProfileRef provider=roxy. Skipping local Chrome CDP checks."
            return
        }
        "chrome_cdp" {
            if (Test-CdpReady -DebugHttp $debugHttp) {
                return
            }
            if ($debugHttp -ne "http://127.0.0.1:9222") {
                Fail "Chrome CDP is not ready at $debugHttp for profile_ref=$resolvedProfileRef."
            }
            Write-Host "[batch-sync] Chrome CDP is not ready at $debugHttp. Trying to start Chrome on port 9222."
            & (Join-Path $ScriptDir "start_browser_cdp.ps1")
            for ($i = 0; $i -lt 15; $i++) {
                Start-Sleep -Seconds 1
                if (Test-CdpReady -DebugHttp $debugHttp) { return }
            }
            Fail "Chrome CDP did not become ready on $debugHttp."
        }
        default {
            Fail "Unsupported browser provider '$provider' for profile_ref=$resolvedProfileRef."
        }
    }
}

$config = Read-SkillEnv -Path $EnvFile
$installDir = $config["INSTALL_DIR"]
$tableUrl = $config["TABLE_URL"]
$token = $config["FEISHU_ACCESS_TOKEN"]
$browserProfileRef = [string]$config["BROWSER_PROFILE_REF"]

if ([string]::IsNullOrWhiteSpace($installDir)) { Fail "INSTALL_DIR is required in $EnvFile." }
if ([string]::IsNullOrWhiteSpace($tableUrl)) { Fail "TABLE_URL is required in $EnvFile." }
if ([string]::IsNullOrWhiteSpace($token)) { Fail "FEISHU_ACCESS_TOKEN is required in $EnvFile." }

$cliPath = Join-Path $installDir ".venv\Scripts\automation-business-scaffold-run.exe"
if (-not (Test-Path -LiteralPath $cliPath)) {
    Fail "Cannot find CLI at $cliPath. Re-run the deployment script."
}

$pythonPath = Join-Path $installDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    Fail "Cannot find Python at $pythonPath. Re-run the deployment script."
}

$env:FEISHU_ACCESS_TOKEN = $token
$browserTarget = Resolve-BrowserTarget -PythonPath $pythonPath -InstallDir $installDir -RequestedProfileRef $ProfileRef -FallbackProfileRef $browserProfileRef
$resolvedProfileRef = [string]$browserTarget.profile_ref

Write-Host "[batch-sync] Using browser profile_ref=$resolvedProfileRef provider=$($browserTarget.provider)"
Ensure-BrowserReady -BrowserTarget $browserTarget

Set-Location -LiteralPath $installDir

Write-Host "[batch-sync] Running tiktok_feishu_batch_sync with run_mode=$RunMode max_records=$MaxRecords"
& $cliPath run `
    --task tiktok_feishu_batch_sync `
    --run-mode $RunMode `
    --param "table_url=$tableUrl" `
    --param "access_token_env=FEISHU_ACCESS_TOKEN" `
    --param "url_field_name=产品链接" `
    --param "profile_ref=$resolvedProfileRef" `
    --param "max_records=$MaxRecords"
