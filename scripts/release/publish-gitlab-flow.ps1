param(
    [Parameter(Mandatory = $true)]
    [string]$TagName,

    [string]$TargetBranch = "main",
    [string]$ReleaseNotesFile = ".\scripts\release\release-notes.template.md",
    [string]$MrTitle,
    [string]$MrDescription = "Auto-created by Codex release flow.",
    [int]$MergeReadyTimeoutSeconds = 90,
    [int]$MergeCompletionTimeoutSeconds = 600,
    [int]$MergePollIntervalSeconds = 3,
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

    function Quote-ProcessArgument([string]$Value) {
        if ([string]::IsNullOrEmpty($Value)) {
            return '""'
        }
        if ($Value -notmatch '[\s"]') {
            return $Value
        }

        $escaped = $Value -replace '(\\*)"', '$1$1\"'
        $escaped = $escaped -replace '(\\+)$', '$1$1'
        return '"' + $escaped + '"'
    }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "git"
    $psi.Arguments = (($Args | ForEach-Object { Quote-ProcessArgument $_ }) -join " ")
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WorkingDirectory = (Get-Location).Path

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $psi
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()

    $output = (($stdout, $stderr) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) -join [Environment]::NewLine
    if ($process.ExitCode -ne 0) {
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

function Format-MergeRequestStatus([object]$MergeRequest) {
    $parts = @(
        "state=$($MergeRequest.state)",
        "detailed_merge_status=$($MergeRequest.detailed_merge_status)"
    )

    if ([string]::IsNullOrWhiteSpace([string]$MergeRequest.prepared_at)) {
        $parts += "prepared_at=<pending>"
    } else {
        $parts += "prepared_at=$($MergeRequest.prepared_at)"
    }

    if ($null -ne $MergeRequest.head_pipeline -and -not [string]::IsNullOrWhiteSpace([string]$MergeRequest.head_pipeline.status)) {
        $parts += "pipeline=$($MergeRequest.head_pipeline.status)"
    }

    return $parts -join ", "
}

function Get-MergeRequest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$MergeRequestsUrl,
        [Parameter(Mandatory = $true)]
        [int]$MergeRequestIid,
        [Parameter(Mandatory = $true)]
        [hashtable]$Headers
    )

    return Invoke-GitLabApi -Method Get -Uri "$MergeRequestsUrl/$MergeRequestIid" -Headers $Headers -Body $null
}

