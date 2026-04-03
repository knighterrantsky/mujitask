param(
    [Parameter(Mandatory = $true)]
    [string]$TagName,

    [string]$TargetBranch = "main",
    [string]$ReleaseNotesFile = ".\scripts\release\release-notes.template.md",
    [string]$MrTitle,
    [string]$MrDescription = "Auto-created by Codex release flow.",
    [switch]$UpdateExistingRelease
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    throw "[publish-gitlab-flow] $Message"
}

function Info([string]$Message) {
    Write-Host "[publish-gitlab-flow] $Message"
}

function Read-TokenInteractive {
    $secure = Read-Host "GitLab token" -AsSecureString
    if (-not $secure) {
        return ""
    }
    return [System.Net.NetworkCredential]::new("", $secure).Password.Trim()
}

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Args
    )

    $output = & git @Args 2>&1
    if ($LASTEXITCODE -ne 0) {
        $joined = $Args -join " "
        Fail "git $joined failed: $output"
    }
    return ($output | Out-String).Trim()
}

function Get-OriginInfo {
    $originUrl = Invoke-Git -Args @("remote", "get-url", "origin")
    if ([string]::IsNullOrWhiteSpace($originUrl)) {
        Fail "Could not determine origin remote URL."
    }
    if ($originUrl -notmatch "^(?<base>https?://[^/]+)/(?<path>.+?)(?:\.git)?$") {
        Fail "Only HTTP(S) GitLab origin URLs are supported by this script. Current origin: $originUrl"
    }

    return @{
        OriginUrl = $originUrl
        BaseUrl   = $Matches["base"]
        Project   = $Matches["path"]
        ProjectId = [System.Uri]::EscapeDataString($Matches["path"])
    }
}

function Get-AuthHeaders([string]$Token) {
    return @{
        "PRIVATE-TOKEN" = $Token
    }
}

function Invoke-GitLabApi {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Method,
        [Parameter(Mandatory = $true)]
        [string]$Uri,
        [Parameter(Mandatory = $true)]
        [hashtable]$Headers,
        [object]$Body
    )

    if ($null -ne $Body) {
        $json = $Body | ConvertTo-Json -Depth 10 -Compress
        return Invoke-RestMethod -Method $Method -Uri $Uri -Headers ($Headers + @{ "Content-Type" = "application/json" }) -Body $json
    }

    return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $Headers
}

function Resolve-ReleaseNotesFile([string]$Path, [string]$MainCommitShort) {
    if (-not (Test-Path -LiteralPath $Path)) {
        Fail "Missing release notes file: $Path"
    }

    $content = Get-Content -Raw -LiteralPath $Path
    if ([string]::IsNullOrWhiteSpace($content)) {
        Fail "Release notes file is empty: $Path"
    }

    $rendered = $content.Replace("commit-sha", $MainCommitShort)
    $tempPath = Join-Path $env:TEMP ("release-notes-" + [System.Guid]::NewGuid().ToString("N") + ".md")
    Set-Content -LiteralPath $tempPath -Value $rendered -NoNewline
    return $tempPath
}

$origin = Get-OriginInfo
Info "Detected GitLab repository: $($origin.Project)"

$token = if ($env:GITLAB_TOKEN) { $env:GITLAB_TOKEN } elseif ($env:GITLAB_API_TOKEN) { $env:GITLAB_API_TOKEN } else { "" }
if ([string]::IsNullOrWhiteSpace($token)) {
    $token = Read-TokenInteractive
}
if ([string]::IsNullOrWhiteSpace($token)) {
    Fail "Set GITLAB_TOKEN or GITLAB_API_TOKEN, or provide a token when prompted."
}

$headers = Get-AuthHeaders -Token $token
$sourceBranch = Invoke-Git -Args @("branch", "--show-current")
if ([string]::IsNullOrWhiteSpace($sourceBranch)) {
    Fail "Could not determine current branch."
}
if ($sourceBranch -eq $TargetBranch) {
    Fail "Current branch is $TargetBranch. Create and switch to a feature branch before publishing."
}

