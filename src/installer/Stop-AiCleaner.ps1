<#
.SYNOPSIS
    Stops the AI Cleaner proxy on this machine.

.DESCRIPTION
    Finds the process listening on the proxy port, confirms it is the AI Cleaner Python process, and
    stops it. Idempotent: if nothing is listening, says so and exits 0. Use Start-AiCleaner.ps1 to
    start it again; running both is a restart (for example after editing aicleaner-config.json or storing
    an API key, since credentials and listen settings are read at start).

.PARAMETER Port
    Proxy port. Default 8787.

.EXAMPLE
    .\Stop-AiCleaner.ps1; .\Start-AiCleaner.ps1 -Detach
    Restart the proxy.

.NOTES
    Contact:       see contact-*.json stored alongside this script
    Last-modified: 2026-10-24
    Requires:      Windows PowerShell 5.1 or PowerShell 7+
#>

#Requires -Version 5.1

[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateRange(1024, 65535)][int]$Port = 8787,
    # Sessions live in their own brokers and survive the proxy stopping (DECISIONS 49) - that is what
    # lets a deploy leave them running. This stops them too, and the Claude Code each one holds.
    [switch]$StopSessions,
    # Only brokers keeping their session under this folder are stopped - the install's own. A test
    # run points it at its temp folder, so running the suite never ends a real session.
    [string]$SessionsRoot = (Join-Path $env:USERPROFILE '.aicleaner\sessions'),
    # The app to stop (AI Cleaner 1.0): an update runs the release's own Stop from where it was fetched, not from the
    # app, and says which app it is. Left out, the app is this script's own folder, as it has always been.
    [string]$AppFolder = ''
)
# The app folder: -AppFolder, else this script's own, or its parent when it sits in installer\ (feature 102).
$script:AppFolder = if ($AppFolder) { $AppFolder } elseif (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }

Set-StrictMode -Version Latest

function Get-AiCleanerApiHeader {
    <#
    .SYNOPSIS
        The header that carries this install's API key (H2, AI Cleaner 1.1): a change through the API from this machine
        is refused without it. Read from the DPAPI-protected file AI Cleaner makes at its first start; none yet (an
        older AI Cleaner) gives no header, as before.
    #>
    [OutputType([hashtable])]
    param([string]$StateFolder = (Join-Path $env:USERPROFILE '.aicleaner'))
    # Given outright (a test, or a script started by AI Cleaner itself): whoever sets this already has the key.
    if ($env:AICLEANER_API_KEY) { return @{ 'X-AI-Cleaner-Key' = [string]$env:AICLEANER_API_KEY } }
    $file = Join-Path $StateFolder 'secrets\api-key.dpapi'
    if (-not (Test-Path -LiteralPath $file)) { return @{} }
    try {
        Add-Type -AssemblyName System.Security
        $bytes = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($file), $null, 'CurrentUser')
        try { return @{ 'X-AI-Cleaner-Key' = [Text.Encoding]::UTF8.GetString($bytes).Trim() } }
        finally { [Array]::Clear($bytes, 0, $bytes.Length) }
    } catch {
        Write-Verbose "The API key could not be read: $($_.Exception.Message)"
        return @{}
    }
}
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

function Remove-CopilotProxy {
    <#
    .SYNOPSIS
        VS Code's proxy setting taken out while AI Cleaner is stopped (Copilot release 6, Brad 2026-10-19).
    .DESCRIPTION
        Pointed at a proxy that is not running, VS Code reaches nothing - marketplace, Settings Sync,
        Copilot. So Stop takes it out (`AI Cleaner copilot-off --keep-switch`: the Copilot switch stays
        on) and the next start puts it back. Also run when nothing was listening, so a Stop after a
        crash gives VS Code its connection back. Nothing to do unless the switch is on.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param()
    $configFile = Join-Path $script:AppFolder 'aicleaner-config.json'
    if (-not (Test-Path -LiteralPath $configFile)) { return }
    try { $config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return }
    if (-not $config.PSObject.Properties['copilot'] -or -not $config.copilot.enabled) { return }
    $venvPython = Join-Path $env:USERPROFILE '.aicleaner\venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Warning "VS Code still points at aicleaner: the Python environment ($venvPython) is missing"
        return
    }
    if (-not $PSCmdlet.ShouldProcess('VS Code settings', 'Take out the proxy setting while AI Cleaner is stopped')) { return }
    $savedEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    Push-Location -LiteralPath $script:AppFolder
    try {
        & $venvPython -m aicleaner copilot-off --keep-switch --config $configFile 2>&1 | ForEach-Object { Write-Host "    Copilot: $_" }
    } finally {
        Pop-Location
        $ErrorActionPreference = $savedEap
    }
}

Write-ContactAttribution

