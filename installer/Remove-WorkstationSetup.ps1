<#
.SYNOPSIS
    Undoes a workstation (client PC) install of aicleaner, changing back only what it changed.

.DESCRIPTION
    Self-contained, so it can be copied to a machine on its own and run there. It removes what the
    workstation install put in place and restores what it altered:

      - the Start Menu shortcut "aicleaner text from screen.lnk" (Ctrl+Alt+T)
      - the PowerToys Keyboard Manager remap of the screenshot keys, and only that one remap
      - the PowerToys module settings it changed (Keyboard Manager, TextExtractor, run at startup),
        each back to the value recorded before the install, or removed when there was none
      - VS Code's telemetry.telemetryLevel, back to what it was, or removed when it was not set
      - the deployed code folder and the deployment record

    PowerToys and Snipping Tool are left installed. They are ordinary applications that were
    probably there first, and the install ledger says whether aicleaner installed PowerToys itself;
    even then, removing an application someone may now rely on is not this script's decision.

    Every file it edits is backed up first (.bak-<timestamp>), and -WhatIf shows the plan.

.PARAMETER AppRoot
    The folder the code was deployed to. Default %USERPROFILE%\.aicleaner\app.

.PARAMETER StateRoot
    Where install-changes.json and deployment.json live. Default %USERPROFILE%\.aicleaner.

.PARAMETER VSCodeSettingsPath
    VS Code user settings. Default %APPDATA%\Code\User\settings.json. '' to leave them alone.

.PARAMETER KeepCode
    Leave the deployed code folder and the record in place; undo only the machine changes.

.EXAMPLE
    .\Remove-WorkstationSetup.ps1 -WhatIf
    Say what would be undone, and change nothing.

.EXAMPLE
    .\Remove-WorkstationSetup.ps1

.NOTES
    Contact:       see contact-*.json stored alongside this script
    Last-modified: 2026-09-21
    Requires:      Windows PowerShell 5.1 or PowerShell 7+
#>

#Requires -Version 5.1

[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [string]$AppRoot = (Join-Path $env:USERPROFILE '.aicleaner\app'),
    [string]$StateRoot = (Join-Path $env:USERPROFILE '.aicleaner'),
    [AllowEmptyString()][string]$VSCodeSettingsPath = (Join-Path $env:APPDATA 'Code\User\settings.json'),
    [switch]$KeepCode
)
# The app folder: this script's own, or its parent when it sits in installer\ (feature 102).
$script:AppFolder = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

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

function Test-JsonComment {
    <# True when the text holds a // or /* comment outside a string: not plain JSON. #>
    [OutputType([bool])]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Text)
    $inString = $false
    $escaped = $false
    for ($i = 0; $i -lt $Text.Length; $i++) {
        $ch = $Text[$i]
        if ($escaped) { $escaped = $false; continue }
        if ($ch -eq '\') { $escaped = $true; continue }
        if ($ch -eq '"') { $inString = -not $inString; continue }
        if (-not $inString -and $ch -eq '/' -and $i + 1 -lt $Text.Length) {
            $next = $Text[$i + 1]
            if ($next -eq '/' -or $next -eq '*') { return $true }
        }
    }
    return $false
}

function Read-JsonFile {
    <# The file as an object, or $null when it is missing or empty. #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $text = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    if (Test-JsonComment -Text $text) { throw "$Path contains comments; it is not plain JSON" }
    return ($text | ConvertFrom-Json)
}

function Backup-File {
    <# A dated copy beside the file, so a wrong guess here is recoverable. #>
    [CmdletBinding(SupportsShouldProcess)]
    [OutputType([string])]
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    $backup = "$Path.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    if ($PSCmdlet.ShouldProcess($Path, "Back up to $(Split-Path -Leaf $backup)")) {
        Copy-Item -LiteralPath $Path -Destination $backup -Force
        Write-Status "Backed up to $backup" -Level Detail
    }
    return $backup
}

function Write-JsonFile {
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)]$Object)
    $json = $Object | ConvertTo-Json -Depth 20
    if ($PSCmdlet.ShouldProcess($Path, 'Write')) {
        $folder = Split-Path -Parent $Path
        if ($folder -and -not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
        [IO.File]::WriteAllText($Path, $json, (New-Object System.Text.UTF8Encoding($false)))
    }
}

