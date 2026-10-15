<#
.SYNOPSIS
    Installs and wires up aicleaner, the local tokenise/rehydrate proxy for Claude Code.

.DESCRIPTION
    Ends in this state, and is safe to re-run:
      - .venv next to this script with aiohttp installed (Python 3.13 preferred)
      - aicleaner-config.json present (seeded from the example, never overwritten) and the configured
        clients folder created when missing (the example mapping is never seeded)
      - the mapping validated with "python -m aicleaner check"
      - %USERPROFILE%\.claude\settings.json carrying skipWebFetchPreflight, and Claude Code's
        telemetry and error reporting off (DISABLE_TELEMETRY, DISABLE_ERROR_REPORTING; -KeepTelemetry
        leaves them). NOT the base URL: Enable-AiCleaner.ps1 routes Claude Code and VS Code through
        the proxy, after its readiness checks - Deploy-AiCleaner.ps1 offers it at the end of a run
      - optionally, VS Code user settings with VS Code's telemetry off (only wanted on a machine
        that runs Claude Code locally; use -SkipVSCode on a Remote-SSH client)
      - a review of user-scope OpenTelemetry variables that would capture pre-hook content
      - optionally, a logon Scheduled Task that starts the proxy (-RegisterStartupTask)
      - the removal of what an earlier install left behind: the DO_NOT_TRACK and
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC user variables, and the weekly Claude Code update
        task. Those switched Claude Code's auto-update off, and with it the feature-flag evaluation
        Remote Control depends on, for every session and for good. The base URL is the whole
        mechanism now: Claude Code refuses Remote Control while it points at the proxy.

    Run this on the machine where Claude Code actually executes (the VS Code server VM in a
    Remote-SSH setup), not on the machine that only hosts the VS Code UI.

    Every file it touches is backed up first (<file>.bak-yyyyMMdd-HHmmss); the settings changes are
    listed and confirmed before they are written. What it changes outside its own folder is recorded
    in %USERPROFILE%\.aicleaner\install-changes.json so Uninstall-AiCleaner.ps1 can put it back.
    Routing is fail-closed by design: once settings.json points at the proxy, Claude Code cannot reach
    Anthropic unless the proxy is running.

.PARAMETER Port
    Port the proxy listens on. Must match aicleaner-config.json. Default 8787.

.PARAMETER ClaudeSettingsPath
    Claude Code user settings file. Default %USERPROFILE%\.claude\settings.json.

.PARAMETER VSCodeSettingsPath
    VS Code user settings file. Default %APPDATA%\Code\User\settings.json.
    Pass '' to skip the VS Code step.

.PARAMETER SkipVSCodeServer
    Do not touch the VS Code Server settings on this machine. Those are what a VS Code window on
    another PC reads when it reaches this one over Remote-SSH.

.PARAMETER VSCodeServerSettingsPath
    The VS Code Server settings to merge into. Default: found under %USERPROFILE%\.vscode-server.

.PARAMETER SkipVSCode
    Skip the VS Code settings step. Use this on the VS Code server VM (no local VS Code UI) and on a
    Remote-SSH client (a base URL there would also break any local Claude Code session).

.PARAMETER RegisterStartupTask
    Register a Scheduled Task (current user, at logon) that starts the proxy hidden.

.PARAMETER ScreenshotKeys
    The keys you press now for a screenshot, e.g. 'win+shift+s' or 'ctrl+prtsc', or 'none' to leave them
    alone (and skip PowerToys). Asked for when not given: the point is to remap the shortcut you already use.

.PARAMETER SkipPowerToys
    Skip installing PowerToys and the Keyboard Manager remap of the screenshot keys. By default the
    installer offers to remap the screenshot keys to Snipping Tool's text extractor, so what reaches the
    clipboard is text (proxied) rather than an image (base64, not inspectable). PowerToys' own Text
    Extractor is left switched off.

.PARAMETER SkipSnippingTool
    Skip checking/installing Snipping Tool (Microsoft.ScreenSketch 11.2503.29.0+). The remap targets
    its text extractor, so both installers make sure it is present and new enough.
.PARAMETER SkipDocToolsInstall
    Skip checking for the local document tooling (pandoc, Poppler's pdftotext, python-docx, openpyxl,
    python-pptx, PyMuPDF). By default each is checked and installed only if missing, so the file
    conversion rules in CLAUDE.md work out of the box.

.PARAMETER SkipScriptAnalyzer
    Skip installing PSScriptAnalyzer (pinned version, current user, PowerShell Gallery). By default it
    is installed when missing, for pwsh and Windows PowerShell alike, so the agent can check the
    PowerShell it writes with Invoke-ScriptAnalyzer.

.PARAMETER ScriptAnalyzerOnly
    Run only the PSScriptAnalyzer step and stop. Deploy-AiCleaner.ps1 -Action Update uses it: an
    update copies files and runs no installer, and this keeps the step in one place.

.PARAMETER KeepTelemetry
    Leave Claude Code's telemetry and error reporting as they are. By default both are switched off
    in the env block of the Claude Code settings file (DISABLE_TELEMETRY, DISABLE_ERROR_REPORTING):
    aicleaner is the remote interface, so Claude Code's own Remote Control - which needed them on -
    is not wanted.

.PARAMETER TelemetryOnly
    Run only the telemetry step and stop. Deploy-AiCleaner.ps1 -Action Update uses it, as it uses
    -ScriptAnalyzerOnly: an update copies files and runs no installer.

.PARAMETER WithLibreOffice
    Also install LibreOffice (docx -> pdf conversion) when the document tooling step runs.

.PARAMETER WithTesseract
    Also install Tesseract, which the proxy prefers for reading an image in a request. Without it
    the engine built into Windows is used, which needs no install; this is for a machine that
    wants the better reader.

.PARAMETER AcceptDefaults
    Take the default answer to every yes/no question without asking, except those about the topics in
    -AskAbout. A question with no default - an API key to paste - is still asked.

.PARAMETER AskAbout
    With -AcceptDefaults: the topics still asked about (comma-separated or several), e.g.
    claude-md,telemetry-vars. What the deploy passes for the numbers chosen from its list.

.PARAMETER PythonExe
    Explicit Python executable to use for the venv. Default: auto-detect (py -3.13, then python).

.PARAMETER LogPath
    Transcript log path. Defaults to Install-AiCleaner.log next to the script. Pass '' to disable.

.EXAMPLE
    .\Install-AiCleaner-VSCode.ps1 -SkipVSCode -RegisterStartupTask
    On the VS Code server VM: venv, seed config, validate mapping, merge settings, logon task.

.EXAMPLE
    .\Install-AiCleaner-VSCode.ps1 -WhatIf
    Show what would change without touching anything.

.NOTES
    Contact:       see contact-*.json stored alongside this script
    Last-modified: 2026-10-06
    Requires:      Windows PowerShell 5.1 or PowerShell 7+; Python 3.11+ (3.13 preferred)
#>

#Requires -Version 5.1

[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [ValidateRange(1024, 65535)][int]$Port = 8787,
    [string]$ClaudeSettingsPath = (Join-Path $env:USERPROFILE '.claude\settings.json'),
    [AllowEmptyString()][string]$VSCodeSettingsPath = (Join-Path $env:APPDATA 'Code\User\settings.json'),
    [switch]$SkipVSCode,
    [switch]$SkipVSCodeServer,
    [AllowEmptyString()][string]$VSCodeServerSettingsPath = '',
    [switch]$RegisterStartupTask,
    [switch]$SkipPowerToys,
    [string]$ScreenshotKeys = '',
    [switch]$SkipDocToolsInstall,
    [switch]$SkipScriptAnalyzer,
    [switch]$ScriptAnalyzerOnly,
    [switch]$KeepTelemetry,
    [switch]$TelemetryOnly,
    [switch]$WithLibreOffice,
    [switch]$WithTesseract,
    [switch]$SkipSnippingTool,
    [string]$PythonExe = '',
    # Take every Confirm-Action's default without asking (the deploy's list of changes)...
    [switch]$AcceptDefaults,
    # ...except the questions about these topics, which the person chose to change.
    [string[]]$AskAbout = @(),
    [AllowEmptyString()][string]$LogPath = $(
        $base = if ($PSCommandPath) { [IO.Path]::GetFileNameWithoutExtension($PSCommandPath) } else { 'Install-AiCleaner-VSCode' }
        $dir = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }
        Join-Path $dir "$base.log"
    )
)
# The app folder: this script's own, or its parent when it sits in installer\ (feature 102).
$script:AppFolder = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:AcceptDefaultsGiven = [bool]$AcceptDefaults
$script:AskTopics = @($AskAbout | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })

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

# ---------------------------------------------------------------------------
# Helpers (inlined so the script is self-contained)
# ---------------------------------------------------------------------------

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

function Confirm-Action {
    [CmdletBinding()]
    [OutputType([bool])]
    param(
        [Parameter(Mandatory)][string]$Message,
        [ValidateSet('Yes', 'No')][string]$Default = 'No',
        # Which change this question is about, so the deploy can ask about some and not others.
        [string]$Topic = ''
    )
    # -AcceptDefaults (the deploy's list, 2026-10-11): every default is taken and said, except the
    # topics the person chose to change (-AskAbout), which are asked.
    $asked = @(Get-Variable -Name 'AskTopics' -Scope Script -ValueOnly -ErrorAction SilentlyContinue | ForEach-Object { $_ })
    $accepting = Get-Variable -Name 'AcceptDefaultsGiven' -Scope Script -ValueOnly -ErrorAction SilentlyContinue
    if ($accepting -and -not ($Topic -and $asked -contains $Topic)) {
        Write-Status "$Message -> $Default (the default)" -Level Detail
        return ($Default -eq 'Yes')
    }
    $suffix = if ($Default -eq 'Yes') { '[Y/n]' } else { '[y/N]' }
    $response = Read-Host "$Message $suffix"
    if ([string]::IsNullOrWhiteSpace($response)) { return ($Default -eq 'Yes') }
    return $response -match '^(y|yes)$'
}

