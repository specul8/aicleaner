<#
.SYNOPSIS
    Starts aicleaner with the computer, before anyone signs in, as you - or puts it back to starting at sign-in.

.DESCRIPTION
    Feature 107 (spec 2026-10-22-f107-run-as-a-service.md; decided 2026-10-24: as you, the password stored by
    Windows - no SYSTEM anywhere).

    -Mode User (needs an administrator: a start-up trigger does):
      - asks for your Windows password (Get-Credential, your name filled in). It goes straight to Windows, which
        keeps it encrypted for the task as it does for any service; this script never writes or logs it. A wrong
        password is asked for again, up to 3 times;
      - registers the task aicleaner-service: at start-up, as you, "whether you are signed in or not", at your
        limited token (RunLevel Limited - not elevated, as today), no time limit, started late if missed,
        restarted on failure. It runs installer\Run-AiCleanerService.ps1, which keeps aicleaner running and
        stops when Stop-AiCleaner.ps1 stops it on purpose;
      - when remote access is set up (the aicleaner-tunnel task exists), registers aicleaner-tunnel-service the
        same way, running what aicleaner-tunnel runs;
      - leaves the sign-in task aicleaner-proxy on: it is the fallback when Windows cannot sign the start-up task
        in (your password changed) - Start-AiCleaner.ps1 then starts aicleaner as you and says so;
      - records what it did in %USERPROFILE%\.aicleaner\state\service.json;
      - stops the copy running in your sign-in session (Stop-AiCleaner.ps1) and starts the task.
    -Mode Off reverses exactly that, from the record.
    -Mode Status prints the state as one JSON line; needs no administrator.

    Run unelevated, User and Off offer to start again as administrator (Windows asks), in a window you can see.

.PARAMETER Mode
    User, Off or Status. Default Status.

.PARAMETER AppFolder
    aicleaner's app folder. Default: this script's folder, or its parent when it sits in installer\.

.PARAMETER UserName
    The account the task runs as. Default the one running this script (DOMAIN\name). Passed on when this script
    starts itself again elevated, so an administrator who is someone else still sets it up for you.

.PARAMETER UserProfile
    That account's profile folder, for the record and the stop marker. Default %USERPROFILE%.

.PARAMETER Port
    aicleaner's port. Default 8787.

.PARAMETER PauseAtEnd
    Wait for Enter before closing - used when Settings starts this script in its own window.

.PARAMETER NoRelaunch
    Do not offer to start again as administrator; say so and stop (exit code 2).

.PARAMETER LogPath
    This run's log (a transcript; the password never reaches it). Default Set-AiCleanerService.log in the app
    folder. '' turns it off.

.EXAMPLE
    .\installer\Set-AiCleanerService.ps1 -Mode User
    From PowerShell: Windows asks for administrator, then a window asks for your password; aicleaner then starts
    with the computer, as you.

.EXAMPLE
    .\installer\Set-AiCleanerService.ps1 -Mode User -WhatIf
    Says what it would register and stop, without asking for a password or changing anything.

.EXAMPLE
    .\installer\Set-AiCleanerService.ps1 -Mode Status
    {"mode":"user","user":"CONTOSO\\alex","task":{"state":"Running","last_result":267009,...},...}

.EXAMPLE
    .\installer\Set-AiCleanerService.ps1 -Mode Off
    Back to starting at sign-in only.

.NOTES
    Last-modified: 2026-10-24
    Requires:      Windows PowerShell 5.1 or PowerShell 7+; administrator for User and Off
#>

#Requires -Version 5.1

[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateSet('User', 'Off', 'Status')][string]$Mode = 'Status',
    [string]$AppFolder = '',
    [string]$UserName = "$env:USERDOMAIN\$env:USERNAME",
    [string]$UserProfile = $env:USERPROFILE,
    [ValidateRange(1024, 65535)][int]$Port = 8787,
    [switch]$PauseAtEnd,
    [switch]$NoRelaunch,
    [AllowEmptyString()][string]$LogPath = $(
        $dir = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }
        Join-Path $dir 'Set-AiCleanerService.log'
    )
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $AppFolder) {
    $AppFolder = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }
}
$script:ServiceTask = 'aicleaner-service'
$script:TunnelServiceTask = 'aicleaner-tunnel-service'
$script:SignInTask = 'aicleaner-proxy'
$script:SignInTunnelTask = 'aicleaner-tunnel'
$script:Record = Join-Path $UserProfile '.aicleaner\state\service.json'
$script:StoppedMarker = Join-Path $UserProfile '.aicleaner\stopped-on-purpose'
$script:MaxPasswordTries = 3
# Windows could not sign the task in: wrong password, logon type not granted, password expired, account disabled.
$script:LogonFailures = @([int64]2147943726, [int64]2147943785, [int64]2147943730, [int64]2147943731, [int64]1326, [int64]1385, [int64]1330, [int64]1331)

