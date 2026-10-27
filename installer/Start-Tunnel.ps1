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
    # Another stored token (encrypted to this account, as the default is). Default:
    # %USERPROFILE%\.aicleaner\cloudflare-tunnel.dpapi.
    [string]$TokenPath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# The app folder: this script's own, or its parent when it sits in installer\ (feature 102).
$script:AppFolder = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }
if (-not $LogPath) { $LogPath = Join-Path $script:AppFolder 'logs\cloudflared.log' }

function Read-TunnelToken {
    <# The run token Enable-RemoteControl.ps1 stored, as plain text for the child's environment only. #>
    [OutputType([string])]
    param([Parameter(Mandatory)][string]$Path)
    Add-Type -AssemblyName System.Security
    $bytes = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($Path), $null, 'CurrentUser')
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

# One tunnel at a time (feature 107): with aicleaner started with the computer, aicleaner-tunnel-service runs this
# before anyone signs in, and the sign-in task aicleaner-tunnel runs it again at sign-in. The second finds the
# first and leaves it be.
# Only this account's: another user's cloudflared is not this tunnel, and must not stop it starting (security review).
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$already = @(Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" -ErrorAction SilentlyContinue |
    Where-Object { [string]$_.CommandLine -match '\stunnel\b.*\srun\b' } |
    Where-Object {
        $owner = Invoke-CimMethod -InputObject $_ -MethodName GetOwnerSid -ErrorAction SilentlyContinue
        $owner -and $owner.Sid -eq $me
    })
if ($already.Count -gt 0) {
    Write-Host "==> the tunnel is already running (cloudflared pid $($already[0].ProcessId)); not starting another" -ForegroundColor Green
    exit 0
}
# D215: from a sign-in session, Windows hides the command line of the start-up task's cloudflared (it runs in
# session 0), so the check above never saw it and a second copy started at every sign-in. The task itself is
# readable by this account: while it runs, the start-up copy owns the tunnel and this one stands down.
$mySession = (Get-Process -Id $PID).SessionId
if ($mySession -ne 0) {
    # Only the task this installer registers for this account: at the root, run as this user, running this script.
    # A task of the same name made by someone else must not keep this account's tunnel from starting (security review).
    $startupTunnel = @(Get-ScheduledTask -TaskName 'aicleaner-tunnel-service' -TaskPath '\' -ErrorAction SilentlyContinue)
    $ours = $false
    if ($startupTunnel.Count -eq 1) {
        $principal = [string]$startupTunnel[0].Principal.UserId
        try { $principalSid = (New-Object Security.Principal.NTAccount($principal)).Translate([Security.Principal.SecurityIdentifier]).Value }
        catch { $principalSid = '' }
        $runsTunnel = @($startupTunnel[0].Actions | Where-Object { "$($_.Execute) $($_.Arguments)" -match 'Start-Tunnel\.ps1' }).Count -gt 0
        $ours = ($principalSid -eq $me) -and $runsTunnel
    }
    if ($ours -and [string]$startupTunnel[0].State -eq 'Running') {
        Write-Host '==> the tunnel runs from the start-up task (aicleaner-tunnel-service); not starting another' -ForegroundColor Green
        exit 0
    }
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

$env:TUNNEL_TOKEN = Read-TunnelToken -Path $tokenPath
try {
    # cloudflared reads TUNNEL_TOKEN for `tunnel run`; the token never appears in the process list.
    & $exe tunnel --no-autoupdate --logfile $LogPath run
    exit $LASTEXITCODE
} finally {
    Remove-Item Env:\TUNNEL_TOKEN -ErrorAction SilentlyContinue
}
