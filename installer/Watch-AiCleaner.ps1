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

    Every watchdog check also appends one row to <StateRoot>\logs\os-health-<yyyyMMdd>.csv (spec
    2026-10-24-session-lifecycle-instrumentation.md, "Instrumentation, at the OS level"): commit, memory and
    page file use, the system's handle count, the claude.exe / node.exe / aicleaner Python processes and how
    many of them have lost their parent, TCP and UDP endpoints with their top owner, the minutes since the
    last clock jump (a resume from sleep or a restored guest), and whether aicleaner answered. The watchdog
    runs whether or not aicleaner is up, so the record covers exactly the time the proxy cannot log for
    itself. A failure there is a warning only: it never stops the check. Files older than -HealthDays go.

    Columns: Time, CommitUsedGB, CommitLimitGB, AvailableMB, PageFileUsedMB, PageFileAllocatedMB,
    SystemHandles, Processes, ClaudeCount, NodeCount, AicleanerPythonCount, DeadParentCount,
    DeadParentDetail (up to 5 "name:pid:ageMin:privateMB"), TcpEndpoints, TcpTopOwner, UdpEndpoints,
    UdpTopOwner, MinutesSinceClockJump, ProxyHealthy.

    The aicleaner Python count is every python.exe / pythonw.exe under <StateRoot>\venv plus the Python each
    of those started: a venv's python.exe is a launcher that runs the base interpreter as its child, and that
    child is the process that is left behind when the launcher goes.

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

.PARAMETER HealthDays
    How many days of os-health-*.csv files to keep. Default 14.

.PARAMETER NoOsHealth
    Skip the OS health row for this check.

.PARAMETER OsHealthOnly
    Write one OS health row (after one /healthz test) and exit, without starting anything. For tests and
    for a look by hand; hidden from tab completion.

.EXAMPLE
    .\Watch-AiCleaner.ps1 -OsHealthOnly -LogPath ''
    Appends one row to %USERPROFILE%\.aicleaner\logs\os-health-<today>.csv and changes nothing else.

.EXAMPLE
    .\Watch-AiCleaner.ps1 -Install
    Registers the watchdog and the PAC service for this user.

.EXAMPLE
    .\Watch-AiCleaner.ps1
    One watchdog check: starts aicleaner and says so if it is down.

.NOTES
    Last-modified: 2026-10-24
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
    ),
    [ValidateRange(1, 3650)][int]$HealthDays = 14,
    [switch]$NoOsHealth,
    [Parameter(DontShow)][switch]$OsHealthOnly
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

# An aicleaner already starting for this config (the same test as Start-AiCleaner's): a process running
# "-m aicleaner run" with this config file. Matched on the path, so a test server is never mistaken for it.
function Get-StartingProxy {
    param([string]$Config)
    $wanted = [IO.Path]::GetFullPath($Config).ToLowerInvariant()
    Get-CimInstance -ClassName Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $line = [string]$_.CommandLine
            $line -match '-m\s+aicleaner\s+run\b' -and $line.ToLowerInvariant().Contains($wanted)
        } | Select-Object -First 1
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

function ConvertTo-CsvField {
    <# One CSV field, invariant culture so a comma decimal separator never splits a column. #>
    param([AllowNull()][object]$Value)
    if ($null -eq $Value) { return '' }
    $text = [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $Value)
    if ([regex]::IsMatch($text, '[",\r\n]')) { return '"' + $text.Replace('"', '""') + '"' }
    return $text
}

