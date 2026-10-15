<#
.SYNOPSIS
    Deploys aicleaner for Claude Desktop (Chat, Cowork and Code) on this machine.

.DESCRIPTION
    Points Claude Desktop's third-party inference at the local aicleaner proxy so every model call from
    the app carries tokens instead of client identifiers. Ends in this state, and is safe to re-run:
      - shared venv at %USERPROFILE%\.aicleaner\venv (outside any synced folder), aiohttp installed
      - aicleaner-config.json present next to this script (seeded from the example, never overwritten)
        and the configured clients folder created when missing; an existing mapping from the VS Code
        deployment is reused as-is
      - a locally generated gateway token at %USERPROFILE%\.aicleaner\secrets\anthropic.gateway.token
      - your Anthropic Console API key stored DPAPI-encrypted at
        %USERPROFILE%\.aicleaner\secrets\anthropic.key.dpapi (the proxy swaps it in; the app never sees it)
      - Claude Desktop policy under HKCU\SOFTWARE\Policies\Claude: gateway provider, base URL, the gateway
        token as its key, organisation instructions, skipWebFetchPreflight, advanced file analysis
      - the proxy running (started, or restarted so it loads the credentials) and a live gateway test

    Coexistence: if the VS Code deployment (Install-AiCleaner-VSCode.ps1) is already on this machine, its
    proxy, config and mapping are reused; the proxy is restarted once so the credential swap is active.
    Deploying this first and Install-AiCleaner-VSCode.ps1 later also works: both share the same files.

    Compromises to know about: Claude Desktop in gateway mode uses a separate profile (Claude-3p), so
    claude.ai chats, projects and memory are not visible there; inference is billed to the Console key,
    not a Max/Pro subscription; the in-app third-party settings window becomes read-only
    ("Organization-managed") while the policy is present. -Remove takes the policy away again.

.PARAMETER Port
    Proxy port. Must match aicleaner-config.json. Default 8787.

