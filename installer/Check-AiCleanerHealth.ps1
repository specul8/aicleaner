<#
.SYNOPSIS
    The local wiring, checked on a timer, with the result somewhere the page can read it.

.DESCRIPTION
    Every part of the local setup can stop being true without anyone noticing, and each one fails
    OPEN: the screenshot keeps working, it just sends an image again. Seen in use - press the
    screenshot shortcut, get the image snip rather than the text extractor, because PowerToys had
    been restarted, updated, or had its remap cleared. Nothing said so.

    This is the readiness gate on a schedule (FUTURE-FEATURES #17). It writes a small state file
    the UI reads, so the page can show a line saying which part has drifted.

    Decided 2026-09-27: every 15 minutes, and REPAIR IS OFFERED RATHER THAN AUTOMATIC. Nothing
    here changes the machine unless -Repair is passed, which is what the page's button does. The
    accepted cost is that a drift nobody looks at stays broken; the alternative was a machine
    that quietly undoes a change somebody made on purpose.

    A toast only on the change from healthy to not - not on every tick, which is how people learn
    to ignore them. The previous state is in the file, so the comparison costs nothing.

    Checks only the wiring this machine actually has. In a client-server setup those are two
    different machines: the client PC (Workstation) has the screenshot tooling and nothing else,
    while the VS Code server has the proxy, the routing and the client files. The deployment
    record decides, falling back to what is demonstrably present when there is no record. A
    check that does not apply is recorded as skipped rather than quietly passed.

.PARAMETER Repair
    Put right what can be put right, rather than only reporting it. This is what the page's
    button runs; the scheduled task never passes it.

.PARAMETER StateRoot
    Where the state file goes. Default %USERPROFILE%\.aicleaner.

.PARAMETER Port
    The proxy's port. Default 8787.

.PARAMETER Install
    Register the scheduled task (every 15 minutes, this user) and exit.

.PARAMETER Uninstall
    Remove the scheduled task and exit.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [switch]$Repair,
    [string]$StateRoot = (Join-Path $env:USERPROFILE '.aicleaner'),
    [int]$Port = 8787,
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Quiet
)
# The app folder: this script's own, or its parent when it sits in installer\ (feature 102).
$script:AppFolder = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:TaskName = 'aicleaner-health'
$script:StatePath = Join-Path $StateRoot 'health.json'
# Named once in script scope rather than read from the parameter inside a function: a function
# reading a variable it was never given is the fault that has broken five installers, and
# tests/test_ps_free_variables.py refuses new ones.
$script:QuietRun = [bool]$Quiet

# Which deployment target puts each piece of wiring on a machine. Workstation is the client PC -
# the screenshot tooling and nothing else; VSCode and Desktop are where the proxy runs. In a
# client-server split these are two machines, and a check for the other one's wiring is noise.
$script:RoleFor = @{
    proxy      = @('VSCode', 'Desktop')
    clients    = @('VSCode', 'Desktop')
    routing    = @('VSCode', 'Desktop')
    screenshot = @('Workstation')
}
$script:PowerToysConfig = Join-Path $env:LOCALAPPDATA 'Microsoft\PowerToys\Keyboard Manager\default.json'
$script:ClaudeSettings = Join-Path $env:USERPROFILE '.claude\settings.json'

function Write-Line {
    param([string]$Text, [ValidateSet('Info', 'Warn', 'Error', 'Done')][string]$Level = 'Info')
    if ($script:QuietRun -and $Level -eq 'Info') { return }
    $prefix = switch ($Level) { 'Warn' { 'WARN ' } 'Error' { 'ERROR' } 'Done' { 'OK   ' } default { '     ' } }
    Write-Host "$prefix $Text"
}

