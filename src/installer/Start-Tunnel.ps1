<# Start-Tunnel.ps1
.SYNOPSIS
    Runs this machine's Cloudflare tunnel as the signed-in user (feature 104, part 2).

.DESCRIPTION
    Started at sign-in by the aicleaner-tunnel scheduled task, which Enable-RemoteControl.ps1
    registers instead of a Windows service, so remote access needs no administrator rights - and the
    tunnel runs exactly while the proxy it serves does, which also needs the user signed in.
    With AI Cleaner started with the computer (feature 107), Run-TunnelService.ps1 - the action of the
    aicleaner-tunnel-service start-up task - runs this script, and runs it again whenever it ends.

    The tunnel's run token is read from %USERPROFILE%\.aicleaner\cloudflare-tunnel.dpapi, encrypted
    to this Windows account (DPAPI, CurrentUser), and handed to cloudflared in its environment
    (TUNNEL_TOKEN) - never on a command line, where any process on the machine could read it.

    Every run writes what it decided (started, stood down, failed - and why) to its own day's log,
    logs\Start-Tunnel-<yyyyMMdd>.log in the app folder, tab-separated; logs older than 14 days are removed.

.PARAMETER LogPath
    Where cloudflared's own log goes. Default: logs\cloudflared.log in the app folder.

.PARAMETER TokenPath
    Another stored token (encrypted to this account, as the default is). Default:
    %USERPROFILE%\.aicleaner\cloudflare-tunnel.dpapi.

.PARAMETER CloudflaredPath
    The cloudflared program to run. Default: cloudflared.exe on PATH, or where winget puts it. The check for a
    tunnel already running looks for processes of this program's name. For tests: a stand-in.

.PARAMETER StartedBy
    Who started this run, for the log (Run-TunnelService passes its name). Default: worked out from the session -
    session 0 is a start-up task, any other a sign-in task or a person.

.PARAMETER RunLogPath
    This script's own log. Default logs\Start-Tunnel-<yyyyMMdd>.log in the app folder (one per day, appended:
    each run is a line or two). '' turns it off.

.EXAMPLE
    .\installer\Start-Tunnel.ps1
    What the sign-in task runs: starts the tunnel unless one is already running for this account.

.NOTES
    Last-modified: 2026-11-12
    Requires: Windows PowerShell 5.1 or PowerShell 7+; cloudflared (Enable-RemoteControl.ps1 installs it).
#>
[CmdletBinding()]
param(
    [AllowEmptyString()][string]$LogPath = '',
    [string]$TokenPath = '',
    [string]$CloudflaredPath = '',
    [string]$StartedBy = '',
    [AllowEmptyString()][string]$RunLogPath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:ScriptVersion = '2026-11-12'
# The app folder: this script's own, or its parent when it sits in installer\ (feature 102).
$script:AppFolder = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }
if (-not $LogPath) { $LogPath = Join-Path $script:AppFolder 'logs\cloudflared.log' }
# Not given: the day's log. Given as '': no log.
if (-not $PSBoundParameters.ContainsKey('RunLogPath')) {
    $RunLogPath = Join-Path $script:AppFolder ("logs\Start-Tunnel-{0}.log" -f (Get-Date -Format 'yyyyMMdd'))
}
$script:RunLog = $RunLogPath
$script:RunId = [guid]::NewGuid().ToString('N').Substring(0, 8)
$script:Session = (Get-Process -Id $PID).SessionId
if (-not $StartedBy) { $StartedBy = if ($script:Session -eq 0) { 'StartupTask' } else { 'SignIn' } }
$script:StartedBy = $StartedBy

