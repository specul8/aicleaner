<#
.SYNOPSIS
    Runs this machine's Cloudflare tunnel as the signed-in user (feature 104, part 2).

.DESCRIPTION
    Started at sign-in by the aicleaner-tunnel scheduled task, which Enable-RemoteControl.ps1
    registers instead of a Windows service, so remote access needs no administrator rights - and the
    tunnel runs exactly while the proxy it serves does, which also needs the user signed in.

    The tunnel's run token is read from %USERPROFILE%\.aicleaner\cloudflare-tunnel.dpapi, encrypted
    to this Windows account (DPAPI, CurrentUser), and handed to cloudflared in its environment
    (TUNNEL_TOKEN) - never on a command line, where any process on the machine could read it.

.PARAMETER LogPath
    Where cloudflared's own log goes. Default: logs\cloudflared.log in the app folder.

.EXAMPLE
    .\installer\Start-Tunnel.ps1

.NOTES
    Requires: Windows PowerShell 5.1 or PowerShell 7+; cloudflared (Enable-RemoteControl.ps1 installs it).
#>
[CmdletBinding()]
param(
    [AllowEmptyString()][string]$LogPath = '',
    # Feature 107: run as SYSTEM by the service task - the token is the machine-scoped copy Set-AiCleanerService.ps1
    # made (readable by SYSTEM and administrators only), not the one encrypted to the user's account.
    [string]$TokenPath = '',
    [ValidateSet('CurrentUser', 'LocalMachine')][string]$TokenScope = 'CurrentUser'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# The app folder: this script's own, or its parent when it sits in installer\ (feature 102).
$script:AppFolder = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }
if (-not $LogPath) { $LogPath = Join-Path $script:AppFolder 'logs\cloudflared.log' }

function Read-TunnelToken {
    <# The run token Enable-RemoteControl.ps1 stored, as plain text for the child's environment only. #>
    [OutputType([string])]
    param([Parameter(Mandatory)][string]$Path, [string]$Scope = 'CurrentUser')
    Add-Type -AssemblyName System.Security
    $bytes = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($Path), $null, $Scope)
    try { return [Text.Encoding]::UTF8.GetString($bytes) }
    finally { [Array]::Clear($bytes, 0, $bytes.Length) }
}

function Find-Cloudflared {
    <# cloudflared.exe on PATH, or where winget puts it. #>
    [OutputType([string])]
    param()
    $onPath = Get-Command 'cloudflared.exe' -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'cloudflared\cloudflared.exe'),
        (Join-Path $env:ProgramFiles 'cloudflared\cloudflared.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\cloudflared.exe')
    )
    foreach ($candidate in $candidates) { if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate } }
    return ''
}

$tokenPath = if ($TokenPath) { $TokenPath } else { Join-Path $env:USERPROFILE '.aicleaner\cloudflare-tunnel.dpapi' }
if (-not (Test-Path -LiteralPath $tokenPath)) {
    Write-Error "No tunnel token at ${tokenPath}: run Enable-RemoteControl.ps1 to set remote access up."
    exit 1
}
$exe = Find-Cloudflared
if (-not $exe) {
    Write-Error 'cloudflared.exe was not found: run Enable-RemoteControl.ps1 again to install it.'
    exit 1
}
$folder = Split-Path -Parent $LogPath
if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }

$env:TUNNEL_TOKEN = Read-TunnelToken -Path $tokenPath -Scope $TokenScope
try {
    # cloudflared reads TUNNEL_TOKEN for `tunnel run`; the token never appears in the process list.
    & $exe tunnel --no-autoupdate --logfile $LogPath run
    exit $LASTEXITCODE
} finally {
    Remove-Item Env:\TUNNEL_TOKEN -ErrorAction SilentlyContinue
}
