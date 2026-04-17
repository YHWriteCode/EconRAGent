[CmdletBinding()]
param(
    [string]$RuntimeRoot = "",
    [string]$Image = "lightrag-mcp-skill-service:latest",
    [string]$SourceRoot = "",
    [string]$SkillName = "",
    [switch]$All
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-RepoRoot {
    return (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
}

function Convert-ToDockerMountPath {
    param([string]$Path)

    return ((Resolve-Path -LiteralPath $Path).Path -replace "\\", "/")
}

$repoRoot = Resolve-RepoRoot
if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
    $RuntimeRoot = Join-Path $repoRoot ".skill-runtime"
}

$resolvedSourceRoot = ""
if (-not [string]::IsNullOrWhiteSpace($SourceRoot)) {
    $resolvedSourceRoot = (Resolve-Path -LiteralPath $SourceRoot).Path
}

if ($All -and -not [string]::IsNullOrWhiteSpace($SkillName)) {
    throw "Specify either -All or -SkillName, not both."
}

$initScript = Join-Path $PSScriptRoot "init-skill-runtime-host.ps1"
if (-not (Test-Path -LiteralPath $initScript)) {
    throw "Missing helper script: $initScript"
}

& $initScript -RuntimeRoot $RuntimeRoot -Image $Image -SourceRoot $resolvedSourceRoot -EmitJsonOnly | Out-Null

$runtimeRootResolved = (Resolve-Path -LiteralPath $RuntimeRoot).Path
$stateDir = Join-Path $runtimeRootResolved "state"
$wheelhouseDir = Join-Path $runtimeRootResolved "wheelhouse"
$pipCacheDir = Join-Path $runtimeRootResolved "pip-cache"
$locksDir = Join-Path $runtimeRootResolved "locks"
$skillsDir = "/app/skills"
$pythonPathEnv = ""
$serverEntrypoint = "/app/server.py"
if (-not [string]::IsNullOrWhiteSpace($resolvedSourceRoot)) {
    $skillsDir = "/src/skills"
    $pythonPathEnv = "/src"
    $serverEntrypoint = "/src/mcp-server/server.py"
}

$dockerArgs = @(
    "run",
    "--rm",
    "-e", "MCP_SKILLS_DIR=$skillsDir",
    "-e", "MCP_WORKSPACE_DIR=/workspace",
    "-e", "MCP_STATE_DIR=/workspace/state",
    "-e", "MCP_WHEELHOUSE_DIR=/workspace/wheelhouse",
    "-e", "MCP_PIP_CACHE_DIR=/workspace/pip-cache",
    "-e", "MCP_LOCKS_DIR=/workspace/locks"
)
if (-not [string]::IsNullOrWhiteSpace($pythonPathEnv)) {
    $dockerArgs += @("-e", "PYTHONPATH=$pythonPathEnv")
}
if (-not [string]::IsNullOrWhiteSpace($resolvedSourceRoot)) {
    $dockerArgs += @(
        "-v",
        "$(Convert-ToDockerMountPath -Path $resolvedSourceRoot):/src:ro"
    )
}
$dockerArgs += @(
    "-v", "$(Convert-ToDockerMountPath -Path $stateDir):/workspace/state",
    "-v", "$(Convert-ToDockerMountPath -Path $wheelhouseDir):/workspace/wheelhouse",
    "-v", "$(Convert-ToDockerMountPath -Path $pipCacheDir):/workspace/pip-cache",
    "-v", "$(Convert-ToDockerMountPath -Path $locksDir):/workspace/locks",
    $Image,
    "python",
    $serverEntrypoint
)

if ($All -or [string]::IsNullOrWhiteSpace($SkillName)) {
    $dockerArgs += "--prefetch-all-skill-wheels"
}
else {
    $dockerArgs += @("--prefetch-skill-wheels", $SkillName)
}

Write-Host "Prefetching skill wheels into $wheelhouseDir" -ForegroundColor Green
if (-not [string]::IsNullOrWhiteSpace($SkillName)) {
    Write-Host "Target skill: $SkillName"
}
else {
    Write-Host "Target skill: all registered skills"
}
if (-not [string]::IsNullOrWhiteSpace($resolvedSourceRoot)) {
    Write-Host "Source root : $resolvedSourceRoot"
}

& docker @dockerArgs
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Wheel prefetch completed." -ForegroundColor Green
Write-Host "  wheelhouse: $wheelhouseDir"
Write-Host "  pip-cache : $pipCacheDir"
