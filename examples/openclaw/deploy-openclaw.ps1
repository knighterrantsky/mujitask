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

    function Log([string]$Message) {
        Write-Host "[deploy-openclaw] $Message"
    }

    function Warn([string]$Message) {
        Write-Warning "[deploy-openclaw] $Message"
    }

    function Fail([string]$Message) {
        throw "[deploy-openclaw] $Message"
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
        $candidate = (& $script:UvBin python find 3.11 | Select-Object -First 1).Trim()
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            Fail "Could not resolve Python 3.11 after uv installation."
        }
        $script:PythonBin = $candidate
    }

    function Invoke-JsonApi([string]$Url, [string]$OutFile) {
        Invoke-WebRequest -Uri $Url -Headers @{ Accept = "application/vnd.github+json" } -OutFile $OutFile
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
            $backupDir = "$TargetDir.backup-$(Get-Date -Format yyyyMMddHHmmss)"
            Log "Existing directory detected, moving it to $backupDir"
            Move-Item -LiteralPath $TargetDir -Destination $backupDir
        }
        New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
    }

    function Copy-DirectoryContents([string]$SourceDir, [string]$DestinationDir) {
        Get-ChildItem -Force -LiteralPath $SourceDir | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $DestinationDir -Recurse -Force
        }
    }

    function Read-ManifestValue([string]$ManifestPath, [string]$Key) {
        $content = Get-Content -Raw -LiteralPath $ManifestPath
        $pattern = "(?m)^" + [regex]::Escape($Key) + ':\s*"?([^"`r`n]+)"?\s*$'
        $match = [regex]::Match($content, $pattern)
        if (-not $match.Success) {
            Fail "Missing $Key in $ManifestPath"
        }
        return $match.Groups[1].Value.Trim()
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

    function Smoke-Check([string]$InstallDir, [string]$TargetSkillDir) {
        $cliPath = Join-Path $InstallDir ".venv\Scripts\automation-business-scaffold-run.exe"
        if (-not (Test-Path -LiteralPath $cliPath)) {
            Fail "Smoke check failed: $cliPath is missing."
        }

        $tasksFile = Join-Path $TempRoot "tasks.json"
        & $cliPath list-tasks | Set-Content -LiteralPath $tasksFile -Encoding UTF8

        $payload = Get-Content -Raw -LiteralPath $tasksFile | ConvertFrom-Json
        $taskNames = @($payload.tasks | ForEach-Object { $_.name })
        foreach ($required in @("tiktok_product_link_cleanup", "tiktok_feishu_batch_sync")) {
            if ($taskNames -notcontains $required) {
                Fail "Smoke check failed: missing task $required."
            }
        }

        foreach ($fileName in @(
            "SKILL.md",
            "skill.local.env",
            "skill.local.env.example",
            "run_cleanup.sh",
            "run_cleanup.ps1",
            "run_batch_sync.sh",
            "run_batch_sync.ps1",
            "start_browser_cdp.sh",
            "start_browser_cdp.ps1"
        )) {
            if (-not (Test-Path -LiteralPath (Join-Path $TargetSkillDir $fileName))) {
                Fail "Smoke check failed: missing $(Join-Path $TargetSkillDir $fileName)."
            }
        }
    }

    Ensure-Uv
    Ensure-Python311

    $repoUrl = Prompt "Repo URL"
    $tag = Prompt "Tag (leave blank to auto-resolve latest)" ""
    $installDir = Prompt "Install directory" (Join-Path $env:USERPROFILE "apps\mujitask")
    $tableUrl = Prompt "Feishu table URL"
    $tokenSecure = Read-Host "Feishu access token" -AsSecureString
    $token = [System.Net.NetworkCredential]::new("", $tokenSecure).Password
    if ([string]::IsNullOrWhiteSpace($token)) {
        Fail "Feishu access token is required."
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
        $archiveUrl = Prompt "Archive URL for the repository source package"
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

    $manifestPath = Join-Path $installDir ".platform\platform-manifest.yaml"
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        Fail "Missing $manifestPath after extraction."
    }

    $frameworkRepoUrl = Read-ManifestValue -ManifestPath $manifestPath -Key "framework_repo_url"
    $frameworkRef = Read-ManifestValue -ManifestPath $manifestPath -Key "framework_commit"
    $frameworkArchive = Join-Path $TempRoot "framework-archive.zip"
    $frameworkArchiveUrl = $null
    $frameworkSlug = Parse-GitHubSlug $frameworkRepoUrl

    if ($frameworkSlug) {
        $frameworkArchiveUrl = "https://api.github.com/repos/$frameworkSlug/zipball/$frameworkRef"
    } else {
        $frameworkArchiveUrl = Prompt "Framework archive URL for automation-framework"
        if ($frameworkArchiveUrl.ToLower().EndsWith(".tar.gz") -or $frameworkArchiveUrl.ToLower().EndsWith(".tgz") -or $frameworkArchiveUrl.ToLower().EndsWith(".tar")) {
            $frameworkArchive = Join-Path $TempRoot "framework-archive.tar.gz"
        }
    }

    Log "Downloading pinned automation-framework source"
    Download-File -Url $frameworkArchiveUrl -TargetPath $frameworkArchive
    $frameworkRoot = Extract-ArchiveFile -ArchivePath $frameworkArchive -OutputDir (Join-Path $TempRoot "framework-extracted")

    Log "Creating project virtual environment"
    & $script:UvBin venv --python 3.11 (Join-Path $installDir ".venv") | Out-Null

    $venvPython = Join-Path $installDir ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Fail "Virtual environment python was not created."
    }

    Log "Installing pinned automation-framework from local source"
    & $script:UvBin pip install --python $venvPython $frameworkRoot

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

    $openClawSkillsDir = Join-Path $env:USERPROFILE ".openclaw\workspace\skills"
    $sourceSkillDir = Join-Path $installDir "skills\mujitask-tiktok-feishu-sync"
    $targetSkillDir = Join-Path $openClawSkillsDir "mujitask-tiktok-feishu-sync"

    if (-not (Test-Path -LiteralPath $sourceSkillDir)) {
        Fail "Missing skill bundle at $sourceSkillDir."
    }

    Prepare-TargetDir -TargetDir $targetSkillDir
    Copy-DirectoryContents -SourceDir $sourceSkillDir -DestinationDir $targetSkillDir
    Write-SkillLocalEnv -SkillDir $targetSkillDir -InstallDir $installDir -TableUrl $tableUrl -Token $token

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
