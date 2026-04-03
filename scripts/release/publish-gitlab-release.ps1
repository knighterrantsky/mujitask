param(
    [Parameter(Mandatory = $true)]
    [string]$TagName,

    [Parameter(Mandatory = $true)]
    [string]$ReleaseNotesFile,

    [string]$ProjectId = "work%2Fmujitask",
    [string]$GitLabBaseUrl = "http://192.168.88.200:8080",
    [switch]$UpdateExisting
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    throw "[publish-gitlab-release] $Message"
}

if (-not (Test-Path -LiteralPath $ReleaseNotesFile)) {
    Fail "Missing release notes file: $ReleaseNotesFile"
}

$token = if ($env:GITLAB_TOKEN) { $env:GITLAB_TOKEN } elseif ($env:GITLAB_API_TOKEN) { $env:GITLAB_API_TOKEN } else { "" }
if ([string]::IsNullOrWhiteSpace($token)) {
    Fail "Set GITLAB_TOKEN or GITLAB_API_TOKEN before publishing a release."
}

$notes = Get-Content -Raw -LiteralPath $ReleaseNotesFile
if ([string]::IsNullOrWhiteSpace($notes)) {
    Fail "Release notes file is empty: $ReleaseNotesFile"
}

$headers = @{
    "PRIVATE-TOKEN" = $token
    "Content-Type"  = "application/json"
}

$payload = @{
    name        = $TagName
    tag_name    = $TagName
    description = $notes
} | ConvertTo-Json -Compress

$releaseUrl = "$GitLabBaseUrl/api/v4/projects/$ProjectId/releases"
$targetUrl = "$releaseUrl/$TagName"

if ($UpdateExisting) {
    Invoke-RestMethod -Method Put -Uri $targetUrl -Headers $headers -Body $payload | Out-Null
    Write-Host "[publish-gitlab-release] Updated release $TagName"
} else {
    Invoke-RestMethod -Method Post -Uri $releaseUrl -Headers $headers -Body $payload | Out-Null
    Write-Host "[publish-gitlab-release] Created release $TagName"
}
