param(
    [string]$SkillDir = (Join-Path $env:USERPROFILE ".openclaw\workspace\skills\mujitask-tiktok-feishu-sync")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $IsWindows) {
    throw "[verify-openclaw] This script currently supports Windows only."
}

function Log([string]$Message) {
    Write-Host "[verify-openclaw] $Message"
}

function Fail([string]$Message) {
    throw "[verify-openclaw] $Message"
}

function Read-KeyValueFile([string]$Path) {
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) {
        Fail "Missing $Path."
    }

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

function Check-Path([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        Fail "Missing required path: $Path"
    }
    Log "OK: $Path"
}

function Test-CdpReady {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:9222/json/version" -Method Get -TimeoutSec 2
        return $response.Browser
    } catch {
        return $null
    }
}

$envFile = Join-Path $SkillDir "skill.local.env"
$config = Read-KeyValueFile -Path $envFile

$installDir = [string]$config["INSTALL_DIR"]
$tableUrl = [string]$config["TABLE_URL"]
$token = [string]$config["FEISHU_ACCESS_TOKEN"]

if ([string]::IsNullOrWhiteSpace($installDir)) { Fail "INSTALL_DIR is missing in $envFile." }
if ([string]::IsNullOrWhiteSpace($tableUrl)) { Fail "TABLE_URL is missing in $envFile." }
if ([string]::IsNullOrWhiteSpace($token)) { Fail "FEISHU_ACCESS_TOKEN is missing in $envFile." }

$cliPath = Join-Path $installDir ".venv\Scripts\automation-business-scaffold-run.exe"
$pythonPath = Join-Path $installDir ".venv\Scripts\python.exe"
$browserProfiles = Join-Path $installDir "config\browser_profiles.json"
$deployState = Join-Path $installDir "runtime\deployment\openclaw-deploy.env"
$skillBackupDirs = @(Get-ChildItem -LiteralPath (Join-Path $env:USERPROFILE ".openclaw\workspace\skills") -Filter "mujitask-tiktok-feishu-sync.backup-*" -Directory -ErrorAction SilentlyContinue)

Log "Checking deployed skill directory"
Check-Path $SkillDir
Check-Path (Join-Path $SkillDir "SKILL.md")
Check-Path (Join-Path $SkillDir "skill.local.env")
Check-Path (Join-Path $SkillDir "skill.local.env.example")
Check-Path (Join-Path $SkillDir "run_feishu_tiktok_sync.sh")
Check-Path (Join-Path $SkillDir "run_feishu_tiktok_sync.ps1")
Check-Path (Join-Path $SkillDir "run_cleanup.ps1")
Check-Path (Join-Path $SkillDir "run_batch_sync.ps1")
Check-Path (Join-Path $SkillDir "start_browser_cdp.ps1")

Log "Checking installed project directory"
Check-Path $installDir
Check-Path $cliPath
Check-Path $pythonPath
Check-Path $browserProfiles
Check-Path $deployState

Log "Checking OpenClaw workspace for obsolete skill backups"
if ($skillBackupDirs.Count -gt 0) {
    Fail "Found obsolete skill backup directories in OpenClaw workspace."
}
Log "OK: no obsolete OpenClaw skill backup directories were found"

Log "Checking list-tasks output"
$tasksFile = Join-Path ([System.IO.Path]::GetTempPath()) ("mujitask-tasks-" + [guid]::NewGuid().ToString("N") + ".json")
try {
    & $cliPath list-tasks | Set-Content -LiteralPath $tasksFile -Encoding UTF8
    $payload = Get-Content -Raw -LiteralPath $tasksFile | ConvertFrom-Json
    $taskNames = @($payload.tasks | ForEach-Object { $_.name })
    foreach ($required in @("tiktok_product_link_cleanup", "tiktok_feishu_batch_sync")) {
        if ($taskNames -notcontains $required) {
            Fail "Missing required task: $required"
        }
    }
    Log "OK: required tasks are present"
}
finally {
    if (Test-Path -LiteralPath $tasksFile) {
        Remove-Item -LiteralPath $tasksFile -Force
    }
}

Log "Checking Chrome CDP availability"
$browserName = Test-CdpReady
if ($browserName) {
    Log "OK: Chrome CDP is reachable at http://127.0.0.1:9222 ($browserName)"
} else {
    Log "WARN: Chrome CDP is not reachable at http://127.0.0.1:9222"
    Log "You can start it with: powershell -ExecutionPolicy Bypass -File `"$SkillDir\start_browser_cdp.ps1`""
}

Log "Verification completed"
Log "Skill directory: $SkillDir"
Log "Install directory: $installDir"
Log "Table URL: $tableUrl"
