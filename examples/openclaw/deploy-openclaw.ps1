Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $IsWindows) {
    throw "[deploy-openclaw] This script currently supports Windows only."
}

$TempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("mujitask-openclaw-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $TempRoot | Out-Null

try {
    $script:UvBin = $null
    $script:PythonBin = $null
    $script:GitHubToken = if ($env:GITHUB_TOKEN) { $env:GITHUB_TOKEN } elseif ($env:GH_TOKEN) { $env:GH_TOKEN } else { "" }
    $script:OpenClawDeployUtils = Join-Path $PSScriptRoot "openclaw_deploy_utils.py"

    function Log([string]$Message) {
        Write-Host "[deploy-openclaw] $Message"
    }

    function Warn([string]$Message) {
        Write-Warning "[deploy-openclaw] $Message"
    }

    function Fail([string]$Message) {
        throw "[deploy-openclaw] $Message"
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

    function Prompt([string]$Label, [string]$DefaultValue = "") {
        if ([string]::IsNullOrWhiteSpace($DefaultValue)) {
            do {
                $value = Read-Host $Label
            } while ([string]::IsNullOrWhiteSpace($value))
            return $value.Trim()
        }

        $raw = Read-Host "$Label [$DefaultValue]"
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return $DefaultValue
        }
        return $raw.Trim()
    }

    function PromptOptional([string]$Label, [string]$DefaultValue = "") {
        if ([string]::IsNullOrWhiteSpace($DefaultValue)) {
            $value = Read-Host $Label
            if ([string]::IsNullOrWhiteSpace($value)) {
                return ""
            }
            return $value.Trim()
        }

        $raw = Read-Host "$Label [$DefaultValue]"
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return $DefaultValue
        }
        return $raw.Trim()
    }

    function PromptOptionalSecret([string]$Label) {
        $value = Read-Host $Label -AsSecureString
        if (-not $value) {
            return ""
        }
        $plain = [System.Net.NetworkCredential]::new("", $value).Password
        if ([string]::IsNullOrWhiteSpace($plain)) {
            return ""
        }
        return $plain.Trim()
    }

    function Resolve-UvBin {
        $candidates = @(
            (Get-Command uv -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
            (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
            (Join-Path $env:USERPROFILE ".cargo\bin\uv.exe")
        ) | Where-Object { $_ }

        foreach ($candidate in $candidates) {
            if (Test-Path -LiteralPath $candidate) {
                return $candidate
            }
        }
        return $null
    }

    function Ensure-Uv {
        $resolved = Resolve-UvBin
        if ($resolved) {
            $script:UvBin = $resolved
            return
        }

        Log "Installing uv"
        powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"

        $resolved = Resolve-UvBin
        if (-not $resolved) {
            Fail "uv installation finished but uv.exe was not found in PATH or common install paths."
        }
        $script:UvBin = $resolved
    }

    function Ensure-Python311 {
        Log "Ensuring Python 3.11 is available through uv"
        & $script:UvBin python install 3.11 | Out-Null
        # Avoid resolving a project-local .venv under the install directory, because
        # the deployment flow may delete and recreate that directory mid-run.
        $candidate = (& $script:UvBin python find --managed-python --no-project --resolve-links 3.11 | Select-Object -First 1).Trim()
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            Fail "Could not resolve Python 3.11 after uv installation."
        }
        if (-not (Test-Path -LiteralPath $candidate)) {
            Fail "Resolved Python 3.11 path does not exist: $candidate"
        }
        $script:PythonBin = $candidate
    }

    function Get-GitHubHeaders {
        $headers = @{ Accept = "application/vnd.github+json" }
        if (-not [string]::IsNullOrWhiteSpace($script:GitHubToken)) {
            $headers["Authorization"] = "Bearer $script:GitHubToken"
            $headers["X-GitHub-Api-Version"] = "2022-11-28"
        }
        return $headers
    }

    function Fail-GitHubDownload([string]$BaseMessage) {
        if ([string]::IsNullOrWhiteSpace($script:GitHubToken)) {
            Fail "$BaseMessage If the GitHub repository is private, rerun and provide a GitHub PAT, or set GITHUB_TOKEN / GH_TOKEN."
        }

        Fail "$BaseMessage GitHub PAT was provided, so verify that the token can read the target repository."
    }

    function Invoke-JsonApi([string]$Url, [string]$OutFile) {
        try {
            Invoke-WebRequest -Uri $Url -Headers (Get-GitHubHeaders) -OutFile $OutFile
        } catch {
            Fail-GitHubDownload "Failed to call GitHub API at $Url."
        }
    }

    function Parse-GitHubSlug([string]$RepoUrl) {
        $patterns = @(
            '^(?:git\+)?https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$',
            '^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$'
        )
        foreach ($pattern in $patterns) {
            $match = [regex]::Match($RepoUrl.Trim(), $pattern)
            if ($match.Success) {
                return "$($match.Groups[1].Value)/$($match.Groups[2].Value)"
            }
        }
        return $null
    }

    function Resolve-LatestGitHubRef([string]$Slug) {
        $latestFile = Join-Path $TempRoot "github-latest.json"
        $tagsFile = Join-Path $TempRoot "github-tags.json"

        try {
            Invoke-JsonApi -Url "https://api.github.com/repos/$Slug/releases/latest" -OutFile $latestFile
            $latest = Get-Content -Raw -LiteralPath $latestFile | ConvertFrom-Json
            if ($latest.tag_name) {
                return [string]$latest.tag_name
            }
        } catch {
        }

        Invoke-JsonApi -Url "https://api.github.com/repos/$Slug/tags?per_page=1" -OutFile $tagsFile
        $tags = Get-Content -Raw -LiteralPath $tagsFile | ConvertFrom-Json
        if (-not $tags -or -not $tags[0].name) {
            Fail "The repository $Slug does not expose a latest release or tag."
        }
        return [string]$tags[0].name
    }

    function Download-File([string]$Url, [string]$TargetPath) {
        if ($Url.StartsWith("https://api.github.com/")) {
            try {
                Invoke-WebRequest -Uri $Url -Headers (Get-GitHubHeaders) -OutFile $TargetPath
            } catch {
                Fail-GitHubDownload "Failed to download GitHub archive from $Url."
            }
            return
        }

        Invoke-WebRequest -Uri $Url -OutFile $TargetPath
    }

    function Extract-ArchiveFile([string]$ArchivePath, [string]$OutputDir) {
        $script = @'
from pathlib import Path
import shutil
import sys
import tarfile
import zipfile

archive_path = Path(sys.argv[1])
output_dir = Path(sys.argv[2])
if output_dir.exists():
    shutil.rmtree(output_dir)
output_dir.mkdir(parents=True, exist_ok=True)

if zipfile.is_zipfile(archive_path):
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(output_dir)
elif tarfile.is_tarfile(archive_path):
    with tarfile.open(archive_path) as archive:
        archive.extractall(output_dir)
else:
    raise SystemExit(f"Unsupported archive format: {archive_path}")

children = [item for item in output_dir.iterdir()]
root = children[0] if len(children) == 1 and children[0].is_dir() else output_dir
print(root)
'@
        return (& $script:PythonBin -c $script $ArchivePath $OutputDir).Trim()
    }

    function Prepare-TargetDir([string]$TargetDir) {
        if (Test-Path -LiteralPath $TargetDir) {
            Log "Existing directory detected, removing it before replacement: $TargetDir"
            Remove-Item -LiteralPath $TargetDir -Recurse -Force
        }
        New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
    }

    function Replace-TargetDir([string]$TargetDir) {
        if (Test-Path -LiteralPath $TargetDir) {
            Log "Existing directory detected, removing it before replacement: $TargetDir"
            Remove-Item -LiteralPath $TargetDir -Recurse -Force
        }
        New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
    }

    function Copy-DirectoryContents([string]$SourceDir, [string]$DestinationDir) {
        Get-ChildItem -Force -LiteralPath $SourceDir | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $DestinationDir -Recurse -Force
        }
    }

    function Read-ProjectDependencies([string]$PyprojectPath) {
        $script = @'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    data = tomllib.load(handle)

for dep in data.get("project", {}).get("dependencies", []):
    if dep.startswith("automation-framework @ "):
        continue
    print(dep)
'@
        return & $script:PythonBin -c $script $PyprojectPath
    }

    function Read-FrameworkDependency([string]$PyprojectPath) {
        $raw = & $script:PythonBin $script:OpenClawDeployUtils read-framework-dependency --path $PyprojectPath
        if ([string]::IsNullOrWhiteSpace($raw)) {
            Fail "automation-framework dependency metadata is missing in $PyprojectPath"
        }
        return $raw | ConvertFrom-Json
    }

    $script:LastFrameworkArchiveUrl = ""

    function Install-FrameworkFromPyproject([string]$PyprojectPath, [string]$VenvPython) {
        $script:LastFrameworkArchiveUrl = ""
        $frameworkDependency = Read-FrameworkDependency -PyprojectPath $PyprojectPath
        $frameworkSource = [string]$frameworkDependency.source
        if ([string]::IsNullOrWhiteSpace($frameworkSource)) {
            Fail "automation-framework dependency source is missing in $PyprojectPath"
        }

        if ([string]$frameworkDependency.kind -eq "git" -and $frameworkDependency.repo_url -and $frameworkDependency.ref) {
            $frameworkSlug = Parse-GitHubSlug ([string]$frameworkDependency.repo_url)
            if ($frameworkSlug) {
                $frameworkArchive = Join-Path $TempRoot "framework-archive.zip"
                $script:LastFrameworkArchiveUrl = "https://api.github.com/repos/$frameworkSlug/zipball/$($frameworkDependency.ref)"
                Log "Downloading automation-framework pinned in pyproject.toml"
                Download-File -Url $script:LastFrameworkArchiveUrl -TargetPath $frameworkArchive
                $frameworkRoot = Extract-ArchiveFile -ArchivePath $frameworkArchive -OutputDir (Join-Path $TempRoot "framework-extracted")
                Log "Installing automation-framework from downloaded source"
                & $script:UvBin pip install --python $VenvPython $frameworkRoot
                return
            }
        }

        Log "Installing automation-framework directly from pyproject.toml"
        & $script:UvBin pip install --python $VenvPython $frameworkSource
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

    function Write-BrowserProfiles([string]$InstallDir) {
        $configDir = Join-Path $InstallDir "config"
        New-Item -ItemType Directory -Force -Path $configDir | Out-Null
        @'
{
  "local-chrome": {
    "provider": "chrome_cdp",
    "profile_id": "local-chrome",
    "metadata": {
      "debug_http": "http://127.0.0.1:9222"
    }
  }
}
'@ | Set-Content -LiteralPath (Join-Path $configDir "browser_profiles.json") -Encoding UTF8
    }

    function Write-SkillLocalEnv([string]$SkillDir, [string]$InstallDir, [string]$TableUrl, [string]$Token) {
        @"
INSTALL_DIR=$InstallDir
TABLE_URL=$TableUrl
FEISHU_ACCESS_TOKEN=$Token
"@ | Set-Content -LiteralPath (Join-Path $SkillDir "skill.local.env") -Encoding UTF8
    }

    function Normalize-KeyValueEntry([string]$Value) {
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

    function Read-KeyValueFile([string]$Path) {
        $map = @{}
        if (-not (Test-Path -LiteralPath $Path)) {
            return $map
        }

        foreach ($line in Get-Content -LiteralPath $Path) {
            $trimmed = $line.Trim()
            if ([string]::IsNullOrWhiteSpace($trimmed)) { continue }
            if ($trimmed.StartsWith("#")) { continue }
            $parts = $line.Split("=", 2)
            if ($parts.Count -ne 2) { continue }
            $key = Normalize-KeyValueEntry $parts[0]
            $value = Normalize-KeyValueEntry $parts[1]
            if ([string]::IsNullOrWhiteSpace($key)) { continue }
            $map[$key] = $value
        }
        return $map
    }

    function Write-DeployState([string]$InstallDir, [string]$RepoUrl, [string]$ResolvedRef, [string]$RepoArchiveUrl, [string]$FrameworkArchiveUrl) {
        $deployDir = Join-Path $InstallDir "runtime\deployment"
        New-Item -ItemType Directory -Force -Path $deployDir | Out-Null

        @"
REPO_URL=$RepoUrl
LAST_RESOLVED_REF=$ResolvedRef
REPO_ARCHIVE_URL=$RepoArchiveUrl
FRAMEWORK_ARCHIVE_URL=$FrameworkArchiveUrl
"@ | Set-Content -LiteralPath (Join-Path $deployDir "openclaw-deploy.env") -Encoding UTF8
    }

    function Smoke-Check([string]$InstallDir, [string]$TargetSkillDir) {
        $cliPath = Join-Path $InstallDir ".venv\Scripts\automation-business-scaffold-run.exe"
        if (-not (Test-Path -LiteralPath $cliPath)) {
            Fail "Smoke check failed: $cliPath is missing."
        }

        $tasksFile = Join-Path $TempRoot "tasks.json"
        & $cliPath list-tasks | Set-Content -LiteralPath $tasksFile -Encoding UTF8

        $payload = Get-Content -Raw -LiteralPath $tasksFile | ConvertFrom-Json
        $taskNames = @($payload.tasks | ForEach-Object { $_.name })
        foreach ($required in @(
            "refresh_current_competitor_table",
            "refresh_competitor_row_by_url",
            "tiktok_fastmoss_product_ingest",
            "search_keyword_competitor_products",
            "sync_tk_influencer_pool"
        )) {
            if ($taskNames -notcontains $required) {
                Fail "Smoke check failed: missing task $required."
            }
        }

        foreach ($fileName in @(
            "SKILL.md",
            "skill.local.env",
            "skill.local.env.example",
            "run_refresh_current_competitor_table_step.sh",
            "run_competitor_row_by_url_step.sh",
            "run_product_url_complete_step.sh",
            "run_keyword_search_step.sh",
            "run_influencer_pool_sync_step.sh",
            "run_skill_step.py",
            "lightweight_submit.py",
            "start_browser_cdp.sh",
            "start_browser_cdp.ps1"
        )) {
            if (-not (Test-Path -LiteralPath (Join-Path $TargetSkillDir $fileName))) {
                Fail "Smoke check failed: missing $(Join-Path $TargetSkillDir $fileName)."
            }
        }

        Test-SkillFrontmatter (Join-Path $TargetSkillDir "SKILL.md")
    }

    Ensure-Uv
    Ensure-Python311

    $openClawSkillsDir = Join-Path $env:USERPROFILE ".openclaw\workspace\skills"
    $existingSkillEnvPath = Join-Path $openClawSkillsDir "mujitask-tiktok-feishu-sync\skill.local.env"
    $existingSkillConfig = Read-KeyValueFile -Path $existingSkillEnvPath
    $defaultInstallDir = if ($existingSkillConfig["INSTALL_DIR"]) { [string]$existingSkillConfig["INSTALL_DIR"] } else { Join-Path $env:USERPROFILE "apps\mujitask" }

    if ([string]::IsNullOrWhiteSpace($script:GitHubToken)) {
        $gitHubTokenInput = PromptOptionalSecret "GitHub PAT for private GitHub repos (optional, press Enter to skip)"
        if (-not [string]::IsNullOrWhiteSpace($gitHubTokenInput)) {
            $script:GitHubToken = $gitHubTokenInput
        }
    }
    $tag = PromptOptional "Tag (leave blank to auto-resolve latest)" ""
    $installDir = Prompt "Install directory" $defaultInstallDir

    $deployStatePath = Join-Path $installDir "runtime\deployment\openclaw-deploy.env"
    $deployState = Read-KeyValueFile -Path $deployStatePath

    if ($deployState["REPO_URL"]) {
        $repoUrl = [string]$deployState["REPO_URL"]
        Log "Reusing existing repo_url from $deployStatePath"
    } else {
        $repoUrl = Prompt "Repo URL"
    }

    if ($deployState["LAST_RESOLVED_REF"]) {
        Log "Current installed ref: $($deployState["LAST_RESOLVED_REF"])"
    }

    if ($existingSkillConfig["TABLE_URL"]) {
        $tableUrl = [string]$existingSkillConfig["TABLE_URL"]
        Log "Reusing existing Feishu table URL from $existingSkillEnvPath"
    } else {
        $tableUrl = Prompt "Feishu table URL"
    }

    if ($existingSkillConfig["FEISHU_ACCESS_TOKEN"]) {
        $token = [string]$existingSkillConfig["FEISHU_ACCESS_TOKEN"]
        Log "Reusing existing Feishu access token from $existingSkillEnvPath"
    } else {
        $tokenSecure = Read-Host "Feishu access token" -AsSecureString
        $token = [System.Net.NetworkCredential]::new("", $tokenSecure).Password
        if ([string]::IsNullOrWhiteSpace($token)) {
            Fail "Feishu access token is required."
        }
    }

    $repoArchive = Join-Path $TempRoot "project-archive.zip"
    $resolvedRef = $tag
    $archiveUrl = $null
    $githubSlug = Parse-GitHubSlug $repoUrl

    if ($githubSlug) {
        if ([string]::IsNullOrWhiteSpace($resolvedRef)) {
            Log "Resolving latest release/tag for $githubSlug"
            $resolvedRef = Resolve-LatestGitHubRef $githubSlug
        }
        $archiveUrl = "https://api.github.com/repos/$githubSlug/zipball/$resolvedRef"
    } else {
        if ($deployState["REPO_ARCHIVE_URL"]) {
            $archiveUrl = [string]$deployState["REPO_ARCHIVE_URL"]
            Log "Reusing existing repository archive URL from $deployStatePath"
        } else {
            $archiveUrl = Prompt "Archive URL for the repository source package"
        }
        if ($archiveUrl.ToLower().EndsWith(".tar.gz") -or $archiveUrl.ToLower().EndsWith(".tgz") -or $archiveUrl.ToLower().EndsWith(".tar")) {
            $repoArchive = Join-Path $TempRoot "project-archive.tar.gz"
        }
        if ([string]::IsNullOrWhiteSpace($resolvedRef)) {
            $resolvedRef = "custom-archive"
        }
    }

    Log "Downloading project archive"
    Download-File -Url $archiveUrl -TargetPath $repoArchive
    $projectRoot = Extract-ArchiveFile -ArchivePath $repoArchive -OutputDir (Join-Path $TempRoot "project-extracted")

    Prepare-TargetDir -TargetDir $installDir
    Copy-DirectoryContents -SourceDir $projectRoot -DestinationDir $installDir

    $pyprojectPath = Join-Path $installDir "pyproject.toml"
    if (-not (Test-Path -LiteralPath $pyprojectPath)) {
        Fail "Missing $pyprojectPath after extraction."
    }

    Log "Creating project virtual environment"
    & $script:UvBin venv --python 3.11 (Join-Path $installDir ".venv") | Out-Null

    $venvPython = Join-Path $installDir ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Fail "Virtual environment python was not created."
    }

    Install-FrameworkFromPyproject -PyprojectPath $pyprojectPath -VenvPython $venvPython

    $projectDeps = @(Read-ProjectDependencies -PyprojectPath (Join-Path $installDir "pyproject.toml"))
    if ($projectDeps.Count -gt 0) {
        Log "Installing project runtime dependencies"
        & $script:UvBin pip install --python $venvPython @projectDeps
    }

    Log "Installing project package"
    & $script:UvBin pip install --python $venvPython -e $installDir --no-deps

    Log "Installing Playwright Chromium"
    & $venvPython -m playwright install chromium

    New-Item -ItemType Directory -Force -Path (Join-Path $installDir "runtime\cli_runs") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $installDir "runtime\artifacts") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $installDir "runtime\downloads") | Out-Null
    Write-BrowserProfiles -InstallDir $installDir

    $chromeExe = Find-Chrome
    if (-not $chromeExe) {
        Warn "Google Chrome was not found."
        Warn "Install Chrome and rerun this deployment script."
        exit 2
    }

    $sourceSkillDir = Join-Path $installDir "skills\mujitask-tiktok-feishu-sync"
    $targetSkillDir = Join-Path $openClawSkillsDir "mujitask-tiktok-feishu-sync"

    if (-not (Test-Path -LiteralPath $sourceSkillDir)) {
        Fail "Missing skill bundle at $sourceSkillDir."
    }

    Replace-TargetDir -TargetDir $targetSkillDir
    Copy-DirectoryContents -SourceDir $sourceSkillDir -DestinationDir $targetSkillDir
    Write-SkillLocalEnv -SkillDir $targetSkillDir -InstallDir $installDir -TableUrl $tableUrl -Token $token
    Write-DeployState -InstallDir $installDir -RepoUrl $repoUrl -ResolvedRef $resolvedRef -RepoArchiveUrl $archiveUrl -FrameworkArchiveUrl $script:LastFrameworkArchiveUrl

    Smoke-Check -InstallDir $installDir -TargetSkillDir $targetSkillDir

    Log "Deployment completed."
    Log "Installed ref: $resolvedRef"
    Log "Project directory: $installDir"
    Log "OpenClaw skill directory: $targetSkillDir"
    Log "Chrome binary: $chromeExe"
}
finally {
    if (Test-Path -LiteralPath $TempRoot) {
        Remove-Item -LiteralPath $TempRoot -Recurse -Force
    }
}
