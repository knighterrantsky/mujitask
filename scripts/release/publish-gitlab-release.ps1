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

function Normalize-MarkdownText([string]$Text) {
    if ($null -eq $Text) {
        return ""
    }

    return ($Text -replace "`r`n", "`n" -replace "`r", "`n").Trim()
}

function Read-TokenInteractive {
    $secure = Read-Host "GitLab token" -AsSecureString
    if (-not $secure) {
        return ""
    }
    return [System.Net.NetworkCredential]::new("", $secure).Password.Trim()
}

function Test-IsWindowsHost {
    if (Get-Variable -Name IsWindows -ErrorAction SilentlyContinue) {
        return [bool]$IsWindows
    }
    return $env:OS -eq "Windows_NT"
}

function Get-GitLabToken {
    $token = if ($env:GITLAB_TOKEN) { $env:GITLAB_TOKEN } elseif ($env:GITLAB_API_TOKEN) { $env:GITLAB_API_TOKEN } else { "" }
    if (-not [string]::IsNullOrWhiteSpace($token)) {
        return $token.Trim()
    }

    if (Test-IsWindowsHost) {
        foreach ($scope in @("User", "Machine")) {
            foreach ($name in @("GITLAB_TOKEN", "GITLAB_API_TOKEN")) {
                $scopedToken = [Environment]::GetEnvironmentVariable($name, $scope)
                if (-not [string]::IsNullOrWhiteSpace($scopedToken)) {
                    return $scopedToken.Trim()
                }
            }
        }
    }

    return ""
}

function Invoke-GitLabJsonApi {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Method,
        [Parameter(Mandatory = $true)]
        [string]$Uri,
        [Parameter(Mandatory = $true)]
        [hashtable]$Headers,
        [object]$Payload
    )

    $params = @{
        Method  = $Method
        Uri     = $Uri
        Headers = $Headers
    }

    if ($null -ne $Payload) {
        $json = $Payload | ConvertTo-Json -Depth 10 -Compress
        $params.ContentType = "application/json; charset=utf-8"
        $params.Body = [System.Text.Encoding]::UTF8.GetBytes($json)
    }

    return Invoke-RestMethod @params
}

if (-not (Test-Path -LiteralPath $ReleaseNotesFile)) {
    Fail "Missing release notes file: $ReleaseNotesFile"
}

$token = Get-GitLabToken
if ([string]::IsNullOrWhiteSpace($token)) {
    $token = Read-TokenInteractive
}
if ([string]::IsNullOrWhiteSpace($token)) {
    Fail "Set GITLAB_TOKEN or GITLAB_API_TOKEN in the current shell or Windows user/machine environment, or provide a token when prompted."
}

$notes = Normalize-MarkdownText (Get-Content -Raw -LiteralPath $ReleaseNotesFile)
if ([string]::IsNullOrWhiteSpace($notes)) {
    Fail "Release notes file is empty: $ReleaseNotesFile"
}

$headers = @{
    "PRIVATE-TOKEN" = $token
}

$releaseUrl = "$GitLabBaseUrl/api/v4/projects/$ProjectId/releases"
$targetUrl = "$releaseUrl/$TagName"

if ($UpdateExisting) {
    Invoke-GitLabJsonApi -Method Put -Uri $targetUrl -Headers $headers -Payload @{
        name        = $TagName
        description = $notes
    } | Out-Null
    Write-Host "[publish-gitlab-release] Updated release $TagName"
} else {
    Invoke-GitLabJsonApi -Method Post -Uri $releaseUrl -Headers $headers -Payload @{
        name        = $TagName
        tag_name    = $TagName
        description = $notes
    } | Out-Null
    Write-Host "[publish-gitlab-release] Created release $TagName"
}