$statusShort = Invoke-Git -Args @("status", "--short")
if (-not [string]::IsNullOrWhiteSpace($statusShort)) {
    Fail "Working tree is not clean. Commit or stash changes before publishing."
}

Info "Pushing source branch $sourceBranch"
Invoke-Git -Args @("push", "-u", "origin", $sourceBranch) | Out-Null

$mergeRequestsUrl = "$($origin.BaseUrl)/api/v4/projects/$($origin.ProjectId)/merge_requests"
$encodedSourceBranch = [System.Uri]::EscapeDataString($sourceBranch)
$encodedTargetBranch = [System.Uri]::EscapeDataString($TargetBranch)
$existingMr = Invoke-GitLabApi -Method Get -Uri "$mergeRequestsUrl?state=opened&source_branch=$encodedSourceBranch&target_branch=$encodedTargetBranch" -Headers $headers -Body $null

if ($existingMr -and $existingMr.Count -gt 0) {
    $mr = $existingMr[0]
    Info "Reusing existing MR !$($mr.iid)"
} else {
    if ([string]::IsNullOrWhiteSpace($MrTitle)) {
        $MrTitle = Invoke-Git -Args @("log", "-1", "--pretty=%s")
    }
    $mr = Invoke-GitLabApi -Method Post -Uri $mergeRequestsUrl -Headers $headers -Body @{
        source_branch = $sourceBranch
        target_branch = $TargetBranch
        title         = $MrTitle
        description   = $MrDescription
        remove_source_branch = $false
    }
    Info "Created MR !$($mr.iid)"
}

$mergeUrl = "$mergeRequestsUrl/$($mr.iid)/merge"
try {
    $mergeResult = Invoke-GitLabApi -Method Put -Uri $mergeUrl -Headers $headers -Body @{
        merge_when_pipeline_succeeds = $false
        should_remove_source_branch  = $false
    }
    Info "Merged MR !$($mr.iid)"
} catch {
    Fail "Failed to merge MR !$($mr.iid). Check approvals, conflicts, or pipeline status. $($_.Exception.Message)"
}

Info "Refreshing $TargetBranch"
Invoke-Git -Args @("switch", $TargetBranch) | Out-Null
Invoke-Git -Args @("pull", "--ff-only", "origin", $TargetBranch) | Out-Null

$mainCommit = Invoke-Git -Args @("rev-parse", "HEAD")
$mainCommitShort = Invoke-Git -Args @("rev-parse", "--short", "HEAD")

$existingTag = & git rev-parse --verify --quiet ("refs/tags/" + $TagName)
if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace(($existingTag | Out-String))) {
    Fail "Tag $TagName already exists locally."
}

Info "Creating tag $TagName on $TargetBranch commit $mainCommitShort"
Invoke-Git -Args @("tag", "-a", $TagName, $mainCommit, "-m", $TagName) | Out-Null
Invoke-Git -Args @("push", "origin", $TagName) | Out-Null

$resolvedNotesFile = Resolve-ReleaseNotesFile -Path $ReleaseNotesFile -MainCommitShort $mainCommitShort
try {
    $publishArgs = @(
        "-ExecutionPolicy", "Bypass",
        "-File", ".\scripts\release\publish-gitlab-release.ps1",
        "-TagName", $TagName,
        "-ReleaseNotesFile", $resolvedNotesFile,
        "-ProjectId", $origin.ProjectId,
        "-GitLabBaseUrl", $origin.BaseUrl
    )
    if ($UpdateExistingRelease) {
        $publishArgs += "-UpdateExisting"
    }

    & powershell @publishArgs
    if ($LASTEXITCODE -ne 0) {
        Fail "Release publishing failed for $TagName."
    }
} finally {
    Remove-Item -LiteralPath $resolvedNotesFile -ErrorAction SilentlyContinue
}

$releaseUrl = "$($origin.OriginUrl -replace '\.git$','')/-/releases/$TagName"
Info "MR: $($mr.web_url)"
Info "Release: $releaseUrl"