# What this run has changed, newest last (defect D16).
#
# Every change was already backed up; what was missing was anything holding the list together, so
# a failure half way left the machine between two states with no way back. Decided 2026-09-27:
# roll back, and say what was undone.
#
# The cost is accepted deliberately: steps that had succeeded are undone too. That is the right
# trade, because "half installed but the first four steps worked" is still between two states,
# which is the whole of the complaint.
$script:RunChanges = New-Object System.Collections.Generic.List[object]

function Register-Change {
    <# Record one change so it can be put back. Kind: File, or EnvUser. #>
    param([Parameter(Mandatory)][ValidateSet('File', 'EnvUser')][string]$Kind,
          [Parameter(Mandatory)][string]$Target,
          [AllowNull()][AllowEmptyString()][string]$Backup = $null,
          [AllowNull()][AllowEmptyString()][string]$Previous = $null,
          [bool]$Existed = $true)
    $script:RunChanges.Add([pscustomobject]@{
        Kind = $Kind; Target = $Target; Backup = $Backup; Previous = $Previous; Existed = $Existed
    })
}

function Undo-Run {
    <#
    .SYNOPSIS
        Put back everything this run changed, newest first, and say what was done (D16).
    .DESCRIPTION
        Newest first so a file touched twice ends at the state it started in. Every step is
        wrapped: a rollback that stops half way is the fault it exists to prevent, so one step
        that cannot be undone is reported and the rest still run.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param()
    if ($script:RunChanges.Count -eq 0) {
        Write-Status 'Nothing had been changed yet, so there is nothing to put back' -Level Detail
        return
    }
    Write-Status "Rolling back $($script:RunChanges.Count) change(s) made by this run" -Level Warn
    $failed = 0
    for ($i = $script:RunChanges.Count - 1; $i -ge 0; $i--) {
        $change = $script:RunChanges[$i]
        try {
            switch ($change.Kind) {
                'File' {
                    if ($change.Existed -and $change.Backup -and (Test-Path -LiteralPath $change.Backup)) {
                        Copy-Item -LiteralPath $change.Backup -Destination $change.Target -Force
                        Write-Status "restored $($change.Target)" -Level Detail
                    } elseif (-not $change.Existed -and (Test-Path -LiteralPath $change.Target)) {
                        # It did not exist before this run, so putting it back means removing it.
                        Remove-Item -LiteralPath $change.Target -Force
                        Write-Status "removed $($change.Target) (this run created it)" -Level Detail
                    }
                }
                'EnvUser' {
                    [Environment]::SetEnvironmentVariable($change.Target, $change.Previous, 'User')
                    $what = if ([string]::IsNullOrEmpty($change.Previous)) { 'removed' } else { 'restored' }
                    Write-Status "$what $($change.Target)" -Level Detail
                }
            }
        }
        catch {
            $failed++
            Write-Status "could not put back $($change.Kind) $($change.Target): $($_.Exception.Message)" -Level Warn
        }
    }
    if ($failed -gt 0) {
        Write-Status "$failed change(s) could not be put back - the machine is NOT as it was. The backups are beside the originals." -Level Error
    } else {
        Write-Status 'The machine is as it was before this run.' -Level Done
    }
    $script:RunChanges.Clear()
}

function Backup-File {
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        # Recorded even so: a file this run creates is put back by being removed (D16).
        Register-Change -Kind 'File' -Target $Path -Existed $false
        return $null
    }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = "$Path.bak-$stamp"
    if ($PSCmdlet.ShouldProcess($Path, "Back up to $backup")) {
        Copy-Item -LiteralPath $Path -Destination $backup
        Write-Status "Backed up to $backup" -Level Detail
        Register-Change -Kind 'File' -Target $Path -Backup $backup -Existed $true
    }
    return $backup
}

function Test-JsonComment {
    <#
    .SYNOPSIS
        True when JSON text has a // or /* comment outside string literals (JSONC, as VS Code allows).
        PowerShell 7 parses such files and a rewrite would silently drop the comments.
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param([AllowEmptyString()][string]$Text)
    $inString = $false
    $escaped = $false
    for ($i = 0; $i -lt $Text.Length - 1; $i++) {
        $ch = $Text[$i]
        if ($inString) {
            if ($escaped) { $escaped = $false }
            elseif ($ch -eq [char]'\') { $escaped = $true }
            elseif ($ch -eq [char]'"') { $inString = $false }
            continue
        }
        if ($ch -eq [char]'"') { $inString = $true; continue }
        if ($ch -eq [char]'/' -and ($Text[$i + 1] -eq [char]'/' -or $Text[$i + 1] -eq [char]'*')) { return $true }
    }
    return $false
}

function Read-JsonFile {
    # Returns $null when the file is missing; throws a descriptive error when it exists but is not
    # plain JSON (VS Code settings.json may contain comments, which ConvertFrom-Json rejects).
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $raw = [string](Get-Content -LiteralPath $Path -Raw -Encoding UTF8)
    if ([string]::IsNullOrWhiteSpace($raw)) { return [pscustomobject]@{} }
    if (Test-JsonComment -Text $raw) { throw "$Path contains comments (JSONC); it is left unchanged so they are not lost" }
    return ($raw | ConvertFrom-Json)
}

function Write-JsonFile {
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)]$Object)
    $dir = Split-Path -Parent $Path
    if ($PSCmdlet.ShouldProcess($Path, 'Write JSON')) {
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
        # -Depth 20 keeps nested env/hooks blocks intact; UTF8 without BOM keeps Claude Code happy.
        $json = $Object | ConvertTo-Json -Depth 20
        [IO.File]::WriteAllText($Path, $json, (New-Object System.Text.UTF8Encoding($false)))
    }
}

function Set-JsonProperty {
    # PSCustomObject-friendly upsert (Add-Member fails on existing names).
    param([Parameter(Mandatory)]$Object, [Parameter(Mandatory)][string]$Name, $Value)
    if ($Object.PSObject.Properties[$Name]) { $Object.$Name = $Value }
    else { $Object | Add-Member -MemberType NoteProperty -Name $Name -Value $Value }
}

function Get-ClientsDir {
    <#
    .SYNOPSIS
        The clients folder named by aicleaner-config.json (clients_dir; relative paths are relative
        to the app folder), or <app>\clients when the config does not name one or does not exist yet.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][string]$ConfigPath, [Parameter(Mandatory)][string]$Root)
    $name = 'clients'
    $cfg = Read-JsonFile -Path $ConfigPath
    if ($cfg -and $cfg.PSObject.Properties['clients_dir'] -and $cfg.clients_dir) { $name = [string]$cfg.clients_dir }
    if ([IO.Path]::IsPathRooted($name)) { return $name }
    return (Join-Path $Root $name)
}

function Get-LedgerPath {
    <# install-changes.json: what the installers changed, so Uninstall-AiCleaner.ps1 can put it back. #>
    return (Join-Path $env:USERPROFILE '.aicleaner\install-changes.json')
}

function Save-LedgerPrevious {
    <#
    .SYNOPSIS
        Records the value a setting had before aicleaner first changed it ($null: it did not exist).
        Only the first record counts: a re-run sees aicleaner's own values and must not overwrite it.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Section, [Parameter(Mandatory)][string]$Name, [AllowNull()]$Value)
    $path = Get-LedgerPath
    $doc = $null
    try { $doc = Read-JsonFile -Path $path } catch { Write-Status "$path is unreadable; not recording $Name" -Level Warn; return }
    if ($null -eq $doc) { $doc = [pscustomobject]@{} }
    if (-not $doc.PSObject.Properties[$Section]) { Set-JsonProperty -Object $doc -Name $Section -Value ([pscustomobject]@{}) }
    if ($doc.$Section.PSObject.Properties[$Name]) { return }
    Set-JsonProperty -Object $doc.$Section -Name $Name -Value $Value
    Write-JsonFile -Path $path -Object $doc
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

function Confirm-TaskFromSyncedFolder {
    <#
    .SYNOPSIS
        A scheduled task runs code from this folder. Under OneDrive anyone who can write to the folder
        could change that code, so ask first (default no). True when the task may be registered.
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param([Parameter(Mandatory)][string]$Root)
    $full = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    foreach ($oneDrive in @($env:OneDriveCommercial, $env:OneDriveConsumer, $env:OneDrive)) {
        if (-not $oneDrive) { continue }
        if ($full.StartsWith([IO.Path]::GetFullPath($oneDrive).TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
            Write-Status "$Root is synced by OneDrive: a scheduled task would run code that anyone who can write to that folder can change." -Level Warn
            Write-Status 'Deploy with Deploy-AiCleaner.ps1 instead (code in %USERPROFILE%\.aicleaner\app).' -Level Detail
            return (Confirm-Action -Topic 'proxy-task' -Message 'Register the scheduled task anyway?' -Default 'No')
        }
    }
    return $true
}

function Get-RoutingIssue {
    <# Messages for settings in an env block (or environment scope) that send Claude Code around the proxy. #>
    [CmdletBinding()]
    [OutputType([string[]])]
    param([Parameter(Mandatory)][string]$Where, [Parameter(Mandatory)][hashtable]$Values, [Parameter(Mandatory)][string]$ProxyUrl)
    $issues = @()
    $base = [string]$Values['ANTHROPIC_BASE_URL']
    if ($base -and $base -ne $ProxyUrl) { $issues += "${Where}: ANTHROPIC_BASE_URL = $base" }
    foreach ($switch in 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY') {
        $value = [string]$Values[$switch]
        if ($value -and @('0', 'false') -notcontains $value.Trim().ToLowerInvariant()) { $issues += "${Where}: $switch = $value" }
    }
    return $issues
}

function Test-RoutingOverride {
    <#
    .SYNOPSIS
        Warns about settings outside the user's settings.json that would send Claude Code around the
        proxy: managed settings and user or machine environment variables (review finding 7).
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$ProxyUrl)
    $issues = @()
    $managed = @((Join-Path $env:ProgramFiles 'ClaudeCode\managed-settings.json'))
    if ($env:ProgramData) { $managed += (Join-Path $env:ProgramData 'ClaudeCode\managed-settings.json') }
    foreach ($path in $managed) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        try { $doc = Read-JsonFile -Path $path } catch { $issues += "${path}: unreadable, check it by hand"; continue }
        $values = @{}
        if ($doc -and $doc.PSObject.Properties['env']) { foreach ($p in $doc.env.PSObject.Properties) { $values[$p.Name] = [string]$p.Value } }
        $issues += @(Get-RoutingIssue -Where $path -Values $values -ProxyUrl $ProxyUrl)
    }
    foreach ($scope in 'User', 'Machine') {
        $values = @{}
        foreach ($name in 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY') {
            $values[$name] = [Environment]::GetEnvironmentVariable($name, $scope)
        }
        $issues += @(Get-RoutingIssue -Where "$scope environment" -Values $values -ProxyUrl $ProxyUrl)
    }
    if ($issues.Count -eq 0) { Write-Status 'No managed settings or environment variables route Claude Code around the proxy' -Level Detail }
    foreach ($issue in $issues) { Write-Status "May bypass the proxy: $issue" -Level Warn }
    Write-Status 'A repo can still override routing in .claude\settings.json: run "aicleaner scan <repo>" before the first session.' -Level Detail
}