function Get-WatchedProcessHealth {
    <#
    .SYNOPSIS
        The counts of claude, node and aicleaner Python processes, and which of them have lost their parent.
    .DESCRIPTION
        A parent is lost when its id is not a live process, or when the live process with that id started after
        the child did - Windows has given the dead parent's id to something new. cmd.exe is watched for the
        dead-parent count too: it is the shell a session's tools run in, and a common orphan.
    #>
    param([object[]]$Process, [string]$VenvPath, [datetime]$Now, [int]$DetailCount = 5)
    $byId = @{}
    foreach ($item in $Process) { $byId[[int]$item.ProcessId] = $item }
    $pythonNames = @('python.exe', 'pythonw.exe')
    $venvPrefix = $VenvPath.TrimEnd('\') + '\'
    $venvIds = @{}
    foreach ($item in $Process) {
        if ($pythonNames -contains $item.Name -and $item.ExecutablePath -and
            $item.ExecutablePath.StartsWith($venvPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            $venvIds[[int]$item.ProcessId] = $true
        }
    }
    # The venv launcher's child is the real interpreter, under the base Python's path.
    $aicleanerPython = @($Process | Where-Object {
        $pythonNames -contains $_.Name -and ($venvIds.ContainsKey([int]$_.ProcessId) -or $venvIds.ContainsKey([int]$_.ParentProcessId))
    })
    $claude = @($Process | Where-Object { $_.Name -eq 'claude.exe' })
    $node = @($Process | Where-Object { $_.Name -eq 'node.exe' })
    $cmd = @($Process | Where-Object { $_.Name -eq 'cmd.exe' })

    $orphans = @(foreach ($item in @($claude + $node + $aicleanerPython + $cmd)) {
        $parent = $byId[[int]$item.ParentProcessId]
        $reused = $parent -and $parent.CreationDate -and $item.CreationDate -and ($parent.CreationDate -gt $item.CreationDate)
        if (-not $parent -or $reused) { $item }
    })
    # The biggest first: memory is what an orphan costs.
    $detail = @($orphans | Sort-Object -Property PrivatePageCount -Descending | Select-Object -First $DetailCount | ForEach-Object {
        $ageMin = if ($_.CreationDate) { [int][math]::Floor(($Now - $_.CreationDate).TotalMinutes) } else { '' }
        $privateMB = if ($_.PrivatePageCount) { [int][math]::Round($_.PrivatePageCount / 1MB) } else { 0 }
        '{0}:{1}:{2}:{3}' -f $_.Name, $_.ProcessId, $ageMin, $privateMB
    })
    [ordered]@{
        ClaudeCount          = $claude.Count
        NodeCount            = $node.Count
        AicleanerPythonCount = $aicleanerPython.Count
        DeadParentCount      = $orphans.Count
        DeadParentDetail     = ($detail -join ';')
    }
}

function Get-TopOwner {
    <# "name:count" for the process that owns the most of the given endpoints. #>
    param([object[]]$Endpoint, [hashtable]$NameById)
    if (-not $Endpoint) { return '' }
    $top = $Endpoint | Group-Object -Property OwningProcess | Sort-Object -Property Count -Descending | Select-Object -First 1
    $owner = $NameById[[int]$top.Name]
    if (-not $owner) { $owner = "pid$($top.Name)" }
    return '{0}:{1}' -f $owner, $top.Count
}

function Get-EndpointHealth {
    <# TCP and UDP endpoint counts with their top owner. Empty where the NetTCPIP cmdlets are missing or fail. #>
    param([hashtable]$NameById)
    $result = [ordered]@{ TcpEndpoints = ''; TcpTopOwner = ''; UdpEndpoints = ''; UdpTopOwner = '' }
    if (Get-Command -Name Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        try {
            $tcp = @(Get-NetTCPConnection -ErrorAction Stop)
            $result.TcpEndpoints = $tcp.Count
            $result.TcpTopOwner = Get-TopOwner -Endpoint $tcp -NameById $NameById
        } catch { Write-Verbose "no TCP endpoints: $($_.Exception.Message)" }
    }
    if (Get-Command -Name Get-NetUDPEndpoint -ErrorAction SilentlyContinue) {
        try {
            $udp = @(Get-NetUDPEndpoint -ErrorAction Stop)
            $result.UdpEndpoints = $udp.Count
            $result.UdpTopOwner = Get-TopOwner -Endpoint $udp -NameById $NameById
        } catch { Write-Verbose "no UDP endpoints: $($_.Exception.Message)" }
    }
    $result
}

function Get-MinutesSinceClockJump {
    <#
    .SYNOPSIS
        Minutes since the latest System clock change bigger than -ThresholdMs, or '' when there is none.
    .DESCRIPTION
        Kernel-General event 1 is written for every time change, including the small ones time sync makes all
        day; a resume from sleep or a restored guest shows as a jump of minutes or hours. The delta comes from
        the event's own new and old times (properties 0 and 1, the same on every Windows language), and only
        when those are missing from the "Time Delta: N ms" line of the English message.
    #>
    param([datetime]$Now, [int]$ThresholdMs = 60000, [int]$LookbackDays = 2)
    $filter = @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-Kernel-General'; Id = 1; StartTime = $Now.AddDays(-$LookbackDays) }
    try { $events = @(Get-WinEvent -FilterHashtable $filter -ErrorAction Stop) }
    catch { return '' }   # none in the window is reported as an error too
    foreach ($record in $events) {
        $deltaMs = $null
        $values = @($record.Properties | ForEach-Object { $_.Value })
        if ($values.Count -ge 2 -and $values[0] -is [datetime] -and $values[1] -is [datetime]) {
            $deltaMs = ($values[0] - $values[1]).TotalMilliseconds
        } elseif ($record.Message) {
            $found = [regex]::Match($record.Message, 'Time Delta:\s*(?<ms>-?\d+)\s*ms')
            if ($found.Success) { $deltaMs = [double]$found.Groups['ms'].Value }
        }
        if ($null -ne $deltaMs -and [math]::Abs($deltaMs) -gt $ThresholdMs) {
            return [int][math]::Floor(($Now - $record.TimeCreated).TotalMinutes)
        }
    }
    return ''
}

function Write-OsHealthRow {
    <#
    .SYNOPSIS
        Append one row of OS health to <Root>\logs\os-health-<yyyyMMdd>.csv, and drop files older than -KeepDays.
    .DESCRIPTION
        One Win32_Process query serves every process figure (counts, handles, orphans, endpoint owners), so a
        row costs a second or two. The header is written when the day's file is new.
    .EXAMPLE
        Write-OsHealthRow -Root "$env:USERPROFILE\.aicleaner" -ProxyHealthy $true
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Root, [AllowEmptyString()][string]$ProxyHealthy = '', [int]$KeepDays = 14)
    $now = Get-Date
    $folder = Join-Path $Root 'logs'
    if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }

    $os = Get-CimInstance -ClassName Win32_OperatingSystem -Property TotalVirtualMemorySize, FreeVirtualMemory, FreePhysicalMemory
    $pageFiles = @(Get-CimInstance -ClassName Win32_PageFileUsage -Property CurrentUsage, AllocatedBaseSize)
    $processes = @(Get-CimInstance -ClassName Win32_Process -Property ProcessId, ParentProcessId, Name, ExecutablePath, CreationDate, HandleCount, PrivatePageCount)
    $nameById = @{}
    foreach ($item in $processes) { $nameById[[int]$item.ProcessId] = $item.Name }
    $invariant = [Globalization.CultureInfo]::InvariantCulture

    # Win32_OperatingSystem reports commit as "virtual memory", in KB.
    $row = [ordered]@{
        Time                = $now.ToString('yyyy-MM-ddTHH:mm:sszzz', $invariant)
        CommitUsedGB        = ([double]$os.TotalVirtualMemorySize - [double]$os.FreeVirtualMemory) / 1MB
        CommitLimitGB       = [double]$os.TotalVirtualMemorySize / 1MB
        AvailableMB         = [int][math]::Round([double]$os.FreePhysicalMemory / 1KB)
        PageFileUsedMB      = [int]($pageFiles | Measure-Object -Property CurrentUsage -Sum).Sum
        PageFileAllocatedMB = [int]($pageFiles | Measure-Object -Property AllocatedBaseSize -Sum).Sum
        SystemHandles       = [long]($processes | Measure-Object -Property HandleCount -Sum).Sum
        Processes           = $processes.Count
    }
    $row.CommitUsedGB = $row.CommitUsedGB.ToString('0.00', $invariant)
    $row.CommitLimitGB = $row.CommitLimitGB.ToString('0.00', $invariant)
    $watched = Get-WatchedProcessHealth -Process $processes -VenvPath (Join-Path $Root 'venv') -Now $now
    foreach ($key in $watched.Keys) { $row[$key] = $watched[$key] }
    $endpoints = Get-EndpointHealth -NameById $nameById
    foreach ($key in $endpoints.Keys) { $row[$key] = $endpoints[$key] }
    $row.MinutesSinceClockJump = Get-MinutesSinceClockJump -Now $now
    $row.ProxyHealthy = $ProxyHealthy

    $file = Join-Path $folder ('os-health-{0}.csv' -f $now.ToString('yyyyMMdd', $invariant))
    $lines = New-Object System.Collections.Generic.List[string]
    if (-not (Test-Path -LiteralPath $file)) { $lines.Add((@($row.Keys) -join ',')) }
    $lines.Add((@($row.Values | ForEach-Object { ConvertTo-CsvField -Value $_ }) -join ','))
    [IO.File]::AppendAllText($file, (($lines -join "`r`n") + "`r`n"), (New-Object System.Text.UTF8Encoding $false))

    $cutoff = $now.AddDays(-$KeepDays)
    Get-ChildItem -LiteralPath $folder -Filter 'os-health-*.csv' -File |
        Where-Object { $_.LastWriteTime -lt $cutoff } | Remove-Item -Force
    Write-Verbose "os health row written to $file"
}

