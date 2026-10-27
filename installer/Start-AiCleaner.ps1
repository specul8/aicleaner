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

.PARAMETER StartTimeoutSeconds
    How long a hidden start waits for /healthz while the process it started is still alive. Default 90: a cold
    boot with OneDrive, Defender and Edge starting took longer than the old 15 seconds (stability spec S3).

.EXAMPLE
    .\Start-AiCleaner.ps1
    Runs the proxy in this console; Ctrl+C stops it.

.EXAMPLE
    .\Start-AiCleaner.ps1 -Detach
    Starts it hidden and returns once /healthz answers.

.NOTES
    Contact:       see contact-*.json stored alongside this script
    Last-modified: 2026-10-24
    Requires:      Windows PowerShell 5.1 or PowerShell 7+; Install-AiCleaner-VSCode.ps1 run first
#>

#Requires -Version 5.1

[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)][int]$Port = 8787,
    [switch]$Detach,
    [ValidateRange(5, 600)][int]$StartTimeoutSeconds = 90,
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

# An aicleaner already starting for this config: a process running "-m aicleaner run" with this config file. Two
# starters at logon (the proxy task and the watchdog) used to each start one; the second crashed on the port
# (stability spec S3). Matched on the config path, so a test server with its own config is never mistaken for it.
function Get-StartingProxy {
    param([string]$Config)
    $wanted = [IO.Path]::GetFullPath($Config).ToLowerInvariant()
    Get-CimInstance -ClassName Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $line = [string]$_.CommandLine
            $line -match '-m\s+aicleaner\s+run\b' -and $line.ToLowerInvariant().Contains($wanted)
        } | Select-Object -First 1
}

# Waits for /healthz up to the deadline, while the condition holds; says so every few seconds.
function Wait-Proxy {
    param([int]$ProxyPort, [datetime]$Deadline, [scriptblock]$StillWorth)
    $begun = Get-Date
    $nextNote = $begun.AddSeconds(3)
    do {
        Start-Sleep -Milliseconds 500
        $answer = Test-Proxy -ProxyPort $ProxyPort
        if (-not $answer -and (Get-Date) -ge $nextNote) {
            Write-Status ("still starting ({0:N0}s)" -f ((Get-Date) - $begun).TotalSeconds) -Level Detail
            $nextNote = (Get-Date).AddSeconds(3)
        }
    } while (-not $answer -and (Get-Date) -lt $Deadline -and (& $StillWorth))
    return $answer
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

function Show-Toast {
    <# A Windows notification (the watchdog's own, inlined so this script stays self-sufficient). The text goes
       through the environment, never spliced into the child's script, so nothing in it can run as code. #>
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
        Write-Status "could not raise a notification: $($_.Exception.Message)" -Level Warn
    }
}

function Get-ServiceProblem {
    <#
    .SYNOPSIS
        Why the start-up task (feature 107) cannot run aicleaner: 'password' when Windows could not sign it in
        (wrong or changed password, logon type not granted, expired, disabled account), 'disabled', or ''.
    #>
    param([Parameter(Mandatory)]$Task)
    if ([string]$Task.State -eq 'Disabled') { return 'disabled' }
    try { $last = [int64](Get-ScheduledTaskInfo -TaskName $Task.TaskName -ErrorAction Stop).LastTaskResult }
    catch { return '' }
    if ($last -lt 0) { $last += 4294967296 }
    if (@([int64]2147943726, [int64]2147943785, [int64]2147943730, [int64]2147943731, [int64]1326, [int64]1385, [int64]1330, [int64]1331) -contains $last) { return 'password' }
    return ''
}

function Invoke-ServiceStart {
    <#
    .SYNOPSIS
        Feature 107: when the aicleaner-service task exists, aicleaner is started (or waited for) through it.
    .DESCRIPTION
        $true when it answers, started by the task. $false when the caller should start aicleaner as the signed-in
        user: there is no such task, Windows cannot sign it in (the notice says so, once per start), or it could
        not be started from this account.
    #>
    param([int]$ProxyPort, [int]$TimeoutSeconds)
    $task = Get-ScheduledTask -TaskName 'aicleaner-service' -ErrorAction SilentlyContinue
    if (-not $task) { return $false }
    $notice = "aicleaner's start-up task could not sign in (password changed?) - Settings > Running aicleaner to enter it again"
    $problem = Get-ServiceProblem -Task $task
    if (-not $problem) {
        if (Test-Proxy -ProxyPort $ProxyPort) {
            Write-Status "aicleaner is running on port $ProxyPort (started with the computer, as you)" -Level Done
            return $true
        }
        if ([string]$task.State -ne 'Running') {
            try { Start-ScheduledTask -TaskName 'aicleaner-service' -ErrorAction Stop }
            catch {
                Write-Status "could not start the start-up task from this account ($($_.Exception.Message)); starting aicleaner here" -Level Warn
                return $false
            }
        }
        Write-Status 'waiting for the start-up task (aicleaner-service) to bring aicleaner up'
        $until = (Get-Date).AddSeconds($TimeoutSeconds)
        while ((Get-Date) -lt $until) {
            if (Test-Proxy -ProxyPort $ProxyPort) {
                Write-Status "aicleaner is running on port $ProxyPort (started with the computer, as you)" -Level Done
                return $true
            }
            # Windows tries the sign-in when the task starts: a changed password shows here within seconds.
            $problem = Get-ServiceProblem -Task $task
            if ($problem) { break }
            Start-Sleep -Seconds 2
        }
        if (-not $problem) {
            Write-Status "the start-up task did not bring aicleaner up within ${TimeoutSeconds}s; starting it here" -Level Warn
            return $false
        }
    }
    if ($problem -eq 'disabled') { $notice = "aicleaner's start-up task is disabled - started at sign-in instead; Settings > Running aicleaner to set it up again" }
    Write-Status $notice -Level Warn
    Show-Toast -Title 'aicleaner started at sign-in instead' -Body $notice
    return $false
}