# The Pythons requirements.lock has wheels for (tools/lock_requirements.py PYTHONS), newest first.
# Any other is passed over: its wheels have no hash in the lock, so pip refuses them - a Store
# Python 3.14 failed the whole install there before 3.14 was locked (2026-10-15). A newer one
# than these means Python 3.13 is installed alongside it.
$script:LockedPythons = @('3.14', '3.13', '3.12', '3.11')

function Find-Python {
    [CmdletBinding()]
    param([string]$Preferred)
    if ($Preferred) {
        if (Test-Path -LiteralPath $Preferred) { return @{ Exe = $Preferred; Args = @() } }
        Write-Status "PythonExe '$Preferred' not found; auto-detecting instead" -Level Warn
    }
    # Native commands writing to stderr raise NativeCommandError under ErrorActionPreference=Stop
    # on PS 5.1, so probing runs with Continue and relies on $LASTEXITCODE instead.
    $savedEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $py = Get-Command py -ErrorAction SilentlyContinue
        if ($py) {
            foreach ($ver in $script:LockedPythons) {
                & $py.Source "-$ver" -c 'import sys' 2>$null
                if ($LASTEXITCODE -eq 0) { return @{ Exe = $py.Source; Args = @("-$ver") } }
            }
        }
        foreach ($name in 'python3', 'python') {
            $cmd = Get-Command $name -ErrorAction SilentlyContinue
            if ($cmd) {
                # No quotes inside the probe: PS 5.1 strips embedded quotes when passing args to native exes.
                $verText = & $cmd.Source -c 'import platform; print(platform.python_version())' 2>$null
                if ($LASTEXITCODE -eq 0 -and $verText -and
                    $script:LockedPythons -contains ('{0}.{1}' -f ([version]$verText).Major, ([version]$verText).Minor)) {
                    return @{ Exe = $cmd.Source; Args = @() }
                }
            }
        }
        return $null
    } finally { $ErrorActionPreference = $savedEap }
}

function Install-Python {
    <#
    .SYNOPSIS
        Offers to install Python with winget, and returns what Find-Python then finds.
    .DESCRIPTION
        Python is the one thing this script cannot do without, and it is a single winget package, so
        it is offered here rather than sending you away to install it and start again.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param()
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) { return $null }
    Write-Status 'No Python 3.11+ found on PATH (py launcher, python3, python).' -Level Warn
    if (-not (Confirm-Action -Topic 'python' -Message 'Install Python 3.13 now with winget?' -Default 'Yes')) { return $null }
    if (-not $PSCmdlet.ShouldProcess('Python.Python.3.13', 'winget install')) { return $null }
    $savedEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $winget.Source install -e --id Python.Python.3.13 --silent --accept-package-agreements --accept-source-agreements --disable-interactivity | Out-Null
    } finally { $ErrorActionPreference = $savedEap }
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    $found = Find-Python
    if ($found) { Write-Status 'Python installed' -Level Done }
    return $found
}

function Get-VSCodeServerSettingsPath {
    <#
    .SYNOPSIS
        Where a VS Code server on this machine keeps the settings its extension host reads.
    .DESCRIPTION
        Reached over Remote-SSH, the extension host runs here, not on the PC holding the window, and
        it does not read %APPDATA%\Code\User\settings.json - that belongs to a VS Code installed
        locally, which a server may not have at all. Machine scope is what Remote-SSH applies to
        this host. Insiders keeps its own folder. The path is returned even when the file is not
        there yet: VS Code Server reads it on the next connection.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([string]$Preferred = '')
    if ($Preferred) { return $Preferred }
    foreach ($folder in '.vscode-server', '.vscode-server-insiders') {
        $root = Join-Path $env:USERPROFILE $folder
        if (Test-Path -LiteralPath $root) { return (Join-Path $root 'data\Machine\settings.json') }
    }
    return ''
}

function Merge-VSCodeSettings {
    <#
    .SYNOPSIS
        Turns one VS Code settings file's telemetry off. Routing is NOT written here.
    .DESCRIPTION
        The same setting whether the file belongs to a local VS Code or to a server on this
        machine; only the file and the ledger section differ, so the uninstaller puts each back to
        what that file held.

        ANTHROPIC_BASE_URL used to be written here as well. It is not, because pointing an editor
        at a filter that has no client and no forms in it breaks the AI service and fills Review
        with everything it sees. Enable-AiCleaner.ps1 writes it, once the filter is ready and a
        request has been proved to survive the whole path.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    [OutputType([bool])]
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Section,
        [Parameter(Mandatory)][string]$Label
    )
    Write-Status "Merging $Label settings at $Path"
    $vs = $null
    try { $vs = Read-JsonFile -Path $Path }
    catch {
        # Comments or trailing commas make settings.json JSONC, which ConvertFrom-Json cannot read.
        # Rewriting it blind would destroy the comments, so this one case stays manual.
        Write-Status "$Label settings.json contains comments or is not plain JSON; set this yourself:" -Level Warn
        Write-Status '"telemetry.telemetryLevel": "off"' -Level Detail
        return $false
    }
    if ($null -eq $vs) { $vs = [pscustomobject]@{} }
    $levelBefore = if ($vs.PSObject.Properties['telemetry.telemetryLevel']) { [string]$vs.'telemetry.telemetryLevel' } else { $null }
    if ($levelBefore -eq 'off') {
        Write-Status "$Label telemetry is already off" -Level Detail
        return $true
    }
    if (-not (Confirm-Action -Topic 'vscode-telemetry' -Message "Set telemetry.telemetryLevel=off in ${Label}?" -Default 'Yes')) {
        return $false
    }
    Backup-File -Path $Path | Out-Null
    Save-LedgerPrevious -Section $Section -Name 'telemetry.telemetryLevel' -Value $levelBefore
    Set-JsonProperty -Object $vs -Name 'telemetry.telemetryLevel' -Value 'off'
    Write-JsonFile -Path $Path -Object $vs
    Write-Status "$Label settings written" -Level Done
    return $true
}

function ConvertTo-KeyCodes {
    <#
    .SYNOPSIS
        "win+shift+s" as the virtual-key codes PowerToys stores, e.g. 91;16;83.
    .DESCRIPTION
        Modifiers, letters, digits, the function keys and Print Screen cover what anybody uses to
        take a screenshot. Anything else is refused by name rather than guessed at.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][string]$Keys)
    $named = @{
        'win' = 91; 'windows' = 91; 'lwin' = 91; 'meta' = 91
        'shift' = 16; 'ctrl' = 162; 'control' = 162; 'alt' = 164
        'prtsc' = 44; 'printscreen' = 44; 'print' = 44; 'prtscn' = 44; 'snapshot' = 44
        'space' = 32; 'tab' = 9; 'esc' = 27; 'escape' = 27
    }
    $codes = New-Object System.Collections.Generic.List[int]
    foreach ($part in ($Keys -split '[+\s,-]+' | Where-Object { $_ })) {
        $token = $part.Trim().ToLowerInvariant()
        if ($named.ContainsKey($token)) { $codes.Add($named[$token]); continue }
        if ($token -match '^[a-z]$') { $codes.Add([int][char]$token.ToUpperInvariant()); continue }
        if ($token -match '^[0-9]$') { $codes.Add(48 + [int]$token); continue }
        if ($token -match '^f([1-9]|1[0-2])$') { $codes.Add(111 + [int]$Matches[1]); continue }
        throw "Not a key this understands: '$part'. Use names like win, ctrl, shift, alt, prtsc, or a letter."
    }
    if ($codes.Count -eq 0) { throw 'No keys given' }
    return ($codes -join ';')
}

function ConvertFrom-KeyCodes {
    <# 91;16;83 back to "Win+Shift+S", for saying what is about to change. #>
    [OutputType([string])]
    param([Parameter(Mandatory)][string]$Codes)
    $names = @{ 91 = 'Win'; 92 = 'Win'; 16 = 'Shift'; 160 = 'Shift'; 161 = 'Shift'
                17 = 'Ctrl'; 162 = 'Ctrl'; 163 = 'Ctrl'; 18 = 'Alt'; 164 = 'Alt'; 165 = 'Alt'
                44 = 'PrtSc'; 32 = 'Space'; 9 = 'Tab'; 27 = 'Esc' }
    $parts = foreach ($code in ($Codes -split ';' | Where-Object { $_ })) {
        $number = [int]$code
        if ($names.ContainsKey($number)) { $names[$number] }
        elseif ($number -ge 65 -and $number -le 90) { [char]$number }
        elseif ($number -ge 48 -and $number -le 57) { [char]$number }
        elseif ($number -ge 112 -and $number -le 123) { 'F' + ($number - 111) }
        else { "VK$number" }
    }
    return ($parts -join '+')
}