.PARAMETER Remove
    Remove the aicleaner values from HKCU\SOFTWARE\Policies\Claude (restores the app's own mode chooser).
    Secrets, config and the proxy are left alone.

.PARAMETER RotateKey
    Prompt for the Anthropic API key again even if one is stored.

.PARAMETER RegisterStartupTask
    Register (or update) the 'aicleaner-proxy' logon task so the proxy is up before Claude Desktop.

.PARAMETER RegisterBackupTask
    Register (or update) the 'aicleaner-claude-backup' daily task (03:00) that runs
    Backup-ClaudeDesktopProfile.ps1: a dated zip of the Claude-3p profile on this PC, with retention.

.PARAMETER BackupRoot
    Folder for the snapshots. Default: %USERPROFILE%\.aicleaner\backups\claude-3p (resolved by the
    backup script). Never OneDrive: the history holds real client names (DECISIONS 54).

.PARAMETER AlsoWireClaudeCode
    Also merge the ANTHROPIC_BASE_URL env block into this machine's %USERPROFILE%\.claude\settings.json,
    for terminal Claude Code sessions here. Off by default: only do this on a machine that runs the proxy.

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
    Explicit Python executable for the venv. Default: auto-detect (py -3.13, then python).

.PARAMETER LogPath
    Transcript log path. Defaults to Install-AiCleaner-Desktop.log next to the script. Pass '' to disable.

.EXAMPLE
    .\Install-AiCleaner-Desktop.ps1 -RegisterStartupTask
    Full deployment: venv, config, secrets, policy, proxy up, gateway test.

.EXAMPLE
    .\Install-AiCleaner-Desktop.ps1 -WhatIf
    Show the plan without changing anything (still prompts for nothing; secrets are not requested).

.EXAMPLE
    .\Install-AiCleaner-Desktop.ps1 -Remove
    Take Claude Desktop back out of gateway mode.

.NOTES
    Contact:       see contact-*.json stored alongside this script
    Last-modified: 2026-09-18
    Requires:      Windows PowerShell 5.1 or PowerShell 7+; Python 3.11+ (3.13 preferred); Claude Desktop
    Reference:     https://claude.com/docs/third-party/claude-desktop/configuration
    Elevation:     HKCU\SOFTWARE\Policies is read-only without elevation. Run the install and -Remove
                   from an elevated PowerShell signed in as the same user; the script checks first.
#>

#Requires -Version 5.1

[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [ValidateRange(1024, 65535)][int]$Port = 8787,
    [switch]$Remove,
    [switch]$RotateKey,
    [switch]$RegisterStartupTask,
    [switch]$RegisterBackupTask,
    [string]$BackupRoot = '',
    [switch]$AlsoWireClaudeCode,
    [switch]$SkipPowerToys,
    [string]$ScreenshotKeys = '',
    [switch]$SkipDocToolsInstall,
    [switch]$WithLibreOffice,
    [switch]$WithTesseract,
    [switch]$SkipSnippingTool,
    [string]$PythonExe = '',
    # Take every Confirm-Action's default without asking (the deploy's list of changes)...
    [switch]$AcceptDefaults,
    # ...except the questions about these topics, which the person chose to change.
    [string[]]$AskAbout = @(),
    [AllowEmptyString()][string]$LogPath = $(
        $base = if ($PSCommandPath) { [IO.Path]::GetFileNameWithoutExtension($PSCommandPath) } else { 'Install-AiCleaner-Desktop' }
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
# Helpers (inlined so the script is self-contained; same set as Install-AiCleaner-VSCode.ps1)
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

function Backup-File {
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $backup = "$Path.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    if ($PSCmdlet.ShouldProcess($Path, "Back up to $backup")) {
        Copy-Item -LiteralPath $Path -Destination $backup
        Write-Status "Backed up to $backup" -Level Detail
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
        $json = $Object | ConvertTo-Json -Depth 20
        [IO.File]::WriteAllText($Path, $json, (New-Object System.Text.UTF8Encoding($false)))
    }
}

function Set-JsonProperty {
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

function Test-PolicyWritable {
    <#
    .SYNOPSIS
        True when this process can write HKCU\SOFTWARE\Policies. A standard (non-elevated) token has
        read access only, so Claude Desktop policy changes need an elevated PowerShell run as the
        same user (elevating as a different account would change that account's policy instead).
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param()
    $key = $null
    try {
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('SOFTWARE\Policies', $true)
        return ($null -ne $key)
    } catch [System.Security.SecurityException], [System.UnauthorizedAccessException] {
        return $false
    } finally {
        if ($key) { $key.Dispose() }
    }
}

function Get-PolicyValues {
    <# The values under a registry key as an ordered name -> string map; empty when there are none. #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$KeyPath)
    $map = [ordered]@{}
    if (-not (Test-Path -LiteralPath $KeyPath)) { return $map }
    $key = Get-Item -LiteralPath $KeyPath
    foreach ($name in @($key.Property)) { if ($name) { $map[$name] = [string]$key.GetValue($name) } }
    return $map
}

function Backup-PolicyKey {
    <#
    .SYNOPSIS
        Saves every value under the Claude policy key as JSON in %USERPROFILE%\.aicleaner\backups,
        with the gateway token replaced by a pointer to its secrets file, then reads the file back.
        Throws when the backup cannot be verified, so nothing is removed without one.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    [OutputType([string])]
    param([Parameter(Mandatory)][string]$KeyPath)
    $values = Get-PolicyValues -KeyPath $KeyPath
    if ($values.Contains('inferenceGatewayApiKey')) {
        $values['inferenceGatewayApiKey'] = '<gateway token: %USERPROFILE%\.aicleaner\secrets\anthropic.gateway.token>'
    }
    $folder = Join-Path $env:USERPROFILE '.aicleaner\backups'
    $path = Join-Path $folder ('claude-policy-{0}.json' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    if (-not $PSCmdlet.ShouldProcess($path, 'Back up the Claude policy values')) { return $path }
    if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Force -Path $folder | Out-Null }
    Write-JsonFile -Path $path -Object ([pscustomobject]$values)
    $check = Read-JsonFile -Path $path
    if ($null -eq $check -or @($check.PSObject.Properties).Count -ne $values.Count) {
        throw "The policy backup $path could not be verified; nothing was removed"
    }
    return $path
}

function Remove-LegacyPolicyExports {
    <# Earlier versions exported the policy key to logs\claude-policy-*.reg, gateway token included. #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Root)
    $old = @(Get-ChildItem -LiteralPath (Join-Path $Root 'logs') -Filter 'claude-policy-*.reg' -File -ErrorAction SilentlyContinue)
    if ($old.Count -eq 0) { return }
    Write-Status "$($old.Count) old policy export(s) in logs\ hold the gateway token in plain text" -Level Warn
    if (-not (Confirm-Action -Message 'Delete them?' -Default 'Yes')) { return }
    foreach ($file in $old) {
        if ($PSCmdlet.ShouldProcess($file.FullName, 'Remove')) { Remove-Item -LiteralPath $file.FullName -Force }
    }
    Write-Status 'Old policy exports deleted' -Level Detail
}

function Remove-PolicyValues {
    <#
    .SYNOPSIS
        Removes the named values from a policy key (and the key when nothing else is left in it),
        then reads the key back and returns the names that are still present.
    #>
    [CmdletBinding()]
    [OutputType([string[]])]
    param([Parameter(Mandatory)][string]$KeyPath, [Parameter(Mandatory)][string[]]$Names)
    foreach ($name in $Names) {
        if ((Get-PolicyValues -KeyPath $KeyPath).Contains($name)) {
            Remove-ItemProperty -Path $KeyPath -Name $name -ErrorAction Stop
        }
    }
    $after = Get-PolicyValues -KeyPath $KeyPath
    $left = @($Names | Where-Object { $after.Contains($_) })
    # The key itself goes only when nothing else lives in it (no other values, no subkeys).
    if ($left.Count -eq 0 -and $after.Count -eq 0 -and (Get-Item -LiteralPath $KeyPath).SubKeyCount -eq 0) {
        Remove-Item -LiteralPath $KeyPath -Confirm:$false -ErrorAction Stop
    }
    return $left
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
    # Native stderr under ErrorActionPreference=Stop raises on PS 5.1; probe with Continue and exit codes.
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

function Test-Proxy {
    param([int]$ProxyPort)
    try { return Invoke-RestMethod -Uri "http://127.0.0.1:$ProxyPort/healthz" -TimeoutSec 2 }
    catch { return $null }
}

function Stop-Proxy {
    # Stops whatever listens on the port if it is our python; returns $true when something was stopped.
    param([int]$ProxyPort)
    $conn = Get-NetTCPConnection -LocalPort $ProxyPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) { return $false }
    $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -like 'python*') {
        Stop-Process -Id $proc.Id -Force
        Start-Sleep -Milliseconds 800
        return $true
    }
    Write-Status "Port $ProxyPort is held by $($proc.ProcessName) (pid $($proc.Id)), not aicleaner" -Level Warn
    return $false
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
        $probe = '[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]; ' +
                 'if ([Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()) { "yes" }'
        $answer = & $shell -NoProfile -NonInteractive -Command $probe 2>$null
        if ($answer -match 'yes') {
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

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

$transcriptStarted = $false
if ($LogPath) {
    try { Start-Transcript -Path $LogPath -Force | Out-Null; $transcriptStarted = $true }
    catch { Write-Status "Could not start transcript at ${LogPath}: $($_.Exception.Message)" -Level Warn }
}

try {
    $root = $script:AppFolder
    $baseUrl = "http://127.0.0.1:$Port/anthropic"
    $localRoot = Join-Path $env:USERPROFILE '.aicleaner'
    $venvPython = Join-Path $localRoot 'venv\Scripts\python.exe'
    $secrets = Join-Path $localRoot 'secrets'
    $tokenFile = Join-Path $secrets 'anthropic.gateway.token'
    $keyFile = Join-Path $secrets 'anthropic.key.dpapi'
    $policyKey = 'HKCU:\SOFTWARE\Policies\Claude'
    $ourValues = 'inferenceProvider', 'inferenceGatewayBaseUrl', 'inferenceGatewayApiKey', 'inferenceGatewayAuthScheme',
                 'skipWebFetchPreflight', 'chatAdvancedFileAnalysisEnabled', 'organizationInstructions', 'claudeAiImport'
    Write-Status "aicleaner Desktop deployment from $root"

    # --- Remove mode ---------------------------------------------------------
    if ($Remove) {
        if (-not (Test-Path $policyKey)) { Write-Status 'No Claude policy present; nothing to remove' -Level Done; return }
        if (-not (Test-PolicyWritable)) {
            Write-Status "Cannot change $policyKey from this PowerShell: policy keys are read-only without elevation." -Level Error
            Write-Status 'Re-run with -Remove from an elevated PowerShell signed in as the same user.' -Level Detail
            throw 'Policy key not writable'
        }
        Remove-LegacyPolicyExports -Root $root
        if ($PSCmdlet.ShouldProcess($policyKey, 'Back up and remove the aicleaner values')) {
            $backup = Backup-PolicyKey -KeyPath $policyKey
            $left = @(Remove-PolicyValues -KeyPath $policyKey -Names $ourValues)
            if ($left.Count -gt 0) {
                Write-Status "Still present after removal: $($left -join ', ')" -Level Error
                throw 'Policy values not removed'
            }
            Write-Status "Policy values removed (backup: $backup). Fully quit and relaunch Claude Desktop." -Level Done
        }
        return
    }

    # --- 0. Preflight: machine policy wins over user policy -------------------
    if (Test-Path 'HKLM:\SOFTWARE\Policies\Claude') {
        Write-Status 'HKLM\SOFTWARE\Policies\Claude exists: Claude Desktop ignores HKCU policy entirely when machine policy is present.' -Level Error
        Write-Status 'Remove or reconcile the machine policy first (needs elevation), then re-run.' -Level Detail
        throw 'Machine policy present'
    }
    # Checked before any secret is created, so a non-elevated run stops cleanly instead of failing
    # half-way with a token and key written but no policy.
    if (-not (Test-PolicyWritable)) {
        Write-Status "Cannot write $policyKey from this PowerShell: policy keys are read-only without elevation." -Level Error
        Write-Status 'Re-run from an elevated PowerShell (Run as administrator) signed in as the same user.' -Level Detail
        if ($WhatIfPreference) { Write-Status 'WhatIf: continuing to show the plan' -Level Detail }
        else { throw 'Policy key not writable' }
    }
    $existingProxy = Test-Proxy -ProxyPort $Port
    if ($existingProxy) {
        Write-Status "A proxy already answers on port $Port (clients=$($existingProxy.clients -join ',')); reusing it" -Level Detail
    }
    if (Test-Path -LiteralPath (Join-Path $env:USERPROFILE '.claude\settings.json')) {
        Write-Status 'Claude Code settings found on this machine; the VS Code deployment may already be here' -Level Detail
    }

    # --- 1. Python and the shared venv (outside synced folders) ----------------
    Write-Status 'Locating Python'
    $python = Find-Python -Preferred $PythonExe
    if (-not $python) { $python = Install-Python }
    if (-not $python) {
        Write-Status 'Python 3.11+ is needed. Install Python 3.13 (winget install Python.Python.3.13) and re-run.' -Level Error
        throw 'Python not available'
    }
    Write-Status ("Using " + ($python.Exe + ' ' + ($python.Args -join ' ')).Trim()) -Level Detail
    if ($python.Exe -like '*\WindowsApps\*') {
        # Store Python virtualises AppData writes into its package sandbox, which is why aicleaner keeps
        # its venv and secrets under %USERPROFILE%\.aicleaner instead of %LOCALAPPDATA%.
        Write-Status 'Microsoft Store Python detected: AppData writes are sandboxed; using %USERPROFILE%\.aicleaner' -Level Detail
    }
    $legacyVenv = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython) -and (Test-Path -LiteralPath $legacyVenv)) {
        Write-Status "Using existing repo venv at $legacyVenv (shared venv not created yet)" -Level Detail
        $venvPython = $legacyVenv
    }
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Status "Creating shared venv at $(Split-Path -Parent (Split-Path -Parent $venvPython))"
        if ($PSCmdlet.ShouldProcess($venvPython, 'Create virtual environment')) {
            New-Item -ItemType Directory -Force $localRoot | Out-Null
            & $python.Exe @($python.Args + @('-m', 'venv', (Join-Path $localRoot 'venv')))
            if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
        }
    } else { Write-Status 'venv already present' -Level Detail }
    if ((Test-Path -LiteralPath $venvPython) -and $PSCmdlet.ShouldProcess('requirements.lock', 'pip install')) {
        # Exactly the hashed wheels in the lock, or nothing (H17).
        & $venvPython -m pip install --quiet --disable-pip-version-check --require-hashes --only-binary :all: -r (Join-Path (Join-Path $root 'requirements') 'requirements.lock')
        if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
    }

    # --- 2. Shared config and mapping (never overwritten) ----------------------
    Write-Status 'Seeding configuration files'
    $cfgPath = Join-Path $root 'aicleaner-config.json'
    if (Test-Path -LiteralPath $cfgPath) { Write-Status 'aicleaner-config.json exists; reusing it' -Level Detail }
    elseif ($PSCmdlet.ShouldProcess($cfgPath, 'Seed from aicleaner-config.example.json')) {
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

    # The credentials block tells the proxy where the gateway token and upstream key live. An
    # aicleaner-config.json written by an older deployment will not have it; add it without touching the rest.
    if (Test-Path -LiteralPath $cfgPath) {
        $cfg = Read-JsonFile -Path $cfgPath
        $hasCred = $cfg.PSObject.Properties['credentials'] -and $cfg.credentials.PSObject.Properties['anthropic']
        if (-not $hasCred) {
            Write-Status 'Adding credentials.anthropic block to aicleaner-config.json'
            if (-not $cfg.PSObject.Properties['credentials']) { Set-JsonProperty -Object $cfg -Name 'credentials' -Value ([pscustomobject]@{}) }
            Set-JsonProperty -Object $cfg.credentials -Name 'anthropic' -Value ([pscustomobject]@{
                gateway_token_file = '%USERPROFILE%/.aicleaner/secrets/anthropic.gateway.token'
                upstream_key_file  = '%USERPROFILE%/.aicleaner/secrets/anthropic.key.dpapi'
                upstream_key_env   = 'AICLEANER_ANTHROPIC_API_KEY'
            })
            Backup-File -Path $cfgPath | Out-Null
            Write-JsonFile -Path $cfgPath -Object $cfg
        }
        if ($cfg.PSObject.Properties['port'] -and [int]$cfg.port -ne $Port) {
            $Port = [int]$cfg.port; $baseUrl = "http://127.0.0.1:$Port/anthropic"
            Write-Status "Using port $Port from aicleaner-config.json" -Level Detail
        }
    }

    # --- 3. Validate the mapping -------------------------------------------------
    Write-Status 'Validating the client mapping'
    if ((Test-Path -LiteralPath $venvPython) -and (Test-Path -LiteralPath $cfgPath)) {
        $checked = Invoke-AiCleanerCli -Python $venvPython -AppRoot $root -Arguments @('check', '--config', $cfgPath)
        if ($checked -ne 0 -and -not (Confirm-Action -Message 'Mapping invalid; continue anyway?' -Default 'No')) {
            throw 'Stopped after mapping validation failure'
        }
    } else { Write-Status 'venv or config not present yet (WhatIf?); skipping validation' -Level Detail }

    # --- 4. Secrets: gateway token and Anthropic key ------------------------------
    Write-Status "Secrets under $secrets"
    $tokenEmpty = (Test-Path -LiteralPath $tokenFile) -and [string]::IsNullOrWhiteSpace([string](Get-Content -LiteralPath $tokenFile -Raw))
    if (-not (Test-Path -LiteralPath $tokenFile) -or $tokenEmpty) {
        if ($PSCmdlet.ShouldProcess($tokenFile, 'Generate gateway token')) {
            New-Item -ItemType Directory -Force $secrets | Out-Null
            $bytes = New-Object byte[] 32
            (New-Object System.Security.Cryptography.RNGCryptoServiceProvider).GetBytes($bytes)
            $token = 'aic-' + (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
            [IO.File]::WriteAllText($tokenFile, $token, (New-Object System.Text.UTF8Encoding($false)))
            Write-Status 'Gateway token generated (local only; it is what Claude Desktop will hold)' -Level Detail
        }
    } else { Write-Status 'Gateway token already present' -Level Detail }
    $gatewayToken = if (Test-Path -LiteralPath $tokenFile) { ([string](Get-Content -LiteralPath $tokenFile -Raw)).Trim() } else { '' }

    $keyExists = (Test-Path -LiteralPath $keyFile) -or (Test-Path -LiteralPath ($keyFile -replace '\.dpapi$', ''))
    if ((-not $keyExists -or $RotateKey) -and (Test-Path -LiteralPath $venvPython) -and -not $WhatIfPreference) {
        Write-Status 'Anthropic Console API key (stored DPAPI-encrypted for this user on this machine; never shown again)'
        $secure = Read-Host 'Paste the key' -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try {
            $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
            if ([string]::IsNullOrWhiteSpace($plain)) { Write-Status 'No key entered; the swap will stay inactive' -Level Warn }
            else {
                Push-Location -LiteralPath $root
                try { $plain | & $venvPython -m aicleaner set-key anthropic --stdin --config $cfgPath }
                finally { Pop-Location }
                if ($LASTEXITCODE -ne 0) { throw 'set-key failed' }
            }
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
            $plain = $null
        }
    } elseif ($keyExists) { Write-Status 'Anthropic key already stored (use -RotateKey to replace)' -Level Detail }

    # --- 5. Claude Desktop policy (HKCU) --------------------------------------------
    Write-Status "Claude Desktop policy at $policyKey"
    Remove-LegacyPolicyExports -Root $root
    $instructionsPath = Join-Path $root 'templates\desktop-instructions.txt'
    $instructions = if (Test-Path -LiteralPath $instructionsPath) { ([string](Get-Content -LiteralPath $instructionsPath -Raw -Encoding UTF8)).Trim() } else { '' }
    # The contact line comes from contact-*.json so the policy text and the landing page agree.
    $contactValue = 'the aicleaner maintainer'
    $contactInfo = Get-ContactInfo
    if ($contactInfo) {
        foreach ($field in @('issues_url', 'email', 'name')) {
            if ($contactInfo.PSObject.Properties[$field] -and $contactInfo.$field) { $contactValue = [string]$contactInfo.$field; break }
        }
    }
    $instructions = $instructions.Replace('__CONTACT__', $contactValue)
    if ($instructions.Length -gt 3000) { throw 'templates\desktop-instructions.txt exceeds the 3000-character limit for organizationInstructions' }
    $desired = [ordered]@{
        inferenceProvider               = 'gateway'
        inferenceGatewayBaseUrl         = $baseUrl
        inferenceGatewayApiKey          = $gatewayToken
        inferenceGatewayAuthScheme      = 'x-api-key'
        skipWebFetchPreflight           = 'true'
        chatAdvancedFileAnalysisEnabled = 'true'
        organizationInstructions        = $instructions
        # Object-typed keys are written as a JSON document in a REG_SZ. enabled: import wizard
        # (claude.ai export zip and earlier local sessions); exportEnabled: Settings > Export.
        claudeAiImport                  = '{"enabled":true,"exportEnabled":true}'
    }
    $current = Get-PolicyValues -KeyPath $policyKey
    $changes = @()
    foreach ($name in $desired.Keys) {
        $cur = if ($current.Contains($name)) { $current[$name] } else { $null }
        if ($cur -ne [string]$desired[$name]) {
            $shown = if ($name -eq 'inferenceGatewayApiKey') { '<gateway token>' } elseif ($name -eq 'organizationInstructions') { "<$($instructions.Length) chars>" } else { $desired[$name] }
            $changes += "$name = $shown"
        }
    }
    if ($changes.Count -eq 0) { Write-Status 'Policy already correct' -Level Detail }
    else {
        $changes | ForEach-Object { Write-Status $_ -Level Detail }
        Write-Status 'This puts Claude Desktop into gateway mode (separate Claude-3p profile) and makes its third-party settings window read-only.' -Level Warn
        if (Confirm-Action -Topic 'desktop-policy' -Message 'Write these policy values?' -Default 'Yes') {
            if ($current.Count -gt 0 -and $PSCmdlet.ShouldProcess($policyKey, 'Back up the existing values')) {
                $backup = Backup-PolicyKey -KeyPath $policyKey
                Write-Status "Existing policy saved to $backup (gateway token not included)" -Level Detail
            }
            if ($PSCmdlet.ShouldProcess($policyKey, 'Write REG_SZ values')) {
                if (-not (Test-Path $policyKey)) { New-Item -Path $policyKey -Force | Out-Null }
                foreach ($name in $desired.Keys) {
                    New-ItemProperty -Path $policyKey -Name $name -Value ([string]$desired[$name]) -PropertyType String -Force | Out-Null
                }
                Write-Status 'Policy written; Claude Desktop reads it at launch' -Level Done
            }
        } else { Write-Status 'Policy not written; Claude Desktop stays in its current mode' -Level Warn }
    }

    # --- 6. Optional: terminal Claude Code on this machine ---------------------------
    # Everything except the base URL. Routing is Enable-AiCleaner.ps1's job, after it has checked
    # the filter has a client with forms in it and that a request survives the whole path; an
    # install that routed straight away pointed Claude Code at an empty filter.
    if ($AlsoWireClaudeCode) {
        $settingsPath = Join-Path $env:USERPROFILE '.claude\settings.json'
        Write-Status "Merging Claude Code settings at $settingsPath"
        $settings = Read-JsonFile -Path $settingsPath
        if ($null -eq $settings) { $settings = [pscustomobject]@{} }
        if (-not $settings.PSObject.Properties['env']) { Set-JsonProperty -Object $settings -Name 'env' -Value ([pscustomobject]@{}) }
        # The opt-out variable an earlier install wrote here goes: it switched off the feature-flag
        # evaluation Remote Control needs, long after an uninstall. The base URL is what keeps a
        # session from being relayed around the filter (Claude Code refuses Remote Control with it),
        # so that protection arrives with activation rather than with the install.
        $staleTraffic = [bool]$settings.env.PSObject.Properties['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC']
        $haveSkip = if ($settings.PSObject.Properties['skipWebFetchPreflight']) { [bool]$settings.skipWebFetchPreflight } else { $false }
        if (-not $staleTraffic -and $haveSkip) {
            Write-Status 'Claude Code settings already correct' -Level Detail
        } else {
            Backup-File -Path $settingsPath | Out-Null
            if ($staleTraffic) {
                $settings.env.PSObject.Properties.Remove('CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC')
                Write-Status 'Removed CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC from the settings env block' -Level Detail
            }
            Set-JsonProperty -Object $settings -Name 'skipWebFetchPreflight' -Value $true
            Write-JsonFile -Path $settingsPath -Object $settings
        }
        # Worth knowing now rather than at activation: a machine-scope variable or a managed
        # settings file beats anything Enable-AiCleaner.ps1 writes later.
        Test-RoutingOverride -ProxyUrl $baseUrl
        Write-Status 'Claude Code is NOT routed through the filter yet; .\Enable-AiCleaner.ps1 does that' -Level Detail
    }

    # --- 7. Proxy up, with the credentials loaded --------------------------------------
    $startScript = Join-Path $PSScriptRoot 'Start-AiCleaner.ps1'
    $tasksAllowed = $true
    if ($RegisterStartupTask -or $RegisterBackupTask) { $tasksAllowed = Confirm-TaskFromSyncedFolder -Root $root }
    if ($RegisterStartupTask -and $tasksAllowed) {
        $taskName = 'aicleaner-proxy'
        Write-Status "Registering logon task '$taskName'"
        $action = Get-HiddenTaskAction -Script $startScript -Arguments '-Detach' -WorkingDirectory $root
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $taskSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
        if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
            if ($PSCmdlet.ShouldProcess($taskName, 'Update scheduled task')) {
                Set-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $taskSettings | Out-Null
            }
        } elseif ($PSCmdlet.ShouldProcess($taskName, 'Register scheduled task')) {
            Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $taskSettings `
                -Description 'aicleaner: local tokenise/rehydrate proxy for Claude Code and Claude Desktop' | Out-Null
        }
    }

    # --- 7b. Optional: daily profile snapshot to OneDrive --------------------------------
    if ($RegisterBackupTask -and $tasksAllowed) {
        $backupTask = 'aicleaner-claude-backup'
        $backupScript = Join-Path $PSScriptRoot 'Backup-ClaudeDesktopProfile.ps1'
        Write-Status "Registering daily task '$backupTask' (03:00)"
        $argLine = ''
        if ($BackupRoot) {
            # A trailing backslash would escape the closing quote on the task's command line; double it.
            $quotedRoot = if ($BackupRoot.EndsWith('\')) { $BackupRoot + '\' } else { $BackupRoot }
            $argLine = "-BackupRoot `"$quotedRoot`""
        }
        $bAction = Get-HiddenTaskAction -Script $backupScript -Arguments $argLine -WorkingDirectory $root
        $bTrigger = New-ScheduledTaskTrigger -Daily -At 3am
        $bSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
        if (Get-ScheduledTask -TaskName $backupTask -ErrorAction SilentlyContinue) {
            if ($PSCmdlet.ShouldProcess($backupTask, 'Update scheduled task')) {
                Set-ScheduledTask -TaskName $backupTask -Action $bAction -Trigger $bTrigger -Settings $bSettings | Out-Null
            }
        } elseif ($PSCmdlet.ShouldProcess($backupTask, 'Register scheduled task')) {
            Register-ScheduledTask -TaskName $backupTask -Action $bAction -Trigger $bTrigger -Settings $bSettings `
                -Description 'aicleaner: nightly snapshot of the Claude Desktop 3P profile to OneDrive' | Out-Null
        }
        Write-Status "Snapshots go to $(if ($BackupRoot) { $BackupRoot } else { '%USERPROFILE%\.aicleaner\backups\claude-3p' }) (never OneDrive); run the script by hand once to check" -Level Detail
    }

    # --- 7c. Screenshot chord to text: Snipping Tool + PowerToys remap ------------
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

    # --- 7d. Document tooling (pandoc, pdftotext, python-docx, openpyxl, python-pptx, PyMuPDF) ---
    if ($SkipDocToolsInstall) { Write-Status 'Document tooling step skipped' -Level Detail }
    else {
        Write-Status 'Document tooling for local file conversion'
        Install-DocumentToolset -Python $python -WithLibreOffice:$WithLibreOffice -WithTesseract:$WithTesseract
        # What the proxy itself can read, which is a different list from what the agent can open.
        Test-OcrEngine | Out-Null
    }

    # Credentials are read at proxy start, so a proxy that predates the key must be restarted once.
    $health = Test-Proxy -ProxyPort $Port
    $needsRestart = $health -and -not ($health.PSObject.Properties['credential_swap'] -and $health.credential_swap -contains 'anthropic')
    if ($needsRestart -and (Confirm-Action -Topic 'proxy-task' -Message 'Proxy is running without the credential swap; restart it now?' -Default 'Yes')) {
        if ($PSCmdlet.ShouldProcess("port $Port", 'Stop proxy')) { Stop-Proxy -ProxyPort $Port | Out-Null }
        $health = $null
    }
    if (-not $health -and (Test-Path -LiteralPath $startScript) -and -not $WhatIfPreference) {
        & $startScript -Detach -Port $Port -LogPath ''
        $health = Test-Proxy -ProxyPort $Port
    }

    # --- 8. Live gateway test: token in, real models out ---------------------------------
    if ($health) {
        $swapOn = $health.PSObject.Properties['credential_swap'] -and $health.credential_swap -contains 'anthropic'
        Write-Status ("Proxy up: clients={0} credential_swap={1}" -f ($health.clients -join ','), $(if ($swapOn) { 'anthropic' } else { 'none' })) -Level Done
        if ($swapOn -and $gatewayToken) {
            try {
                $models = Invoke-RestMethod -Uri "$baseUrl/v1/models" -Headers @{ 'x-api-key' = $gatewayToken } -TimeoutSec 20
                $ids = @($models.data | ForEach-Object { $_.id }) | Select-Object -First 5
                Write-Status "Gateway test passed: Anthropic answered with models ($($ids -join ', ') ...)" -Level Done
            } catch {
                Write-Status "Gateway test failed: $($_.Exception.Message). Check the API key (-RotateKey) and network." -Level Error
            }
        } elseif (-not $swapOn) {
            Write-Status 'Credential swap inactive: store the key (re-run, or -RotateKey) and restart the proxy' -Level Warn
        }
    } else { Write-Status 'Proxy not running; start it with .\Start-AiCleaner.ps1 -Detach' -Level Warn }

    Write-Status 'Desktop deployment complete' -Level Done
    if ($AlsoWireClaudeCode) {
        Write-Status 'Claude Code and VS Code are NOT going through the filter yet. Set a client up first (phrases that name them, a repo scan), then run .\Enable-AiCleaner.ps1 - it checks the filter is ready before it switches anything over.' -Level Warn
    }
    Write-Status 'Fully quit Claude Desktop (tray icon > Quit) and relaunch. Confirm gateway mode under Help > Troubleshooting.' -Level Detail
    Write-Status 'Evidence: logs\evidence-<host>-<date>.jsonl, auth="gateway-swap" for Desktop traffic.' -Level Detail
}
catch {
    Write-Status "Deployment failed: $($_.Exception.Message)" -Level Error
    Write-Error $_ -ErrorAction Continue
    exit 1
}
finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
