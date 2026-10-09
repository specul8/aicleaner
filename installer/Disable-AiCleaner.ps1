<#
.SYNOPSIS
    Points Claude Code and VS Code back at the real API, leaving everything else installed.

.DESCRIPTION
    The opposite of Enable-AiCleaner.ps1, and nothing else: the proxy keeps running, the mapping,
    logs and settings all stay, and only the two base-URL settings change.

    This exists because getting back to work should not require uninstalling anything. When a
    filter problem stops traffic, the fix is to stop routing through it - a few seconds - not to
    tear the install down and put it back afterwards.

    ANTHROPIC_BASE_URL is pointed at https://api.anthropic.com rather than removed. A session
    already running follows a value that CHANGES and does not notice a key that disappears, so
    this takes effect without restarting anything. It is the public endpoint, where Claude Code
    goes anyway, and it is not what refuses Remote Control - that is a host OTHER than
    api.anthropic.com.

.PARAMETER LogPath
    Transcript log path. Pass '' to disable.

.EXAMPLE
    .\Disable-AiCleaner.ps1
    Stop routing through the proxy, right now.

.NOTES
    Contact:       see contact-*.json stored alongside this script
    Requires:      Windows PowerShell 5.1 or PowerShell 7+
#>

#Requires -Version 5.1

[CmdletBinding(SupportsShouldProcess)]
param(
    [AllowEmptyString()][string]$LogPath = $(
        $dir = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }
        Join-Path $dir 'Disable-AiCleaner.log'
    )
)
# The app folder: this script's own, or its parent when it sits in installer\ (feature 102).
$script:AppFolder = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$PUBLIC_ANTHROPIC = 'https://api.anthropic.com'

function Write-Status {
    param([Parameter(Mandatory, Position = 0)][AllowEmptyString()][string]$Message,
          [ValidateSet('Info', 'Detail', 'Warn', 'Error', 'Done')][string]$Level = 'Info')
    switch ($Level) {
        'Detail' { Write-Host "    $Message" }
        'Warn'   { Write-Warning $Message }
        'Error'  { Write-Host "==> $Message" -ForegroundColor Red }
        'Done'   { Write-Host "==> $Message" -ForegroundColor Green }
        default  { Write-Host "==> $Message" -ForegroundColor Cyan }
    }
}

function Read-JsonFile {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $raw = [string](Get-Content -LiteralPath $Path -Raw -Encoding UTF8)
    if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
    try { return ($raw | ConvertFrom-Json) } catch { return $null }
}

function Write-JsonFile {
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)]$Object)
    if ($PSCmdlet.ShouldProcess($Path, 'Write JSON')) {
        $backup = "$Path.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Copy-Item -LiteralPath $Path -Destination $backup -ErrorAction SilentlyContinue
        [IO.File]::WriteAllText($Path, ($Object | ConvertTo-Json -Depth 20),
                                (New-Object System.Text.UTF8Encoding($false)))
    }
}

function Set-JsonProperty {
    param([Parameter(Mandatory)]$Object, [Parameter(Mandatory)][string]$Name, $Value)
    if ($Object.PSObject.Properties[$Name]) { $Object.$Name = $Value }
    else { $Object | Add-Member -MemberType NoteProperty -Name $Name -Value $Value }
}

function Get-VsCodeSettingsFiles {
    <# Every settings.json an installer can write: the installed editors and the Remote-SSH servers. #>
    $paths = @(
        (Join-Path $env:APPDATA 'Code\User\settings.json'),
        (Join-Path $env:APPDATA 'Code - Insiders\User\settings.json')
    )
    foreach ($server in '.vscode-server', '.vscode-server-insiders') {
        $data = Join-Path $env:USERPROFILE "$server\data\Machine\settings.json"
        if (Test-Path -LiteralPath $data) { $paths += $data }
        $user = Join-Path $env:USERPROFILE "$server\data\User\settings.json"
        if (Test-Path -LiteralPath $user) { $paths += $user }
    }
    return $paths | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -Unique
}

function Set-RoutingOff {
    <# Both base-URL settings back at the public endpoint. Returns how many files changed. #>
    [CmdletBinding(SupportsShouldProcess)]
    param()
    $changed = 0

    $settingsPath = Join-Path $env:USERPROFILE '.claude\settings.json'
    $settings = Read-JsonFile -Path $settingsPath
    if ($settings -and $settings.PSObject.Properties['env'] -and
        $settings.env.PSObject.Properties['ANTHROPIC_BASE_URL']) {
        if ([string]$settings.env.ANTHROPIC_BASE_URL -eq $PUBLIC_ANTHROPIC) {
            Write-Status "Claude Code already points at $PUBLIC_ANTHROPIC" -Level Detail
        } elseif ($PSCmdlet.ShouldProcess($settingsPath, "Point ANTHROPIC_BASE_URL at $PUBLIC_ANTHROPIC")) {
            Set-JsonProperty -Object $settings.env -Name 'ANTHROPIC_BASE_URL' -Value $PUBLIC_ANTHROPIC
            Write-JsonFile -Path $settingsPath -Object $settings
            Write-Status "Claude Code: ANTHROPIC_BASE_URL now $PUBLIC_ANTHROPIC" -Level Detail
            $changed++
        }
    } else {
        Write-Status 'Claude Code settings.json does not route through the proxy' -Level Detail
    }

    foreach ($vsPath in Get-VsCodeSettingsFiles) {
        $vs = Read-JsonFile -Path $vsPath
        if (-not $vs -or -not $vs.PSObject.Properties['claudeCode.environmentVariables']) { continue }
        $entries = @($vs.'claudeCode.environmentVariables')
        $current = $entries | Where-Object { $_.name -eq 'ANTHROPIC_BASE_URL' } | Select-Object -First 1
        if (-not $current) { continue }
        if ([string]$current.value -eq $PUBLIC_ANTHROPIC) {
            Write-Status "$vsPath already points at $PUBLIC_ANTHROPIC" -Level Detail
            continue
        }
        if ($PSCmdlet.ShouldProcess($vsPath, "Point ANTHROPIC_BASE_URL at $PUBLIC_ANTHROPIC")) {
            $vs.'claudeCode.environmentVariables' =
                @($entries | Where-Object { $_.name -ne 'ANTHROPIC_BASE_URL' }) +
                @([pscustomobject]@{ name = 'ANTHROPIC_BASE_URL'; value = $PUBLIC_ANTHROPIC })
            Write-JsonFile -Path $vsPath -Object $vs
            Write-Status "VS Code: ANTHROPIC_BASE_URL now $PUBLIC_ANTHROPIC in $vsPath" -Level Detail
            $changed++
        }
    }
    return $changed
}

$transcriptStarted = $false
if ($LogPath) { try { Start-Transcript -Path $LogPath -Append | Out-Null; $transcriptStarted = $true } catch { } }

try {
    Write-Status 'Pointing Claude Code and VS Code back at the real API'
    $changed = Set-RoutingOff
    if ($changed -gt 0) {
        Write-Status "Routing is off ($changed file(s) changed). Sessions follow a value that changes, so they should reach the API again without restarting." -Level Done
    } else {
        Write-Status 'Routing was already off; nothing to change.' -Level Done
    }
    Write-Status 'Everything else is untouched: the proxy, the mapping, the logs and the settings all stay.' -Level Detail
    Write-Status 'Switch it back on with .\Enable-AiCleaner.ps1 (which re-runs the readiness checks first).' -Level Detail
}
catch {
    Write-Status "Could not switch routing off: $($_.Exception.Message)" -Level Error
    Write-Error $_ -ErrorAction Continue
    exit 1
}
finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
