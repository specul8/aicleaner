<#
.SYNOPSIS
    Starts the aicleaner proxy from the venv created by Install-AiCleaner-VSCode.ps1.

.DESCRIPTION
    Idempotent: if /healthz already answers on the configured port, reports that and exits.
    Otherwise runs "python -m aicleaner run" in the foreground (default) or as a detached hidden
    process (-Detach), waits for /healthz, and prints the mapping summary. The proxy's own
    evidence log lives under logs\ next to this script; this transcript covers only the start-up.

.PARAMETER Port
    Port to health-check. Default 8787 (must match aicleaner-config.json).

.PARAMETER Detach
    Start the proxy as a hidden background process and return. Used by the logon task.

.PARAMETER ConfigPath
    Path to aicleaner-config.json. Default: next to this script.

.PARAMETER LogPath
    Transcript log path. Defaults to Start-AiCleaner.log next to the script. Pass '' to disable.

.EXAMPLE
    .\Start-AiCleaner.ps1
    Runs the proxy in this console; Ctrl+C stops it.

.EXAMPLE
    .\Start-AiCleaner.ps1 -Detach
    Starts it hidden and returns once /healthz answers.

.NOTES
    Contact:       see contact-*.json stored alongside this script
    Last-modified: 2026-09-17
    Requires:      Windows PowerShell 5.1 or PowerShell 7+; Install-AiCleaner-VSCode.ps1 run first
#>

#Requires -Version 5.1