function Get-DeployedRoles {
    <# Only check what this machine actually had installed: a workstation should not be told that
       Claude Desktop is misconfigured. #>
    param([string]$Root)
    $record = Join-Path $Root 'deployment.json'
    if (-not (Test-Path -LiteralPath $record)) { return @() }
    try {
        $doc = Get-Content -LiteralPath $record -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch { return @() }
    $rows = if ($doc.PSObject.Properties['deployments']) { @($doc.deployments) } else { @($doc) }
    return @($rows | ForEach-Object { @($_.targets) } | Where-Object { $_ } | Sort-Object -Unique)
}

function Test-ProxyAnswers {
    param([int]$ProxyPort)
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ProxyPort/healthz" -TimeoutSec 5
        return [pscustomobject]@{ Name = 'proxy'; Ok = $true
            Why = "answering on $ProxyPort, $($health.forms) forms"; Fix = '' }
    } catch {
        return [pscustomobject]@{ Name = 'proxy'; Ok = $false
            Why = "nothing answered http://127.0.0.1:$ProxyPort/healthz"
            Fix = 'Start-AiCleaner.ps1 -Detach' }
    }
}

function Test-RoutingPointsAtUs {
    <# Fails open as traffic going straight to the provider, which is the quietest failure here. #>
    param([int]$ProxyPort)
    $want = "http://127.0.0.1:$ProxyPort"
    $settings = $script:ClaudeSettings
    if (-not (Test-Path -LiteralPath $settings)) {
        return [pscustomobject]@{ Name = 'routing'; Ok = $true
            Why = 'no Claude Code settings file on this machine'; Fix = '' }
    }
    try {
        $doc = Get-Content -LiteralPath $settings -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return [pscustomobject]@{ Name = 'routing'; Ok = $false
            Why = "$settings is not readable as JSON"; Fix = 'Fix the file, then Enable-AiCleaner.ps1' }
    }
    $have = ''
    if ($doc.PSObject.Properties['env'] -and $doc.env -and $doc.env.PSObject.Properties['ANTHROPIC_BASE_URL']) {
        $have = [string]$doc.env.ANTHROPIC_BASE_URL
    }
    if ($have -eq $want) {
        return [pscustomobject]@{ Name = 'routing'; Ok = $true; Why = "Claude Code points at $want"; Fix = '' }
    }
    $saw = if ($have) { $have } else { 'nothing' }
    return [pscustomobject]@{ Name = 'routing'; Ok = $false
        Why = "Claude Code's ANTHROPIC_BASE_URL is $saw, not $want - traffic is going straight to the provider"
        Fix = 'Enable-AiCleaner.ps1' }
}

function Test-ScreenshotRemap {
    <# Fails open as an image snip, silently. The remap is the thing that has actually broken. #>
    $config = $script:PowerToysConfig
    try {
        $text = Get-Content -LiteralPath $config -Raw -Encoding UTF8
    } catch {
        return [pscustomobject]@{ Name = 'screenshot'; Ok = $false
            Why = 'the PowerToys remap file could not be read'; Fix = 'Install-AiCleaner-Workstation.ps1' }
    }
    # The screenshot keys start Capture-TextFromScreen.ps1 (2026-10-12), or on an older install go to
    # Win+Shift+T (84 is T). Matched on the text rather than parsed, because the shape of this file
    # is PowerToys' to change and a miss here must not be read as a drift.
    if ($text -match 'Capture-TextFromScreen\.ps1') {
        return [pscustomobject]@{ Name = 'screenshot'; Ok = $true; Why = 'the screenshot keys start the screen capture'; Fix = '' }
    }
    if ($text -match '"84"' -or $text -match 'Win\+Shift\+T' -or $text -match '91;16;84') {
        return [pscustomobject]@{ Name = 'screenshot'; Ok = $true; Why = 'Win+Shift+T is still mapped'; Fix = '' }
    }
    return [pscustomobject]@{ Name = 'screenshot'; Ok = $false
        Why = 'the screenshot remap is not in the PowerToys config - a screenshot sends an image instead of text'
        Fix = 'Install-AiCleaner-Workstation.ps1' }
}