# A deliberate stop (route B's watchdog, Watch-AiCleaner.ps1, leaves AI Cleaner stopped while this is here).
# Start-AiCleaner removes it. Claude Desktop's chat stays failing closed meanwhile: Disable is the way to send it
# out unfiltered on purpose.
# Feature 107: the start-up task's launcher loop (Run-AiCleanerService.ps1) reads the same marker - it starts AI Cleaner
# again after it stops unless the marker is there, so writing it first makes this a stop, not a restart.
$stoppedMarker = Join-Path $env:USERPROFILE '.aicleaner\stopped-on-purpose'
if ($PSCmdlet.ShouldProcess($stoppedMarker, 'Mark AI Cleaner as stopped on purpose')) {
    try { Set-Content -LiteralPath $stoppedMarker -Value (Get-Date -Format o) -ErrorAction Stop }
    catch { Write-Verbose "could not mark the stop: $($_.Exception.Message)" }
}

if ($StopSessions) {
    # Only a Python started as `-m aicleaner.broker`: a looser match once stopped a shell whose
    # command line merely mentioned the name.
    $brokers = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like 'python*' -and $_.CommandLine -and $_.CommandLine -match '\s-m\s+aicleaner\.broker(\s|$)' -and
                       $_.CommandLine.IndexOf($SessionsRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0 })
    foreach ($broker in $brokers) {
        if ($PSCmdlet.ShouldProcess("pid $($broker.ProcessId)", 'Stop a session broker and its Claude Code')) {
            # Its Claude Code first: a broker stopped on its own leaves the child reading a closed pipe.
            Get-CimInstance Win32_Process -Filter "ParentProcessId=$($broker.ProcessId)" -ErrorAction SilentlyContinue |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Stop-Process -Id $broker.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Host "==> $($brokers.Count) session broker(s) stopped" -ForegroundColor Green
}

function Stop-UnlistenedProxy {
    <#
    .SYNOPSIS
        Stops AI Cleaner proxies started from this install that no longer hold the port (D198).
    .DESCRIPTION
        A proxy whose listener asyncio closed keeps running with nothing on the port, so finding it by the
        port alone missed it - and a restart then started a second proxy beside it (2026-10-21: three ran
        at once, sharing one log). Matched by its command line: `-m aicleaner run` with this install's
        config. The venv launcher in front of each goes with it.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([int]$ProxyPort = 8787, [int]$KeepPid = 0)
    $config = Join-Path $script:AppFolder 'aicleaner-config.json'
    # The proxy that does hold the port, and the venv launcher in front of it, are left to the stop below.
    $keep = @($KeepPid)
    if ($KeepPid) {
        $holder = Get-CimInstance Win32_Process -Filter "ProcessId=$KeepPid" -ErrorAction SilentlyContinue
        if ($holder) { $keep += $holder.ParentProcessId }
    }
    $stale = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like 'python*' -and $_.CommandLine -and $_.CommandLine -match '\s-m\s+aicleaner\s+run(\s|$)' -and
                       $_.CommandLine.IndexOf($config, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
                       $keep -notcontains $_.ProcessId })
    foreach ($process in $stale) {
        if ($PSCmdlet.ShouldProcess("pid $($process.ProcessId)", 'Stop an AI Cleaner proxy that no longer holds the port')) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    if ($stale.Count) { Write-Host "==> $($stale.Count) AI Cleaner process(es) no longer holding port $ProxyPort stopped" -ForegroundColor Green }
}

function Wait-PortFree {
    <# .SYNOPSIS True once nothing listens on the port, false if something still does after the given seconds. #>
    param([int]$ProxyPort, [int]$Seconds = 20)
    $until = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $until -and (Get-NetTCPConnection -LocalPort $ProxyPort -State Listen -ErrorAction SilentlyContinue)) {
        Start-Sleep -Milliseconds 500
    }
    return -not (Get-NetTCPConnection -LocalPort $ProxyPort -State Listen -ErrorAction SilentlyContinue)
}

