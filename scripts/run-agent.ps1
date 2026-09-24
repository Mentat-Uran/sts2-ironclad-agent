param(
    [ValidateSet("mcp", "monitor")]
    [string]$Mode = "mcp"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$localSettings = Join-Path $projectRoot "local.settings.ps1"
if (Test-Path -LiteralPath $localSettings -PathType Leaf) {
    . $localSettings
}
if ([string]::IsNullOrWhiteSpace($env:STS2_AGENT_CONFIG)) {
    $env:STS2_AGENT_CONFIG = Join-Path $projectRoot "config.example.toml"
}
$env:PYTHONUTF8 = "1"

# Keep the Luna credential in this launcher process and its child only. Never
# write it to a config file, command line, log, or Windows user environment.
# Codex's sanitized MCP subprocess environment can omit PROGRAMDATA. Windows
# OpenSSH uses that system variable to locate the machine-level ssh_config.
if ([string]::IsNullOrWhiteSpace($env:PROGRAMDATA)) {
    $machineProgramData = [Environment]::GetEnvironmentVariable("ProgramData", "Machine")
    if ([string]::IsNullOrWhiteSpace($machineProgramData) -and $env:SystemDrive) {
        $machineProgramData = Join-Path $env:SystemDrive "ProgramData"
    }
    if (-not [string]::IsNullOrWhiteSpace($machineProgramData)) {
        $env:PROGRAMDATA = $machineProgramData
    }
}

$originalApiKey = $env:STS2_LUNA_API_KEY
$apiKeyWasLoadedByLauncher = $false
if ([string]::IsNullOrWhiteSpace($originalApiKey)) {
    $sshTarget = $env:STS2_LUNA_KEY_SSH_TARGET
    $remoteKeyPath = $env:STS2_LUNA_KEY_REMOTE_PATH
    if (-not [string]::IsNullOrWhiteSpace($sshTarget) -and
        $sshTarget -match '^[A-Za-z0-9][A-Za-z0-9._@:-]*$' -and
        -not [string]::IsNullOrWhiteSpace($remoteKeyPath) -and
        $remoteKeyPath -match '^[A-Za-z0-9_./-]+$') {
        try {
            $sshCommand = Get-Command ssh.exe -ErrorAction Stop
            # -n is essential for an MCP stdio server: SSH must not read the
            # pending JSON-RPC initialize message from the same stdin pipe.
            $remoteCommand = "cat -- $remoteKeyPath"
            $sshOutput = & $sshCommand.Source -n -o BatchMode=yes -o ConnectTimeout=5 `
                $sshTarget $remoteCommand 2>$null
            if ($LASTEXITCODE -eq 0) {
                $retrievedKey = (@($sshOutput) -join "`n").Trim()
                if (-not [string]::IsNullOrWhiteSpace($retrievedKey)) {
                    $env:STS2_LUNA_API_KEY = $retrievedKey
                    $apiKeyWasLoadedByLauncher = $true
                }
            }
        } catch {
            # Continue without Luna credentials; strategic Luna operations pause safely.
        }
    }
}

if ([string]::IsNullOrWhiteSpace($env:STS2_LUNA_API_KEY)) {
    [Console]::Error.WriteLine("[sts2-agent] Luna credentials are not available; Luna-required choices will pause safely.")
}

Set-Location $projectRoot
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Project virtual environment is missing. Run 'uv sync' before starting the agent."
}
if ($Mode -eq "monitor") {
    & $pythonExe -m sts2_agent.monitor
} else {
    & $pythonExe -m sts2_agent.mcp_server
}
$agentExitCode = $LASTEXITCODE

if ($apiKeyWasLoadedByLauncher) {
    Remove-Item Env:STS2_LUNA_API_KEY -ErrorAction SilentlyContinue
}
exit $agentExitCode