function Get-ScreenshotKeys {
    <#
    .SYNOPSIS
        The keys this person presses for a screenshot, as PowerToys key codes, or '' when they want
        them left alone.
    .DESCRIPTION
        Remapping the shortcut somebody already uses is the point: the habit stays, and what it
        does changes. Assuming everyone presses Win+Shift+S would leave most people's habit
        untouched and the remap useless to them. The remap is offered, not imposed: "Leave my
        screenshot key alone" skips it, and PowerToys with it (the user, 2026-10-13). -ScreenshotKeys
        'none' says the same without asking.

        The question is asked with Read-Host rather than the bootstrap's helpers: this script is
        copied to machines on its own, so anything it calls has to be defined in it. Answering
        nothing - including a run with no console behind it - takes the first option.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([string]$Preferred = '')
    if ($Preferred -eq 'none') { return '' }
    if ($Preferred) { return (ConvertTo-KeyCodes -Keys $Preferred) }
    $options = [ordered]@{
        '1' = @{ Codes = '91;16;83'; Label = 'Win+Shift+S (the Snipping Tool shortcut)' }
        '2' = @{ Codes = '44';       Label = 'PrtSc' }
        '3' = @{ Codes = '162;44';   Label = 'Ctrl+PrtSc' }
        '4' = @{ Codes = '164;44';   Label = 'Alt+PrtSc' }
        '5' = @{ Codes = '';         Label = 'Something else (type the keys)' }
        '6' = @{ Codes = 'none';     Label = 'Leave my screenshot key alone (no remap, no PowerToys)' }
    }
    Write-Host ''
    Write-Host 'Which keys do you press now to take a screenshot? They can be remapped to Win+Shift+T,' -ForegroundColor Cyan
    Write-Host "Snipping Tool's text extractor, so what you paste is text the proxy can filter." -ForegroundColor Cyan
    foreach ($number in $options.Keys) {
        $marker = if ($number -eq '1') { '*' } else { ' ' }
        Write-Host ("  {0}{1}. {2}" -f $marker, $number, $options[$number].Label)
    }
    $answer = ([string](Read-Host 'Type your selection and press Enter (just press Enter to choose 1)')).Trim()
    if (-not $answer) { $answer = '1' }
    if (-not $options.Contains($answer)) { throw "Not one of the choices: $answer" }
    $codes = [string]$options[$answer].Codes
    if ($codes -eq 'none') { return '' }
    if ($codes) { return $codes }
    $typed = ([string](Read-Host 'Type them, e.g. ctrl+alt+p')).Trim()
    if (-not $typed) { throw 'No keys given' }
    return (ConvertTo-KeyCodes -Keys $typed)
}

function Install-PowerToysTextExtractor {
    <#
    Installs PowerToys (winget, per user), enables Keyboard Manager and remaps the screenshot keys
    (FromKeys, from Get-ScreenshotKeys) to Win+Shift+T, so the screenshot reflex lands on Snipping
    Tool's text extractor. PowerToys' own Text Extractor is switched OFF so it never claims Win+Shift+T
    ahead of Snipping Tool. Idempotent: existing settings are merged, not replaced. Returns $true when
    ready. (2026-10-13: back to this after aicleaner's own screen capture was withdrawn, D181-D190.)
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$FromKeys)
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    $exe = @("$env:LOCALAPPDATA\PowerToys\PowerToys.exe", "$env:ProgramFiles\PowerToys\PowerToys.exe") | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $exe) {
        if (-not $winget) { Write-Status 'winget not available and PowerToys not installed; install PowerToys by hand and re-run' -Level Warn; return $false }
        Write-Status 'Installing PowerToys (winget, user scope)'
        if ($PSCmdlet.ShouldProcess('Microsoft.PowerToys', 'winget install')) {
            $savedEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
            try { & $winget.Source install -e --id Microsoft.PowerToys --scope user --silent --accept-package-agreements --accept-source-agreements --disable-interactivity | Out-Null }
            finally { $ErrorActionPreference = $savedEap }
            $exe = @("$env:LOCALAPPDATA\PowerToys\PowerToys.exe", "$env:ProgramFiles\PowerToys\PowerToys.exe") | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
            if (-not $exe) { Write-Status "PowerToys install did not produce PowerToys.exe (winget exit $LASTEXITCODE)" -Level Warn; return $false }
            Write-Status "PowerToys installed at $exe" -Level Detail
            Save-LedgerPrevious -Section 'powertoys_install' -Name 'installed_by_aicleaner' -Value $true
        } else { return $false }
    } else {
        Write-Status "PowerToys was already here ($exe); leaving it alone" -Level Detail
        Save-LedgerPrevious -Section 'powertoys_install' -Name 'installed_by_aicleaner' -Value $false
    }

    $settingsDir = Join-Path $env:LOCALAPPDATA 'Microsoft\PowerToys'
    if (-not $PSCmdlet.ShouldProcess($settingsDir, 'Write PowerToys module settings')) { return $true }
    New-Item -ItemType Directory -Force $settingsDir, (Join-Path $settingsDir 'Keyboard Manager') | Out-Null

    # settings.json: Keyboard Manager on, Text Extractor off (Snipping Tool owns Win+Shift+T), run at startup.
    $rootPath = Join-Path $settingsDir 'settings.json'
    $rootCfg = Read-JsonFile -Path $rootPath
    if ($null -eq $rootCfg) { $rootCfg = [pscustomobject]@{} }
    if (-not $rootCfg.PSObject.Properties['enabled']) { Set-JsonProperty -Object $rootCfg -Name 'enabled' -Value ([pscustomobject]@{}) }
    # Previous values, so the uninstaller can put PowerToys back the way it was (paths use '/').
    foreach ($module in 'Keyboard Manager', 'TextExtractor') {
        $before = if ($rootCfg.enabled.PSObject.Properties[$module]) { $rootCfg.enabled.$module } else { $null }
        Save-LedgerPrevious -Section 'powertoys' -Name "enabled/$module" -Value $before
    }
    Save-LedgerPrevious -Section 'powertoys' -Name 'startup' -Value $(if ($rootCfg.PSObject.Properties['startup']) { $rootCfg.startup } else { $null })
    Set-JsonProperty -Object $rootCfg.enabled -Name 'Keyboard Manager' -Value $true
    Set-JsonProperty -Object $rootCfg.enabled -Name 'TextExtractor' -Value $false
    Set-JsonProperty -Object $rootCfg -Name 'startup' -Value $true
    Write-JsonFile -Path $rootPath -Object $rootCfg
    # Keyboard Manager: the screenshot keys -> Win+Shift+T (91;16;84), global, merged into the default profile.
    $fromLabel = ConvertFrom-KeyCodes -Codes $FromKeys
    if ($FromKeys -eq '91;16;84') {
        Write-Status 'That is already the Snipping Tool text extractor shortcut; no remap needed' -Level Done
        return $true
    }
    $kbmSettings = Join-Path $settingsDir 'Keyboard Manager\settings.json'
    if (-not (Test-Path -LiteralPath $kbmSettings)) {
        Write-JsonFile -Path $kbmSettings -Object ([pscustomobject]@{ name = 'Keyboard Manager'; version = '1'; properties = [pscustomobject]@{
            activeConfiguration = [pscustomobject]@{ value = 'default' }; keyboardConfigurations = [pscustomobject]@{ value = @('default') } } })
    }
    $kbmPath = Join-Path $settingsDir 'Keyboard Manager\default.json'
    $kbm = Read-JsonFile -Path $kbmPath
    if ($null -eq $kbm) {
        $kbm = [pscustomobject]@{
            remapKeys = [pscustomobject]@{ inProcess = @() }; remapKeysToText = [pscustomobject]@{ inProcess = @() }
            remapShortcuts = [pscustomobject]@{ global = @(); appSpecific = @() }; remapShortcutsToText = [pscustomobject]@{ global = @(); appSpecific = @() } }
    }
    if (-not $kbm.PSObject.Properties['remapShortcuts']) { Set-JsonProperty -Object $kbm -Name 'remapShortcuts' -Value ([pscustomobject]@{ global = @(); appSpecific = @() }) }
    # Whatever that shortcut did before this: theirs to have back, and without this line the
    # uninstaller has no way of knowing there was anything.
    $previousRemap = @($kbm.remapShortcuts.global | Where-Object { [string]$_.originalKeys -eq $FromKeys } |
        Select-Object -First 1)
    $previousKeys = if ($previousRemap.Count -gt 0 -and $previousRemap[0].PSObject.Properties['newRemapKeys']) { [string]$previousRemap[0].newRemapKeys } else { '' }
    Save-LedgerPrevious -Section 'powertoys_remap' -Name $FromKeys -Value $(if ($previousKeys) { $previousKeys } else { $null })
    $global_ = @($kbm.remapShortcuts.global | Where-Object { [string]$_.originalKeys -ne $FromKeys })
    $global_ += [pscustomobject]@{ originalKeys = $FromKeys; newRemapKeys = '91;16;84' }
    Set-JsonProperty -Object $kbm.remapShortcuts -Name 'global' -Value $global_
    Write-JsonFile -Path $kbmPath -Object $kbm
    # The newer editor shows its own copy of the mappings, editorSettings.json, and went on showing
    # an old mapping after the engine's file had changed (the user, 2026-10-12). Without the copy it
    # builds a new one from the engine's mappings the next time it opens.
    $editorCopy = Join-Path $settingsDir 'Keyboard Manager\editorSettings.json'
    if (Test-Path -LiteralPath $editorCopy) {
        Backup-File -Path $editorCopy | Out-Null
        Remove-Item -LiteralPath $editorCopy -Force
        Write-Status 'Keyboard Manager editor: its old copy of the mappings set aside (backed up beside it); it rebuilds from the live ones' -Level Detail
    }

    # PowerToys reads module settings at start; restart it so the remap is live now.
    Get-Process PowerToys -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Start-Process -FilePath $exe | Out-Null
    Write-Status "PowerToys running: $fromLabel now opens the Snipping Tool text extractor (Win+Shift+T)" -Level Done
    return $true
}

