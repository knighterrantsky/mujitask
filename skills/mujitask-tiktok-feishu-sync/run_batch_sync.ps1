param(
    [ValidateSet("draft", "canary", "full_auto", "live")]
    [string]$RunMode = "draft",
    [int]$MaxRecords = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvFile = Join-Path $ScriptDir "skill.local.env"

function Fail([string]$Message) {
    throw "[batch-sync] $Message"
}

function Read-SkillEnv([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        Fail "Missing $Path. Copy skill.local.env.example and fill it first."
    }

    $map = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed)) { continue }
        if ($trimmed.StartsWith("#")) { continue }
        $parts = $line.Split("=", 2)
        if ($parts.Count -ne 2) { continue }
        $map[$parts[0].Trim()] = $parts[1].Trim()
    }
    return $map
}

function Test-CdpReady {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:9222/json/version" -UseBasicParsing -TimeoutSec 2
        return ($response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

$config = Read-SkillEnv -Path $EnvFile
$installDir = $config["INSTALL_DIR"]
$tableUrl = $config["TABLE_URL"]
$token = $config["FEISHU_ACCESS_TOKEN"]

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

if (-not (Test-CdpReady)) {
    Write-Host "[batch-sync] Chrome CDP is not ready. Trying to start Chrome on port 9222."
    & (Join-Path $ScriptDir "start_browser_cdp.ps1")
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 1
        if (Test-CdpReady) { break }
    }
}

if (-not (Test-CdpReady)) {
    Fail "Chrome CDP did not become ready on http://127.0.0.1:9222."
}

Set-Location -LiteralPath $installDir

Write-Host "[batch-sync] Running tiktok_feishu_batch_sync with run_mode=$RunMode max_records=$MaxRecords"
& $cliPath run `
    --task tiktok_feishu_batch_sync `
    --run-mode $RunMode `
    --param "table_url=$tableUrl" `
    --param "access_token_env=FEISHU_ACCESS_TOKEN" `
    --param "url_field_name=产品链接" `
    --param "profile_ref=local-chrome" `
    --param "max_records=$MaxRecords"