function Write-RunLog {
    <# One tab-separated line in this script's day log (the owner's logging standard), with a header when the file
       is new; logs of this script older than 14 days are removed when the day's file is made. Never throws: the
       tunnel matters more than its log. #>
    param([ValidateSet('STEP', 'INFO', 'DONE', 'WARN', 'ERROR')][string]$Level, [string]$Phase, [string]$Message,
          [string]$ExitCode = '')
    if (-not $script:RunLog) { return }
    try {
        $folder = Split-Path -Parent $script:RunLog
        if ($folder -and -not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
        $columns = 'UtcTime', 'LocalTime', 'Level', 'RunId', 'Computer', 'User', 'Script', 'ScriptVersion', 'Phase',
                   'StartType', 'Session', 'ExitCode', 'Message'
        if (-not (Test-Path -LiteralPath $script:RunLog)) {
            [IO.File]::AppendAllText($script:RunLog, ($columns -join "`t") + "`r`n", (New-Object Text.UTF8Encoding($false)))
            if ($folder) {
                Get-ChildItem -LiteralPath $folder -Filter 'Start-Tunnel-*.log' -File -ErrorAction SilentlyContinue |
                    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } | Remove-Item -Force -ErrorAction SilentlyContinue
            }
        }
        $now = Get-Date
        $values = @($now.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ'), $now.ToString('yyyy-MM-ddTHH:mm:ss.fffzzz'),
                    $Level, $script:RunId, $env:COMPUTERNAME, [Security.Principal.WindowsIdentity]::GetCurrent().Name,
                    'Start-Tunnel.ps1', $script:ScriptVersion, $Phase, $script:StartedBy, [string]$script:Session,
                    $ExitCode, $Message) | ForEach-Object { ([string]$_) -replace "`t", ' ' -replace "`r?`n", ' | ' }
        [IO.File]::AppendAllText($script:RunLog, ($values -join "`t") + "`r`n", (New-Object Text.UTF8Encoding($false)))
    } catch { Write-Verbose "not logged: $($_.Exception.Message)" }
}

function Read-TunnelToken {
    <# The run token Enable-RemoteControl.ps1 stored, as plain text for the child's environment only. #>
    [OutputType([string])]
    param([Parameter(Mandatory)][string]$Path)
    Add-Type -AssemblyName System.Security
    $bytes = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($Path), $null, 'CurrentUser')
    try { return [Text.Encoding]::UTF8.GetString($bytes) }
    finally { [Array]::Clear($bytes, 0, $bytes.Length) }
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