function Get-MergeRequestDecision([object]$MergeRequest) {
    if ($MergeRequest.state -ne "opened") {
        return @{
            Action = "fail"
            Reason = "MR is not open."
        }
    }

    if ($MergeRequest.draft -or $MergeRequest.work_in_progress) {
        return @{
            Action = "fail"
            Reason = "MR is still marked as draft."
        }
    }

    if ($MergeRequest.user.can_merge -ne $true) {
        return @{
            Action = "fail"
            Reason = "Current token user cannot merge this MR."
        }
    }

    $status = [string]$MergeRequest.detailed_merge_status

    if ($MergeRequest.has_conflicts -or $status -eq "conflict") {
        return @{
            Action = "fail"
            Reason = "MR has merge conflicts."
        }
    }

    if ($MergeRequest.blocking_discussions_resolved -eq $false -or $status -eq "discussions_not_resolved") {
        return @{
            Action = "fail"
            Reason = "MR still has unresolved blocking discussions."
        }
    }

    if ([string]::IsNullOrWhiteSpace([string]$MergeRequest.prepared_at)) {
        return @{
            Action = "wait"
            Reason = "GitLab is still preparing the merge request."
        }
    }

    switch ($status) {
        "mergeable" {
            return @{
                Action = "merge"
                Reason = "MR is mergeable."
            }
        }
        "ci_still_running" {
            if ($null -ne $MergeRequest.head_pipeline) {
                return @{
                    Action = "auto_merge"
                    Reason = "Pipeline is still running."
                }
            }

            return @{
                Action = "wait"
                Reason = "Waiting for merge request pipeline details."
            }
        }
        "ci_must_pass" {
            if ($null -ne $MergeRequest.head_pipeline) {
                return @{
                    Action = "auto_merge"
                    Reason = "Pipeline must pass before merge."
                }
            }

            return @{
                Action = "wait"
                Reason = "Waiting for required pipeline details."
            }
        }
        "checking" {
            return @{
                Action = "wait"
                Reason = "GitLab is checking mergeability."
            }
        }
        "preparing" {
            return @{
                Action = "wait"
                Reason = "GitLab is still preparing merge refs."
            }
        }
        "unchecked" {
            return @{
                Action = "wait"
                Reason = "GitLab has not checked mergeability yet."
            }
        }
        "approvals_syncing" {
            return @{
                Action = "wait"
                Reason = "GitLab is syncing approvals."
            }
        }
        "commits_status" {
            return @{
                Action = "wait"
                Reason = "GitLab is still processing commit status."
            }
        }
        "merge_time" {
            return @{
                Action = "wait"
                Reason = "Merge is blocked until the configured merge time."
            }
        }
        "not_approved" {
            return @{
                Action = "fail"
                Reason = "MR still needs approval."
            }
        }
        "requested_changes" {
            return @{
                Action = "fail"
                Reason = "A reviewer requested changes."
            }
        }
        "need_rebase" {
            return @{
                Action = "fail"
                Reason = "MR must be rebased before merge."
            }
        }
        "merge_request_blocked" {
            return @{
                Action = "fail"
                Reason = "MR is blocked by another merge request."
            }
        }
        "security_policy_violations" {
            return @{
                Action = "fail"
                Reason = "MR violates security policy requirements."
            }
        }
        "title_regex" {
            return @{
                Action = "fail"
                Reason = "MR title does not satisfy repository rules."
            }
        }
        "locked_paths" {
            return @{
                Action = "fail"
                Reason = "MR touches locked paths."
            }
        }
        "locked_lfs_files" {
            return @{
                Action = "fail"
                Reason = "MR touches locked LFS files."
            }
        }
        "security_policy_pipeline_check" {
            return @{
                Action = "wait"
                Reason = "Waiting for security policy pipeline checks."
            }
        }
        "status_checks_must_pass" {
            return @{
                Action = "wait"
                Reason = "Waiting for status checks to pass."
            }
        }
        default {
            return @{
                Action = "wait"
                Reason = "Current merge status is '$status'."
            }
        }
    }
}

function Wait-ForMergeRequestAction {
    param(
        [Parameter(Mandatory = $true)]
        [string]$MergeRequestsUrl,
        [Parameter(Mandatory = $true)]
        [int]$MergeRequestIid,
        [Parameter(Mandatory = $true)]
        [hashtable]$Headers,
        [Parameter(Mandatory = $true)]
        [int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)]
        [int]$PollIntervalSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastStatus = ""

    while ($true) {
        $mergeRequest = Get-MergeRequest -MergeRequestsUrl $MergeRequestsUrl -MergeRequestIid $MergeRequestIid -Headers $Headers
        $decision = Get-MergeRequestDecision -MergeRequest $mergeRequest
        $lastStatus = Format-MergeRequestStatus -MergeRequest $mergeRequest

        if ($decision.Action -eq "merge" -or $decision.Action -eq "auto_merge") {
            return @{
                MergeRequest = $mergeRequest
                Decision     = $decision
            }
        }

        if ($decision.Action -eq "fail") {
            Fail "MR !$MergeRequestIid is not mergeable. $($decision.Reason) Current status: $lastStatus"
        }

        if ((Get-Date) -ge $deadline) {
            Fail "Timed out waiting for MR !$MergeRequestIid to become mergeable. Last status: $lastStatus"
        }

        Info "MR !$MergeRequestIid is not ready yet. $($decision.Reason) Current status: $lastStatus"
        Start-Sleep -Seconds $PollIntervalSeconds
    }
}

