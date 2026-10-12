<#
.SYNOPSIS
    Sets up the client PC (the machine showing the VS Code window) for aicleaner.

.DESCRIPTION
    In a Remote-SSH setup Claude Code runs on the VS Code server VM, and so does the proxy: this
    machine needs neither. What it does need is the screenshot habit, because screenshots are taken
    here and pasted into the window here. This script:
      - makes sure Snipping Tool is present and new enough for its text extractor
      - offers to remap the keys you press for a screenshot to Win+Shift+T, Snipping Tool's text
        extractor, and installs PowerToys (user scope) to do it, so what reaches the clipboard is
        text, which the proxy can filter, rather than an image, which it cannot. Declined, neither
        happens.
      - turns VS Code's own telemetry off (a setting on this machine, not the VM)
    It deliberately does NOT set ANTHROPIC_BASE_URL here: a base URL on this machine would also
    apply to any local Claude Code session, where no proxy is listening, and fail it closed.

    What it changes outside its own folder is recorded in
    %USERPROFILE%\.aicleaner\install-changes.json, so Uninstall-AiCleaner.ps1 puts it back.

.PARAMETER ScreenshotKeys
    The keys you press now for a screenshot, e.g. 'win+shift+s' or 'ctrl+prtsc', or 'none' to leave them
    alone (and skip PowerToys). Asked for when not given: the point is to remap the shortcut you already use.

.PARAMETER SkipPowerToys
    Skip PowerToys and the Keyboard Manager remap.

.PARAMETER SkipSnippingTool
    Skip checking and installing Snipping Tool (Microsoft.ScreenSketch 11.2503.29.0+).

.PARAMETER VSCodeSettingsPath
    VS Code user settings on this machine. Default %APPDATA%\Code\User\settings.json. Pass '' to
    leave VS Code alone.

.PARAMETER LogPath
    Transcript log path. Defaults to Install-AiCleaner-Workstation.log next to the script. Pass ''
    to disable.

.EXAMPLE
    .\Install-AiCleaner-Workstation.ps1
    Screenshot to text, and VS Code telemetry off.

.EXAMPLE
    .\Install-AiCleaner-Workstation.ps1 -WhatIf
    Show what would change.

.NOTES
    Contact:       see contact-*.json stored alongside this script
    Last-modified: 2026-09-20
    Requires:      Windows PowerShell 5.1 or PowerShell 7+; winget for the installs
#>

#Requires -Version 5.1

[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [switch]$SkipPowerToys,
    [string]$ScreenshotKeys = '',
    [switch]$SkipSnippingTool,
    [AllowEmptyString()][string]$VSCodeSettingsPath = (Join-Path $env:APPDATA 'Code\User\settings.json'),
    [AllowEmptyString()][string]$LogPath = $(
        $base = if ($PSCommandPath) { [IO.Path]::GetFileNameWithoutExtension($PSCommandPath) } else { 'Install-AiCleaner-Workstation' }
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
        [ValidateSet('Yes', 'No')][string]$Default = 'No'
    )
    $suffix = if ($Default -eq 'Yes') { '[Y/n]' } else { '[y/N]' }
    $response = Read-Host "$Message $suffix"
    if ([string]::IsNullOrWhiteSpace($response)) { return ($Default -eq 'Yes') }
    return $response -match '^(y|yes)$'
}

