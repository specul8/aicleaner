<#
.SYNOPSIS
    Snapshots the Claude Desktop third-party profile (Claude-3p) into a dated zip on this PC.

.DESCRIPTION
    Claude Desktop in gateway mode keeps all history locally in its Claude-3p profile, so once the
    claude.ai copy is deleted this folder is the only copy. This script mirrors the profile's data
    folders (caches and logs excluded) to a staging area with robocopy, zips the result into
    <BackupRoot>\claude-3p-<host>-<yyyyMMdd-HHmmss>.zip, and prunes old zips beyond -Keep.

    The history holds real client names, so the zips stay on this PC and never go to OneDrive
    (DECISIONS 54, 2026-10-01): they get the protection the rest of the client data on the device
    has. The cost, accepted: a lost or rebuilt PC loses this history.

    While the app is open it holds some files (its LevelDB and cookie stores) locked; robocopy
    cannot copy those. When the only failures are such locked files the snapshot is kept, named
    -partial, with a warning, and older snapshots are not pruned after it. Any other copy failure
    fails the run. Close the app first, or let the task run at a quiet time (03:00 by default),
    for a complete snapshot.

.PARAMETER BackupRoot
    Folder that receives the zips. Default: %USERPROFILE%\.aicleaner\backups\claude-3p. A folder
    inside any OneDrive is replaced by that default, with a warning.

.PARAMETER Keep
    Number of zips to retain. Older ones are deleted after a successful new snapshot. Default 14.

.PARAMETER Staging
    Working folder for the mirror before zipping. Default %TEMP%\claude-3p-snapshot.

.PARAMETER LogPath
    Transcript log path. Defaults to Backup-ClaudeDesktopProfile.log next to the script. Pass '' to disable.

.EXAMPLE
    .\Backup-ClaudeDesktopProfile.ps1
    Snapshot now into %USERPROFILE%\.aicleaner\backups\claude-3p with default retention.

.EXAMPLE
    .\Backup-ClaudeDesktopProfile.ps1 -BackupRoot D:\Backups\Claude -Keep 30 -WhatIf

.NOTES
    Contact:       see contact-*.json stored alongside this script
    Last-modified: 2026-09-20
    Requires:      Windows PowerShell 5.1 or PowerShell 7+; robocopy (in-box)
#>

#Requires -Version 5.1

[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$BackupRoot = '',
    [ValidateRange(1, 365)][int]$Keep = 14,
    [string]$Staging = (Join-Path $env:TEMP 'claude-3p-snapshot'),
    [AllowEmptyString()][string]$LogPath = $(
        $base = if ($PSCommandPath) { [IO.Path]::GetFileNameWithoutExtension($PSCommandPath) } else { 'Backup-ClaudeDesktopProfile' }
        $dir = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }
        Join-Path $dir "$base.log"
    )
)
# The app folder: this script's own, or its parent when it sits in installer\ (feature 102).
$script:AppFolder = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- Contact attribution (inlined per script so each script stays self-contained) ---------
function Get-ContactInfo {
    <#
    .SYNOPSIS
        The first contact-*.json beside this script (ordinal order of the lower-cased name) as an
        object, or $null when there is none or it does not parse. Only the maintainer's machine
        has more than one; a deployment has exactly one.
    #>
    param([string]$Folder = $script:AppFolder)
    $candidates = @(Get-ChildItem -LiteralPath $Folder -Filter 'contact-*.json' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like 'contact-*.json' })
    if ($candidates.Count -eq 0) { return $null }
    $sortKeys = [string[]]@($candidates | ForEach-Object { $_.Name.ToLowerInvariant() })
    $sorted = [System.IO.FileInfo[]]$candidates
    [Array]::Sort($sortKeys, $sorted, [System.StringComparer]::Ordinal)
    try { return (Get-Content -LiteralPath $sorted[0].FullName -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}

function Write-ContactAttribution {
    <# One line naming who maintains this deployment; nothing when there is no contact file. #>
    $contact = Get-ContactInfo
    if (-not $contact) { return }
    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($field in @('organisation', 'name', 'email', 'issues_url')) {
        if ($contact.PSObject.Properties[$field] -and $contact.$field) { $parts.Add([string]$contact.$field) }
    }
    if ($parts.Count -gt 0) { Write-Host ('aicleaner | ' + ($parts -join ' | ')) }
}

Write-ContactAttribution

function Write-Status {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory, Position = 0)][AllowEmptyString()][string]$Message,
        [ValidateSet('Step', 'Detail', 'Done', 'Warn', 'Error')][string]$Level = 'Step'
    )
    switch ($Level) {
        'Step'   { Write-Host "==> $Message" -ForegroundColor Cyan }
        'Detail' { Write-Host "    $Message" }
        'Done'   { Write-Host "==> $Message" -ForegroundColor Green }
        'Warn'   { Write-Warning $Message }
        'Error'  { Write-Host "==> $Message" -ForegroundColor Red }
    }
}

