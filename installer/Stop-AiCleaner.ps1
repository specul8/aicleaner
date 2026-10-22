<#
.SYNOPSIS
    Stops the aicleaner proxy on this machine.

.DESCRIPTION
    Finds the process listening on the proxy port, confirms it is the aicleaner Python process, and
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
    Last-modified: 2026-10-19
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
    [string]$SessionsRoot = (Join-Path $env:USERPROFILE '.aicleaner\sessions')
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

function Remove-CopilotProxy {
    <#
    .SYNOPSIS
        VS Code's proxy setting taken out while aicleaner is stopped (Copilot release 6, Brad 2026-10-19).
    .DESCRIPTION
        Pointed at a proxy that is not running, VS Code reaches nothing - marketplace, Settings Sync,
        Copilot. So Stop takes it out (`aicleaner copilot-off --keep-switch`: the Copilot switch stays
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
    if (-not $PSCmdlet.ShouldProcess('VS Code settings', 'Take out the proxy setting while aicleaner is stopped')) { return }
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
        Stops aicleaner proxies started from this install that no longer hold the port (D198).
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
        if ($PSCmdlet.ShouldProcess("pid $($process.ProcessId)", 'Stop an aicleaner proxy that no longer holds the port')) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    if ($stale.Count) { Write-Host "==> $($stale.Count) aicleaner process(es) no longer holding port $ProxyPort stopped" -ForegroundColor Green }
}

$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $conn) {
    Stop-UnlistenedProxy -ProxyPort $Port
    Write-Host "==> Nothing is listening on port $Port; aicleaner is not running" -ForegroundColor Green; Remove-CopilotProxy; exit 0
}
Stop-UnlistenedProxy -ProxyPort $Port -KeepPid $conn.OwningProcess
$proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
if (-not $proc) { Write-Host "==> Port $Port is held by pid $($conn.OwningProcess), which is no longer running" -ForegroundColor Yellow; Remove-CopilotProxy; exit 0 }
if ($proc.ProcessName -notlike 'python*') {
    Write-Warning "Port $Port is held by $($proc.ProcessName) (pid $($proc.Id)), not aicleaner; not stopping it"
    exit 1
}
if ($PSCmdlet.ShouldProcess("pid $($proc.Id) ($($proc.ProcessName))", 'Stop aicleaner proxy')) {
    Stop-Process -Id $proc.Id -Force
    Start-Sleep -Milliseconds 800
    Write-Host "==> aicleaner stopped (was pid $($proc.Id))" -ForegroundColor Green
    Remove-CopilotProxy
}
