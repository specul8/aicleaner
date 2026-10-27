<#
.SYNOPSIS
    Keeps aicleaner running for the start-up task (feature 107): runs it, and runs it again when it stops.

.DESCRIPTION
    The action of the aicleaner-service scheduled task that Set-AiCleanerService.ps1 -Mode User registers: it runs
    at start-up as you, whether you are signed in or not, with the password Windows keeps for the task.

    It runs "python -m aicleaner run --config <app>\aicleaner-config.json" from the shared venv with
    AICLEANER_SERVICE=user in its environment, in the foreground, and when that ends:
      - stops, when %USERPROFILE%\.aicleaner\stopped-on-purpose exists (Stop-AiCleaner.ps1 writes it), so Stop
        stops aicleaner and the loop with it;
      - waits and tries again when the exit code is 3 (the port is taken: another copy is running or starting);
      - otherwise starts it again after 5 seconds - which is how Restart (the process ending) and a crash are
        brought back.
    A stale stopped-on-purpose from before the task started is removed first: the task starting is the intent.
    Each start writes to its own logs\proxy-stderr-<time>.log and proxy-stdout-<time>.log, as Start-AiCleaner does.

    The command is a parameter, so a test can put a stand-in in place of the proxy.

.PARAMETER AppFolder
    aicleaner's app folder. Default: this script's folder, or its parent when it sits in installer\.

.PARAMETER PythonPath
    The program to run. Default: the shared venv's python.exe (%USERPROFILE%\.aicleaner\venv), or the app's own
    .venv when there is no shared one.

.PARAMETER ArgumentList
    Its arguments. Default: -m aicleaner run --config <AppFolder>\aicleaner-config.json.

.PARAMETER StoppedMarker
    The file whose presence means "stopped on purpose". Default %USERPROFILE%\.aicleaner\stopped-on-purpose.

.PARAMETER RetrySeconds
    Wait before starting it again after it stops. Default 5.

.PARAMETER PortTakenSeconds
    Wait before trying again when it found the port taken (exit code 3). Default 30.

.PARAMETER MaxRuns
    Stop after this many runs (0, the default, is no limit). For tests.

.PARAMETER LogPath
    This loop's own log (a transcript). Default Run-AiCleanerService.log in the app folder. '' turns it off.

.EXAMPLE
    .\installer\Run-AiCleanerService.ps1
    What the start-up task runs: aicleaner, kept running until Stop-AiCleaner.ps1 stops it.

.EXAMPLE
    .\installer\Run-AiCleanerService.ps1 -PythonPath powershell.exe -ArgumentList '-NoProfile', '-File', 'C:\Temp\stand-in.ps1' -RetrySeconds 1 -MaxRuns 5 -LogPath ''
    Runs a stand-in instead of the proxy, as the tests do.

.NOTES
    Last-modified: 2026-10-24
    Requires:      Windows PowerShell 5.1 or PowerShell 7+; aicleaner installed for this account
#>

#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$AppFolder = '',
    [string]$PythonPath = '',
    [string[]]$ArgumentList = @(),
    [string]$StoppedMarker = (Join-Path $env:USERPROFILE '.aicleaner\stopped-on-purpose'),
    [ValidateRange(0, 3600)][int]$RetrySeconds = 5,
    [ValidateRange(0, 3600)][int]$PortTakenSeconds = 30,
    [ValidateRange(0, 1000000)][int]$MaxRuns = 0,
    [AllowEmptyString()][string]$LogPath = $(
        $dir = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }
        Join-Path $dir 'Run-AiCleanerService.log'
    )
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$PortTakenExit = 3
if (-not $AppFolder) {
    $AppFolder = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }
}

function Write-Line {
    param([string]$Text, [ValidateSet('Info', 'Done', 'Warn', 'Error')][string]$Level = 'Info')
    $colour = @{ Info = 'Cyan'; Done = 'Green'; Warn = 'Yellow'; Error = 'Red' }[$Level]
    Write-Host ("==> {0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Text) -ForegroundColor $colour
}

function ConvertTo-ArgumentString {
    <# One command line from the arguments. Start-Process joins an array with spaces and quotes nothing, so a
       config path with a space in it would arrive as two arguments. #>
    param([string[]]$Arguments)
    $parts = foreach ($argument in $Arguments) {
        if ($argument -eq '' -or $argument -match '[\s"]') { '"' + ($argument -replace '"', '\"') + '"' } else { $argument }
    }
    return ($parts -join ' ')
}

function Remove-OldLog {
    <# Keeps the newest 30 of each stamped log, as Start-AiCleaner's Move-ProxyLog does. Never throws. #>
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$LogDir, [int]$Keep = 30)
    if (-not $PSCmdlet.ShouldProcess($LogDir, 'Remove all but the newest proxy logs')) { return }
    foreach ($name in 'proxy-stderr', 'proxy-stdout') {
        try {
            Get-ChildItem -LiteralPath $LogDir -Filter "$name-*.log" -File -ErrorAction Stop |
                Sort-Object Name -Descending | Select-Object -Skip $Keep | Remove-Item -Force -ErrorAction SilentlyContinue
        } catch { Write-Verbose "old logs not pruned: $($_.Exception.Message)" }
    }
}