Write-RunLog -Level STEP -Phase 'start' -Message "Start-Tunnel $($script:ScriptVersion) started by $($script:StartedBy) in session $($script:Session), as $([Security.Principal.WindowsIdentity]::GetCurrent().Name)"
try {
    $exe = if ($CloudflaredPath) { $CloudflaredPath } else { Find-Cloudflared }
    $exeName = if ($exe) { Split-Path -Leaf $exe } else { 'cloudflared.exe' }

    # One tunnel at a time (feature 107): with AI Cleaner started with the computer, aicleaner-tunnel-service runs
    # this before anyone signs in, and the sign-in task aicleaner-tunnel runs it again at sign-in. The second finds
    # the first and leaves it be.
    # Only this account's: another user's cloudflared is not this tunnel, and must not stop it starting (security review).
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $already = @(Get-CimInstance Win32_Process -Filter "Name = '$($exeName -replace "'", "''")'" -ErrorAction SilentlyContinue |
        Where-Object { [string]$_.CommandLine -match '\stunnel\b.*\srun\b' } |
        Where-Object {
            $owner = Invoke-CimMethod -InputObject $_ -MethodName GetOwnerSid -ErrorAction SilentlyContinue
            $owner -and $owner.Sid -eq $me
        })
    if ($already.Count -gt 0) {
        Write-Host "==> the tunnel is already running (cloudflared pid $($already[0].ProcessId)); not starting another" -ForegroundColor Green
        Write-RunLog -Level DONE -Phase 'one-tunnel' -ExitCode '0' -Message "stood down: this account's tunnel is already running (pid $($already[0].ProcessId))"
        exit 0
    }
    # D215: from a sign-in session, Windows hides the command line of the start-up task's cloudflared (it runs in
    # session 0), so the check above never saw it and a second copy started at every sign-in. The task itself is
    # readable by this account: while it runs, the start-up copy owns the tunnel and this one stands down.
    if ($script:Session -ne 0) {
        # Only the task this installer registers for this account: at the root, run as this user, running the
        # tunnel - Run-TunnelService.ps1 (its supervisor, 2026-11-12) or Start-Tunnel.ps1 (a task saved before).
        # A task of the same name made by someone else must not keep this account's tunnel from starting (security review).
        $startupTunnel = @(Get-ScheduledTask -TaskName 'aicleaner-tunnel-service' -TaskPath '\' -ErrorAction SilentlyContinue)
        $ours = $false
        if ($startupTunnel.Count -eq 1) {
            $principal = [string]$startupTunnel[0].Principal.UserId
            try { $principalSid = (New-Object Security.Principal.NTAccount($principal)).Translate([Security.Principal.SecurityIdentifier]).Value }
            catch { $principalSid = '' }
            $runsTunnel = @($startupTunnel[0].Actions | Where-Object { "$($_.Execute) $($_.Arguments)" -match '(Run-TunnelService|Start-Tunnel)\.ps1' }).Count -gt 0
            $ours = ($principalSid -eq $me) -and $runsTunnel
        }
        if ($ours -and [string]$startupTunnel[0].State -eq 'Running') {
            Write-Host '==> the tunnel runs from the start-up task (aicleaner-tunnel-service); not starting another' -ForegroundColor Green
            Write-RunLog -Level DONE -Phase 'one-tunnel' -ExitCode '0' -Message 'stood down: the start-up task aicleaner-tunnel-service is running and owns the tunnel'
            exit 0
        }
    }

    $tokenPath = if ($TokenPath) { $TokenPath } else { Join-Path $env:USERPROFILE '.aicleaner\cloudflare-tunnel.dpapi' }
    if (-not (Test-Path -LiteralPath $tokenPath)) {
        Write-RunLog -Level ERROR -Phase 'pre-flight' -ExitCode '1' -Message "failed: no tunnel token at $tokenPath"
        Write-Error "No tunnel token at ${tokenPath}: run Enable-RemoteControl.ps1 to set remote access up." -ErrorAction Continue
        exit 1
    }
    if (-not $exe) {
        Write-RunLog -Level ERROR -Phase 'pre-flight' -ExitCode '1' -Message 'failed: cloudflared.exe was not found (PATH, Program Files, WinGet links)'
        Write-Error 'cloudflared.exe was not found: run Enable-RemoteControl.ps1 again to install it.' -ErrorAction Continue
        exit 1
    }
    $folder = Split-Path -Parent $LogPath
    if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }

    $env:TUNNEL_TOKEN = Read-TunnelToken -Path $tokenPath
    $began = Get-Date
    try {
        Write-RunLog -Level INFO -Phase 'run' -Message "started: $exe tunnel run (cloudflared log $LogPath)"
        # cloudflared reads TUNNEL_TOKEN for `tunnel run`; the token never appears in the process list.
        & $exe tunnel --no-autoupdate --logfile $LogPath run
        $code = $LASTEXITCODE
        $seconds = [int]((Get-Date) - $began).TotalSeconds
        $level = if ($code -eq 0) { 'DONE' } else { 'WARN' }
        Write-RunLog -Level $level -Phase 'run' -ExitCode ([string]$code) -Message "cloudflared ended with code $code after $seconds s"
        exit $code
    } finally {
        Remove-Item Env:\TUNNEL_TOKEN -ErrorAction SilentlyContinue
    }
} catch {
    # Whatever stopped it, in full: the start-up task's 0x80070003 of 2026-11-12 left no trace anywhere.
    $record = $_
    Write-RunLog -Level ERROR -Phase 'error' -Message ("failed: {0}: {1} [{2}; HResult 0x{3:X8}] at {4}" -f
        $record.Exception.GetType().FullName, $record.Exception.Message, $record.FullyQualifiedErrorId,
        $record.Exception.HResult, $record.ScriptStackTrace)
    throw
}
