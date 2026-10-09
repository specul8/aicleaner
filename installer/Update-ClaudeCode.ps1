<#
.SYNOPSIS
    Updates Claude Code on this machine, by hand, however it was installed.

.DESCRIPTION
    Claude Code keeps itself up to date: aicleaner no longer switches its auto-update off. It used
    to, by setting CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1, and registered a weekly task that ran
    this script; that variable also switched off the feature-flag evaluation Remote Control depends
    on, and it outlived the install. Both are gone. This script stays for updating on demand - after
    a proxy change, or on a machine that has been offline - and nothing schedules it.

    How it updates depends on how Claude Code was installed, which this works out:
      winget (Anthropic.ClaudeCode)   winget upgrade
      npm (a global install)          npm install -g @anthropic-ai/claude-code@latest
      the native build                claude update

    The version before and after is printed, and a line is appended to logs\claude-update.log. It
    never installs Claude Code: there is nothing for aicleaner to do on a machine without an agent,
    and choosing how to install one is not this script's business.

.PARAMETER LogPath
    Where to append the result. Default: logs\claude-update.log beside this script. '' to disable.

.EXAMPLE
    .\Update-ClaudeCode.ps1

.EXAMPLE
    .\Update-ClaudeCode.ps1 -WhatIf
    Say which command would run, and change nothing.

.NOTES
    Contact:       see contact-*.json stored alongside this script
    Last-modified: 2026-09-20
    Requires:      Windows PowerShell 5.1 or PowerShell 7+
#>

#Requires -Version 5.1

[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Low')]
param(
    [AllowEmptyString()][string]$LogPath = $(
        $dir = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }
        Join-Path $dir 'logs\claude-update.log'
    )
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

function Write-Line {
    <# One line per run, so a task nobody watches still leaves a trail. #>
    param([Parameter(Mandatory)][string]$Message)
    if (-not $LogPath) { return }
    $folder = Split-Path -Parent $LogPath
    if ($folder -and -not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
    Add-Content -LiteralPath $LogPath -Value ("{0}  {1}" -f (Get-Date -Format 's'), $Message) -Encoding UTF8
}

function Get-ClaudeVersion {
    <# The version string, or '' when Claude Code is not here. #>
    [OutputType([string])]
    param()
    $claude = Get-Command claude -ErrorAction SilentlyContinue
    if (-not $claude) { return '' }
    $savedEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $output = & $claude.Source --version 2>&1 } finally { $ErrorActionPreference = $savedEap }
    return (($output | ForEach-Object { "$_" }) -join ' ').Trim()
}

function Get-UpdateMethod {
    <#
    .SYNOPSIS
        How Claude Code got here, and the command that updates it: @{ Name; Exe; Arguments }.
    #>
    [CmdletBinding()]
    param()
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        $savedEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { $listed = & $winget.Source list --id Anthropic.ClaudeCode -e --disable-interactivity 2>&1 } finally { $ErrorActionPreference = $savedEap }
        if (($listed | Out-String) -match 'Anthropic\.ClaudeCode') {
            return @{ Name = 'winget'; Exe = $winget.Source
                      Arguments = @('upgrade', '--id', 'Anthropic.ClaudeCode', '-e', '--silent',
                                    '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity') }
        }
    }
    $claude = Get-Command claude -ErrorAction SilentlyContinue
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if ($claude -and $npm) {
        # A global npm install puts the shim beside npm itself; the native build does not.
        $npmFolder = Split-Path -Parent $npm.Source
        if ([IO.Path]::GetFullPath((Split-Path -Parent $claude.Source)).TrimEnd('\') -eq
            [IO.Path]::GetFullPath($npmFolder).TrimEnd('\')) {
            # Get-Command finds npm.ps1 first; the .cmd beside it is what a scheduled task can run.
            $exe = $npm.Source
            $cmd = Join-Path $npmFolder 'npm.cmd'
            if (Test-Path -LiteralPath $cmd) { $exe = $cmd }
            return @{ Name = 'npm'; Exe = $exe
                      Arguments = @('install', '-g', '@anthropic-ai/claude-code@latest') }
        }
    }
    if ($claude) {
        return @{ Name = 'native'; Exe = $claude.Source; Arguments = @('update') }
    }
    return $null
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

try {
    $before = Get-ClaudeVersion
    if (-not $before) {
        Write-Status 'Claude Code is not on this machine; nothing to update.' -Level Warn
        Write-Status 'Install it with winget install Anthropic.ClaudeCode, or npm install -g @anthropic-ai/claude-code' -Level Detail
        Write-Line 'skipped: Claude Code not installed'
        return
    }
    $method = Get-UpdateMethod
    if (-not $method) {
        Write-Status 'Could not work out how Claude Code was installed; update it by hand.' -Level Warn
        Write-Line 'skipped: update method unknown'
        return
    }
    Write-Status "Claude Code $before, installed with $($method.Name)"
    $command = "$($method.Exe) $($method.Arguments -join ' ')"
    if (-not $PSCmdlet.ShouldProcess('Claude Code', "Update with $($method.Name)")) {
        Write-Status "Would run: $command" -Level Detail
        return
    }
    $savedEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $output = & $method.Exe @($method.Arguments) 2>&1 } finally { $ErrorActionPreference = $savedEap }
    $exit = $LASTEXITCODE
    $after = Get-ClaudeVersion
    $tail = (($output | ForEach-Object { "$_" }) -join ' ').Trim()
    if ($tail.Length -gt 200) { $tail = $tail.Substring(0, 200) }
    if ($exit -ne 0 -and $before -eq $after) {
        Write-Status "Update failed (exit $exit): $tail" -Level Warn
        Write-Line "failed ($($method.Name), exit $exit): $tail"
        exit 1
    }
    if ($before -eq $after) {
        Write-Status "Already current ($after)" -Level Done
        Write-Line "no change ($($method.Name)): $after"
    } else {
        Write-Status "Updated: $before -> $after" -Level Done
        Write-Line "updated ($($method.Name)): $before -> $after"
    }
}
catch {
    Write-Status "Update failed: $($_.Exception.Message)" -Level Error
    Write-Line "failed: $($_.Exception.Message)"
    Write-Error $_ -ErrorAction Continue
    exit 1
}