function Write-Line {
    param([string]$Text, [ValidateSet('Info', 'Done', 'Warn', 'Error')][string]$Level = 'Info')
    $colour = @{ Info = 'Cyan'; Done = 'Green'; Warn = 'Yellow'; Error = 'Red' }[$Level]
    Write-Host "==> $Text" -ForegroundColor $colour
}

function Test-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Read-YesNo {
    <# A yes/no question with a default on Enter; $null when nobody can answer (no console input). #>
    param([string]$Question, [bool]$Default = $true)
    $hint = if ($Default) { '[Y/n]' } else { '[y/N]' }
    try { $answer = Read-Host "$Question $hint" } catch { return $null }
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return ($answer.Trim() -match '^(y|yes)$')
}

function Test-LogonFailure {
    <# True when a task result (signed or not) means Windows could not sign the task in. #>
    param($Result)
    if ($null -eq $Result) { return $false }
    $code = [int64]$Result
    if ($code -lt 0) { $code += 4294967296 }
    return ($script:LogonFailures -contains $code)
}

function Get-TaskState {
    <# One task's state for Status: $null when it does not exist (or this account cannot see it). #>
    param([string]$Name)
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $task) { return $null }
    $lastResult = $null
    $lastRun = ''
    try {
        $info = Get-ScheduledTaskInfo -TaskName $Name -ErrorAction Stop
        $lastResult = [int64]$info.LastTaskResult
        if ($info.LastRunTime) { $lastRun = ([datetime]$info.LastRunTime).ToString('o') }
    } catch { Write-Verbose "no run information for ${Name}: $($_.Exception.Message)" }
    return [ordered]@{
        name             = $Name
        state            = [string]$task.State
        last_result      = $lastResult
        last_run         = $lastRun
        user             = [string]$task.Principal.UserId
        logon_type       = [string]$task.Principal.LogonType
        run_level        = [string]$task.Principal.RunLevel
        password_problem = ((Test-LogonFailure -Result $lastResult) -or ([string]$task.State -eq 'Disabled'))
        # 0x800710E0: a start was asked for while it was already running (IgnoreNew) - refused, and harmless.
        last_result_note = $(if ($null -ne $lastResult -and (([int64]$lastResult + 4294967296) % 4294967296) -eq 2147946720) { 'a start was asked for while it was running (harmless)' } else { '' })
    }
}

function Get-State {
    $service = Get-TaskState -Name $script:ServiceTask
    $tunnel = Get-TaskState -Name $script:TunnelServiceTask
    $record = $null
    if (Test-Path -LiteralPath $script:Record) {
        try { $record = Get-Content -LiteralPath $script:Record -Raw | ConvertFrom-Json } catch { $record = $null }
    }
    $mode = 'off'
    if ($service) { $mode = 'user' }
    $since = ''
    if ($record -and $record.PSObject.Properties['at']) { $since = [string]$record.at }
    $user = ''
    if ($service) { $user = $service.user }
    $problem = $false
    if ($service) { $problem = [bool]$service.password_problem }
    return [ordered]@{
        mode             = $mode
        user             = $user
        since            = $since
        task             = $service
        tunnel           = $tunnel
        password_problem = $problem
        record           = [bool]$record
    }
}

