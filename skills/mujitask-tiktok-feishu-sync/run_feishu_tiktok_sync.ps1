Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvFile = Join-Path $ScriptDir "skill.local.env"
$cleanupScript = Join-Path $ScriptDir "run_cleanup.ps1"
$batchScript = Join-Path $ScriptDir "run_batch_sync.ps1"

function Fail([string]$Message) {
    throw "[feishu-tiktok-sync] $Message"
}

function Read-SkillEnv([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        Fail "Missing $Path."
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

$config = Read-SkillEnv -Path $EnvFile
$installDir = [string]$config["INSTALL_DIR"]
$tableUrl = [string]$config["TABLE_URL"]
$token = [string]$config["FEISHU_ACCESS_TOKEN"]

if ([string]::IsNullOrWhiteSpace($installDir)) { Fail "INSTALL_DIR is required in $EnvFile." }
if ([string]::IsNullOrWhiteSpace($tableUrl)) { Fail "TABLE_URL is required in $EnvFile." }
if ([string]::IsNullOrWhiteSpace($token)) { Fail "FEISHU_ACCESS_TOKEN is required in $EnvFile." }

if (-not (Test-Path -LiteralPath $cleanupScript)) {
    Fail "Missing $cleanupScript."
}

if (-not (Test-Path -LiteralPath $batchScript)) {
    Fail "Missing $batchScript."
}

Write-Host "[feishu-tiktok-sync] Step 1/2: normalizing and deduplicating TikTok links in Feishu"
& $cleanupScript -RunMode canary

Write-Host "[feishu-tiktok-sync] Step 2/2: crawling TikTok competitor data and writing results back to Feishu"
& $batchScript -RunMode canary -MaxRecords 0