function ConvertFrom-KeyCodes {
    <# 91;16;83 back to "Win+Shift+S", for saying what changed. #>
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

function Get-LedgerValue {
    <#
    .SYNOPSIS
        What a setting was before the install, from install-changes.json.
    .DESCRIPTION
        Returns @{ Known; Value }: Known says the install recorded this setting at all, and Value
        is what it held ($null meaning it did not exist and should go away again).
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Ledger, [Parameter(Mandatory)][string]$Section, [Parameter(Mandatory)][string]$Name)
    if (-not $Ledger -or -not $Ledger.PSObject.Properties[$Section]) { return @{ Known = $false; Value = $null } }
    # Not $section: PowerShell would see the [string]$Section parameter and turn the object back
    # into a string, and every lookup on it would then miss.
    $recorded = $Ledger.$Section
    if (-not $recorded.PSObject.Properties[$Name]) { return @{ Known = $false; Value = $null } }
    return @{ Known = $true; Value = $recorded.$Name }
}

function Remove-JsonProperty {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Object, [Parameter(Mandatory)][string]$Name)
    if ($Object.PSObject.Properties[$Name]) { $Object.PSObject.Properties.Remove($Name) }
}

function Restore-JsonValue {
    <# Puts one property back to what the ledger recorded, or removes it when there was none. #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Object,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)]$Recorded,
        [Parameter(Mandatory)][string]$What
    )
    if (-not $Recorded.Known) {
        Write-Status "$What was not recorded at install time; leaving it as it is" -Level Warn
        return $false
    }
    if ($null -eq $Recorded.Value) {
        if ($Object.PSObject.Properties[$Name]) {
            Remove-JsonProperty -Object $Object -Name $Name
            Write-Status "$What removed (it did not exist before)" -Level Detail
            return $true
        }
        return $false
    }
    $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Recorded.Value -Force
    Write-Status "$What put back to $($Recorded.Value)" -Level Detail
    return $true
}

function Remove-CaptureShortcut {
    <# The Start Menu shortcut with the Ctrl+Alt+T hotkey. #>
    [CmdletBinding(SupportsShouldProcess)]
    param()
    $shortcut = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\aicleaner text from screen.lnk'
    if (-not (Test-Path -LiteralPath $shortcut)) {
        Write-Status 'No screen-text shortcut here' -Level Detail
        return
    }
    if ($PSCmdlet.ShouldProcess($shortcut, 'Remove the screen-text shortcut')) {
        Remove-Item -LiteralPath $shortcut -Force
        Write-Status 'Screen-text shortcut removed (Ctrl+Alt+T)' -Level Done
    }
}