function Backup-File {
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = "$Path.bak-$stamp"
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
    Ensures Snipping Tool (Microsoft.ScreenSketch) is present and new enough for its text extractor
    (11.2503.29.0 or later, Win+Shift+T needs Windows 11 build 26100+). Installs or updates through
    winget's Store source; if the Store is unreachable, re-registers the provisioned package. Snipping
    Tool's own "Automatically copy text" preference lives in the app's settings store and is set once
    in the app. Returns $true when a capable Snipping Tool is installed.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param()
    $minVersion = [version]'11.2503.29.0'
    $storeId = '9MZ95KL8MR0L'
    $pkg = Get-AppxPackage -Name 'Microsoft.ScreenSketch' -ErrorAction SilentlyContinue | Sort-Object { [version]$_.Version } -Descending | Select-Object -First 1
    $build = [Environment]::OSVersion.Version.Build
    if ($build -lt 26100) {
        Write-Status "Windows build $build predates Win+Shift+T text extraction (needs 26100+); the remap will open the older Snipping Tool" -Level Warn
    }
    if ($pkg -and [version]$pkg.Version -ge $minVersion) {
        Write-Status "Snipping Tool $($pkg.Version) present" -Level Detail
        return $true
    }
    $why = if ($pkg) { "version $($pkg.Version) is older than $minVersion" } else { 'not installed for this user' }
    Write-Status "Snipping Tool $why; installing/updating from the Store"
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
    Write-Status "Snipping Tool is $(if ($pkg) { $pkg.Version } else { 'missing' }); update it from the Microsoft Store app (Library > Get updates) and re-run" -Level Warn
    return $false
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

Write-ContactAttribution

$transcriptStarted = $false
if ($LogPath) {
    try { Start-Transcript -Path $LogPath -Force | Out-Null; $transcriptStarted = $true }
    catch { Write-Status "Could not start transcript at ${LogPath}: $($_.Exception.Message)" -Level Warn }
}

try {
    Write-Status 'aicleaner workstation setup (no proxy and no Claude Code settings on this machine)'

    # --- 1. Screenshots become text -----------------------------------------
    if ($SkipSnippingTool) { Write-Status 'Snipping Tool step skipped' -Level Detail }
    else {
        Write-Status 'Snipping Tool (text extractor capable)'
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

    # --- 2. VS Code telemetry (this machine hosts the window) ----------------
    if ([string]::IsNullOrWhiteSpace($VSCodeSettingsPath)) {
        Write-Status 'VS Code step skipped' -Level Detail
    } elseif (-not (Test-Path -LiteralPath $VSCodeSettingsPath)) {
        Write-Status "No VS Code settings at $VSCodeSettingsPath; nothing to change" -Level Detail
    } else {
        $vs = $null
        try { $vs = Read-JsonFile -Path $VSCodeSettingsPath }
        catch { Write-Status "VS Code settings.json is not plain JSON; set telemetry.telemetryLevel to off by hand" -Level Warn }
        if ($null -ne $vs) {
            $level = if ($vs.PSObject.Properties['telemetry.telemetryLevel']) { [string]$vs.'telemetry.telemetryLevel' } else { '' }
            if ($level -eq 'off') { Write-Status 'VS Code telemetry already off' -Level Detail }
            elseif (Confirm-Action -Default 'Yes' -Message ('Turn VS Code telemetry off on this machine? ' +
                'Yes stops VS Code telemetry carrying client-specific details to Microsoft')) {
                Backup-File -Path $VSCodeSettingsPath | Out-Null
                Save-LedgerPrevious -Section 'vscode' -Name 'telemetry.telemetryLevel' -Value $(if ($level) { $level } else { $null })
                Set-JsonProperty -Object $vs -Name 'telemetry.telemetryLevel' -Value 'off'
                Write-JsonFile -Path $VSCodeSettingsPath -Object $vs
                if (-not $WhatIfPreference) { Write-Status 'VS Code telemetry off (restart VS Code)' -Level Done }
            }
        }
    }

    # --- 3. What this machine must not have ----------------------------------
    foreach ($scope in 'User', 'Machine') {
        if ([Environment]::GetEnvironmentVariable('ANTHROPIC_BASE_URL', $scope)) {
            Write-Status "ANTHROPIC_BASE_URL is set at $scope scope on this machine: a local Claude Code session would send to it, and there is no proxy here." -Level Warn
        }
    }

    Write-Status 'Workstation ready: paste text, not screenshots (Win+Shift+T extracts the text from part of the screen)' -Level Done
    Write-Status 'The proxy, the mapping and the UI live on the VS Code server VM; reach the UI through VS Code port forwarding.' -Level Detail
}
catch {
    Write-Status "Workstation setup failed: $($_.Exception.Message)" -Level Error
    Write-Error $_ -ErrorAction Continue
    exit 1
}
finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