function Get-UserSid {
    param([string]$Account)
    $name = $Account
    if ($name.StartsWith('\')) { $name = $name.Substring(1) }
    return (New-Object Security.Principal.NTAccount($name)).Translate([Security.Principal.SecurityIdentifier]).Value
}

function Get-ServiceTaskDefinition {
    <# The task: at start-up, as the user with a stored password, at the limited token, no time limit, started
       late if the start was missed, restarted on failure, never a second copy. Readable and runnable by the user
       (the security descriptor), so Start-AiCleaner and the watchdog - run as you, unelevated - can see it and
       start it again. #>
    param([Parameter(Mandatory)][object[]]$Actions, [Parameter(Mandatory)][string]$Account, [string]$Description)
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId $Account -LogonType Password -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    $task = New-ScheduledTask -Action $Actions -Trigger $trigger -Principal $principal -Settings $settings -Description $Description
    $sid = Get-UserSid -Account $Account
    $sddl = "D:(A;;FA;;;SY)(A;;FA;;;BA)(A;;FRFX;;;$sid)"
    # Checked here: a malformed descriptor would otherwise surface as an unhelpful registration error.
    $null = New-Object Security.AccessControl.RawSecurityDescriptor($sddl)
    $task.SecurityDescriptor = $sddl
    return $task
}

function Get-LauncherAction {
    param([string]$App)
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $launcher = Join-Path $App 'installer\Run-AiCleanerService.ps1'
    $arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$launcher`""
    return New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $App
}

function Test-LogonError {
    <# A registration refused because Windows could not sign the account in (a wrong password, most often). #>
    param([System.Management.Automation.ErrorRecord]$Record)
    $text = "$($Record.Exception.Message) $($Record.FullyQualifiedErrorId)"
    if ($text -match '0x8007052e|0x80070569|user name or password is incorrect|logon failure|logon type') { return $true }
    return (Test-LogonFailure -Result $Record.Exception.HResult)
}

function Register-WithPassword {
    <# Asks for the password and registers each task with it, up to $script:MaxPasswordTries times. The password
       lives only in a local variable, handed to Register-ScheduledTask; nothing here writes it. Returns $true
       when every task is registered. #>
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Tasks, [Parameter(Mandatory)][string]$Account)
    for ($try = 1; $try -le $script:MaxPasswordTries; $try++) {
        $credential = Get-Credential -UserName $Account -Message "aicleaner: your Windows password for $Account, so the start-up task can sign in as you before anyone signs in. Windows keeps it, encrypted."
        if (-not $credential) {
            Write-Line 'No password given; nothing changed.' -Level Warn
            return $false
        }
        $refused = $false
        foreach ($name in $Tasks.Keys) {
            try {
                Register-ScheduledTask -TaskName $name -InputObject $Tasks[$name] -User $credential.UserName `
                    -Password $credential.GetNetworkCredential().Password -Force -ErrorAction Stop | Out-Null
                Write-Line "registered $name (at start-up, as $($credential.UserName), limited token)" -Level Done
            } catch {
                if (-not (Test-LogonError -Record $_)) { throw }
                $refused = $true
                break
            }
        }
        $credential = $null
        if (-not $refused) { return $true }
        $left = $script:MaxPasswordTries - $try
        if ($left -gt 0) { Write-Line "Windows did not accept that password; $left more tries" -Level Warn }
    }
    Write-Line 'Windows did not accept the password; nothing more was changed.' -Level Error
    return $false
}

function Stop-SignInCopy {
    <# The aicleaner (and tunnel) running in the sign-in session, stopped cleanly so the task can take the port. #>
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$App, [int]$ProxyPort, [switch]$Tunnel)
    $stop = Join-Path $App 'installer\Stop-AiCleaner.ps1'
    if ((Test-Path -LiteralPath $stop) -and $PSCmdlet.ShouldProcess('aicleaner in the sign-in session', 'Stop (Stop-AiCleaner.ps1)')) {
        & $stop -Port $ProxyPort
    }
    if (-not $Tunnel) { return }
    $running = @(Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" -ErrorAction SilentlyContinue |
        Where-Object { [string]$_.CommandLine -match '\stunnel\b.*\srun\b' })
    foreach ($process in $running) {
        if ($PSCmdlet.ShouldProcess("cloudflared pid $($process.ProcessId)", 'Stop the tunnel')) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}

function Wait-Healthy {
    param([int]$ProxyPort, [int]$Seconds = 60)
    $until = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $until) {
        try { return (Invoke-RestMethod -Uri "http://127.0.0.1:$ProxyPort/healthz" -TimeoutSec 3 -ErrorAction Stop) }
        catch { Start-Sleep -Seconds 2 }
    }
    return $null
}