function Remove-PowerToysRemap {
    <#
    .SYNOPSIS
        Puts Win+Shift+S back the way it was, and leaves everything else PowerToys holds.
    .DESCRIPTION
        What happens to that key comes from the ledger: a remap recorded there was yours before the
        install and is put back; a recorded "none" means aicleaner added the entry and it goes. With
        no record at all - an install from before this was tracked - the remap is left alone and
        said out loud, because deleting something that might be yours is the worse mistake.

        Other remaps are never touched, and PowerToys itself stays installed with the modules it
        had on before put back the way they were.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)]$Ledger)
    $settingsDir = Join-Path $env:LOCALAPPDATA 'Microsoft\PowerToys'
    if (-not (Test-Path -LiteralPath $settingsDir)) {
        Write-Status 'No PowerToys settings on this machine' -Level Detail
        return
    }
    # --- the remap itself ---------------------------------------------------
    $kbmPath = Join-Path $settingsDir 'Keyboard Manager\default.json'
    $kbm = $null
    try { $kbm = Read-JsonFile -Path $kbmPath } catch { Write-Status "$kbmPath is unreadable; leaving it alone" -Level Warn }
    if ($kbm -and $kbm.PSObject.Properties['remapShortcuts'] -and $kbm.remapShortcuts.PSObject.Properties['global']) {
        $before = @($kbm.remapShortcuts.global)
        # Which keys were taken is in the ledger, because the person chose them: Win+Shift+S for
        # one, PrtSc for another. Anything pointing at Win+Shift+T with no record is left alone.
        $taken = @()
        if ($Ledger -and $Ledger.PSObject.Properties['powertoys_remap']) {
            $taken = @($Ledger.powertoys_remap.PSObject.Properties | ForEach-Object { $_.Name })
        }
        # Ours either way: the capture script started by a run-program remap (2026-10-12), or
        # the older remap to Win+Shift+T. Properties checked by name: other entries may lack them.
        $ours = @($before | Where-Object { ($_.PSObject.Properties['newRemapKeys'] -and [string]$_.newRemapKeys -eq '91;16;84') -or
            ($_.PSObject.Properties['runProgramArgs'] -and [string]$_.runProgramArgs -like '*Capture-TextFromScreen.ps1*') })
        $unrecorded = @($ours | Where-Object { $taken -notcontains [string]$_.originalKeys })
        if ($ours.Count -eq 0) {
            Write-Status 'Nothing here is remapped to the screen capture or the text extractor; no shortcut to put back' -Level Detail
        } elseif ($taken.Count -eq 0) {
            $keys = (($ours | ForEach-Object { ConvertFrom-KeyCodes -Codes ([string]$_.originalKeys) }) -join ', ')
            Write-Status "$keys is remapped to Win+Shift+T, but nothing recorded what it did before." -Level Warn
            Write-Status 'Left in place: if you set that remap up yourself, it is still yours. Remove it in' -Level Detail
            Write-Status 'PowerToys > Keyboard Manager > Remap a shortcut if aicleaner added it.' -Level Detail
        } elseif ($PSCmdlet.ShouldProcess($kbmPath, 'Put the screenshot shortcut back')) {
            Backup-File -Path $kbmPath | Out-Null
            $restored = New-Object System.Collections.Generic.List[object]
            foreach ($entry in $before) {
                $keys = [string]$entry.originalKeys
                if ($taken -notcontains $keys) { $restored.Add($entry); continue }   # not ours to touch
                $label = ConvertFrom-KeyCodes -Codes $keys
                $was = [string]$Ledger.powertoys_remap.$keys
                if ($was) {
                    $restored.Add([pscustomobject]@{ originalKeys = $keys; newRemapKeys = $was })
                    Write-Status "$label put back to your own remap (-> $(ConvertFrom-KeyCodes -Codes $was))" -Level Done
                } else {
                    Write-Status "$label remap removed: aicleaner added it, and you had none on those keys" -Level Done
                }
            }
            # ToArray, not @(): the adapter refuses a List here with "Argument types do not match".
            $kbm.remapShortcuts | Add-Member -NotePropertyName 'global' -NotePropertyValue $restored.ToArray() -Force
            Write-JsonFile -Path $kbmPath -Object $kbm
            $untouched = @($before | Where-Object { $taken -notcontains [string]$_.originalKeys }).Count
            if ($untouched -gt 0) { Write-Status "$untouched other remap(s) untouched" -Level Detail }
        }
        if ($unrecorded.Count -gt 0 -and $taken.Count -gt 0) {
            $keys = (($unrecorded | ForEach-Object { ConvertFrom-KeyCodes -Codes ([string]$_.originalKeys) }) -join ', ')
            Write-Status "$keys also points at the text extractor and was not recorded here; left alone" -Level Detail
        }
    }
    # --- the module settings it changed --------------------------------------
    $rootPath = Join-Path $settingsDir 'settings.json'
    $rootCfg = $null
    try { $rootCfg = Read-JsonFile -Path $rootPath } catch { Write-Status "$rootPath is unreadable; leaving it alone" -Level Warn }
    if (-not $rootCfg) { return }
    $changed = $false
    $backedUp = $false
    foreach ($module in 'Keyboard Manager', 'TextExtractor') {
        $recorded = Get-LedgerValue -Ledger $Ledger -Section 'powertoys' -Name "enabled/$module"
        if (-not $recorded.Known) { continue }
        if (-not $rootCfg.PSObject.Properties['enabled']) { continue }
        if (-not $backedUp) { Backup-File -Path $rootPath | Out-Null; $backedUp = $true }
        if (Restore-JsonValue -Object $rootCfg.enabled -Name $module -Recorded $recorded -What "PowerToys $module") { $changed = $true }
    }
    $startup = Get-LedgerValue -Ledger $Ledger -Section 'powertoys' -Name 'startup'
    if ($startup.Known) {
        if (-not $backedUp) { Backup-File -Path $rootPath | Out-Null; $backedUp = $true }
        if (Restore-JsonValue -Object $rootCfg -Name 'startup' -Recorded $startup -What 'PowerToys run at startup') { $changed = $true }
    }
    if ($changed -and $PSCmdlet.ShouldProcess($rootPath, 'Write the PowerToys settings back')) {
        Write-JsonFile -Path $rootPath -Object $rootCfg
    }
    $installed = Get-LedgerValue -Ledger $Ledger -Section 'powertoys_install' -Name 'installed_by_aicleaner'
    if ($installed.Known -and $installed.Value -eq $true) {
        Write-Status 'The ledger says aicleaner installed PowerToys; it is left in place.' -Level Detail
        Write-Status 'Remove it yourself if you want it gone: winget uninstall Microsoft.PowerToys' -Level Detail
    } else {
        Write-Status 'PowerToys was here before aicleaner and is left exactly as it is' -Level Detail
    }
    Write-Status 'Restart PowerToys (or sign out and in) for the keyboard change to take effect' -Level Detail
}

