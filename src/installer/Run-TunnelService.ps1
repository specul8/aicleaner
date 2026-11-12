<# Run-TunnelService.ps1
.SYNOPSIS
    Keeps the remote-access tunnel running for the start-up task (feature 107): runs Start-Tunnel.ps1, and runs it
    again whenever it ends or fails, until the task is stopped.

.DESCRIPTION
    The action of the aicleaner-tunnel-service scheduled task that Set-AiCleanerService.ps1 -Mode User registers:
    it runs at start-up as you, whether you are signed in or not, with the password Windows keeps for the task.

    On 2026-11-12 that task, then running Start-Tunnel.ps1 once, ended at early boot with 0x80070003 (path not
    found) and left no trace; nothing tried again until sign-in. This loop is the fix, modelled on
    Run-AiCleanerService.ps1:
      - before each attempt it checks and logs what the tunnel needs: the profile, the token file, cloudflared,
        the process query (WMI/CIM) and the network (is one up, does Cloudflare's tunnel name resolve);
      - it runs Start-Tunnel.ps1 in this process, so any error comes back here as a full error record and is
        logged with its exception type, message, HResult and where it was thrown;
      - when Start-Tunnel ends - cloudflared stopped or crashed, or it failed, or it stood down because this
        account's tunnel was already running - it waits and tries again: 15 seconds, doubling to at most 60;
        a run that lasted a minute or more starts the wait at 15 again. It never gives up: it ends only when the
        task is stopped (Set-AiCleanerService -Mode Off, Uninstall) or -MaxRuns is reached (tests).
    Never two tunnels: Start-Tunnel's one-tunnel check runs on every attempt, so while this account's cloudflared
    runs (from any start) it stands down; and while this task is Running, the sign-in copy stands down (D215).

    Its log is a new file per run, logs\Run-TunnelService-<yyyyMMdd-HHmmss>.log in the app folder,
    tab-separated with a header; its own logs older than 14 days are removed at each start.

.PARAMETER AppFolder
    AI Cleaner's app folder. Default: this script's folder, or its parent when it sits in installer\.

.PARAMETER StartTunnelPath
    The script that runs the tunnel once. Default <AppFolder>\installer\Start-Tunnel.ps1.

.PARAMETER TokenPath
    The stored tunnel token, passed on to Start-Tunnel. Default %USERPROFILE%\.aicleaner\cloudflare-tunnel.dpapi.

.PARAMETER CloudflaredPath
    The cloudflared program, passed on to Start-Tunnel. Default: Start-Tunnel finds it. For tests: a stand-in.

.PARAMETER TunnelLogPath
    cloudflared's own log, passed on to Start-Tunnel as -LogPath. Default: Start-Tunnel's (logs\cloudflared.log).

.PARAMETER StartTunnelLogPath
    Start-Tunnel's own day log, passed on as -RunLogPath. Default: Start-Tunnel's. '' turns it off.

.PARAMETER FirstRetrySeconds
    The first wait before trying again. Default 15.

.PARAMETER MaxRetrySeconds
    The longest wait before trying again (the wait doubles up to it). Default 60.

.PARAMETER HealthySeconds
    A run that lasted at least this long was a working tunnel: the next wait starts at FirstRetrySeconds again.
    Default 60.

.PARAMETER ProbeHost
    The name resolved to check DNS before each attempt. Default region1.v2.argotunnel.com (Cloudflare's tunnel
    edge). '' skips the check.

.PARAMETER MaxRuns
    Stop after this many attempts (0, the default, is no limit). For tests.

.PARAMETER KeepDays
    This script's own logs older than this are removed at each start. Default 14.

.PARAMETER LogPath
    This run's log. Default logs\Run-TunnelService-<yyyyMMdd-HHmmss>.log in the app folder. '' turns it off.

.EXAMPLE
    .\installer\Run-TunnelService.ps1
    What the start-up task runs: the tunnel, kept running until the task is stopped.

.EXAMPLE
    .\installer\Run-TunnelService.ps1 -CloudflaredPath C:\Temp\fake-cloudflared.cmd -TokenPath C:\Temp\token.dpapi -FirstRetrySeconds 0 -MaxRuns 3
    Runs a stand-in for cloudflared three times, as the tests do.

.NOTES
    Last-modified: 2026-11-12
    Requires:      Windows PowerShell 5.1 or PowerShell 7+; remote access set up (Enable-RemoteControl.ps1)
#>

#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$AppFolder = '',
    [string]$StartTunnelPath = '',
    [string]$TokenPath = '',
    [string]$CloudflaredPath = '',
    [string]$TunnelLogPath = '',
    [AllowEmptyString()][string]$StartTunnelLogPath = '',
    [ValidateRange(0, 3600)][int]$FirstRetrySeconds = 15,
    [ValidateRange(0, 3600)][int]$MaxRetrySeconds = 60,
    [ValidateRange(1, 86400)][int]$HealthySeconds = 60,
    [AllowEmptyString()][string]$ProbeHost = 'region1.v2.argotunnel.com',
    [ValidateRange(0, 1000000)][int]$MaxRuns = 0,
    [ValidateRange(1, 3650)][int]$KeepDays = 14,
    [AllowEmptyString()][string]$LogPath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:ScriptVersion = '2026-11-12'
if (-not $AppFolder) {
    $AppFolder = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }
}
# Not given: one file per run, stamped, in the app's logs\ (D219). Given as '': no log.
if (-not $PSBoundParameters.ContainsKey('LogPath')) {
    $LogPath = Join-Path $AppFolder ("logs\Run-TunnelService-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
}
$script:Log = $LogPath
$script:RunId = [guid]::NewGuid().ToString('N').Substring(0, 8)
$script:Session = (Get-Process -Id $PID).SessionId
$script:StartType = if ($script:Session -eq 0) { 'ScheduledTask' } else { 'Manual' }
$script:Columns = 'UtcTime', 'LocalTime', 'Level', 'RunId', 'Computer', 'User', 'Script', 'ScriptVersion', 'Phase',
                  'StartType', 'Attempt', 'ExitCode', 'DurationMs', 'Message'
if (-not $StartTunnelPath) { $StartTunnelPath = Join-Path $AppFolder 'installer\Start-Tunnel.ps1' }
# Not given: Start-Tunnel's own default. Given, even as '' (off): passed on.
$script:PassStartTunnelLog = $PSBoundParameters.ContainsKey('StartTunnelLogPath')

function Write-Event {
    <# A status line on the screen and a tab-separated line in this run's log (the owner's logging standard).
       Never throws: the tunnel matters more than its log. #>
    param([ValidateSet('STEP', 'INFO', 'DONE', 'WARN', 'ERROR')][string]$Level = 'INFO', [string]$Phase = 'loop',
          [string]$Message, [string]$Attempt = '', [string]$ExitCode = '', [string]$DurationMs = '')
    $colour = @{ STEP = 'Cyan'; INFO = 'Gray'; DONE = 'Green'; WARN = 'Yellow'; ERROR = 'Red' }[$Level]
    $now = Get-Date
    Write-Host ("==> {0} {1}" -f $now.ToString('yyyy-MM-dd HH:mm:ss'), $Message) -ForegroundColor $colour
    if (-not $script:Log) { return }
    try {
        $values = @($now.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ'), $now.ToString('yyyy-MM-ddTHH:mm:ss.fffzzz'),
                    $Level, $script:RunId, $env:COMPUTERNAME, [Security.Principal.WindowsIdentity]::GetCurrent().Name,
                    'Run-TunnelService.ps1', $script:ScriptVersion, $Phase, $script:StartType, $Attempt, $ExitCode,
                    $DurationMs, $Message) | ForEach-Object { ([string]$_) -replace "`t", ' ' -replace "`r?`n", ' | ' }
        [IO.File]::AppendAllText($script:Log, ($values -join "`t") + "`r`n", (New-Object Text.UTF8Encoding($false)))
    } catch { Write-Verbose "not logged: $($_.Exception.Message)" }
}

function Initialize-RunLog {
    <# Makes the log's folder and header, and removes this script's own logs older than -Days beside it. A log path
       passed in of another name is written to as given; nothing else beside it is touched. Never throws. #>
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$Path, [int]$Days)
    if (-not $Path) { return }
    try {
        $folder = Split-Path -Parent $Path
        if ($folder -and -not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
        if (-not (Test-Path -LiteralPath $Path)) {
            [IO.File]::AppendAllText($Path, ($script:Columns -join "`t") + "`r`n", (New-Object Text.UTF8Encoding($false)))
        }
        if ($folder -and $PSCmdlet.ShouldProcess($folder, "Remove Run-TunnelService logs older than $Days days")) {
            Get-ChildItem -LiteralPath $folder -Filter 'Run-TunnelService-*.log' -File -ErrorAction Stop |
                Where-Object { $_.Name -match '^Run-TunnelService-\d{8}(-\d{6})?\.log$' -and $_.LastWriteTime -lt (Get-Date).AddDays(-$Days) } |
                Remove-Item -Force -ErrorAction SilentlyContinue
        }
    } catch { Write-Verbose "log not prepared: $($_.Exception.Message)" }
}

function Format-ErrorRecord {
    <# An error record on one line: what it was, where it came from and the code Windows gave it. #>
    param([System.Management.Automation.ErrorRecord]$Record)
    $inner = $Record.Exception.InnerException
    $innerText = if ($inner) { " (inner: $($inner.GetType().FullName): $($inner.Message))" } else { '' }
    return ("{0}: {1}{2} [{3}; category {4}; HResult 0x{5:X8}] at {6}" -f $Record.Exception.GetType().FullName,
        $Record.Exception.Message, $innerText, $Record.FullyQualifiedErrorId, $Record.CategoryInfo.Category,
        $Record.Exception.HResult, (($Record.ScriptStackTrace -split "`r?`n") -join ' < '))
}

function Find-Cloudflared {
    <# cloudflared.exe on PATH, or where winget puts it. #>
    [OutputType([string])]
    param()
    $onPath = Get-Command 'cloudflared.exe' -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'cloudflared\cloudflared.exe'),
        (Join-Path $env:ProgramFiles 'cloudflared\cloudflared.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\cloudflared.exe')
    )
    foreach ($candidate in $candidates) { if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate } }
    return ''
}