function Install-SnippingTool {
    <#
    Snipping Tool (Microsoft.ScreenSketch), new enough to carry its text engine (11.2503.29.0 or
    later): aicleaner reads the text in every image and scanned PDF that reaches this machine with
    that engine, layout kept (feature 113) - whoever took the picture, wherever (feature 115). Present:
    said. Missing or older: asked (topic snipping-tool, default Yes), then installed or updated
    through winget's Store source, or the provisioned package re-registered when the Store is
    unreachable. Returns $true when a capable Snipping Tool is installed.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param()
    $minVersion = [version]'11.2503.29.0'
    $storeId = '9MZ95KL8MR0L'
    $pkg = Get-AppxPackage -Name 'Microsoft.ScreenSketch' -ErrorAction SilentlyContinue | Sort-Object { [version]$_.Version } -Descending | Select-Object -First 1
    $build = [Environment]::OSVersion.Version.Build
    if ($pkg -and [version]$pkg.Version -ge $minVersion) {
        Write-Status "Snipping Tool $($pkg.Version) present: it reads the text in images here, layout kept" -Level Detail
        if ($build -lt 26100) {
            Write-Status "Windows build $build predates Win+Shift+T text extraction (needs 26100+); a key remapped to it opens the older Snipping Tool" -Level Detail
        }
        return $true
    }
    $why = if ($pkg) { "version $($pkg.Version) is older than $minVersion" } else { 'not installed for this user' }
    Write-Status "Snipping Tool $why"
    $offer = ("Install Snipping Tool from the Microsoft Store? aicleaner reads the text in images and scanned PDFs " +
              "with its engine, on this machine, with the layout kept; without it, Windows' older engine reads them, less accurately")
    if (-not (Confirm-Action -Topic 'snipping-tool' -Message $offer -Default 'Yes')) {
        Write-Status "Snipping Tool not installed: images are read with Windows' older engine" -Level Detail
        return $false
    }
    if (-not $PSCmdlet.ShouldProcess('Microsoft.ScreenSketch', 'winget install (msstore)')) { return $false }
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    $savedEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        if ($winget) {
            & $winget.Source install -e --id $storeId --source msstore --accept-package-agreements --accept-source-agreements --disable-interactivity | Out-Null
            if ($LASTEXITCODE -in -1978335189, -1978335135) {
                Write-Status 'winget: the Store has no newer Snipping Tool than what is installed' -Level Detail
            } elseif ($LASTEXITCODE -ne 0) {
                # Store blocked or offline: fall back to the copy provisioned in the Windows image.
                Write-Status "winget msstore install returned $LASTEXITCODE; re-registering the provisioned package instead" -Level Detail
                Add-AppxPackage -RegisterByFamilyName -MainPackage 'Microsoft.ScreenSketch_8wekyb3d8bbwe' -ErrorAction SilentlyContinue
            }
        } else {
            Add-AppxPackage -RegisterByFamilyName -MainPackage 'Microsoft.ScreenSketch_8wekyb3d8bbwe' -ErrorAction SilentlyContinue
        }
    } finally { $ErrorActionPreference = $savedEap }
    $pkg = Get-AppxPackage -Name 'Microsoft.ScreenSketch' -ErrorAction SilentlyContinue | Sort-Object { [version]$_.Version } -Descending | Select-Object -First 1
    if ($pkg -and [version]$pkg.Version -ge $minVersion) {
        Write-Status "Snipping Tool $($pkg.Version) ready" -Level Done
        return $true
    }
    Write-Status ("Snipping Tool is $(if ($pkg) { $pkg.Version } else { 'missing' }): the Store could not provide it (on Windows Server it " +
                  "may not be offered). Images are read with Windows' older engine; install Snipping Tool from the Microsoft Store app and re-run to change that.") -Level Warn
    return $false
}
function Test-OcrEngine {
    <#
    .SYNOPSIS
        Which engine the proxy will use to read an image, and a warning when there is none.
    .DESCRIPTION
        An image arriving in a request is turned into text on this machine so it can be filtered;
        without an engine it is withheld and replaced by a notice instead. Nothing here installs
        anything: Tesseract is used when somebody has installed it, and otherwise the engine that
        ships with Windows, which costs nothing and is already there.

        The Windows engine has to be driven from Windows PowerShell 5.1. The WinRT projections
        were taken out of PowerShell 7, so the type cannot even be loaded there - the same reason
        Read-ImageText.ps1 refuses PS7.

        First choice (feature 113): Snipping Tool's own engine, from the user's installed copy - read
        by the same reader, with the layout kept. Nothing is installed for it.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param()
    $snipping = Get-AppxPackage -Name 'Microsoft.ScreenSketch' -ErrorAction SilentlyContinue | Sort-Object { [version]$_.Version } -Descending | Select-Object -First 1
    if ($snipping -and (Test-Path -LiteralPath (Join-Path $snipping.InstallLocation 'SnippingTool\oneocr.dll'))) {
        Write-Status "Reading images: Snipping Tool's engine ($($snipping.Version)), the layout kept" -Level Detail
        return 'snipping-tool'
    }
    # Where its installer puts it as well as PATH: the build winget installs leaves PATH alone (2026-10-13).
    if ((Get-Command tesseract -ErrorAction SilentlyContinue) -or ($env:ProgramFiles -and (Test-Path -LiteralPath (Join-Path $env:ProgramFiles 'Tesseract-OCR\tesseract.exe')))) {
        Write-Status 'Reading images: Tesseract with the Windows engine, the better reading of each word' -Level Detail
        return 'tesseract'
    }
    $shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (Test-Path -LiteralPath $shell) {
        # The answer comes back as an exit code, not as text. Passing -Command a string that
        # contains quotes loses them on the way into the child shell, so a probe ending in
        # { "yes" } arrives as { yes } and the child tries to run a command called yes.
        $probe = '[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]; ' +
                 'if ([Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()) { exit 0 }; exit 3'
        & $shell -NoProfile -NonInteractive -Command $probe 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Status 'Reading images: the OCR engine built into Windows' -Level Detail
            return 'windows'
        }
        Write-Status ('No OCR language pack for your display languages, so images in a request will be withheld ' +
                      'rather than read. Settings > Time & language > Language & region > (your language) > ' +
                      'Language options > Optional features: add Optical character recognition.') -Level Warn
        return ''
    }
    Write-Status 'No OCR engine on this machine; images in a request will be withheld rather than read' -Level Warn
    return ''
}

function Install-DocumentToolset {
    <#
    Makes sure the local document tooling the CLAUDE.md rules rely on is present: pandoc and Poppler
    (pdftotext) via winget, and python-docx, openpyxl, python-pptx, PyMuPDF, lxml and defusedxml
    (the last two for the Office schema validator) in the USER's Python
    (the one Claude Code's shell finds; not the aicleaner venv). Each item is checked first and only
    installed when missing. -WithLibreOffice adds LibreOffice for docx -> pdf conversion.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([hashtable]$Python, [switch]$WithLibreOffice, [switch]$WithTesseract)
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    $savedEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        # Poppler carries pdftoppm beside pdftotext, which is what rasterises a scanned PDF so its
        # pages can be read: the scan path needs nothing this does not already install.
        $apps = @(@{ Cmd = 'pandoc'; Id = 'JohnMacFarlane.Pandoc' }, @{ Cmd = 'pdftotext'; Id = 'oschwartz10612.Poppler' })
        if ($WithLibreOffice) { $apps += @{ Cmd = 'soffice'; Id = 'TheDocumentFoundation.LibreOffice' } }
        if ($WithTesseract) { $apps += @{ Cmd = 'tesseract'; Id = 'UB-Mannheim.TesseractOCR' } }
        foreach ($a in $apps) {
            if (Get-Command $a.Cmd -ErrorAction SilentlyContinue) { Write-Status "$($a.Cmd) present" -Level Detail; continue }
            if (-not $winget) { Write-Status "$($a.Cmd) missing and winget unavailable; install $($a.Id) by hand" -Level Warn; continue }
            if ($PSCmdlet.ShouldProcess($a.Id, 'winget install')) {
                & $winget.Source install -e --id $a.Id --silent --accept-package-agreements --accept-source-agreements --disable-interactivity | Out-Null
                Write-Status "$($a.Id) installed (exit $LASTEXITCODE); open a new terminal for PATH changes" -Level Detail
            }
        }
        if ($Python) {
            # Module name -> pip package. Probe with one interpreter call, install only the missing ones.
            # lxml and defusedxml are what the docx/pptx schema validator (validate.py) imports, so a
            # document build can prove each Word or PowerPoint file it writes.
            $mods = [ordered]@{ docx = 'python-docx'; openpyxl = 'openpyxl'; pptx = 'python-pptx'; fitz = 'pymupdf'
                                lxml = 'lxml'; defusedxml = 'defusedxml' }
            $missing = @()
            foreach ($m in $mods.Keys) {
                & $Python.Exe @($Python.Args + @('-c', "import $m")) 2>$null
                if ($LASTEXITCODE -ne 0) { $missing += $mods[$m] }
            }
            if (-not $missing) { Write-Status "Python document libraries present ($(@($mods.Values) -join ', '))" -Level Detail }
            elseif ($PSCmdlet.ShouldProcess(($missing -join ', '), 'pip install --user')) {
                # --no-warn-script-location: pip warns that pymupdf.exe's folder is not on PATH. It
                # never needs to be - the conversions run "python -c import fitz", not the exe - and
                # a WARNING in the middle of an install reads as something to fix.
                # From the hashed lock (H17): exact versions, or nothing. The whole lock, not only the
                # missing ones: --require-hashes needs every package pinned, dependencies included.
                $docsLock = Join-Path (Join-Path $script:AppFolder 'requirements') 'requirements-docs.lock'
                & $Python.Exe @($Python.Args + @('-m', 'pip', 'install', '--user', '--quiet', '--disable-pip-version-check',
                                                 '--no-warn-script-location', '--require-hashes', '--only-binary', ':all:', '-r', $docsLock))
                Write-Status "Installed: $($missing -join ', ') (exit $LASTEXITCODE)" -Level Detail
            }
        }
    } finally { $ErrorActionPreference = $savedEap }
}