function Test-ServiceCannotSignIn {
    <# Feature 107: true when the start-up task is disabled or its last result says Windows could not sign it in
       (wrong or changed password, logon type not granted, expired password, disabled account). #>
    param([Parameter(Mandatory)]$Task)
    if ([string]$Task.State -eq 'Disabled') { return $true }
    try { $last = [int64](Get-ScheduledTaskInfo -TaskName $Task.TaskName -ErrorAction Stop).LastTaskResult }
    catch { return $false }
    if ($last -lt 0) { $last += 4294967296 }
    return (@([int64]2147943726, [int64]2147943785, [int64]2147943730, [int64]2147943731, [int64]1326, [int64]1385, [int64]1330, [int64]1331) -contains $last)
}

function Invoke-Watch {
    param([int]$ProxyPort, [string]$Root, [switch]$SkipHealth, [int]$KeepDays = 14)
    $healthy = Test-Healthy -ProxyPort $ProxyPort
    # The record is a side job: whatever goes wrong in it, the check below still runs.
    if (-not $SkipHealth) {
        try { Write-OsHealthRow -Root $Root -ProxyHealthy $healthy -KeepDays $KeepDays }
        catch { Write-Line "no OS health row this time: $($_.Exception.Message)" -Level Warn }
    }
    if ($healthy) { return }
    if (Test-Path -LiteralPath (Join-Path $Root 'stopped-on-purpose')) {
        Write-Line 'aicleaner is stopped on purpose (Stop-AiCleaner); not started'
        return
    }
    # Twice, 20 seconds apart: a restart already under way is given the time to finish.
    Start-Sleep -Seconds 20
    if (Test-Healthy -ProxyPort $ProxyPort) { return }
    # Still starting - a cold boot can take longer than 20 seconds - is not down. At logon the proxy's own task and
    # this check both started a copy, and the second crashed on the port (stability spec S3).
    $starting = Get-StartingProxy -Config (Join-Path $script:AppFolder 'aicleaner-config.json')
    if ($starting) {
        Write-Line "aicleaner is still starting (pid $($starting.ProcessId)); not starting another"
        return
    }
    Write-Line "aicleaner is not answering on port $ProxyPort; starting it" -Level Warn
    $started = $false
    $service = Get-ScheduledTask -TaskName 'aicleaner-service' -ErrorAction SilentlyContinue
    # Feature 107: started with the computer as you. A task Windows cannot sign in (password changed) or that is
    # disabled is never started again and again: aicleaner is started as you instead, as before (Start-AiCleaner
    # says why).
    $serviceUsable = $service -and -not (Test-ServiceCannotSignIn -Task $service)
    if ($serviceUsable -and [string]$service.State -eq 'Running') {
        # Running but not answering yet - still starting. Asking again is refused (0x800710E0) and only leaves a
        # result that reads like a failure (2026-10-25's live test).
        Write-Line 'the start-up task is running; aicleaner is still starting'
        return
    }
    if ($serviceUsable) {
        try { Start-ScheduledTask -TaskName 'aicleaner-service' -ErrorAction Stop; $started = $true }
        catch { Write-Line "could not start aicleaner-service from this account ($($_.Exception.Message))" -Level Warn }
    }
    if ($started) {
        Write-Verbose 'started through the start-up task'
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
    if ($OsHealthOnly) {
        Write-OsHealthRow -Root $StateRoot -ProxyHealthy (Test-Healthy -ProxyPort $Port) -KeepDays $HealthDays
        return
    }
    Invoke-Watch -ProxyPort $Port -Root $StateRoot -SkipHealth:$NoOsHealth -KeepDays $HealthDays
} catch {
    Write-Line $_.Exception.Message -Level Error
    exit 1
} finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