function Enable-Service {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$App, [int]$ProxyPort, [string]$Account, [string]$ProfileFolder)
    $python = Join-Path $ProfileFolder '.aicleaner\venv\Scripts\python.exe'
    foreach ($needed in $python, (Join-Path $App 'aicleaner-config.json'), (Join-Path $App 'installer\Run-AiCleanerService.ps1')) {
        if (-not (Test-Path -LiteralPath $needed)) {
            Write-Line "not found: $needed - is aicleaner installed for this account? Nothing changed." -Level Error
            return $false
        }
    }
    $tasks = [ordered]@{}
    $tasks[$script:ServiceTask] = Get-ServiceTaskDefinition -Actions @(Get-LauncherAction -App $App) -Account $Account `
        -Description 'aicleaner, started with the computer as you (feature 107); kept running by Run-AiCleanerService.ps1'
    $signInTunnel = Get-ScheduledTask -TaskName $script:SignInTunnelTask -ErrorAction SilentlyContinue
    if ($signInTunnel) {
        $tasks[$script:TunnelServiceTask] = Get-ServiceTaskDefinition -Actions @($signInTunnel.Actions) -Account $Account `
            -Description 'aicleaner remote access: the Cloudflare tunnel, started with the computer as you (feature 107)'
    }
    $names = @($tasks.Keys) -join ' and '
    if (-not $PSCmdlet.ShouldProcess($names, "Register at start-up as $Account (asks for the password)")) {
        Write-Line "would stop aicleaner in the sign-in session and start $($script:ServiceTask); the sign-in task stays as the fallback"
        return $true
    }
    if (-not (Register-WithPassword -Tasks $tasks -Account $Account)) { return $false }

    $folder = Split-Path -Parent $script:Record
    if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
    $record = [ordered]@{
        user    = $Account
        app     = $App
        tasks   = @($tasks.Keys)
        tunnel  = [bool]$signInTunnel
        at      = (Get-Date -Format o)
    }
    $record | ConvertTo-Json | Set-Content -LiteralPath $script:Record -Encoding UTF8
    Write-Line "recorded in $($script:Record)" -Level Done

    Stop-SignInCopy -App $App -ProxyPort $ProxyPort -Tunnel:([bool]$signInTunnel)
    Remove-Item -LiteralPath $script:StoppedMarker -ErrorAction SilentlyContinue
    foreach ($name in $tasks.Keys) { Start-ScheduledTask -TaskName $name }
    # Up to three minutes: the first start re-attaches every session left running, which took 75 seconds on
    # 2026-10-25 (since fixed, but a busy machine is slower than this one was).
    $health = Wait-Healthy -ProxyPort $ProxyPort -Seconds 180
    if ($health -and $health.PSObject.Properties['service'] -and $health.service -eq 'user') {
        Write-Line "aicleaner answers on port $ProxyPort, started by $($script:ServiceTask) as you" -Level Done
        # The task must give aicleaner the limited token (RunLevel Limited); the proxy reports its own.
        if ($health.PSObject.Properties['elevated'] -and $health.elevated -eq $true) {
            Write-Line 'aicleaner is running with an administrator token - not expected; switch this off and tell the developer' -Level Error
        } elseif ($health.PSObject.Properties['elevated'] -and $health.elevated -eq $false) {
            Write-Line 'aicleaner is running with your limited token, as intended' -Level Done
        }
    } elseif ($health) {
        Write-Line "aicleaner answers on port $ProxyPort, but not from the start-up task (another copy?)" -Level Warn
    } else {
        $state = Get-TaskState -Name $script:ServiceTask
        if ($state -and $state.password_problem) {
            Write-Line 'Windows could not sign the task in. Run this again and enter your password.' -Level Error
        } else {
            Write-Line "aicleaner did not answer within 3 minutes; see $App\Run-AiCleanerService.log and $App\logs" -Level Warn
        }
    }
    return $true
}

