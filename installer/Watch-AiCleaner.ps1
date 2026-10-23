<#
.SYNOPSIS
    Route B's safeguards for when aicleaner is down: the watchdog (every 5 minutes) and the PAC service.

.DESCRIPTION
    While Claude Desktop's chat is filtered (route B), the Windows proxy setting points claude.ai at aicleaner.
    Two things keep a stopped aicleaner from meaning an unfiltered chat that nothing mentions (spec
    2026-10-21-desktop-chat-route-b.md, section 6, decided 2026-10-21):

    - the PAC service (task aicleaner-pac): a tiny server of its own that serves the PAC file aicleaner wrote,
      so with aicleaner down the PAC still sends claude.ai to aicleaner's forward proxy with no DIRECT
      fallback - Desktop's chat fails instead of going out unfiltered. It runs in the foreground of its task,
      so Task Scheduler restarts it if it ends;
    - the watchdog (task aicleaner-watchdog, this script with no switches, every 5 minutes): when aicleaner's
      /healthz does not answer twice, 20 seconds apart, it starts aicleaner again and raises a Windows
      notification saying Claude Desktop's chat may not have been filtered. A deliberate stop
      (Stop-AiCleaner.ps1 leaves stopped-on-purpose in the state folder) is respected.

    aicleaner registers both when route B is switched on (-Install) and removes them when it is switched off
    (-Uninstall). Neither needs administrator: both are this user's own tasks.

.PARAMETER Install
    Register both tasks (and start the PAC service now), then exit.

.PARAMETER Uninstall
    Remove both tasks, then exit.

.PARAMETER Port
    aicleaner's port. Default 8787.

.PARAMETER PacPort
    The PAC service's port. Default 8789.

.PARAMETER PacFile
    The PAC file aicleaner writes. Default %USERPROFILE%\.aicleaner\state\claudeai.pac.

.PARAMETER StateRoot
    aicleaner's per-user folder. Default %USERPROFILE%\.aicleaner.

.PARAMETER LogPath
    Where this run's log goes. Default Watch-AiCleaner.log next to the app. Pass '' to turn it off.

.EXAMPLE
    .\Watch-AiCleaner.ps1 -Install
    Registers the watchdog and the PAC service for this user.

.EXAMPLE
    .\Watch-AiCleaner.ps1
    One watchdog check: starts aicleaner and says so if it is down.

.NOTES
    Last-modified: 2026-10-22
    Requires:      Windows PowerShell 5.1 or PowerShell 7+, Windows 10 or later
#>

#Requires -Version 5.1

[CmdletBinding(SupportsShouldProcess)]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [ValidateRange(1024, 65535)][int]$Port = 8787,
    [ValidateRange(1024, 65535)][int]$PacPort = 8789,
    [string]$PacFile = '',
    [string]$StateRoot = (Join-Path $env:USERPROFILE '.aicleaner'),
    [AllowEmptyString()][string]$LogPath = $(
        $dir = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }
        Join-Path $dir 'Watch-AiCleaner.log'
    )
)
# The app folder: this script's own, or its parent when it sits in installer\ (feature 102).
$script:AppFolder = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:WatchTask = 'aicleaner-watchdog'
$script:PacTask = 'aicleaner-pac'
if (-not $PacFile) { $PacFile = Join-Path $StateRoot 'state\claudeai.pac' }

function Write-Line {
    param([string]$Text, [ValidateSet('Info', 'Done', 'Warn', 'Error')][string]$Level = 'Info')
    $colour = @{ Info = 'Cyan'; Done = 'Green'; Warn = 'Yellow'; Error = 'Red' }[$Level]
    Write-Host "==> $Text" -ForegroundColor $colour
}