$transcriptStarted = $false
if ($LogPath) {
    try { Start-Transcript -Path $LogPath -Force | Out-Null; $transcriptStarted = $true }
    catch { Write-Status "Could not start transcript at ${LogPath}: $($_.Exception.Message)" -Level Warn }
}

try {
    $root = $script:AppFolder
    if (-not $ConfigPath) { $ConfigPath = Join-Path $root 'aicleaner-config.json' }
    # Started again: route B's watchdog may restart it from now on (Stop-AiCleaner left the marker).
    Remove-Item -LiteralPath (Join-Path $env:USERPROFILE '.aicleaner\stopped-on-purpose') -ErrorAction SilentlyContinue
    # Shared venv lives outside synced folders; a repo-local .venv from an earlier install still works.
    $sharedPython = Join-Path $env:USERPROFILE '.aicleaner\venv\Scripts\python.exe'
    $venvPython = $sharedPython
    if (-not (Test-Path -LiteralPath $venvPython)) { $venvPython = Join-Path $root '.venv\Scripts\python.exe' }

    # Feature 107: started with the computer as you by the aicleaner-service task, whose launcher loop keeps it running.
    # Started (or waited for) through that task rather than a second copy here - unless Windows cannot sign the task
    # in (your password changed), when it is started here as you, as before, and you are told.
    if (Invoke-ServiceStart -ProxyPort $Port -TimeoutSeconds $StartTimeoutSeconds) { exit 0 }
    $health = Test-Proxy -ProxyPort $Port
    if ($health) {
        Write-Status "aicleaner already running on port $Port (clients=$($health.clients -join ','))" -Level Done
        exit 0
    }
    # Already starting (another starter got there first): wait for that one, never start a second.
    if (Test-Path -LiteralPath $ConfigPath) {
        $starting = Get-StartingProxy -Config $ConfigPath
        if ($starting) {
            Write-Status "aicleaner is already starting (pid $($starting.ProcessId)); waiting for it rather than starting another"
            $startingId = $starting.ProcessId
            $health = Wait-Proxy -ProxyPort $Port -Deadline (Get-Date).AddSeconds($StartTimeoutSeconds) `
                -StillWorth { [bool](Get-Process -Id $startingId -ErrorAction SilentlyContinue) }
            if ($health) {
                Write-Status "aicleaner up (pid $startingId) clients=$($health.clients -join ',')" -Level Done
                exit 0
            }
            if (Get-Process -Id $startingId -ErrorAction SilentlyContinue) {
                Write-Status "aicleaner (pid $startingId) is running but did not answer /healthz within ${StartTimeoutSeconds}s" -Level Error
                exit 1
            }
            Write-Status "the copy that was starting (pid $startingId) has gone; starting aicleaner" -Level Warn
        }
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
        # Each start writes to its own logs, named for when it started (D198, Brad 2026-10-21): a restart -
        # or a second proxy left running beside the new one - can never overwrite or rename the log that
        # explains why the restart was needed. Move-ProxyLog still moves an old fixed-name log aside.
        $startStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $stderrLog = Join-Path $logDir "proxy-stderr-$startStamp.log"
        $stdoutLog = Join-Path $logDir "proxy-stdout-$startStamp.log"
        Write-Status "Starting aicleaner hidden on port $Port (usually a few seconds; waits up to ${StartTimeoutSeconds}s)"
        $proc = Start-Process -FilePath $venvPython -ArgumentList @('-m', 'aicleaner', 'run', '--config', "`"$ConfigPath`"") `
            -WorkingDirectory $root -WindowStyle Hidden -PassThru `
            -RedirectStandardError $stderrLog `
            -RedirectStandardOutput $stdoutLog
        Write-Status "Process $($proc.Id) started; waiting for http://127.0.0.1:$Port/healthz to answer" -Level Detail
        # Poll rather than sleep a fixed time; the venv start-up varies with disk cache state. Waits while the
        # process lives, up to the timeout: a slow cold boot is not a failure.
        $started = Get-Date
        $deadline = $started.AddSeconds($StartTimeoutSeconds)
        $health = Wait-Proxy -ProxyPort $Port -Deadline $deadline -StillWorth { -not $proc.HasExited }
        # Exit code 3: it found the port taken (another copy beat it to it) - wait for that copy instead.
        if (-not $health -and $proc.HasExited -and $proc.ExitCode -eq 3) {
            Write-Status 'another aicleaner holds the port; waiting for it to answer' -Level Detail
            $health = Wait-Proxy -ProxyPort $Port -Deadline $deadline -StillWorth { $true }
        }
        if ($health) {
            $took = ((Get-Date) - $started).TotalSeconds
            Write-Status ("aicleaner up in {0:N1}s (pid {1}) clients={2} forms={3}" -f $took, $proc.Id, ($health.clients -join ','), $health.forms) -Level Done
            if (-not $health.forms) {
                Write-Status 'The mapping is empty: nothing is being tokenised. Check the clients folder in aicleaner-config.json.' -Level Warn
            }
        } elseif ($proc.HasExited) {
            Write-Status "aicleaner exited straight away (code $($proc.ExitCode)); see $stderrLog" -Level Error
            exit 1
        } else {
            Write-Status "aicleaner did not answer /healthz within ${StartTimeoutSeconds}s; see $stderrLog" -Level Error
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