function Invoke-OneRun {
    <# Runs the command once, in the foreground, and returns its exit code. #>
    param([string]$Program, [string[]]$Arguments, [string]$WorkingDirectory, [string]$LogDir)
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $started = Start-Process -FilePath $Program -ArgumentList (ConvertTo-ArgumentString -Arguments $Arguments) `
        -WorkingDirectory $WorkingDirectory -NoNewWindow -PassThru `
        -RedirectStandardError (Join-Path $LogDir "proxy-stderr-$stamp.log") `
        -RedirectStandardOutput (Join-Path $LogDir "proxy-stdout-$stamp.log")
    # The handle is read now: without it, Windows PowerShell 5.1 forgets the exit code of a process it started.
    $null = $started.Handle
    Write-Line "started pid $($started.Id)"
    $started.WaitForExit()
    return [int]$started.ExitCode
}

$transcriptStarted = $false
if ($LogPath) { try { Start-Transcript -Path $LogPath -Force | Out-Null; $transcriptStarted = $true } catch { Write-Verbose "no transcript: $($_.Exception.Message)" } }
try {
    if (-not $PythonPath) {
        $PythonPath = Join-Path $env:USERPROFILE '.aicleaner\venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $PythonPath)) { $PythonPath = Join-Path $AppFolder '.venv\Scripts\python.exe' }
    }
    if (-not $ArgumentList -or $ArgumentList.Count -eq 0) {
        $ArgumentList = @('-m', 'aicleaner', 'run', '--config', (Join-Path $AppFolder 'aicleaner-config.json'))
    }
    $logDir = Join-Path $AppFolder 'logs'
    if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
    # Read by aicleaner: Project sessions stay on, Restart ends the process (this loop brings it back), and a
    # sign-out elsewhere is not a reason to leave.
    $env:AICLEANER_SERVICE = 'user'
    if (Test-Path -LiteralPath $StoppedMarker) {
        Remove-Item -LiteralPath $StoppedMarker -Force -ErrorAction SilentlyContinue
        Write-Line 'a stop from before this start was cleared: the start-up task starting is the intent to run'
    }
    Write-Line "keeping aicleaner running: $PythonPath $(ConvertTo-ArgumentString -Arguments $ArgumentList)"
    $runs = 0
    while ($true) {
        $runs++
        # Uninstalled (or the venv rebuilt under it): nothing to keep running.
        if (-not (Test-Path -LiteralPath $PythonPath) -and -not (Get-Command $PythonPath -ErrorAction SilentlyContinue)) {
            Write-Line "$PythonPath is not there; not starting it" -Level Error
            exit 1
        }
        Remove-OldLog -LogDir $logDir
        $code = -1
        try {
            $code = Invoke-OneRun -Program $PythonPath -Arguments $ArgumentList -WorkingDirectory $AppFolder -LogDir $logDir
        } catch {
            Write-Line "could not start it: $($_.Exception.Message)" -Level Error
        }
        if (Test-Path -LiteralPath $StoppedMarker) {
            Write-Line "it ended (code $code) and was stopped on purpose; not starting it again" -Level Done
            break
        }
        if ($MaxRuns -gt 0 -and $runs -ge $MaxRuns) {
            Write-Line "it ended (code $code); $runs runs, the most asked for" -Level Warn
            break
        }
        $wait = $RetrySeconds
        if ($code -eq $PortTakenExit) {
            $wait = $PortTakenSeconds
            Write-Line "the port is taken (another aicleaner is running or starting); trying again in $wait s"
        } else {
            Write-Line "it ended (code $code); starting it again in $wait s" -Level Warn
        }
        Start-Sleep -Seconds $wait
        # A Stop during the wait counts too.
        if (Test-Path -LiteralPath $StoppedMarker) {
            Write-Line 'stopped on purpose while waiting; not starting it again' -Level Done
            break
        }
    }
} catch {
    Write-Line $_.Exception.Message -Level Error
    exit 1
} finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