function Test-Healthy {
    param([int]$ProxyPort)
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:$ProxyPort/healthz" -TimeoutSec 5 -ErrorAction Stop | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Show-Toast {
    <# The notice that aicleaner was down. The text goes through the environment, never spliced into the
       child's script, so nothing in it can close the string and run as code. #>
    param([string]$Title, [string]$Body)
    $toastScript = @'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
$xml = @"
<toast><visual><binding template="ToastGeneric">
  <text>$title</text>
  <text>$body</text>
</binding></visual></toast>
"@
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($doc))
'@
    try {
        $prelude = '$title = [Security.SecurityElement]::Escape($env:AICLEANER_TOAST_TITLE)' + [Environment]::NewLine +
                   '$body = [Security.SecurityElement]::Escape($env:AICLEANER_TOAST_BODY)' + [Environment]::NewLine
        $env:AICLEANER_TOAST_TITLE = $Title
        $env:AICLEANER_TOAST_BODY = $Body
        try {
            Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @(
                '-NoProfile', '-NonInteractive', '-Command', ($prelude + $toastScript)) | Out-Null
        } finally {
            Remove-Item -LiteralPath 'Env:AICLEANER_TOAST_TITLE', 'Env:AICLEANER_TOAST_BODY' -ErrorAction SilentlyContinue
        }
    } catch {
        Write-Line "could not raise a notification: $($_.Exception.Message)" -Level Warn
    }
}

function Get-HiddenTaskAction {
    <#
    .SYNOPSIS
        A scheduled-task action that runs a program with no window anyone can see (D175).
    .DESCRIPTION
        -WindowStyle Hidden is not enough where Windows Terminal is the default terminal: it ignores the request,
        and closing the window stops the task. conhost.exe --headless gives a console nobody can see. Windows
        before 10 1809 has no --headless; there the program runs as it is. The same function is in every script
        that registers a task, because each one runs on its own.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Execute, [string]$Arguments = '',
          [Parameter(Mandatory)][string]$WorkingDirectory)
    $conhost = Join-Path $env:SystemRoot 'System32\conhost.exe'
    if ([Environment]::OSVersion.Version.Build -ge 17763 -and (Test-Path -LiteralPath $conhost)) {
        return New-ScheduledTaskAction -Execute $conhost -Argument "--headless `"$Execute`" $Arguments" -WorkingDirectory $WorkingDirectory
    }
    return New-ScheduledTaskAction -Execute $Execute -Argument $Arguments -WorkingDirectory $WorkingDirectory
}

function Install-Safeguard {
    [CmdletBinding(SupportsShouldProcess)]
    param([int]$ProxyPort, [int]$ServicePort, [string]$ServiceFile, [string]$Root)
    $self = $PSCommandPath
    if (-not $self) { throw 'Install needs this script to be on disk' }
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $watch = Get-HiddenTaskAction -Execute $powershell -WorkingDirectory (Split-Path -Parent $self) `
        -Arguments "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$self`" -Port $ProxyPort -StateRoot `"$Root`""
    # Every 5 minutes (decided 2026-10-21): from now, and again from each logon. A logon trigger alone only began
    # repeating at the next sign-in, so the watchdog registered mid-session never ran (found 2026-10-23).
    $repeat = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)).Repetition
    $fromNow = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)
    $fromNow.Repetition = $repeat
    $atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $atLogon.Repetition = $repeat
    $trigger = @($fromNow, $atLogon)
    $watchSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4)

    $python = Join-Path $Root 'venv\Scripts\pythonw.exe'
    if (-not (Test-Path -LiteralPath $python)) { $python = Join-Path $Root 'venv\Scripts\python.exe' }
    if (-not (Test-Path -LiteralPath $python)) { throw "aicleaner's Python ($python) is missing" }
    # Started from the app folder, where the aicleaner package is (the task sets its working folder there).
    Push-Location -LiteralPath $script:AppFolder
    try {
        $pac = New-ScheduledTaskAction -Execute $python -WorkingDirectory $script:AppFolder `
            -Argument "-m aicleaner.pacserve --file `"$ServiceFile`" --port $ServicePort"
    } finally {
        Pop-Location
    }
    $pacTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    # No time limit, restarted when it ends: the PAC has to be there whenever Claude Desktop asks.
    $pacSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

    if ($PSCmdlet.ShouldProcess("$($script:WatchTask) and $($script:PacTask)", 'Register scheduled tasks')) {
        Register-ScheduledTask -TaskName $script:WatchTask -Action $watch -Trigger $trigger -Settings $watchSettings -Force -ErrorAction Stop | Out-Null
        Register-ScheduledTask -TaskName $script:PacTask -Action $pac -Trigger $pacTrigger -Settings $pacSettings -Force -ErrorAction Stop | Out-Null
        $running = Get-ScheduledTask -TaskName $script:PacTask -ErrorAction SilentlyContinue
        if ($running -and $running.State -ne 'Running') { Start-ScheduledTask -TaskName $script:PacTask }
        Write-Line "registered $($script:WatchTask) (every 5 minutes) and $($script:PacTask) (port $ServicePort)" -Level Done
    }
}