function Restore-VSCodeTelemetry {
    <# telemetry.telemetryLevel back to what it was, or gone when it was not set. #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)]$Ledger, [Parameter(Mandatory)][AllowEmptyString()][string]$Path)
    if (-not $Path) { return }
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Status 'No VS Code user settings here' -Level Detail
        return
    }
    $recorded = Get-LedgerValue -Ledger $Ledger -Section 'vscode' -Name 'telemetry.telemetryLevel'
    if (-not $recorded.Known) {
        Write-Status 'The install did not record a VS Code telemetry setting; leaving settings.json alone' -Level Warn
        return
    }
    $settings = $null
    try { $settings = Read-JsonFile -Path $Path }
    catch {
        Write-Status "$Path is not plain JSON (comments); set telemetry.telemetryLevel back by hand" -Level Warn
        return
    }
    if (-not $settings) { return }
    if ($PSCmdlet.ShouldProcess($Path, 'Put the telemetry setting back')) {
        Backup-File -Path $Path | Out-Null
        [void](Restore-JsonValue -Object $settings -Name 'telemetry.telemetryLevel' -Recorded $recorded -What 'VS Code telemetry')
        Write-JsonFile -Path $Path -Object $settings
        Write-Status 'VS Code telemetry setting restored (restart VS Code)' -Level Done
    }
}

function Remove-DeployedCode {
    <# The code folder and the record; data folders are left for you to look at. #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)][string]$State)
    if (Test-Path -LiteralPath $Root) {
        if ($PSCmdlet.ShouldProcess($Root, 'Remove the deployed code')) {
            Remove-Item -LiteralPath $Root -Recurse -Force
            Write-Status "Code removed from $Root" -Level Done
        }
    } else {
        Write-Status "No code at $Root" -Level Detail
    }
    foreach ($name in 'deployment.json', 'install-changes.json') {
        $file = Join-Path $State $name
        if (-not (Test-Path -LiteralPath $file)) { continue }
        if ($PSCmdlet.ShouldProcess($file, 'Remove')) {
            Remove-Item -LiteralPath $file -Force
            Write-Status "$name removed" -Level Detail
        }
    }
    if ((Test-Path -LiteralPath $State) -and -not @(Get-ChildItem -LiteralPath $State -Force -ErrorAction SilentlyContinue).Count) {
        if ($PSCmdlet.ShouldProcess($State, 'Remove the empty state folder')) { Remove-Item -LiteralPath $State -Force }
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

try {
    Write-Status 'Undoing the aicleaner workstation setup'
    $ledgerPath = Join-Path $StateRoot 'install-changes.json'
    $ledger = $null
    try { $ledger = Read-JsonFile -Path $ledgerPath } catch { Write-Status "$ledgerPath is unreadable" -Level Warn }
    if ($ledger) {
        Write-Status "Reading what the install changed, from $ledgerPath" -Level Detail
    } else {
        Write-Status "No install ledger at ${ledgerPath}: settings it recorded cannot be put back exactly." -Level Warn
        Write-Status 'The shortcut, the remap and the deployed code are still removed.' -Level Detail
    }

    Remove-CaptureShortcut
    Remove-PowerToysRemap -Ledger $ledger
    Restore-VSCodeTelemetry -Ledger $ledger -Path $VSCodeSettingsPath
    if ($KeepCode) {
        Write-Status 'Code and record kept (-KeepCode)' -Level Detail
    } else {
        Remove-DeployedCode -Root $AppRoot -State $StateRoot
    }

    Write-Status 'Workstation setup undone' -Level Done
    Write-Status 'Left alone on purpose: PowerToys, Snipping Tool, and every other remap you had.' -Level Detail
}
catch {
    Write-Status "Could not finish: $($_.Exception.Message)" -Level Error
    Write-Error $_ -ErrorAction Continue
    exit 1
}
