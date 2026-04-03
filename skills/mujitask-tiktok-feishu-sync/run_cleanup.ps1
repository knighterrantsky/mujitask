param(
    [ValidateSet("draft", "canary", "full_auto", "live")]
    [string]$RunMode = "draft"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvFile = Join-Path $ScriptDir "skill.local.env"

function Fail([string]$Message) {
    throw "[cleanup] $Message"
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

$env:FEISHU_ACCESS_TOKEN = $token
Set-Location -LiteralPath $installDir

Write-Host "[cleanup] Running tiktok_product_link_cleanup with run_mode=$RunMode"
& $cliPath run `
    --task tiktok_product_link_cleanup `
    --run-mode $RunMode `
    --param "table_url=$tableUrl" `
    --param "access_token_env=FEISHU_ACCESS_TOKEN" `
    --param "url_field_name=产品链接"