function Uninstall-Safeguard {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    foreach ($name in $script:WatchTask, $script:PacTask) {
        $found = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        if ($PSCmdlet.ShouldProcess($name, 'Remove scheduled task')) {
            if ($found.State -eq 'Running') { Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue }
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Line "removed $name" -Level Done
        }
    }
}

function Invoke-Watch {
    param([int]$ProxyPort, [string]$Root)
    if (Test-Healthy -ProxyPort $ProxyPort) { return }
    if (Test-Path -LiteralPath (Join-Path $Root 'stopped-on-purpose')) {
        Write-Line 'aicleaner is stopped on purpose (Stop-AiCleaner); not started'
        return
    }
    # Twice, 20 seconds apart: a restart already under way is given the time to finish.
    Start-Sleep -Seconds 20
    if (Test-Healthy -ProxyPort $ProxyPort) { return }
    Write-Line "aicleaner is not answering on port $ProxyPort; starting it" -Level Warn
    $started = $false
    $service = Get-ScheduledTask -TaskName 'aicleaner-service' -ErrorAction SilentlyContinue
    if ($service -and $service.State -ne 'Disabled') {
        # Feature 107: run as SYSTEM; its own task checks it every 5 minutes too. Started here when allowed.
        try { Start-ScheduledTask -TaskName 'aicleaner-service' -ErrorAction Stop; $started = $true }
        catch { Write-Line "could not start aicleaner-service from this account ($($_.Exception.Message)); its own 5-minute check will" -Level Warn }
    } elseif (Get-ScheduledTask -TaskName 'aicleaner-proxy' -ErrorAction SilentlyContinue) {
        Start-ScheduledTask -TaskName 'aicleaner-proxy'
        $started = $true
    } else {
        $start = Join-Path $script:AppFolder 'installer\Start-AiCleaner.ps1'
        if (Test-Path -LiteralPath $start) {
            & $start -Port $ProxyPort -Detach -LogPath ''
            $started = $true
        }
    }
    $body = if ($started) { 'It was started again. Claude Desktop''s chat may not have been filtered while it was down.' }
            else { 'It could not be started. Claude Desktop''s chat may not be filtered - start aicleaner.' }
    Show-Toast -Title 'aicleaner was down' -Body $body
    Write-Line $body -Level $(if ($started) { 'Done' } else { 'Error' })
}

$transcriptStarted = $false
if ($LogPath) { try { Start-Transcript -Path $LogPath -Force | Out-Null; $transcriptStarted = $true } catch { Write-Verbose "no transcript: $($_.Exception.Message)" } }
try {
    if ($Install) { Install-Safeguard -ProxyPort $Port -ServicePort $PacPort -ServiceFile $PacFile -Root $StateRoot; return }
    if ($Uninstall) { Uninstall-Safeguard; return }
    Invoke-Watch -ProxyPort $Port -Root $StateRoot
} catch {
    Write-Line $_.Exception.Message -Level Error
    exit 1
} finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