function Test-ClientFilesReadable {
    param([string]$Root)
    $clients = Join-Path $Root 'clients'
    # OneDrive and Dropbox each name a conflict copy differently; the shapes that matter are
    # "name (2).json", "... conflicted copy ...", and "...-<machine>s conflicted copy...".
    $conflicts = @(Get-ChildItem -LiteralPath $clients -Filter '*.json' -File -ErrorAction SilentlyContinue |
                   Where-Object { $_.Name -match '\(\d+\)' -or $_.Name -match 'conflicted copy' })
    if ($conflicts.Count -gt 0) {
        return [pscustomobject]@{ Name = 'clients'; Ok = $false
            Why = "a sync conflict copy has appeared beside the client files: $(($conflicts | ForEach-Object { $_.Name }) -join ', ')"
            Fix = '' }
    }
    return [pscustomobject]@{ Name = 'clients'; Ok = $true; Why = 'no conflict copies'; Fix = '' }
}

function Test-CheckApplies {
    <#
    .SYNOPSIS
        Whether a named check means anything on this machine.
    .DESCRIPTION
        A check nobody installed the thing for is noise, and noise is how a person learns to stop
        reading the panel. The deployment record is the first answer: Workstation is the client PC
        and has only the screenshot tooling; VSCode and Desktop are where the proxy, the routing
        and the client files live. In a client-server split those are two different machines and
        neither should be told about the other's wiring.

        With no record - installed by hand, or by a release older than the one that started
        writing targets - fall back to what is demonstrably ON the machine rather than to
        everything, which is what made a VS Code server complain about a PowerToys remap it was
        never going to have.

        Role OR evidence, not role alone: somebody who set PowerToys up by hand on a machine
        recorded as VSCode still wants to know when the remap goes.
    #>
    param([Parameter(Mandatory)][string]$Name, [string[]]$Roles, [string]$Root)
    $wanted = $script:RoleFor[$Name]
    if ($Roles -and @($Roles | Where-Object { $wanted -contains $_ }).Count -gt 0) { return $true }
    switch ($Name) {
        'screenshot' { return (Test-Path -LiteralPath $script:PowerToysConfig) }
        'routing'    { return (Test-Path -LiteralPath $script:ClaudeSettings) }
        'clients'    { return (Test-Path -LiteralPath (Join-Path $Root 'clients')) }
        default      { return $true }   # the proxy is this app's own, so always worth asking
    }
}

function Get-HealthChecks {
    <# Returns every check that applies here, plus a Skipped list so the state file can say
       "not checked on this machine" rather than leaving a silent gap that reads as a pass. #>
    param([string]$Root, [int]$ProxyPort)
    $roles = @(Get-DeployedRoles -Root $Root)
    $checks = @()
    $skipped = @()
    $applies = @{}
    foreach ($name in @('proxy', 'clients', 'routing', 'screenshot')) {
        $applies[$name] = Test-CheckApplies -Name $name -Roles $roles -Root $Root
        if (-not $applies[$name]) {
            $where = if ($roles.Count) { "this machine is $($roles -join ', ')" } else { 'it is not set up here' }
            $skipped += [pscustomobject]@{ Name = $name; Why = "not checked - $where" }
        }
    }
    $proxy = $null
    if ($applies['proxy']) { $proxy = Test-ProxyAnswers -ProxyPort $ProxyPort; $checks += $proxy }
    if ($applies['clients']) { $checks += Test-ClientFilesReadable -Root $Root }
    if ($applies['routing']) {
        if ($proxy -and -not $proxy.Ok) {
            # Routing away from a proxy that is not running is the SAFE state, not a drift - it
            # is the escape hatch working (feature 61). Calling it a fault here would cry wolf
            # every time the proxy is deliberately stopped, which is exactly how a person learns
            # to ignore this check. The proxy being down is already the line above.
            $checks += [pscustomobject]@{ Name = 'routing'; Ok = $true
                Why = 'the proxy is not running, so pointing elsewhere is correct rather than a drift'
                Fix = '' }
        } else {
            $checks += Test-RoutingPointsAtUs -ProxyPort $ProxyPort
        }
    }
    if ($applies['screenshot']) { $checks += Test-ScreenshotRemap }
    return [pscustomobject]@{ Checks = @($checks); Skipped = @($skipped) }
}