function Get-RobocopyFailure {
    <#
    .SYNOPSIS
        Reads a robocopy log and counts the files that failed: those in use by another process
        (sharing or lock violation, error 32 or 33, what Claude Desktop's open LevelDB and cookie
        files give) and those that failed for any other reason. Each file is counted once.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$LogPath)
    $locked = New-Object 'System.Collections.Generic.HashSet[string]'
    $other = New-Object 'System.Collections.Generic.HashSet[string]'
    if (Test-Path -LiteralPath $LogPath) {
        foreach ($line in @(Get-Content -LiteralPath $LogPath)) {
            $hit = [regex]::Match([string]$line, 'ERROR (?<code>\d+) \(0x[0-9A-Fa-f]+\) (?<what>.*)$')
            if (-not $hit.Success) { continue }
            if (@('32', '33') -contains $hit.Groups['code'].Value) { [void]$locked.Add($hit.Groups['what'].Value) }
            else { [void]$other.Add($hit.Groups['what'].Value) }
        }
    }
    return [pscustomobject]@{ Locked = $locked.Count; Other = $other.Count }
}

function Test-UnderFolder {
    <# True when Path is Folder or inside it (case-insensitive, full paths). #>
    [OutputType([bool])]
    param([Parameter(Mandatory)][string]$Path, [string]$Folder)
    if (-not $Folder) { return $false }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\') + '\'
    $root = [IO.Path]::GetFullPath($Folder).TrimEnd('\') + '\'
    return $full.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)
}

function Resolve-BackupRoot {
    <#
    .SYNOPSIS
        Where the snapshots go: this PC only, never OneDrive (H20, DECISIONS 54).
    .DESCRIPTION
        The history holds real client names. It used to go to OneDrive in the clear; it now stays
        beside the rest of aicleaner's local data. A folder under any OneDrive is not refused with
        an error - the scheduled run would just stop backing up - but replaced by the local default,
        with a warning that says so.
    #>
    [OutputType([string])]
    param([string]$Requested = '')
    $local = Join-Path $env:USERPROFILE '.aicleaner\backups\claude-3p'
    if (-not $Requested) { return $local }
    foreach ($oneDrive in @($env:OneDriveCommercial, $env:OneDriveConsumer, $env:OneDrive)) {
        if ($oneDrive -and (Test-UnderFolder -Path $Requested -Folder $oneDrive)) {
            Write-Warning "$Requested is in OneDrive; the history backup stays on this PC: using $local"
            return $local
        }
    }
    return $Requested
}

$transcriptStarted = $false
if ($LogPath) {
    try { Start-Transcript -Path $LogPath -Force | Out-Null; $transcriptStarted = $true }
    catch { Write-Status "Could not start transcript at ${LogPath}: $($_.Exception.Message)" -Level Warn }
}