# PSScriptAnalyzer, pinned like every other dependency (H17): a newer release is a deliberate change
# to this constant, not something a re-run picks up.
$ScriptAnalyzerVersion = '1.25.0'

function Install-ScriptAnalyzer {
    <#
    Makes sure PSScriptAnalyzer is there for the agent to lint the PowerShell it writes
    (Invoke-ScriptAnalyzer). For the current user, from the PowerShell Gallery, at the pinned
    version, and only when missing. Installed for each edition present: a module put in by Windows
    PowerShell 5.1 lives where pwsh 7 does not look, and Claude Code's PowerShell tool is pwsh.
    A failure is a warning, never a stop - the proxy does not need it.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$Version)
    $install = "if (-not (Get-Module -ListAvailable PSScriptAnalyzer | Where-Object { `$_.Version -eq '$Version' })) { " +
               "if (`$PSVersionTable.PSEdition -eq 'Desktop') { Install-PackageProvider NuGet -MinimumVersion 2.8.5.201 -Scope CurrentUser -Force | Out-Null }; " +
               "Install-Module PSScriptAnalyzer -RequiredVersion '$Version' -Scope CurrentUser -Repository PSGallery -Force -ErrorAction Stop; 'installed' } else { 'present' }"
    $hosts = @()
    foreach ($name in 'pwsh', 'powershell') {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { $hosts += $command.Source }
    }
    if (-not $hosts) { Write-Status 'No PowerShell host found to install PSScriptAnalyzer into' -Level Warn; return }
    foreach ($exe in $hosts) {
        $edition = [IO.Path]::GetFileNameWithoutExtension($exe)
        if (-not $PSCmdlet.ShouldProcess("PSScriptAnalyzer $Version ($edition)", 'Install-Module -Scope CurrentUser')) { continue }
        $savedEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try {
            $outcome = & $exe -NoProfile -NonInteractive -Command $install 2>&1 | Select-Object -Last 1
            if ($LASTEXITCODE -eq 0) { Write-Status "PSScriptAnalyzer $Version $outcome ($edition)" -Level Detail }
            else { Write-Status "PSScriptAnalyzer not installed for $edition`: $outcome" -Level Warn }
        } finally { $ErrorActionPreference = $savedEap }
    }
}

function Set-ClaudeTelemetryOff {
    <#
    .SYNOPSIS
        Switch Claude Code's telemetry and error reporting off in its settings file's env block.
    .DESCRIPTION
        The user, 2026-10-06: "This should be switched off again ... The aicleaner tool IS the remote
        interface - there's no need for a second one through Claude Code Remote Control any more."
        Telemetry was turned back on while Remote Control was in use; it is off again by default.

        The settings file rather than user environment variables: every Claude Code launch reads it
        (VS Code, a terminal, the Session page's own sessions) and a running one follows a change,
        where a variable reaches new terminals only. Two switches, not the umbrella
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: that one also stops Claude Code updating itself,
        which is what an earlier install's opt-out broke.

        Idempotent: values already '1' are left alone. A value someone else set to something other
        than '1' is reported and left alone, as step 6 treats variables it did not set.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)][string]$SettingsPath,
        [switch]$Keep
    )
    Write-Status 'Claude Code telemetry and error reporting'
    if ($Keep) {
        Write-Status 'Left as they are (-KeepTelemetry)' -Level Detail
        return
    }
    $settings = Read-JsonFile -Path $SettingsPath
    if ($null -eq $settings) { $settings = [pscustomobject]@{} }
    if (-not $settings.PSObject.Properties['env']) { Set-JsonProperty -Object $settings -Name 'env' -Value ([pscustomobject]@{}) }
    $wanted = @()
    foreach ($name in 'DISABLE_TELEMETRY', 'DISABLE_ERROR_REPORTING') {
        $current = if ($settings.env.PSObject.Properties[$name]) { [string]$settings.env.$name } else { '' }
        if ($current -eq '1') { Write-Status "$name already 1" -Level Detail }
        elseif ($current) { Write-Status "$name=$current was not set by us; left alone" -Level Warn }
        else { $wanted += $name }
    }
    if ($wanted.Count -eq 0) { return }
    if (-not $PSCmdlet.ShouldProcess($SettingsPath, "Set env $($wanted -join ', ') = 1")) { return }
    Backup-File -Path $SettingsPath | Out-Null
    foreach ($name in $wanted) { Set-JsonProperty -Object $settings.env -Name $name -Value '1' }
    Write-JsonFile -Path $SettingsPath -Object $settings
    Write-Status "Switched off: $($wanted -join ', ') (Claude Code sessions pick it up as they start)" -Level Done
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if ($ScriptAnalyzerOnly) {
    Write-Status 'PSScriptAnalyzer for checking PowerShell scripts'
    Install-ScriptAnalyzer -Version $ScriptAnalyzerVersion
    return
}

if ($TelemetryOnly) {
    # From Deploy's Update, unattended: the setting is the deployment's policy, so no question.
    Set-ClaudeTelemetryOff -SettingsPath $ClaudeSettingsPath -Keep:$KeepTelemetry
    return
}

$transcriptStarted = $false
if ($LogPath) {
    try { Start-Transcript -Path $LogPath -Force | Out-Null; $transcriptStarted = $true }
    catch { Write-Status "Could not start transcript at ${LogPath}: $($_.Exception.Message)" -Level Warn }
}