function Disable-Service {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$App, [int]$ProxyPort)
    $record = $null
    if (Test-Path -LiteralPath $script:Record) {
        try { $record = Get-Content -LiteralPath $script:Record -Raw | ConvertFrom-Json } catch { $record = $null }
    }
    # Only ever aicleaner's own two start-up tasks, at the root. The record is in the user's profile, so anything
    # running as the user could rewrite it: its list narrows what is removed, never widens it (security review).
    $owned = @($script:ServiceTask, $script:TunnelServiceTask)
    $names = $owned
    if ($record -and $record.PSObject.Properties['tasks']) { $names = @($owned | Where-Object { @($record.tasks) -contains $_ }) }
    $hadTunnel = $false
    # The launcher's loop is told to stop first (stopped on purpose), then aicleaner itself is stopped.
    if ($PSCmdlet.ShouldProcess($script:StoppedMarker, 'Mark aicleaner as stopped on purpose (ends the launcher loop)')) {
        Set-Content -LiteralPath $script:StoppedMarker -Value (Get-Date -Format o)
    }
    foreach ($name in $names) {
        if (-not (Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue)) { continue }
        if ($name -eq $script:TunnelServiceTask) { $hadTunnel = $true }
        if ($PSCmdlet.ShouldProcess($name, 'Stop and remove')) {
            Stop-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $name -TaskPath '\' -Confirm:$false
            Write-Line "removed $name" -Level Done
        }
    }
    Stop-SignInCopy -App $App -ProxyPort $ProxyPort -Tunnel:$hadTunnel
    if ($PSCmdlet.ShouldProcess($script:Record, 'Remove the record')) {
        Remove-Item -LiteralPath $script:Record -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $script:StoppedMarker -ErrorAction SilentlyContinue
    }
    # Started again the usual way: the sign-in tasks, as you.
    $restarted = $false
    foreach ($name in $script:SignInTask, $script:SignInTunnelTask) {
        if ((Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) -and $PSCmdlet.ShouldProcess($name, 'Start the sign-in task')) {
            Start-ScheduledTask -TaskName $name
            if ($name -eq $script:SignInTask) { $restarted = $true }
        }
    }
    # Never started from here: this window is elevated, and aicleaner would run with the administrator token
    # (security review). Only the sign-in task starts it, as you with the limited token.
    if (-not $restarted) {
        Write-Line "the sign-in task $($script:SignInTask) is missing: sign out and in, or run Start-AiCleaner.ps1 from a normal (not elevated) window" -Level Warn
        return $true
    }
    if ($WhatIfPreference) { return $true }
    if (Wait-Healthy -ProxyPort $ProxyPort) { Write-Line "aicleaner answers on port $ProxyPort, started at sign-in as before" -Level Done }
    else { Write-Line "aicleaner did not answer within 60 seconds; start it from Task Scheduler ($($script:SignInTask))" -Level Warn }
    return $true
}

function Invoke-Elevated {
    <# This script again, as administrator, in a window you can see (it asks for the password there). #>
    param([string]$RunMode, [int]$ProxyPort, [string]$App, [string]$Account, [string]$ProfileFolder, [string]$Log)
    $powershell = (Get-Process -Id $PID).Path
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Mode', $RunMode,
                   '-Port', $ProxyPort, '-AppFolder', "`"$App`"", '-UserName', "`"$Account`"",
                   '-UserProfile', "`"$ProfileFolder`"", '-PauseAtEnd')
    if ($Log) { $arguments += @('-LogPath', "`"$Log`"") } else { $arguments += @('-LogPath', '""') }
    Start-Process -FilePath $powershell -Verb RunAs -ArgumentList ($arguments -join ' ') | Out-Null
}

if ($Mode -eq 'Status') {
    Get-State | ConvertTo-Json -Compress -Depth 4
    exit 0
}

# -WhatIf changes nothing, so it needs no administrator: it says what would be done.
if (-not $WhatIfPreference -and -not (Test-Elevated)) {
    $why = 'Starting aicleaner with the computer needs an administrator (a start-up task).'
    if ($NoRelaunch) {
        Write-Line "$why Run this from an elevated PowerShell (Run as administrator)." -Level Error
        exit 2
    }
    $again = Read-YesNo -Question "$why Start this again as administrator (Windows asks)?" -Default $true
    if ($again) {
        try {
            Invoke-Elevated -RunMode $Mode -ProxyPort $Port -App $AppFolder -Account $UserName -ProfileFolder $UserProfile -Log $LogPath
            Write-Line 'Continued in the administrator window.' -Level Done
            exit 0
        } catch {
            Write-Line "Not started as administrator: $($_.Exception.Message)" -Level Error
            exit 2
        }
    }
    Write-Line "$why Nothing changed." -Level Warn
    exit 2
}

$transcriptStarted = $false
if ($LogPath) { try { Start-Transcript -Path $LogPath -Force | Out-Null; $transcriptStarted = $true } catch { Write-Verbose "no transcript: $($_.Exception.Message)" } }
$exitCode = 0
try {
    Write-Line "aicleaner start-up task: $Mode, for $UserName, app $AppFolder"
    if ($Mode -eq 'User') { $done = Enable-Service -App $AppFolder -ProxyPort $Port -Account $UserName -ProfileFolder $UserProfile }
    else { $done = Disable-Service -App $AppFolder -ProxyPort $Port }
    if (-not $done) { $exitCode = 1 }
    Get-State | ConvertTo-Json -Compress -Depth 4
} catch {
    Write-Line $_.Exception.Message -Level Error
    $exitCode = 1
} finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
if ($PauseAtEnd) { try { Read-Host 'Press Enter to close' | Out-Null } catch { Write-Verbose 'no console to wait on' } }
exit $exitCode