function Read-PreviousState {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { return $null }
}

function Save-State {
    param([string]$Path, [Parameter(Mandatory)][object[]]$Checks, [object[]]$Skipped = @())
    $folder = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
    $healthy = -not (@($Checks | Where-Object { -not $_.Ok }).Count)
    $doc = [pscustomobject]@{
        checked_at = (Get-Date).ToString('s')
        healthy    = [bool]$healthy
        checks     = @($Checks | ForEach-Object {
            [pscustomobject]@{ name = $_.Name; ok = [bool]$_.Ok; why = [string]$_.Why; fix = [string]$_.Fix }
        })
        # Recorded so the page can tell "not checked here" from "checked and fine" - a silent
        # gap reads as a pass, and that is the failure this whole feature exists to stop.
        skipped    = @($Skipped | ForEach-Object {
            [pscustomobject]@{ name = $_.Name; why = [string]$_.Why }
        })
    }
    $json = $doc | ConvertTo-Json -Depth 6
    [IO.File]::WriteAllText($Path, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function Show-Toast {
    <# Only on the change from healthy to not. A toast every fifteen minutes is a toast nobody
       reads, which is worse than none. #>
    param([string]$Body)
    $toastScript = @'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
$xml = @"
<toast><visual><binding template="ToastGeneric">
  <text>aicleaner: something has drifted</text>
  <text>$body</text>
</binding></visual></toast>
"@
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($doc))
'@
    try {
        # The text goes through the environment, never spliced into the script: the reasons are
        # written here today, but they quote file names that came off disk, and a name carrying a
        # quote or a backtick would otherwise close the string and run as code. Single-quoted
        # here-string above, so $body is expanded by the CHILD after it reads the variable, and
        # XML-escaped there rather than stripped here.
        # Set on this process and inherited by the child, rather than Start-Process -Environment,
        # which is PowerShell 7.4 and up: the scheduled task runs this under powershell.exe.
        $prelude = '$body = [Security.SecurityElement]::Escape($env:AICLEANER_TOAST_BODY)' + [Environment]::NewLine
        $env:AICLEANER_TOAST_BODY = $Body
        try {
            Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @(
                '-NoProfile', '-NonInteractive', '-Command', ($prelude + $toastScript)) | Out-Null
        } finally {
            Remove-Item -LiteralPath 'Env:AICLEANER_TOAST_BODY' -ErrorAction SilentlyContinue
        }
    } catch {
        Write-Line "could not raise a toast: $($_.Exception.Message)" -Level Warn
    }
}

function Get-HiddenTaskAction {
    <#
    .SYNOPSIS
        A scheduled-task action that runs a PowerShell script with no window anyone can see (D175).
    .DESCRIPTION
        -WindowStyle Hidden is not enough: where Windows Terminal is the default terminal it ignores
        the request, so the window stays on the desktop showing the script's output - and closing
        it stops the task. conhost.exe --headless gives the script a console nobody can see, and
        Windows Terminal is never handed it. Windows before 10 1809 has no --headless; there the
        old action is used. The same function is in every script that registers a task, because
        each one runs on its own.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Script, [string]$Arguments = '',
          [Parameter(Mandatory)][string]$WorkingDirectory)
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $run = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`""
    if ($Arguments) { $run += " $Arguments" }
    $conhost = Join-Path $env:SystemRoot 'System32\conhost.exe'
    if ([Environment]::OSVersion.Version.Build -ge 17763 -and (Test-Path -LiteralPath $conhost)) {
        return New-ScheduledTaskAction -Execute $conhost -Argument "--headless `"$powershell`" $run" -WorkingDirectory $WorkingDirectory
    }
    return New-ScheduledTaskAction -Execute $powershell -Argument $run -WorkingDirectory $WorkingDirectory
}

