param(
    [Parameter(Mandatory = $true)]
    [string]$GameDir
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$resolvedGameDir = (Resolve-Path -LiteralPath $GameDir).Path
$releaseInfoPath = Join-Path $resolvedGameDir "release_info.json"
$modsDir = Join-Path $resolvedGameDir "mods"
$sourceDll = Join-Path $projectRoot "vendor\STS2MCP\out\STS2_MCP\STS2_MCP.dll"
$sourceManifest = Join-Path $projectRoot "vendor\STS2MCP\mod_manifest.json"
$targetDll = Join-Path $modsDir "STS2_MCP.dll"
$targetManifest = Join-Path $modsDir "STS2_MCP.json"
$expectedVersion = "v0.107.1"
$expectedModVersion = "0.4.4"

if (-not (Test-Path -LiteralPath $releaseInfoPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $modsDir -PathType Container) -or
    -not (Test-Path -LiteralPath $sourceDll -PathType Leaf) -or
    -not (Test-Path -LiteralPath $sourceManifest -PathType Leaf) -or
    -not (Test-Path -LiteralPath $targetDll -PathType Leaf) -or
    -not (Test-Path -LiteralPath $targetManifest -PathType Leaf)) {
    throw "Required game metadata, build files, or existing STS2_MCP targets are missing; nothing was installed."
}

$runningGame = Get-Process -Name "SlayTheSpire2" -ErrorAction SilentlyContinue
if ($runningGame) {
    throw "SlayTheSpire2.exe is running (PID $($runningGame[0].Id)); close the game before replacing its Mod files."
}

$release = Get-Content -LiteralPath $releaseInfoPath -Raw | ConvertFrom-Json
if ($release.version -ne $expectedVersion) {
    throw "Installed game is $($release.version), but this Mod build was validated for $expectedVersion; nothing was installed."
}
$manifest = Get-Content -LiteralPath $sourceManifest -Raw | ConvertFrom-Json
if ($manifest.id -ne "STS2_MCP" -or $manifest.version -ne $expectedModVersion) {
    throw "Build manifest identity/version mismatch; expected STS2_MCP $expectedModVersion."
}
$installedManifest = Get-Content -LiteralPath $targetManifest -Raw | ConvertFrom-Json
if ($installedManifest.id -ne "STS2_MCP") {
    throw "Existing target manifest is not STS2_MCP; refusing to overwrite it."
}

$modsFull = [System.IO.Path]::GetFullPath($modsDir).TrimEnd('\') + '\'
$targetPaths = @(
    [System.IO.Path]::GetFullPath($targetDll),
    [System.IO.Path]::GetFullPath($targetManifest)
)
$unrelatedBefore = @{}
foreach ($file in Get-ChildItem -LiteralPath $modsDir -File -Recurse -Force) {
    $full = [System.IO.Path]::GetFullPath($file.FullName)
    if ($targetPaths -contains $full) { continue }
    $relative = $full.Substring($modsFull.Length)
    $unrelatedBefore[$relative] = (Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash
}

$stamp = Get-Date -Format "yyyyMMddTHHmmssfff"
$backupDir = Join-Path $projectRoot "runtime\mod-backups\$stamp"
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
Copy-Item -LiteralPath $targetDll -Destination (Join-Path $backupDir "STS2_MCP.dll")
Copy-Item -LiteralPath $targetManifest -Destination (Join-Path $backupDir "STS2_MCP.json")

$stageDll = Join-Path $modsDir ".STS2_MCP.dll.installing"
$stageManifest = Join-Path $modsDir ".STS2_MCP.json.installing"
$sourceHash = (Get-FileHash -LiteralPath $sourceDll -Algorithm SHA256).Hash
try {
    Copy-Item -LiteralPath $sourceDll -Destination $stageDll -Force
    Copy-Item -LiteralPath $sourceManifest -Destination $stageManifest -Force
    if ((Get-FileHash -LiteralPath $stageDll -Algorithm SHA256).Hash -ne $sourceHash) {
        throw "Staged DLL hash differs from the built DLL."
    }

    Move-Item -LiteralPath $stageDll -Destination $targetDll -Force
    Move-Item -LiteralPath $stageManifest -Destination $targetManifest -Force
    $installedNow = Get-Content -LiteralPath $targetManifest -Raw | ConvertFrom-Json
    $installedHash = (Get-FileHash -LiteralPath $targetDll -Algorithm SHA256).Hash
    if ($installedNow.id -ne "STS2_MCP" -or $installedNow.version -ne $expectedModVersion -or $installedHash -ne $sourceHash) {
        throw "Installed Mod verification failed."
    }
} catch {
    Copy-Item -LiteralPath (Join-Path $backupDir "STS2_MCP.dll") -Destination $targetDll -Force
    Copy-Item -LiteralPath (Join-Path $backupDir "STS2_MCP.json") -Destination $targetManifest -Force
    Remove-Item -LiteralPath $stageDll, $stageManifest -Force -ErrorAction SilentlyContinue
    throw
}

$unrelatedAfter = @{}
foreach ($file in Get-ChildItem -LiteralPath $modsDir -File -Recurse -Force) {
    $full = [System.IO.Path]::GetFullPath($file.FullName)
    if ($targetPaths -contains $full) { continue }
    $relative = $full.Substring($modsFull.Length)
    $unrelatedAfter[$relative] = (Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash
}
$changedUnrelated = @(
    $unrelatedBefore.Keys | Where-Object { -not $unrelatedAfter.ContainsKey($_) -or $unrelatedAfter[$_] -ne $unrelatedBefore[$_] }
)
$addedUnrelated = @($unrelatedAfter.Keys | Where-Object { -not $unrelatedBefore.ContainsKey($_) })
if ($changedUnrelated.Count -or $addedUnrelated.Count) {
    throw "Unrelated files in the game's mods directory changed during installation; inspect before proceeding."
}

[pscustomobject]@{
    GameVersion = $release.version
    ModId = $installedNow.id
    ModVersion = $installedNow.version
    InstalledDllSha256 = $installedHash
    BackupDirectory = $backupDir
    UnrelatedModFilesChanged = 0
} | ConvertTo-Json -Compress
