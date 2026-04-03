Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    throw "[browser-cdp] $Message"
}

function Find-Chrome {
    $candidates = @()
    if ($env:ProgramFiles) {
        $candidates += Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates += Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"
    }
    if ($env:LOCALAPPDATA) {
        $candidates += Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe"
    }

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    return $null
}

$chromeExe = Find-Chrome
if (-not $chromeExe) {
    Fail "Google Chrome was not found. Install Chrome and rerun deployment or this script."
}

$profileDir = if ($env:MUJITASK_CHROME_PROFILE_DIR) {
    $env:MUJITASK_CHROME_PROFILE_DIR
} elseif ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "mujitask\chrome-cdp-profile"
} else {
    Join-Path $env:USERPROFILE "AppData\Local\mujitask\chrome-cdp-profile"
}

New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

Write-Host "[browser-cdp] Starting Chrome with remote debugging on port 9222"
Start-Process -FilePath $chromeExe -ArgumentList @(
    "--remote-debugging-port=9222",
    "--user-data-dir=$profileDir"
) | Out-Null