function Install-HealthTask {
    param([int]$ProxyPort, [string]$Root)
    $self = $PSCommandPath
    if (-not $self) { throw 'Install needs this script to be on disk' }
    $action = Get-HiddenTaskAction -Script $self -Arguments "-Quiet -Port $ProxyPort -StateRoot `"$Root`"" `
        -WorkingDirectory (Split-Path -Parent $self)
    # Per-user, not a service: everything it looks at is per-user and a service would have to
    # impersonate. Every 15 minutes (decided 2026-09-27), from logon.
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration ([TimeSpan]::MaxValue)).Repetition
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    if ($PSCmdlet.ShouldProcess($script:TaskName, 'Register scheduled task')) {
        Register-ScheduledTask -TaskName $script:TaskName -Action $action -Trigger $trigger `
            -Settings $settings -Force | Out-Null
        Write-Line "registered $($script:TaskName): every 15 minutes, this user" -Level Done
    }
}

function Uninstall-HealthTask {
    $found = Get-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue
    if (-not $found) { Write-Line "no $($script:TaskName) task to remove"; return }
    if ($PSCmdlet.ShouldProcess($script:TaskName, 'Remove scheduled task')) {
        Unregister-ScheduledTask -TaskName $script:TaskName -Confirm:$false
        Write-Line "removed $($script:TaskName)" -Level Done
    }
}

# ---- what the run does ------------------------------------------------------------------------

if ($Install) { Install-HealthTask -ProxyPort $Port -Root $StateRoot; return }
if ($Uninstall) { Uninstall-HealthTask; return }

$previous = Read-PreviousState -Path $script:StatePath
$found = Get-HealthChecks -Root $StateRoot -ProxyPort $Port
$checks = @($found.Checks)
$skipped = @($found.Skipped)
$bad = @($checks | Where-Object { -not $_.Ok })

if ($Repair) {
    # Offered, never automatic: this only runs because somebody pressed the button (decided
    # 2026-09-27). The installers are idempotent, so re-running a wiring step is the repair, and
    # the check that just failed is its verification.
    $appRoot = Split-Path -Parent $PSCommandPath
    foreach ($check in $bad) {
        if (-not $check.Fix) {
            Write-Line "$($check.Name): nothing to run for this one - $($check.Why)" -Level Warn
            continue
        }
        $script = Join-Path $appRoot $check.Fix.Split(' ')[0]
        if (-not (Test-Path -LiteralPath $script)) {
            Write-Line "$($check.Name): $($check.Fix) is not beside this script" -Level Warn
            continue
        }
        Write-Line "$($check.Name): running $($check.Fix)"
        if ($PSCmdlet.ShouldProcess($script, 'Run the wiring step again')) {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -AssumeDefaults 2>&1 |
                ForEach-Object { Write-Line "  $_" }
        }
    }
    $found = Get-HealthChecks -Root $StateRoot -ProxyPort $Port
    $checks = @($found.Checks)
    $skipped = @($found.Skipped)
    $bad = @($checks | Where-Object { -not $_.Ok })
}

Save-State -Path $script:StatePath -Checks $checks -Skipped $skipped

foreach ($check in $checks) {
    if ($check.Ok) { Write-Line "$($check.Name): $($check.Why)" -Level Done }
    else { Write-Line "$($check.Name): $($check.Why)" -Level Warn }
}
foreach ($miss in $skipped) { Write-Line "$($miss.Name): $($miss.Why)" }

if ($bad.Count -gt 0) {
    $wasHealthy = $previous -and $previous.PSObject.Properties['healthy'] -and $previous.healthy
    # Only on the change, and only when nobody asked: a repair run has a person watching it.
    if (($wasHealthy -or -not $previous) -and -not $Repair) {
        Show-Toast -Body (($bad | ForEach-Object { $_.Why }) -join '; ')
    }
    Write-Line "$($bad.Count) part(s) of the wiring have drifted" -Level Error
    exit 1
}
Write-Line 'every part of the wiring is as it should be' -Level Done
