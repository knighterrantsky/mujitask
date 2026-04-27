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

function Test-SkillFrontmatter([string]$SkillMdPath) {
    $content = Get-Content -Raw -LiteralPath $SkillMdPath
    $frontmatterMatch = [regex]::Match($content, '(?s)\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)')
    if (-not $frontmatterMatch.Success) {
        Fail "$SkillMdPath is missing YAML frontmatter."
    }

    $frontmatter = $frontmatterMatch.Groups[1].Value
    $nameMatch = [regex]::Match($frontmatter, '(?m)^\s*name:\s*(\S.*?)\s*$')
    if (-not $nameMatch.Success) {
        Fail "$SkillMdPath frontmatter is missing name."
    }
    if ($nameMatch.Groups[1].Value.Trim() -ne "mujitask-tiktok-feishu-sync") {
        Fail "$SkillMdPath frontmatter name must be mujitask-tiktok-feishu-sync."
    }
    if (-not [regex]::IsMatch($frontmatter, '(?m)^\s*description:\s*.+$')) {
        Fail "$SkillMdPath frontmatter is missing description."
    }
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
$feishuBaseUrl = [string]$config["MUJITASK_FEISHU_BASE_URL"]
$token = [string]$config["MUJITASK_FEISHU_ACCESS_TOKEN"]

if ([string]::IsNullOrWhiteSpace($installDir)) { Fail "INSTALL_DIR is missing in $envFile." }
if ([string]::IsNullOrWhiteSpace($feishuBaseUrl)) { Fail "MUJITASK_FEISHU_BASE_URL is missing in $envFile." }
if ([string]::IsNullOrWhiteSpace([string]$config["MUJITASK_FEISHU_TK_SELECTION_TABLE_ID"])) { Fail "MUJITASK_FEISHU_TK_SELECTION_TABLE_ID is missing in $envFile." }
if ([string]::IsNullOrWhiteSpace([string]$config["MUJITASK_FEISHU_TK_SELECTION_VIEW_ID"])) { Fail "MUJITASK_FEISHU_TK_SELECTION_VIEW_ID is missing in $envFile." }
if ([string]::IsNullOrWhiteSpace([string]$config["MUJITASK_FEISHU_TK_COMPETITOR_TABLE_ID"])) { Fail "MUJITASK_FEISHU_TK_COMPETITOR_TABLE_ID is missing in $envFile." }
if ([string]::IsNullOrWhiteSpace([string]$config["MUJITASK_FEISHU_TK_COMPETITOR_VIEW_ID"])) { Fail "MUJITASK_FEISHU_TK_COMPETITOR_VIEW_ID is missing in $envFile." }
if ([string]::IsNullOrWhiteSpace([string]$config["MUJITASK_FEISHU_TK_INFLUENCER_POOL_TABLE_ID"])) { Fail "MUJITASK_FEISHU_TK_INFLUENCER_POOL_TABLE_ID is missing in $envFile." }
if ([string]::IsNullOrWhiteSpace([string]$config["MUJITASK_FEISHU_TK_INFLUENCER_POOL_VIEW_ID"])) { Fail "MUJITASK_FEISHU_TK_INFLUENCER_POOL_VIEW_ID is missing in $envFile." }
if ([string]::IsNullOrWhiteSpace([string]$config["MUJITASK_FEISHU_TK_INFLUENCER_OUTREACH_TABLE_ID"])) { Fail "MUJITASK_FEISHU_TK_INFLUENCER_OUTREACH_TABLE_ID is missing in $envFile." }
if ([string]::IsNullOrWhiteSpace([string]$config["MUJITASK_FEISHU_TK_INFLUENCER_OUTREACH_VIEW_ID"])) { Fail "MUJITASK_FEISHU_TK_INFLUENCER_OUTREACH_VIEW_ID is missing in $envFile." }
if ([string]::IsNullOrWhiteSpace([string]$config["MUJITASK_FEISHU_TK_HOT_VIDEO_TABLE_ID"])) { Fail "MUJITASK_FEISHU_TK_HOT_VIDEO_TABLE_ID is missing in $envFile." }
if ([string]::IsNullOrWhiteSpace([string]$config["MUJITASK_FEISHU_TK_HOT_VIDEO_VIEW_ID"])) { Fail "MUJITASK_FEISHU_TK_HOT_VIDEO_VIEW_ID is missing in $envFile." }
if ([string]::IsNullOrWhiteSpace($token)) { Fail "MUJITASK_FEISHU_ACCESS_TOKEN is missing in $envFile." }

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
Check-Path (Join-Path $SkillDir "run_refresh_current_competitor_table_step.sh")
Check-Path (Join-Path $SkillDir "run_competitor_row_by_url_step.sh")
Check-Path (Join-Path $SkillDir "run_product_url_complete_step.sh")
Check-Path (Join-Path $SkillDir "run_keyword_search_step.sh")
Check-Path (Join-Path $SkillDir "run_influencer_pool_sync_step.sh")
Check-Path (Join-Path $SkillDir "run_skill_step.py")
Check-Path (Join-Path $SkillDir "lightweight_submit.py")
Check-Path (Join-Path $SkillDir "start_browser_cdp.ps1")

Log "Checking installed project directory"
Check-Path $installDir
Check-Path $cliPath
Check-Path $pythonPath
Check-Path $browserProfiles
Check-Path $deployState

Log "Checking SKILL.md frontmatter"
Test-SkillFrontmatter (Join-Path $SkillDir "SKILL.md")
Log "OK: SKILL.md frontmatter contains the required OpenClaw metadata"

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
    foreach ($required in @(
        "tiktok_product_link_cleanup",
        "feishu_pending_rows_scan",
        "feishu_single_row_update",
        "feishu_seed_row_insert",
        "fastmoss_keyword_candidate_discovery",
        "fastmoss_login_check"
    )) {
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