function Get-TunnelCheck {
    <# What the tunnel needs, each checked on its own so one failure does not hide the next: the answers as
       'name=value' strings for the log. Never throws. #>
    [OutputType([string[]])]
    param([string]$Token, [string]$Program, [string]$Script, [string]$DnsName)
    $found = New-Object System.Collections.Generic.List[string]
    $found.Add("profile=$env:USERPROFILE exists:$(Test-Path -LiteralPath $env:USERPROFILE)")
    $found.Add("startTunnel=$(Test-Path -LiteralPath $Script)")
    try {
        $item = Get-Item -LiteralPath $Token -ErrorAction Stop
        $found.Add("token=$Token bytes:$($item.Length)")
    } catch { $found.Add("token=$Token MISSING ($($_.Exception.Message))") }
    try {
        $exe = if ($Program) { $Program } else { Find-Cloudflared }
        if ($exe) { $found.Add("cloudflared=$exe exists:$(Test-Path -LiteralPath $exe)") } else { $found.Add('cloudflared=NOT FOUND (PATH, Program Files, WinGet links)') }
    } catch { $found.Add("cloudflared=lookup failed ($($_.Exception.Message))") }
    try {
        $name = if ($Program) { Split-Path -Leaf $Program } else { 'cloudflared.exe' }
        $processes = @(Get-CimInstance Win32_Process -Filter "Name = '$($name -replace "'", "''")'" -OperationTimeoutSec 15 -ErrorAction Stop)
        $found.Add("cim=ok $name processes:$($processes.Count)$(if ($processes.Count) { ' pids:' + (($processes | ForEach-Object ProcessId) -join ',') })")
    } catch { $found.Add("cim=FAILED $(Format-ErrorRecord -Record $_)") }
    try { $found.Add("network=$([Net.NetworkInformation.NetworkInterface]::GetIsNetworkAvailable())") }
    catch { $found.Add("network=unknown ($($_.Exception.Message))") }
    if ($DnsName) {
        try {
            # Async with a time limit: at early boot a lookup can hang for as long as DNS is not up.
            $lookup = [Net.Dns]::GetHostAddressesAsync($DnsName)
            if ($lookup.Wait(5000)) { $found.Add("dns=$DnsName -> $(($lookup.Result | Select-Object -First 2) -join ',')") }
            else { $found.Add("dns=$DnsName no answer in 5 s") }
        } catch {
            $why = $_.Exception
            while ($why.InnerException) { $why = $why.InnerException }
            $found.Add("dns=$DnsName FAILED ($($why.Message))")
        }
    }
    return $found.ToArray()
}

function Invoke-TunnelOnce {
    <# Start-Tunnel.ps1 once, in this process (so an error arrives here whole); returns its exit code and the error
       record if it threw. Start-Tunnel runs cloudflared in the foreground, so this returns when the tunnel ends. #>
    param([string]$Script, [hashtable]$Arguments)
    $global:LASTEXITCODE = 0
    try {
        # To the screen, not the pipeline: cloudflared's output must not become this function's result.
        & $Script @Arguments | Out-Host
        return @{ Code = [int]$global:LASTEXITCODE; Failure = $null }
    } catch {
        return @{ Code = -1; Failure = $_ }
    }
}

Write-Host "==> Started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Cyan
$runStarted = Get-Date
Initialize-RunLog -Path $script:Log -Days $KeepDays
$exitCode = 0
try {
    $token = if ($TokenPath) { $TokenPath } else { Join-Path $env:USERPROFILE '.aicleaner\cloudflare-tunnel.dpapi' }
    Write-Event -Level STEP -Phase 'start' -Message ("Run-TunnelService {0} keeping the tunnel running: session {1}, pid {2}, app {3}, start {4}, wait {5}-{6} s, MaxRuns {7}" -f
        $script:ScriptVersion, $script:Session, $PID, $AppFolder, $StartTunnelPath, $FirstRetrySeconds, $MaxRetrySeconds, $MaxRuns)
    # What Start-Tunnel is given: only what was asked for here, so its own defaults hold otherwise.
    $startArguments = @{ StartedBy = 'Run-TunnelService' }
    if ($TokenPath) { $startArguments.TokenPath = $TokenPath }
    if ($CloudflaredPath) { $startArguments.CloudflaredPath = $CloudflaredPath }
    if ($TunnelLogPath) { $startArguments.LogPath = $TunnelLogPath }
    if ($script:PassStartTunnelLog) { $startArguments.RunLogPath = $StartTunnelLogPath }
    $attempt = 0
    $wait = $FirstRetrySeconds
    while ($true) {
        $attempt++
        Write-Event -Phase 'check' -Attempt $attempt -Message ("checks: " + ((Get-TunnelCheck -Token $token -Program $CloudflaredPath -Script $StartTunnelPath -DnsName $ProbeHost) -join '; '))
        $began = Get-Date
        if (-not (Test-Path -LiteralPath $StartTunnelPath)) {
            $result = @{ Code = -1; Failure = $null }
            Write-Event -Level ERROR -Phase 'run' -Attempt $attempt -Message "$StartTunnelPath is not there; nothing to run"
        } else {
            Write-Event -Phase 'run' -Attempt $attempt -Message "running $StartTunnelPath"
            $result = Invoke-TunnelOnce -Script $StartTunnelPath -Arguments $startArguments
        }
        $took = [int64]((Get-Date) - $began).TotalMilliseconds
        if ($result.Failure) {
            Write-Event -Level ERROR -Phase 'run' -Attempt $attempt -ExitCode '-1' -DurationMs $took -Message ("Start-Tunnel failed: " + (Format-ErrorRecord -Record $result.Failure))
        } else {
            $level = if ($result.Code -eq 0) { 'INFO' } else { 'WARN' }
            Write-Event -Level $level -Phase 'run' -Attempt $attempt -ExitCode ([string]$result.Code) -DurationMs $took -Message ("Start-Tunnel ended with code {0} after {1:N0} s (it stood down, or cloudflared ended; Start-Tunnel's day log says which)" -f $result.Code, ($took / 1000))
        }
        if ($MaxRuns -gt 0 -and $attempt -ge $MaxRuns) {
            Write-Event -Level WARN -Phase 'loop' -Attempt $attempt -Message "$attempt attempts, the most asked for; stopping"
            break
        }
        # A tunnel that ran a while was working: its end is a fresh problem, so the wait starts short again.
        if ($took -ge ($HealthySeconds * 1000)) { $wait = $FirstRetrySeconds }
        Write-Event -Phase 'loop' -Attempt $attempt -Message "trying again in $wait s"
        Start-Sleep -Seconds $wait
        $wait = [Math]::Min([Math]::Max($wait * 2, 1), $MaxRetrySeconds)
    }
} catch {
    Write-Event -Level ERROR -Phase 'error' -Message ("stopped by an error: " + (Format-ErrorRecord -Record $_))
    $exitCode = 1
} finally {
    $seconds = [int]((Get-Date) - $runStarted).TotalSeconds
    $level = if ($exitCode -eq 0) { 'DONE' } else { 'ERROR' }
    Write-Event -Level $level -Phase 'end' -ExitCode ([string]$exitCode) -DurationMs ([string]([int64]$seconds * 1000)) -Message "Run-TunnelService ended after $seconds s"
    Write-Host "==> Finished $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Cyan
    if ($script:Log) { Write-Host "    log: $($script:Log)" }
}
exit $exitCode
