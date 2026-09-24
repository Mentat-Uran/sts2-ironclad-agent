param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$vendorRoot = Join-Path $projectRoot "vendor"
$checkout = Join-Path $vendorRoot "STS2MCP"
$patchFile = Join-Path $projectRoot "patches\STS2MCP-v0.4.4.patch"
$upstream = "https://github.com/Gennadiyev/STS2MCP.git"
$commit = "55e064850a68f3b4cde7e5fd525bf9b2dec4e885"

if (Test-Path -LiteralPath $checkout) {
    throw "vendor\STS2MCP already exists. Review that checkout manually; this script will not overwrite it."
}
if (-not (Test-Path -LiteralPath $patchFile -PathType Leaf)) {
    throw "The pinned STS2MCP patch is missing: $patchFile"
}

New-Item -ItemType Directory -Path $vendorRoot -Force | Out-Null
& git clone --no-checkout $upstream $checkout
if ($LASTEXITCODE -ne 0) { throw "Could not clone the pinned STS2MCP upstream." }

& git -C $checkout checkout --detach $commit
if ($LASTEXITCODE -ne 0) { throw "Could not check out the pinned STS2MCP commit $commit." }

& git -C $checkout apply --check $patchFile
if ($LASTEXITCODE -ne 0) { throw "The project patch does not apply cleanly to the pinned upstream commit." }
& git -C $checkout apply $patchFile
if ($LASTEXITCODE -ne 0) { throw "Could not apply the STS2MCP compatibility patch." }

[pscustomobject]@{
    Upstream = $upstream
    Commit = $commit
    Patch = [System.IO.Path]::GetFileName($patchFile)
    Checkout = $checkout
} | ConvertTo-Json -Compress