try {
    $root = $script:AppFolder
    $baseUrl = "http://127.0.0.1:$Port/anthropic"
    Write-Status "aicleaner install from $root"

    # --- 1. Python and venv ------------------------------------------------
    Write-Status 'Locating Python'
    $python = Find-Python -Preferred $PythonExe
    if (-not $python) { $python = Install-Python }
    if (-not $python) {
        Write-Status 'Python 3.11+ is needed and is not here.' -Level Error
        Write-Status 'Install Python 3.13 from python.org or winget (winget install Python.Python.3.13), then re-run.' -Level Detail
        throw 'Python not available'
    }
    $pyDisplay = ($python.Exe + ' ' + ($python.Args -join ' ')).Trim()
    Write-Status "Using $pyDisplay" -Level Detail
    if ($python.Exe -like '*\WindowsApps\*') {
        # Store Python virtualises AppData writes into its package sandbox, which is why aicleaner keeps
        # its venv and secrets under %USERPROFILE%\.aicleaner instead of %LOCALAPPDATA%.
        Write-Status 'Microsoft Store Python detected: AppData writes are sandboxed; using %USERPROFILE%\.aicleaner' -Level Detail
    }

    # Shared venv at %USERPROFILE%\.aicleaner\venv: outside OneDrive so it never syncs between machines.
    # A repo-local .venv from an earlier install is honoured if the shared one does not exist yet.
    $venv = Join-Path $env:USERPROFILE '.aicleaner\venv'
    $venvPython = Join-Path $venv 'Scripts\python.exe'
    $legacyVenv = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython) -and (Test-Path -LiteralPath $legacyVenv)) {
        $venv = Join-Path $root '.venv'; $venvPython = $legacyVenv
        Write-Status "Using existing repo venv at $venv" -Level Detail
    }
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Status "Creating venv at $venv"
        if ($PSCmdlet.ShouldProcess($venv, 'Create virtual environment')) {
            New-Item -ItemType Directory -Force (Split-Path -Parent $venv) | Out-Null
            & $python.Exe @($python.Args + @('-m', 'venv', $venv))
            if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
        }
    } else {
        Write-Status 'venv already present' -Level Detail
    }

    Write-Status 'Installing requirements (pip is idempotent)'
    if ((Test-Path -LiteralPath $venvPython) -and $PSCmdlet.ShouldProcess('requirements.lock', 'pip install')) {
        # Exactly the hashed wheels in the lock, or nothing (H17).
        & $venvPython -m pip install --quiet --disable-pip-version-check --require-hashes --only-binary :all: -r (Join-Path (Join-Path $root 'requirements') 'requirements.lock')
        if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
    }

    # --- 2. Seed config files ----------------------------------------------
    Write-Status 'Seeding configuration files'
    $cfgPath = Join-Path $root 'aicleaner-config.json'
    if (Test-Path -LiteralPath $cfgPath) {
        Write-Status 'aicleaner-config.json exists; leaving it alone' -Level Detail
    } elseif ($PSCmdlet.ShouldProcess($cfgPath, 'Seed from aicleaner-config.example.json')) {
        Copy-Item -LiteralPath (Join-Path (Join-Path $root 'examples') 'aicleaner-config.example.json') -Destination $cfgPath
        Write-Status 'Created aicleaner-config.json from the example' -Level Detail
    }
    # Client files live in the configured clients folder (the bootstrap may point it at a shared
    # folder). An empty folder is a valid, empty mapping, so the example mapping is never seeded: it
    # would collide with or merge demo entries into real clients. A legacy client-map.json is left
    # for the proxy to migrate, so the folder is only created when there is none.
    $clientsDir = Get-ClientsDir -ConfigPath $cfgPath -Root $root
    if (-not (Test-Path -LiteralPath $clientsDir) -and -not (Test-Path -LiteralPath (Join-Path $root 'client-map.json'))) {
        if ($PSCmdlet.ShouldProcess($clientsDir, 'Create the clients folder')) {
            New-Item -ItemType Directory -Force -Path $clientsDir | Out-Null
            Write-Status "Created the clients folder $clientsDir" -Level Detail
        }
    }

    # Keep the configured port and this script's -Port in step; the settings we write must match.
    if (Test-Path -LiteralPath $cfgPath) {
        $cfg = Read-JsonFile -Path $cfgPath
        if ($cfg.PSObject.Properties['port'] -and [int]$cfg.port -ne $Port) {
            Write-Status "aicleaner-config.json has port $($cfg.port) but -Port is $Port" -Level Warn
            if (Confirm-Action -Message "Update aicleaner-config.json to port ${Port}?" -Default 'Yes') {
                Backup-File -Path $cfgPath | Out-Null
                Set-JsonProperty -Object $cfg -Name 'port' -Value $Port
                Write-JsonFile -Path $cfgPath -Object $cfg
            } else {
                $Port = [int]$cfg.port
                $baseUrl = "http://127.0.0.1:$Port/anthropic"
                Write-Status "Continuing with port $Port from aicleaner-config.json" -Level Detail
            }
        }
    }

    # --- 3. Validate the mapping -------------------------------------------
    Write-Status 'Validating the client mapping'
    if ((Test-Path -LiteralPath $venvPython) -and (Test-Path -LiteralPath $cfgPath)) {
        # Python reports problems on stderr; under ErrorActionPreference=Stop that would terminate the
        # script before we can offer to continue, so run the check with Continue and read the exit code.
        $checked = Invoke-AiCleanerCli -Python $venvPython -AppRoot $root -Arguments @('check', '--config', $cfgPath)
        if ($checked -ne 0) {
            Write-Status 'Mapping is invalid; the proxy will refuse to start until it is fixed' -Level Warn
            if (-not (Confirm-Action -Message 'Continue wiring up settings anyway?' -Default 'Yes')) {
                throw 'Stopped at user request after mapping validation failure'
            }
        }
    } else {
        Write-Status 'venv or config files not present yet (WhatIf?); skipping validation' -Level Detail
    }

    # --- 4. Claude Code settings.json --------------------------------------
    Write-Status "Merging Claude Code settings at $ClaudeSettingsPath"
    $settings = Read-JsonFile -Path $ClaudeSettingsPath
    if ($null -eq $settings) { $settings = [pscustomobject]@{} }
    if (-not $settings.PSObject.Properties['env']) { Set-JsonProperty -Object $settings -Name 'env' -Value ([pscustomobject]@{}) }

    # NOT the base URL. Routing is the one step that can stop you working, so it is not a side
    # effect of installing: Enable-AiCleaner.ps1 writes it, after checking the filter is ready and
    # that a request survives the whole path. Installing used to point Claude Code at a proxy that
    # was not running yet and whose mapping was empty, which broke the AI service and filled the
    # review queue with everything it saw.
    #
    # The base URL is also what keeps Claude Code's Remote Control off while the proxy is in the
    # path (since v2.1.196 it refuses Remote Control for a host other than api.anthropic.com), so
    # that protection arrives with activation rather than with the install.
    $skipCurrent = if ($settings.PSObject.Properties['skipWebFetchPreflight']) { [bool]$settings.skipWebFetchPreflight } else { $false }
    $changes = @()
    if (-not $skipCurrent) { $changes += 'skipWebFetchPreflight = true' }

    if ($changes.Count -eq 0) {
        Write-Status 'Claude Code settings already correct' -Level Detail
    } else {
        $changes | ForEach-Object { Write-Status $_ -Level Detail }
        if (Confirm-Action -Topic 'claude-settings' -Message 'Apply these Claude Code settings changes?' -Default 'Yes') {
            Backup-File -Path $ClaudeSettingsPath | Out-Null
            Set-JsonProperty -Object $settings -Name 'skipWebFetchPreflight' -Value $true
            Write-JsonFile -Path $ClaudeSettingsPath -Object $settings
            Write-Status 'Claude Code settings written' -Level Done
        } else {
            Write-Status 'Skipped Claude Code settings' -Level Warn
        }
    }

    # --- 4c. Which MCP servers would carry real names out in a tool call --------
    # Tool-call arguments are rehydrated before the tool runs, which is right for a local tool and
    # wrong for a server that posts them somewhere. Nobody keeps that list current by hand.
    if ((Test-Path -LiteralPath $venvPython) -and (Test-Path -LiteralPath $cfgPath)) {
        $audited = Invoke-AiCleanerCli -Python $venvPython -AppRoot $root -Arguments @('mcp-audit', '--config', $cfgPath)
        if ($audited -ne 0) {
            if (Confirm-Action -Topic 'mcp-tokenise' -Message 'Keep those servers'' tool arguments tokenised (recommended)?' -Default 'Yes') {
                [void](Invoke-AiCleanerCli -Python $venvPython -AppRoot $root `
                    -Arguments @('mcp-audit', '--config', $cfgPath, '--write'))
            } else {
                Write-Status 'Left as it is: those servers will receive real identifiers in tool calls' -Level Warn
            }
        }
    }
    # --- 5. VS Code settings, local and on the server ----------------------
    # Telemetry only. Routing is Enable-AiCleaner.ps1's job, after the readiness checks.
    if ($SkipVSCode -or [string]::IsNullOrWhiteSpace($VSCodeSettingsPath)) {
        Write-Status 'Local VS Code step skipped' -Level Detail
    } else {
        [void](Merge-VSCodeSettings -Path $VSCodeSettingsPath -Section 'vscode' -Label 'VS Code')
    }
    # Reached over Remote-SSH, the extension host runs here and reads the server's own settings.
    if ($SkipVSCodeServer) {
        Write-Status 'VS Code Server step skipped' -Level Detail
    } else {
        $serverSettings = Get-VSCodeServerSettingsPath -Preferred $VSCodeServerSettingsPath
        if (-not $serverSettings) {
            Write-Status 'No VS Code Server on this machine (nothing under ~\.vscode-server)' -Level Detail
        } else {
            [void](Merge-VSCodeSettings -Path $serverSettings -Section 'vscode_server' -Label 'VS Code Server')
        }
    }

    # --- 5b. CLAUDE.md rules block (global user memory) --------------------
    $claudeMd = Join-Path (Split-Path -Parent $ClaudeSettingsPath) 'CLAUDE.md'
    $template = Join-Path $root 'templates\CLAUDE.md'
    Write-Status "Merging aicleaner rules block into $claudeMd"
    if (-not (Test-Path -LiteralPath $template)) {
        Write-Status 'templates\CLAUDE.md missing; skipping rules block' -Level Warn
    } else {
        $block = ([string](Get-Content -LiteralPath $template -Raw -Encoding UTF8)).TrimEnd()
        $existing = if (Test-Path -LiteralPath $claudeMd) { [string](Get-Content -LiteralPath $claudeMd -Raw -Encoding UTF8) } else { '' }
        # The template holds two versions of the file rule: convert everything first, or Read images
        # and PDFs directly because the proxy reads them on this machine. The proxy picks one at every
        # start (rules.py); this keeps the one already written, so an update does not flip it back.
        $readingHere = $existing.Contains('## Files: images and PDFs are read on this machine')
        $dropTags = if ($readingHere) { @('<!-- aicleaner:binaries:start -->', '<!-- aicleaner:binaries:end -->') }
                    else { @('<!-- aicleaner:reading:start -->', '<!-- aicleaner:reading:end -->') }
        $cutFrom = $block.IndexOf($dropTags[0]); $cutTo = $block.IndexOf($dropTags[1])
        if ($cutFrom -ge 0 -and $cutTo -gt $cutFrom) {
            $block = $block.Substring(0, $cutFrom) + $block.Substring($cutTo + $dropTags[1].Length)
        }
        foreach ($tag in '<!-- aicleaner:binaries:start -->', '<!-- aicleaner:binaries:end -->',
                         '<!-- aicleaner:reading:start -->', '<!-- aicleaner:reading:end -->') {
            $block = $block.Replace($tag + "`n", '').Replace($tag + "`r`n", '').Replace($tag, '')
        }
        $block = ($block -replace "(`r?`n){3,}", "`n`n").TrimEnd()
        # Plain index arithmetic rather than regex replacement: the block contains $ and backticks.
        $startTag = '<!-- aicleaner:start -->'; $endTag = '<!-- aicleaner:end -->'
        $i = $existing.IndexOf($startTag); $j = $existing.IndexOf($endTag)
        $merged = if ($i -ge 0 -and $j -gt $i) { $existing.Substring(0, $i) + $block + $existing.Substring($j + $endTag.Length) }
                  elseif ([string]::IsNullOrWhiteSpace($existing)) { $block + "`n" }
                  else { $existing.TrimEnd() + "`n`n" + $block + "`n" }
        if ($merged -eq $existing) {
            Write-Status 'CLAUDE.md rules block already current' -Level Detail
        } elseif (Confirm-Action -Topic 'claude-md' -Message "Write the aicleaner rules block to ${claudeMd}?" -Default 'Yes') {
            Backup-File -Path $claudeMd | Out-Null
            if ($PSCmdlet.ShouldProcess($claudeMd, 'Write CLAUDE.md')) {
                $dir = Split-Path -Parent $claudeMd
                if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
                [IO.File]::WriteAllText($claudeMd, $merged, (New-Object System.Text.UTF8Encoding($false)))
            }
            Write-Status 'CLAUDE.md rules block written' -Level Done
        }
    }

    # --- 6. Opt-out variables an earlier install set: take them back off --------------------
    # These used to be set here to switch Claude Code's own auto-update off. They also switch off
    # the feature-flag evaluation that Remote Control needs, so they disabled it everywhere, for
    # every session, and went on doing so after an uninstall. They stay off; telemetry and error
    # reporting are switched off on their own in the settings file instead (6a), which leaves
    # auto-update alone.
    Write-Status 'Removing opt-out variables an earlier install set (DO_NOT_TRACK, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC)'
    foreach ($name in 'DO_NOT_TRACK', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC') {
        $cur = [Environment]::GetEnvironmentVariable($name, 'User')
        if ([string]::IsNullOrEmpty($cur)) { Write-Status "$name is not set" -Level Detail }
        elseif ($cur -ne '1') { Write-Status "$name=$cur was not set by us; left alone" -Level Detail }
        elseif ($PSCmdlet.ShouldProcess($name, 'Remove user environment variable')) {
            # Recorded so a failed run puts it back (D16).
            Register-Change -Kind 'EnvUser' -Target $name -Previous $cur
            [Environment]::SetEnvironmentVariable($name, $null, 'User')
            Write-Status "$name removed (new terminals only; a running VS Code server keeps the old value)" -Level Detail
        }
    }

    # --- 6a. Claude Code telemetry and error reporting off (the user, 2026-10-06) -------------
    Set-ClaudeTelemetryOff -SettingsPath $ClaudeSettingsPath -Keep:$KeepTelemetry

    # Desktop Commander MCP server, if present on this machine, has its own telemetry switch.
    $dcConfig = Join-Path $env:USERPROFILE '.claude-server-commander\config.json'
    if (Test-Path -LiteralPath $dcConfig) {
        try {
            $dc = Read-JsonFile -Path $dcConfig
            if ($dc.PSObject.Properties['telemetryEnabled'] -and -not [bool]$dc.telemetryEnabled) {
                Write-Status 'Desktop Commander telemetry already off' -Level Detail
            } elseif (Confirm-Action -Topic 'desktop-commander' -Message 'Turn Desktop Commander telemetry off?' -Default 'Yes') {
                Backup-File -Path $dcConfig | Out-Null
                $telemetryBefore = if ($dc.PSObject.Properties['telemetryEnabled']) { $dc.telemetryEnabled } else { $null }
                Save-LedgerPrevious -Section 'desktop_commander' -Name 'telemetryEnabled' -Value $telemetryBefore
                Set-JsonProperty -Object $dc -Name 'telemetryEnabled' -Value $false
                Write-JsonFile -Path $dcConfig -Object $dc
                Write-Status 'Desktop Commander telemetry off (restart the MCP server)' -Level Done
            }
        } catch { Write-Status "Could not read $dcConfig ($($_.Exception.Message)); set telemetryEnabled=false by hand" -Level Warn }
    }

    # --- 6b. OpenTelemetry exporters that capture pre-hook content --------------
    Write-Status 'Reviewing user-scope telemetry variables'
    $telemetryVars = 'CLAUDE_CODE_ENABLE_TELEMETRY', 'OTEL_EXPORTER_OTLP_ENDPOINT', 'OTEL_LOGS_EXPORTER',
                     'OTEL_METRICS_EXPORTER', 'OTEL_TRACES_EXPORTER', 'OTEL_EXPORTER_OTLP_HEADERS'
    $setVars = @()
    foreach ($v in $telemetryVars) {
        $val = [Environment]::GetEnvironmentVariable($v, 'User')
        # Name only: a value can be an auth header, and this output also goes to the transcript.
        if ($val) { $setVars += $v; Write-Status "$v is set" -Level Detail }
    }
    if ($setVars.Count -eq 0) {
        Write-Status 'None set; nothing exports content around the proxy' -Level Detail
    } else {
        Write-Status 'OpenTelemetry exporters record tool output before hooks run and outside the proxy path' -Level Warn
        if (Confirm-Action -Topic 'telemetry-vars' -Message 'Remove these user-scope variables?' -Default 'No') {
            foreach ($v in $setVars) {
                if ($PSCmdlet.ShouldProcess($v, 'Remove user environment variable')) {
                    # Names only: the values can be credentials, so they are not kept for a restore.
                    Save-LedgerPrevious -Section 'otel_removed' -Name $v -Value $true
                    [Environment]::SetEnvironmentVariable($v, $null, 'User')
                }
            }
            Write-Status 'Removed; open a new terminal for it to take effect' -Level Done
        }
    }

    # --- 6c. Anything else that would route Claude Code around the proxy -----
    Write-Status 'Checking for settings that bypass the proxy'
    Test-RoutingOverride -ProxyUrl $baseUrl

    # --- 7. Optional startup task ------------------------------------------
    if ($RegisterStartupTask -and (Confirm-TaskFromSyncedFolder -Root $root)) {
        $taskName = 'aicleaner-proxy'
        Write-Status "Registering logon task '$taskName'"
        $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        $startScript = Join-Path $PSScriptRoot 'Start-AiCleaner.ps1'
        $action = Get-HiddenTaskAction -Script $startScript -Arguments '-Detach' -WorkingDirectory $root
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $taskSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
        if ($existingTask) {
            Write-Status 'Task exists; updating its action and trigger' -Level Detail
            if ($PSCmdlet.ShouldProcess($taskName, 'Update scheduled task')) {
                Set-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $taskSettings | Out-Null
            }
        } elseif ($PSCmdlet.ShouldProcess($taskName, 'Register scheduled task')) {
            Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $taskSettings `
                -Description 'aicleaner: local tokenise/rehydrate proxy for Claude Code' | Out-Null
        }
        if ((Confirm-Action -Topic 'proxy-task' -Message 'Start the task now?' -Default 'Yes') -and $PSCmdlet.ShouldProcess($taskName, 'Start scheduled task')) {
            Start-ScheduledTask -TaskName $taskName
            Start-Sleep -Seconds 3
        }
    }

    # --- 7a. The weekly update task an earlier install registered: remove it -------------------
    # It existed only to stand in for Claude Code's own auto-update, which the opt-out variables
    # above used to switch off. Nothing switches it off now, so Claude Code keeps itself current
    # and a second updater would only fight it. Update-ClaudeCode.ps1 stays for updating by hand.
    $updateTask = 'aicleaner-claude-update'
    if (Get-ScheduledTask -TaskName $updateTask -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($updateTask, 'Remove scheduled task')) {
            Unregister-ScheduledTask -TaskName $updateTask -Confirm:$false
            Write-Status "Removed task $updateTask; Claude Code updates itself again" -Level Detail
        }
    } else {
        Write-Status 'No Claude Code update task to remove; Claude Code updates itself' -Level Detail
    }

    # --- 7b. Screenshot chord to text: Snipping Tool + PowerToys remap ------------
    if ($SkipSnippingTool) { Write-Status 'Snipping Tool step skipped' -Level Detail }
    else {
        Write-Status 'Snipping Tool, which reads the text in images on this machine'
        Install-SnippingTool | Out-Null
    }
    if ($SkipPowerToys) { Write-Status 'PowerToys step skipped' -Level Detail }
    else {
        # Offered, not imposed (the user, 2026-10-13): "leave my screenshot key alone" skips PowerToys too.
        $fromKeys = Get-ScreenshotKeys -Preferred $ScreenshotKeys
        if (-not $fromKeys) { Write-Status 'Screenshot key left alone: no remap, and PowerToys not installed for it' -Level Detail }
        else {
            Write-Status 'Screenshot shortcut: remapping it to the Snipping Tool text extractor'
            Install-PowerToysTextExtractor -FromKeys $fromKeys | Out-Null
        }
    }

    # --- 7c. Document tooling (pandoc, pdftotext, python-docx, openpyxl, python-pptx, PyMuPDF) ---
    if ($SkipDocToolsInstall) { Write-Status 'Document tooling step skipped' -Level Detail }
    else {
        Write-Status 'Document tooling for local file conversion'
        Install-DocumentToolset -Python $python -WithLibreOffice:$WithLibreOffice -WithTesseract:$WithTesseract
        # What the proxy itself can read, which is a different list from what the agent can open.
        Test-OcrEngine | Out-Null
    }

    # --- 7d. PSScriptAnalyzer, so the agent can lint the PowerShell it writes -----------------
    if ($SkipScriptAnalyzer) { Write-Status 'PSScriptAnalyzer step skipped' -Level Detail }
    else {
        Write-Status 'PSScriptAnalyzer for checking PowerShell scripts'
        Install-ScriptAnalyzer -Version $ScriptAnalyzerVersion
    }

    # --- 8. Health check if the proxy is up --------------------------------
    Write-Status "Checking http://127.0.0.1:$Port/healthz"
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 3
        Write-Status "Proxy is up: clients=$($health.clients -join ',') forms=$($health.forms)" -Level Done
    } catch {
        Write-Status 'Proxy not running yet. Start it with .\Start-AiCleaner.ps1 (or -RegisterStartupTask).' -Level Detail
    }

    Write-Status 'Install complete - but nothing is going through the filter yet' -Level Done
    Write-Status 'Next: open the UI, add a client with the phrases that name them, scan a repo of theirs, and clear the obvious noise out of Review.' -Level Detail
    Write-Status 'Then run .\Enable-AiCleaner.ps1 to switch Claude Code and VS Code over. It checks the filter is ready first, and a running session picks the change up without a restart.' -Level Detail
}
catch {
    Write-Status "Install failed: $($_.Exception.Message)" -Level Error
    # Put this run's changes back rather than leaving the machine between two states (D16).
    # After the message, so the reason for the failure is the first thing read and the rollback
    # is the second - not the other way round.
    try { Undo-Run }
    catch { Write-Status "Rollback itself failed: $($_.Exception.Message)" -Level Error }
    Write-Error $_ -ErrorAction Continue
    exit 1
}
finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