[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)][int]$Port = 8787,
    [switch]$Detach,
    [string]$ConfigPath = '',
    [AllowEmptyString()][string]$LogPath = $(
        $base = if ($PSCommandPath) { [IO.Path]::GetFileNameWithoutExtension($PSCommandPath) } else { 'Start-AiCleaner' }
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

function Test-Proxy {
    param([int]$ProxyPort)
    try { return Invoke-RestMethod -Uri "http://127.0.0.1:$ProxyPort/healthz" -TimeoutSec 2 }
    catch { return $null }
}

function Move-ProxyLog {
    <#
    .SYNOPSIS
    Moves the previous proxy-stderr.log and proxy-stdout.log aside before a start overwrites them.
    .DESCRIPTION
    Each start redirects into the same two files, so a restart used to empty the one log that could
    explain why the restart was needed (DECISIONS 47). A non-empty file is renamed
    proxy-stderr-yyyyMMdd-HHmmss.log - the time it would have been overwritten - and only the newest
    -Keep of each are kept. Never throws: a log that cannot be moved must not stop the proxy starting.
    #>
    param(
        [Parameter(Mandatory)][string]$LogDir,
        [int]$Keep = 30
    )
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    foreach ($name in @('proxy-stderr', 'proxy-stdout')) {
        $current = Join-Path $LogDir "$name.log"
        try {
            $file = Get-Item -LiteralPath $current -ErrorAction SilentlyContinue
            if ($file -and $file.Length -gt 0) {
                Move-Item -LiteralPath $current -Destination (Join-Path $LogDir "$name-$stamp.log") -Force
            }
            elseif ($file) {
                Remove-Item -LiteralPath $current -Force
            }
            $old = @(Get-ChildItem -LiteralPath $LogDir -Filter "$name-*.log" -File | Sort-Object Name -Descending)
            if ($old.Count -gt $Keep) {
                $old | Select-Object -Skip $Keep | Remove-Item -Force -ErrorAction SilentlyContinue
            }
        }
        catch {
            Write-Warning "Could not keep the previous $name.log: $($_.Exception.Message)"
        }
    }
}

$transcriptStarted = $false
if ($LogPath) {
    try { Start-Transcript -Path $LogPath -Force | Out-Null; $transcriptStarted = $true }
    catch { Write-Status "Could not start transcript at ${LogPath}: $($_.Exception.Message)" -Level Warn }
}

try {
    $root = $script:AppFolder
    if (-not $ConfigPath) { $ConfigPath = Join-Path $root 'aicleaner-config.json' }
    # Shared venv lives outside synced folders; a repo-local .venv from an earlier install still works.
    $sharedPython = Join-Path $env:USERPROFILE '.aicleaner\venv\Scripts\python.exe'
    $venvPython = $sharedPython
    if (-not (Test-Path -LiteralPath $venvPython)) { $venvPython = Join-Path $root '.venv\Scripts\python.exe' }

    $health = Test-Proxy -ProxyPort $Port
    if ($health) {
        Write-Status "aicleaner already running on port $Port (clients=$($health.clients -join ','))" -Level Done
        exit 0
    }
    if (-not (Test-Path -LiteralPath $venvPython)) {
        # Name the venv installs actually use, not the old fallback beside the code nobody creates.
        Write-Status "No Python environment at $sharedPython; run .\Install-AiCleaner-VSCode.ps1 (or .\Deploy-AiCleaner.ps1) to create it" -Level Error
        exit 1
    }
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        Write-Status "config not found at $ConfigPath; run .\Install-AiCleaner-VSCode.ps1 first" -Level Error
        exit 1
    }
    $logDir = Join-Path $root 'logs'
    if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

    if ($Detach) {
        Move-ProxyLog -LogDir $logDir
        Write-Status "Starting aicleaner hidden on port $Port (usually a few seconds; gives up after 15)"
        $proc = Start-Process -FilePath $venvPython -ArgumentList @('-m', 'aicleaner', 'run', '--config', "`"$ConfigPath`"") `
            -WorkingDirectory $root -WindowStyle Hidden -PassThru `
            -RedirectStandardError (Join-Path $logDir 'proxy-stderr.log') `
            -RedirectStandardOutput (Join-Path $logDir 'proxy-stdout.log')
        Write-Status "Process $($proc.Id) started; waiting for http://127.0.0.1:$Port/healthz to answer" -Level Detail
        # Poll rather than sleep a fixed time; the venv start-up varies with disk cache state. Say
        # so every few seconds, so a slow start does not look like a hang.
        $started = Get-Date
        $deadline = $started.AddSeconds(15)
        $nextNote = $started.AddSeconds(3)
        do {
            Start-Sleep -Milliseconds 500
            $health = Test-Proxy -ProxyPort $Port
            if (-not $health -and (Get-Date) -ge $nextNote -and -not $proc.HasExited) {
                Write-Status ("still starting ({0:N0}s)" -f ((Get-Date) - $started).TotalSeconds) -Level Detail
                $nextNote = (Get-Date).AddSeconds(3)
            }
        } while (-not $health -and (Get-Date) -lt $deadline -and -not $proc.HasExited)
        if ($health) {
            $took = ((Get-Date) - $started).TotalSeconds
            Write-Status ("aicleaner up in {0:N1}s (pid {1}) clients={2} forms={3}" -f $took, $proc.Id, ($health.clients -join ','), $health.forms) -Level Done
            if (-not $health.forms) {
                Write-Status 'The mapping is empty: nothing is being tokenised. Check the clients folder in aicleaner-config.json.' -Level Warn
            }
        } elseif ($proc.HasExited) {
            Write-Status "aicleaner exited straight away (code $($proc.ExitCode)); see $(Join-Path $logDir 'proxy-stderr.log')" -Level Error
            exit 1
        } else {
            Write-Status "aicleaner did not answer /healthz within 15s; see $(Join-Path $logDir 'proxy-stderr.log')" -Level Error
            exit 1
        }
    } else {
        Write-Status "Starting aicleaner on port $Port (Ctrl+C to stop)"
        # From the app folder, as the detached start already does with -WorkingDirectory: the package
        # is imported from there, not from the venv.
        Push-Location -LiteralPath $root
        try { & $venvPython -m aicleaner run --config $ConfigPath }
        finally { Pop-Location }
        exit $LASTEXITCODE
    }
}
catch {
    Write-Status "Start failed: $($_.Exception.Message)" -Level Error
    exit 1
}
finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
