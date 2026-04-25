[CmdletBinding()]
param(
    [string]$RuntimeRoot = "",
    [string]$ServerName = "skill-runtime",
    [string]$Image = "econragent-mcp-skill-service:latest",
    [string]$SourceRoot = "",
    [string]$ConfigOutputPath = "",
    [switch]$EmitJsonOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-RepoRoot {
    return (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
}

function Ensure-Directory {
    param([string]$Path)

    $item = New-Item -ItemType Directory -Force -Path $Path
    return $item.FullName
}

function Convert-ToDockerMountPath {
    param([string]$Path)

    return ((Resolve-Path -LiteralPath $Path).Path -replace "\\", "/")
}

$repoRoot = Resolve-RepoRoot
$resolvedSourceRoot = ""
if (-not [string]::IsNullOrWhiteSpace($SourceRoot)) {
    $resolvedSourceRoot = (Resolve-Path -LiteralPath $SourceRoot).Path
}
$skillOutputDir = Ensure-Directory -Path (Join-Path $repoRoot "skill_output")
$runtimeVolumes = [ordered]@{
    runs = "mcp_skill_runs"
    state = "mcp_skill_state"
    envs = "mcp_skill_envs"
    wheelhouse = "mcp_skill_wheelhouse"
    "pip-cache" = "mcp_skill_pip_cache"
    locks = "mcp_skill_locks"
}

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
    "-i",
    "-e", "MCP_SKILLS_DIR=$skillsDir",
    "-e", "MCP_WORKSPACE_DIR=/workspace",
    "-e", "MCP_RUNS_DIR=/workspace/runs",
    "-e", "MCP_STATE_DIR=/workspace/state",
    "-e", "MCP_ENVS_DIR=/workspace/envs",
    "-e", "MCP_OUTPUT_DIR=/workspace/output",
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
    "-v", "$(Convert-ToDockerMountPath -Path $skillOutputDir):/workspace/output",
    "-v", "$($runtimeVolumes.runs):/workspace/runs",
    "-v", "$($runtimeVolumes.state):/workspace/state",
    "-v", "$($runtimeVolumes.envs):/workspace/envs",
    "-v", "$($runtimeVolumes.wheelhouse):/workspace/wheelhouse",
    "-v", "$($runtimeVolumes.'pip-cache'):/workspace/pip-cache",
    "-v", "$($runtimeVolumes.locks):/workspace/locks",
    $Image,
    "python",
    $serverEntrypoint
)

$dockerConfig = @(
    [ordered]@{
        name = $ServerName
        command = "docker"
        stdio_framing = "json_lines"
        args = $dockerArgs
        discover_tools = $false
    }
)

$json = ConvertTo-Json -InputObject $dockerConfig -Depth 8 -Compress
if (-not [string]::IsNullOrWhiteSpace($ConfigOutputPath)) {
    $outputDir = Split-Path -Parent $ConfigOutputPath
    if (-not [string]::IsNullOrWhiteSpace($outputDir)) {
        New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
    }
    Set-Content -LiteralPath $ConfigOutputPath -Value $json -Encoding utf8
}

if ($EmitJsonOnly) {
    Write-Output $json
    return
}

Write-Host "Initialized skill runtime mounts:" -ForegroundColor Green
Write-Host ("  {0,-10} {1}" -f "output", $skillOutputDir)
foreach ($entry in $runtimeVolumes.GetEnumerator()) {
    Write-Host ("  {0,-10} {1}" -f $entry.Key, $entry.Value)
}
if (-not [string]::IsNullOrWhiteSpace($resolvedSourceRoot)) {
    Write-Host ("  {0,-10} {1}" -f "source", $resolvedSourceRoot)
}

if (-not [string]::IsNullOrWhiteSpace($ConfigOutputPath)) {
    Write-Host ""
    Write-Host "Saved KG_AGENT_MCP_SERVERS_JSON payload to:" -ForegroundColor Green
    Write-Host "  $ConfigOutputPath"
}

Write-Host ""
Write-Host "KG_AGENT_MCP_SERVERS_JSON:" -ForegroundColor Green
Write-Output $json
Write-Host ""
Write-Host "PowerShell example:" -ForegroundColor Green
Write-Host "  `$env:KG_AGENT_MCP_SERVERS_JSON = '$json'"