function Wait-ForMergeRequestMerged {
    param(
        [Parameter(Mandatory = $true)]
        [string]$MergeRequestsUrl,
        [Parameter(Mandatory = $true)]
        [int]$MergeRequestIid,
        [Parameter(Mandatory = $true)]
        [hashtable]$Headers,
        [Parameter(Mandatory = $true)]
        [int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)]
        [int]$PollIntervalSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $pipelineFailureStates = @("failed", "canceled")

    while ($true) {
        $mergeRequest = Get-MergeRequest -MergeRequestsUrl $MergeRequestsUrl -MergeRequestIid $MergeRequestIid -Headers $Headers
        $statusText = Format-MergeRequestStatus -MergeRequest $mergeRequest

        if ($mergeRequest.state -eq "merged") {
            return $mergeRequest
        }

        if ($mergeRequest.state -eq "closed") {
            Fail "MR !$MergeRequestIid was closed before it merged. Last status: $statusText"
        }

        if (-not [string]::IsNullOrWhiteSpace([string]$mergeRequest.merge_error)) {
            Fail "GitLab reported a merge error for MR !$MergeRequestIid: $($mergeRequest.merge_error)"
        }

        if ($null -ne $mergeRequest.head_pipeline -and $mergeRequest.head_pipeline.status -in $pipelineFailureStates) {
            Fail "MR !$MergeRequestIid pipeline finished with status '$($mergeRequest.head_pipeline.status)'. Last status: $statusText"
        }

        if ((Get-Date) -ge $deadline) {
            Fail "Timed out waiting for MR !$MergeRequestIid to merge. Last status: $statusText"
        }

        Info "Waiting for MR !$MergeRequestIid to finish merging. Current status: $statusText"
        Start-Sleep -Seconds $PollIntervalSeconds
    }
}

function Submit-MergeRequestMerge {
    param(
        [Parameter(Mandatory = $true)]
        [string]$MergeUrl,
        [Parameter(Mandatory = $true)]
        [string]$MergeRequestsUrl,
        [Parameter(Mandatory = $true)]
        [int]$MergeRequestIid,
        [Parameter(Mandatory = $true)]
        [hashtable]$Headers,
        [Parameter(Mandatory = $true)]
        [int]$ReadyTimeoutSeconds,
        [Parameter(Mandatory = $true)]
        [int]$PollIntervalSeconds
    )

    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $mergePrepared = Wait-ForMergeRequestAction -MergeRequestsUrl $MergeRequestsUrl -MergeRequestIid $MergeRequestIid -Headers $Headers -TimeoutSeconds $ReadyTimeoutSeconds -PollIntervalSeconds $PollIntervalSeconds
        $currentMr = $mergePrepared.MergeRequest
        $mergeDecision = $mergePrepared.Decision

        $mergePayload = @{
            sha                         = $currentMr.sha
            should_remove_source_branch = $false
        }

        if ($mergeDecision.Action -eq "auto_merge") {
            $mergePayload.auto_merge = $true
        }

        try {
            Invoke-GitLabApi -Method Put -Uri $MergeUrl -Headers $Headers -Body $mergePayload | Out-Null
            return @{
                MergeRequest  = $currentMr
                MergeDecision = $mergeDecision
            }
        } catch {
            $message = $_.Exception.Message
            if ($message -match "\(405\)" -and $attempt -lt 3) {
                Info "GitLab is still finalizing MR !$MergeRequestIid for API merge. Retrying in $PollIntervalSeconds seconds."
                Start-Sleep -Seconds $PollIntervalSeconds
                continue
            }

            Fail "Failed to merge MR !$MergeRequestIid. Current status: $(Format-MergeRequestStatus -MergeRequest $currentMr). $message"
        }
    }
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
    Set-Content -LiteralPath $tempPath -Value $rendered -Encoding UTF8 -NoNewline
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
$existingMr = Invoke-GitLabApi -Method Get -Uri "${mergeRequestsUrl}?state=opened&source_branch=$encodedSourceBranch&target_branch=$encodedTargetBranch" -Headers $headers -Body $null

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
$mergeSubmitted = Submit-MergeRequestMerge -MergeUrl $mergeUrl -MergeRequestsUrl $mergeRequestsUrl -MergeRequestIid $mr.iid -Headers $headers -ReadyTimeoutSeconds $MergeReadyTimeoutSeconds -PollIntervalSeconds $MergePollIntervalSeconds
$currentMr = $mergeSubmitted.MergeRequest
$mergeDecision = $mergeSubmitted.MergeDecision

if ($mergeDecision.Action -eq "auto_merge") {
    Info "Enabled auto-merge for MR !$($mr.iid)"
} else {
    Info "Merge accepted for MR !$($mr.iid)"
}

$mergedMr = Wait-ForMergeRequestMerged -MergeRequestsUrl $mergeRequestsUrl -MergeRequestIid $mr.iid -Headers $headers -TimeoutSeconds $MergeCompletionTimeoutSeconds -PollIntervalSeconds $MergePollIntervalSeconds
Info "Merged MR !$($mr.iid)"

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