function Stop-HiddenListener {
    <#
    .SYNOPSIS
        Stops AI Cleaner on a port whose listener Windows names no owner for; returns the exit code (0 or 1).
    .DESCRIPTION
        Windows can name pid 0 as the owner of a listener it will not show this session (2026-10-29, after a boot
        start: the owner read as Idle, the real proxy was taken for a stale one and stopped, and the stop then failed
        on "Idle"). So the listener is asked who it is: AI Cleaner answers /healthz with its forms. Started with the
        computer it is asked to stop; otherwise it is found by its command line, as a stale proxy is.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([int]$ProxyPort = 8787)
    $health = $null
    try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ProxyPort/healthz" -TimeoutSec 5 -ErrorAction Stop } catch { $health = $null }
    if (-not ($health -and $health.PSObject.Properties['ok'] -and $health.PSObject.Properties['forms'])) {
        Write-Warning "Port $ProxyPort is held by a process this session cannot see, and it is not AI Cleaner; not stopping it"
        return 1
    }
    if (-not $PSCmdlet.ShouldProcess("AI Cleaner on port $ProxyPort (owner not visible)", 'Stop AI Cleaner proxy')) { return 0 }
    if ($health.PSObject.Properties['service'] -and [string]$health.service) {
        try {
            Invoke-RestMethod -Headers (Get-AiCleanerApiHeader) -Method Post -Uri "http://127.0.0.1:$ProxyPort/api/restart" -ContentType 'application/json' `
                -Body '{}' -TimeoutSec 10 -ErrorAction Stop | Out-Null
        } catch {
            Write-Warning "AI Cleaner did not take the request to stop: $($_.Exception.Message)"
            return 1
        }
    } else {
        Stop-UnlistenedProxy -ProxyPort $ProxyPort -Confirm:$false
    }
    if (-not (Wait-PortFree -ProxyPort $ProxyPort)) {
        Write-Warning "AI Cleaner was asked to stop but still holds port $ProxyPort"
        return 1
    }
    Write-Host "==> AI Cleaner stopped (its process was not visible to this session)" -ForegroundColor Green
    Remove-CopilotProxy
    return 0
}

function Exit-StopFailed {
    <#
    .SYNOPSIS
        Ends a stop that did not happen with exit code 1, taking stopped-on-purpose away again (D220).
    .DESCRIPTION
        The marker is written before anything is stopped, so the start-up task's loop does not start AI Cleaner again
        under the stop. Left behind by a stop that failed, it kept AI Cleaner down: the loop saw the proxy end with the
        marker there and ended too, and the watchdog would not start it (2026-10-29, twice in an afternoon). A stop
        that did not happen is not a stop on purpose, so the marker goes and the watchdog may bring AI Cleaner back.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Marker)
    if ($PSCmdlet.ShouldProcess($Marker, 'Take the stopped-on-purpose marker away again')) {
        Remove-Item -LiteralPath $Marker -Force -ErrorAction SilentlyContinue
    }
    exit 1
}

$listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if (-not $listeners) {
    Stop-UnlistenedProxy -ProxyPort $Port
    Write-Host "==> Nothing is listening on port $Port; AI Cleaner is not running" -ForegroundColor Green; Remove-CopilotProxy; exit 0
}
# Pid 0 and 4 are never aicleaner: with only those named, the owner is hidden from this session.
$conn = $listeners | Where-Object { $_.OwningProcess -gt 4 } | Select-Object -First 1
if (-not $conn) {
    if ((Stop-HiddenListener -ProxyPort $Port) -ne 0) { Exit-StopFailed -Marker $stoppedMarker }
    exit 0
}
Stop-UnlistenedProxy -ProxyPort $Port -KeepPid $conn.OwningProcess
$proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
if (-not $proc) { Write-Host "==> Port $Port is held by pid $($conn.OwningProcess), which is no longer running" -ForegroundColor Yellow; Remove-CopilotProxy; exit 0 }
if ($proc.ProcessName -notlike 'python*') {
    Write-Warning "Port $Port is held by $($proc.ProcessName) (pid $($proc.Id)), not AI Cleaner; not stopping it"
    Exit-StopFailed -Marker $stoppedMarker
}
# Feature 107: started with the computer, AI Cleaner runs in another Windows logon session, and a process there cannot
# be ended from this one ("Access is denied" - the 2026-10-25 live test). It is asked to end itself instead, through
# its own restart (which in this mode is the process leaving); the launcher sees stopped-on-purpose and stops too.
$health = $null
try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 5 -ErrorAction Stop } catch { $health = $null }
if ($health -and $health.PSObject.Properties['service'] -and [string]$health.service) {
    if ($PSCmdlet.ShouldProcess("pid $($proc.Id) (started with the computer)", 'Ask AI Cleaner to stop')) {
        try {
            Invoke-RestMethod -Headers (Get-AiCleanerApiHeader) -Method Post -Uri "http://127.0.0.1:$Port/api/restart" -ContentType 'application/json' `
                -Body '{}' -TimeoutSec 10 -ErrorAction Stop | Out-Null
        } catch {
            Write-Warning "AI Cleaner did not take the request to stop: $($_.Exception.Message)"
            Exit-StopFailed -Marker $stoppedMarker
        }
        if (-not (Wait-PortFree -ProxyPort $Port)) {
            Write-Warning "AI Cleaner was asked to stop but still holds port $Port"
            Exit-StopFailed -Marker $stoppedMarker
        }
        Write-Host "==> AI Cleaner stopped (was pid $($proc.Id), started with the computer)" -ForegroundColor Green
        Remove-CopilotProxy
    }
    exit 0
}
if ($PSCmdlet.ShouldProcess("pid $($proc.Id) ($($proc.ProcessName))", 'Stop AI Cleaner proxy')) {
    try { Stop-Process -Id $proc.Id -Force -ErrorAction Stop }
    catch { Write-Warning "AI Cleaner (pid $($proc.Id)) could not be stopped: $($_.Exception.Message)"; Exit-StopFailed -Marker $stoppedMarker }
    Start-Sleep -Milliseconds 800
    Write-Host "==> AI Cleaner stopped (was pid $($proc.Id))" -ForegroundColor Green
    Remove-CopilotProxy
}