try {
    $BackupRoot = Resolve-BackupRoot -Requested $BackupRoot
    $host_ = ($env:COMPUTERNAME).ToLower()
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $zipPath = Join-Path $BackupRoot "claude-3p-$host_-$stamp.zip"

    # Both profile roots hold data; caches, GPU shader blobs, crash dumps and logs are excluded.
    # The Store build is packaged, so what it writes to %APPDATA% actually lands in its container
    # under %LOCALAPPDATA%\Packages\Claude_<hash>\LocalCache; look in both places.
    $candidates = @(
        @{ Name = 'Roaming'; Path = Join-Path $env:APPDATA 'Claude-3p' },
        @{ Name = 'Local';   Path = Join-Path $env:LOCALAPPDATA 'Claude-3p' }
    )
    foreach ($container in @(Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Packages\Claude_*') `
                             -Directory -ErrorAction SilentlyContinue)) {
        $cache = Join-Path $container.FullName 'LocalCache'
        $candidates += @{ Name = 'PackagedRoaming'; Path = Join-Path $cache 'Roaming\Claude-3p' }
        $candidates += @{ Name = 'PackagedLocal';   Path = Join-Path $cache 'Local\Claude-3p' }
    }
    $sources = @($candidates | Where-Object { Test-Path -LiteralPath $_.Path })
    if ($sources.Count -eq 0) {
        Write-Status 'No Claude-3p profile found on this machine (the app has not run in gateway mode yet); nothing to snapshot' -Level Done
        return
    }
    $excludeDirs = 'Cache', 'Code Cache', 'GPUCache', 'DawnCache', 'DawnGraphiteCache', 'DawnWebGPUCache',
                   'Crashpad', 'logs', 'blob_storage', 'Service Worker', 'Session Storage', 'Partitions', 'CachedData'

    Write-Status "Snapshot of Claude-3p to $BackupRoot"
    if ($PSCmdlet.ShouldProcess($Staging, 'Prepare the staging folder')) {
        if (Test-Path -LiteralPath $Staging) { Remove-Item -LiteralPath $Staging -Recurse -Force }
        New-Item -ItemType Directory -Force $Staging | Out-Null
        New-Item -ItemType Directory -Force $BackupRoot | Out-Null
    }
    $lockedFiles = 0

    foreach ($src in $sources) {
        $dest = Join-Path $Staging $src.Name
        Write-Status "Mirroring $($src.Path)" -Level Detail
        if ($PSCmdlet.ShouldProcess($src.Path, "robocopy to $dest")) {
            # /MIR mirror, /R:1 /W:1 one quick retry on locked files, /XJ skip junctions, /NP no progress noise.
            $roboLog = Join-Path ([IO.Path]::GetTempPath()) ("claude-3p-robocopy-{0}.log" -f $src.Name)
            $roboArgs = @($src.Path, $dest, '/MIR', '/R:1', '/W:1', '/XJ', '/NP', '/NFL', '/NDL', '/NJH', "/LOG:$roboLog", '/XD') +
                $excludeDirs + @('/XF', '*.log', '*.lock')
            $savedEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
            try { & robocopy.exe @roboArgs | Out-Null } finally { $ErrorActionPreference = $savedEap }
            $roboExit = $LASTEXITCODE
            # robocopy: 0-7 success (1=copied, 2=extras, 4=mismatches); 8 and above = some files failed.
            if ($roboExit -ge 8) {
                $failure = Get-RobocopyFailure -LogPath $roboLog
                if ($failure.Other -gt 0 -or $failure.Locked -eq 0) {
                    throw "robocopy failed for $($src.Path) (exit $roboExit, $($failure.Other) file(s) failed); see $roboLog"
                }
                $lockedFiles += $failure.Locked
                Write-Status "$($failure.Locked) file(s) in $($src.Name) are held open by Claude Desktop and were skipped" -Level Warn
            }
            if ($roboExit -lt 8 -or $lockedFiles -gt 0) { Remove-Item -LiteralPath $roboLog -Force -ErrorAction SilentlyContinue }
        }
    }

    if ($lockedFiles -gt 0) { $zipPath = $zipPath -replace '\.zip$', '-partial.zip' }
    if ($PSCmdlet.ShouldProcess($zipPath, 'Compress snapshot')) {
        $fileCount = @(Get-ChildItem -LiteralPath $Staging -Recurse -File).Count
        if ($fileCount -eq 0) {
            Write-Status 'Profile folders exist but hold no data files after exclusions; nothing to snapshot' -Level Done
            Remove-Item -LiteralPath $Staging -Recurse -Force
            return
        }
        Compress-Archive -Path (Join-Path $Staging '*') -DestinationPath $zipPath -CompressionLevel Optimal -Force
        $size = [math]::Round((Get-Item -LiteralPath $zipPath).Length / 1MB, 1)
        Write-Status "Snapshot written ($fileCount files, $size MB)" -Level Done
        Remove-Item -LiteralPath $Staging -Recurse -Force
    }

    # Retention: keep the newest $Keep zips for this host, but never prune after a partial snapshot.
    if ($lockedFiles -gt 0) {
        Write-Status "Partial snapshot ($lockedFiles locked file(s) skipped): older snapshots kept; close Claude Desktop for a complete one" -Level Warn
        return
    }
    $old = Get-ChildItem -LiteralPath $BackupRoot -Filter "claude-3p-$host_-*.zip" | Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep
    foreach ($f in $old) {
        if ($PSCmdlet.ShouldProcess($f.FullName, 'Remove old snapshot')) { Remove-Item -LiteralPath $f.FullName -Force; Write-Status "Pruned $($f.Name)" -Level Detail }
    }
    Write-Status "Retention: $Keep snapshots kept in $BackupRoot" -Level Detail
}
catch {
    Write-Status "Backup failed: $($_.Exception.Message)" -Level Error
    Write-Error $_ -ErrorAction Continue
    exit 1
}
finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
