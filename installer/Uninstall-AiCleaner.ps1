<#
.SYNOPSIS
    Removes the aicleaner wiring from this machine while keeping every data file.

.DESCRIPTION
    Undoes what Install-AiCleaner-VSCode.ps1 and Install-AiCleaner-Desktop.ps1 changed, so a reinstall
    starts clean. Removed:
      - the proxy process and the 'aicleaner-proxy', 'aicleaner-claude-backup' and
        'aicleaner-claude-update' scheduled tasks
      - remote control: the cloudflared service on this machine, and - with the API token
        Enable-RemoteControl.ps1 stored, encrypted to this Windows account - the Access application,
        the DNS record and the tunnel in your Cloudflare account. Only what that script created is
        touched; anything it merely reused is named in the closing steps instead. The token cannot
        delete itself, so the steps for that are printed and the stored copy is removed from here
      - CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC from %USERPROFILE%\.claude\settings.json
        (skipWebFetchPreflight is left; it is harmless without the proxy)
      - ANTHROPIC_BASE_URL is pointed back at https://api.anthropic.com rather than removed, in
        settings.json and in claudeCode.environmentVariables in every VS Code settings file the
        installer can write (the installed VS Code and Insiders, and the Remote-SSH servers under
        %USERPROFILE%\.vscode-server[-insiders], when the file is plain JSON). A session already
        running follows a value that changes and does not notice a key that disappears, so this
        is what lets work carry on without restarting anything. It is the public endpoint, which
        is where Claude Code goes anyway, and is not what disables Remote Control - that is a host
        OTHER than api.anthropic.com.
        This is the FIRST thing done, before the proxy is stopped and before the Cloudflare
        teardown, which makes Access and tunnel calls with up to six retries. Done last, everything
        between was dead air: the proxy gone and the settings still pointed at it. Done first, the
        break in service is the moment that flip takes.
      - the aicleaner block from %USERPROFILE%\.claude\CLAUDE.md
      - the aicleaner values under HKCU\SOFTWARE\Policies\Claude (Claude Desktop returns to its own mode chooser;
        needs an elevated PowerShell run as the same user, otherwise they are reported as left in place)
      - the PowerToys Keyboard Manager remap of the screenshot keys (PowerToys itself stays installed)
      - the Start Menu shortcut for the screen-text tool (Ctrl+Alt+T)
      - the DO_NOT_TRACK and CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC user variables, whether or not the
        change record survived: each switches off the feature-flag evaluation Claude Code's Remote Control
        depends on, so they must not outlive the install (a value other than '1' is somebody else's and is left)
      - what the installers recorded in %USERPROFILE%\.aicleaner\install-changes.json is put back:
        Desktop Commander and VS Code telemetry switches, PowerToys module switches
      - the shared venv at %USERPROFILE%\.aicleaner\venv

    NEVER removed: client-map.json, clients\*, _global.json, ignore-list.json, candidates-*.jsonl, logs\, aicleaner-config.json,
    the repo folder itself, and the secrets under %USERPROFILE%\.aicleaner\secrets (pass -RemoveSecrets to
    remove those). A reinstall therefore picks up the same mapping, ignore lists and API key.

    Every file it edits is backed up first (<file>.bak-yyyyMMdd-HHmmss). The Claude Desktop policy is
    saved as JSON in %USERPROFILE%\.aicleaner\backups (gateway token left out) before it is removed.

.PARAMETER RemoveSecrets
    Also delete %USERPROFILE%\.aicleaner\secrets (gateway token and DPAPI-encrypted API key).

.PARAMETER RemoveRemote
    Also offer to take remote access down: the tunnel task (or service) and stored tokens here, and -
    with the stored Cloudflare API token - the tunnel, its DNS record and the Access application in
    the account; the config's remote block is cleared. Without it remote access is left as it is, and
    a later install offers to keep it or replace it.

.PARAMETER RemoteOnly
    Take remote access down (as -RemoveRemote) and nothing else: aicleaner stays installed.

.PARAMETER RemovePowerToys
    Also uninstall PowerToys (winget). Default: leave it installed, only the remap is removed.

.PARAMETER RemoveFeedbackIdentity
    Also forget the feedback connection (%LOCALAPPDATA%\aicleaner-feedback). Default: kept, so a
    reinstall on this machine reconnects to the same tickets without asking again.

.PARAMETER Port
    Proxy port to stop. Default: the port in aicleaner-config.json, else 8787.

.PARAMETER LogPath
    Transcript log path. Defaults to Uninstall-AiCleaner.log next to the script. Pass '' to disable.

.EXAMPLE
    .\Uninstall-AiCleaner.ps1
    Remove all wiring, keep data and secrets.

.EXAMPLE
    .\Uninstall-AiCleaner.ps1 -RemoveSecrets -WhatIf
    Show what a full removal would do.

.NOTES
    Contact:       see contact-*.json stored alongside this script
    Last-modified: 2026-09-18
    Requires:      Windows PowerShell 5.1 or PowerShell 7+
#>

#Requires -Version 5.1

[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [switch]$RemoveSecrets,
    [switch]$RemoveRemote,
    [switch]$RemoteOnly,
    [switch]$RemovePowerToys,
    [switch]$RemoveFeedbackIdentity,
    [ValidateRange(1024, 65535)][int]$Port = 8787,
    [AllowEmptyString()][string]$LogPath = $(
        $base = if ($PSCommandPath) { [IO.Path]::GetFileNameWithoutExtension($PSCommandPath) } else { 'Uninstall-AiCleaner' }
        $dir = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }
        Join-Path $dir "$base.log"
    )
)
# The app folder: this script's own, or its parent when it sits in installer\ (feature 102).
$script:AppFolder = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Where Claude Code goes when nothing is in the way. Named explicitly on the way out, because a
# value that changes is noticed by a session already running and a key that disappears is not.
# Safe to name: Remote Control is refused for a host OTHER than this one.
$PUBLIC_ANTHROPIC = 'https://api.anthropic.com'

# --- Remote control teardown (Cloudflare tunnel, DNS, Access, the stored token) ------------
function Get-TokenStorePath {
    <# Where Enable-RemoteControl.ps1 kept the API token, encrypted to this Windows account. #>
    [OutputType([string])]
    param()
    return (Join-Path $env:USERPROFILE '.aicleaner\cloudflare-token.dpapi')
}

function Read-CloudflareToken {
    <# The stored token as plain text, or '' when there is none (or it cannot be read back). #>
    [OutputType([string])]
    param()
    $path = Get-TokenStorePath
    if (-not (Test-Path -LiteralPath $path)) { return '' }
    try {
        Add-Type -AssemblyName System.Security
        $protected = [IO.File]::ReadAllBytes($path)
        $bytes = [Security.Cryptography.ProtectedData]::Unprotect($protected, $null, 'CurrentUser')
        return [Text.Encoding]::UTF8.GetString($bytes)
    } catch {
        # Another account, or a restored profile: DPAPI will not give it back. Say so and go on.
        Write-Status "The stored Cloudflare token could not be read back on this account: $($_.Exception.Message)" -Level Warn
        return ''
    }
}

function Invoke-CloudflareApi {
    <# One Cloudflare API call. Failures are returned, not thrown: teardown continues regardless. #>
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)][string]$Token,
        [Parameter(Mandatory)][string]$Path,
        [string]$Method = 'GET'
    )
    try {
        $response = Invoke-RestMethod -Uri "https://api.cloudflare.com/client/v4$Path" -Method $Method `
            -Headers @{ Authorization = "Bearer $Token" } -ContentType 'application/json' -ErrorAction Stop
        if ($response.PSObject.Properties['success'] -and -not $response.success) {
            $why = @($response.errors | ForEach-Object { "$($_.code): $($_.message)" }) -join '; '
            return [pscustomobject]@{ Ok = $false; Why = $why; Result = $null }
        }
        return [pscustomobject]@{ Ok = $true; Why = ''; Result = $response.result }
    } catch {
        return [pscustomobject]@{ Ok = $false; Why = $_.Exception.Message; Result = $null }
    }
}

function Show-ManualTunnelSteps {
    <# What to click when the token is gone, or an API call was refused. #>
    param([string]$PublicHostname = '', [string]$TunnelName = '', [switch]$TokenOnly)
    Write-Status 'Finish in the Cloudflare dashboard (https://dash.cloudflare.com):' -Level Warn
    if (-not $TokenOnly) {
        if ($PublicHostname) {
            Write-Status "  Zero Trust > Access > Applications: delete the application for $PublicHostname" -Level Detail
            Write-Status "  Your domain > DNS > Records: delete the CNAME for $PublicHostname" -Level Detail
        } else {
            Write-Status '  Zero Trust > Access > Applications: delete the aicleaner application' -Level Detail
            Write-Status '  Your domain > DNS > Records: delete the CNAME that points at the tunnel' -Level Detail
        }
        $named = if ($TunnelName) { " named $TunnelName" } else { '' }
        Write-Status "  Zero Trust > Networks > Tunnels: delete the tunnel$named" -Level Detail
    }
    Write-Status '  https://dash.cloudflare.com/profile/api-tokens: delete the aicleaner API token' -Level Detail
}

function Clear-RemoteConfig {
    <#
    .SYNOPSIS
        Forgets the remote connection in aicleaner-config.json once it has been taken down.
    .DESCRIPTION
        The config is the owner's data and survives an uninstall; left holding the old hostname and
        tunnel ids, a reinstall adopted a connection that no longer existed. Remote access is turned
        off and the ids and hostname removed; the rest of the config is untouched (backed up first).
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Root)
    $path = Join-Path $Root 'aicleaner-config.json'
    $config = Read-JsonFile -Path $path
    if (-not $config -or -not $config.PSObject.Properties['remote']) { return }
    if (-not $PSCmdlet.ShouldProcess($path, 'Forget the remote connection')) { return }
    Backup-File -Path $path
    foreach ($name in 'hostname', 'tunnel_id', 'tunnel_name', 'account_id', 'zone_id', 'access_app_id', 'dns_record_id',
                      'access_app_mine', 'dns_record_mine', 'audience', 'team_domain', 'idp_id') {
        $config.remote.PSObject.Properties.Remove($name)
    }
    Set-JsonProperty -Object $config.remote -Name 'enabled' -Value $false
    Write-JsonFile -Path $path -Object $config
    Write-Status 'The remote connection is forgotten in aicleaner-config.json' -Level Detail
}

function Invoke-RemoteRemoval {
    <# Offers to take remote access down when asked; says what is left when not. #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Root, [switch]$Asked)
    $config = Read-JsonFile -Path (Join-Path $Root 'aicleaner-config.json')
    $hostname = if ($config -and $config.PSObject.Properties['remote'] -and $config.remote.PSObject.Properties['hostname']) { [string]$config.remote.hostname } else { '' }
    $here = $hostname -or (Get-ScheduledTask -TaskName 'aicleaner-tunnel' -ErrorAction SilentlyContinue) -or (Get-Service -Name 'Cloudflared' -ErrorAction SilentlyContinue)
    if (-not $here) { return }
    $name = if ($hostname) { "https://$hostname" } else { 'the remote connection' }
    if (-not $Asked) {
        Write-Status "Remote access kept ($name); -RemoveRemote takes it down" -Level Detail
        return
    }
    if (Confirm-Action -Message "Take remote access down ($name): the tunnel here, and its tunnel, DNS record and Access application at Cloudflare?" -Default 'Yes') {
        Remove-RemoteControl -Root $Root
    } else { Write-Status "Remote access kept ($name)" -Level Detail }
}

function Remove-RemoteControl {
    <#
    .SYNOPSIS
        Takes down what Enable-RemoteControl.ps1 put up: the connector on this machine, the Access
        application, the DNS record and the tunnel itself.
    .DESCRIPTION
        Reversed in the order that keeps the account tidy at every step: the connector first, so the
        tunnel has no live connections; then Access, so the hostname is never briefly reachable with
        nothing in front of it; then DNS; then the tunnel. Only ids the installer recorded as its
        own are used - a hostname that already existed, or an Access application somebody else made,
        is left alone and named in the closing steps instead.

        The stored token cannot delete itself: that needs User > API Tokens > Edit, which would let
        anyone holding the file mint and revoke tokens across the account. The two clicks for it are
        printed at the end, and the stored copy is removed from this machine either way.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Root)

    $config = Read-JsonFile -Path (Join-Path $Root 'aicleaner-config.json')
    $remote = if ($config -and $config.PSObject.Properties['remote']) { $config.remote } else { $null }
    $tokenPath = Get-TokenStorePath
    $hostname = if ($remote -and $remote.PSObject.Properties['hostname']) { [string]$remote.hostname } else { '' }
    $tunnelName = if ($remote -and $remote.PSObject.Properties['tunnel_name']) { [string]$remote.tunnel_name } else { '' }
    $service = Get-Service -Name 'Cloudflared' -ErrorAction SilentlyContinue
    # The per-user tunnel task and its token (feature 104, part 2).
    $tunnelTask = Get-ScheduledTask -TaskName 'aicleaner-tunnel' -ErrorAction SilentlyContinue
    $tunnelToken = Join-Path $env:USERPROFILE '.aicleaner\cloudflare-tunnel.dpapi'
    if ($tunnelTask -and $PSCmdlet.ShouldProcess('aicleaner-tunnel', 'Stop and remove the tunnel task')) {
        Stop-ScheduledTask -TaskName 'aicleaner-tunnel' -ErrorAction SilentlyContinue
        Get-Process -Name 'cloudflared' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName 'aicleaner-tunnel' -Confirm:$false
        Write-Status 'Tunnel task removed' -Level Detail
    }
    if ((Test-Path -LiteralPath $tunnelToken) -and $PSCmdlet.ShouldProcess($tunnelToken, 'Remove the stored tunnel token')) {
        Remove-Item -LiteralPath $tunnelToken -Force
    }
    if (-not $remote -and -not $service -and -not $tunnelTask -and -not (Test-Path -LiteralPath $tokenPath)) {
        Write-Status 'No remote control was set up on this machine' -Level Detail
        return
    }

    # --- the connector on this machine ------------------------------------------------------
    if ($service) {
        if ($PSCmdlet.ShouldProcess('cloudflared', 'Stop and uninstall the tunnel service')) {
            try { Stop-Service -Name 'Cloudflared' -Force -ErrorAction Stop; Write-Status 'cloudflared service stopped' -Level Detail }
            catch { Write-Status "cloudflared service would not stop: $($_.Exception.Message)" -Level Warn }
            $exe = Get-Command 'cloudflared.exe' -ErrorAction SilentlyContinue
            if ($exe) {
                $savedEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
                try { & $exe.Source service uninstall 2>&1 | ForEach-Object { Write-Status "$_" -Level Detail } }
                finally { $ErrorActionPreference = $savedEap }
                Write-Status 'cloudflared service uninstalled' -Level Detail
            } else {
                Write-Status 'cloudflared.exe is not on PATH: remove the service with "cloudflared service uninstall"' -Level Warn
            }
        }
    }

    $token = Read-CloudflareToken
    if (-not $token) {
        Write-Status 'No stored Cloudflare token: the tunnel and its Zero Trust settings stay in your account' -Level Warn
        Show-ManualTunnelSteps -PublicHostname $hostname -TunnelName $tunnelName
        return
    }

    $accountId = if ($remote -and $remote.PSObject.Properties['account_id']) { [string]$remote.account_id } else { '' }
    $zoneId = if ($remote -and $remote.PSObject.Properties['zone_id']) { [string]$remote.zone_id } else { '' }
    $tunnelId = if ($remote -and $remote.PSObject.Properties['tunnel_id']) { [string]$remote.tunnel_id } else { '' }
    $appId = if ($remote -and $remote.PSObject.Properties['access_app_id']) { [string]$remote.access_app_id } else { '' }
    $recordId = if ($remote -and $remote.PSObject.Properties['dns_record_id']) { [string]$remote.dns_record_id } else { '' }
    $appMine = if ($remote -and $remote.PSObject.Properties['access_app_mine']) { [bool]$remote.access_app_mine } else { $false }
    $recordMine = if ($remote -and $remote.PSObject.Properties['dns_record_mine']) { [bool]$remote.dns_record_mine } else { $false }
    $leftovers = @()

    # --- Access application (its policies go with it) ----------------------------------------
    if ($accountId -and $appId) {
        $app = (Invoke-CloudflareApi -Token $token -Path "/accounts/$accountId/access/apps/$appId").Result
        $name = if ($app -and $app.PSObject.Properties['name']) { [string]$app.name } else { '' }
        # The stamp on the object outranks the flag in the file: a run that created this and died
        # before recording it still left its name behind, and that is what says whose it is.
        $stamped = $name -eq "aicleaner ($hostname)"
        Write-Status "Access application: $(if ($name) { $name } else { $appId })" -Level Detail
        Write-Status "  $(if ($stamped -or $appMine) { 'this install created it' } else { 'it was already there; this install only used it' })" -Level Detail
        if (Confirm-Action -Message '  Delete it from Cloudflare?' -Default $(if ($stamped -or $appMine) { 'Yes' } else { 'No' })) {
            if ($PSCmdlet.ShouldProcess("Access application $appId", 'Delete')) {
                $gone = Invoke-CloudflareApi -Token $token -Path "/accounts/$accountId/access/apps/$appId" -Method 'DELETE'
                if ($gone.Ok) { Write-Status 'Access application deleted' -Level Detail }
                else { Write-Status "Access application not deleted: $($gone.Why)" -Level Warn; $leftovers += 'Access application' }
            }
        } else { Write-Status '  Left in place' -Level Detail; $leftovers += 'Access application' }
    } elseif ($hostname) {
        Write-Status "No Access application is recorded for $hostname; left as it is" -Level Detail
    }

    # --- the DNS record ------------------------------------------------------------------------
    if ($zoneId -and $recordId) {
        $record = (Invoke-CloudflareApi -Token $token -Path "/zones/$zoneId/dns_records/$recordId").Result
        $comment = if ($record -and $record.PSObject.Properties['comment']) { [string]$record.comment } else { '' }
        $stamped = $comment -eq 'aicleaner remote control'
        Write-Status "DNS record: $hostname -> $(if ($record) { $record.content } else { $recordId })" -Level Detail
        Write-Status "  $(if ($stamped -or $recordMine) { 'this install created it' } else { 'it pointed somewhere else before this install and was repointed' })" -Level Detail
        if (-not ($stamped -or $recordMine)) {
            Write-Status '  Deleting it removes a name that was in use before this was installed.' -Level Warn
        }
        if (Confirm-Action -Message '  Delete it from Cloudflare?' -Default $(if ($stamped -or $recordMine) { 'Yes' } else { 'No' })) {
            if ($PSCmdlet.ShouldProcess("DNS record $hostname", 'Delete')) {
                $gone = Invoke-CloudflareApi -Token $token -Path "/zones/$zoneId/dns_records/$recordId" -Method 'DELETE'
                if ($gone.Ok) { Write-Status "DNS record for $hostname deleted" -Level Detail }
                else { Write-Status "DNS record not deleted: $($gone.Why)" -Level Warn; $leftovers += 'DNS record' }
            }
        } else { Write-Status '  Left in place' -Level Detail; $leftovers += 'DNS record' }
    } elseif ($hostname) {
        Write-Status "No DNS record is recorded for $hostname; left as it is" -Level Warn
    }

    # --- the tunnel: its connections first, or Cloudflare refuses while any are tracked --------
    if ($accountId -and $tunnelId) {
        if ($PSCmdlet.ShouldProcess("tunnel $tunnelId", 'Delete')) {
            Invoke-CloudflareApi -Token $token -Path "/accounts/$accountId/cfd_tunnel/$tunnelId/connections" -Method 'DELETE' | Out-Null
            $gone = $null
            foreach ($attempt in 1..6) {
                $gone = Invoke-CloudflareApi -Token $token -Path "/accounts/$accountId/cfd_tunnel/$tunnelId" -Method 'DELETE'
                if ($gone.Ok) { break }
                # Cloudflare keeps tracking a connection for a short while after it goes, to make
                # reconnecting quick; the tunnel cannot be deleted until that clears.
                Start-Sleep -Seconds 5
            }
            if ($gone.Ok) { Write-Status 'Tunnel deleted' -Level Detail }
            else { Write-Status "Tunnel not deleted: $($gone.Why)" -Level Warn; $leftovers += 'tunnel' }
        }
    }

    # --- the stored token ----------------------------------------------------------------------
    if ((Test-Path -LiteralPath $tokenPath) -and $PSCmdlet.ShouldProcess($tokenPath, 'Remove the stored Cloudflare token')) {
        Remove-Item -LiteralPath $tokenPath -Force
        Write-Status 'Stored Cloudflare token removed from this machine' -Level Detail
    }
    if ($leftovers.Count) {
        Write-Status ("Left in your account: {0}" -f ($leftovers -join ', ')) -Level Warn
        Show-ManualTunnelSteps -PublicHostname $hostname -TunnelName $tunnelName
    } else {
        Write-Status 'Tunnel, DNS and Access application removed from your Cloudflare account' -Level Done
        Clear-RemoteConfig -Root $Root
        Write-Status 'The API token itself cannot delete itself (that needs a far broader permission):' -Level Warn
        Show-ManualTunnelSteps -TokenOnly
    }
}

# --- Contact attribution (inlined per script so each script stays self-contained) ---------
function Get-LedgerPath {
    <# install-changes.json: what the installers changed, so this script can put it back. #>
    return (Join-Path $env:USERPROFILE '.aicleaner\install-changes.json')
}

function Get-LedgerSection {
    <#
    .SYNOPSIS
        One section of install-changes.json, or $null when there is no record or no such section.
        A function, so every step can read the ledger: the PowerToys remap step used a script block
        defined inside Restore-InstallChanges, and under StrictMode the uninstall stopped there
        ("The variable '$section' cannot be retrieved", 2026-10-16).
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Name)
    $ledgerPath = Get-LedgerPath
    if (-not (Test-Path -LiteralPath $ledgerPath)) { return $null }
    try { $ledger = Read-JsonFile -Path $ledgerPath } catch { return $null }
    if ($null -ne $ledger -and $ledger.PSObject.Properties[$Name]) { return $ledger.$Name }
    return $null
}

function Restore-JsonSection {
    <#
    .SYNOPSIS
        Puts recorded previous values back into a JSON settings file. Names are paths with '/'
        between levels; a $null previous value means the setting did not exist and is removed.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Path, [AllowNull()]$Values, [Parameter(Mandatory)][string]$Label)
    if ($null -eq $Values -or -not (Test-Path -LiteralPath $Path)) { return }
    try { $doc = Read-JsonFile -Path $Path }
    catch { Write-Status "${Label}: $Path is not plain JSON; restore it by hand" -Level Warn; return }
    foreach ($entry in @($Values.PSObject.Properties)) {
        $parts = $entry.Name.Split('/')
        $node = $doc
        for ($level = 0; $level -lt $parts.Count - 1; $level++) {
            if (-not $node.PSObject.Properties[$parts[$level]]) { Set-JsonProperty -Object $node -Name $parts[$level] -Value ([pscustomobject]@{}) }
            $node = $node.($parts[$level])
        }
        $leaf = $parts[$parts.Count - 1]
        if ($null -eq $entry.Value) { if ($node.PSObject.Properties[$leaf]) { $node.PSObject.Properties.Remove($leaf) } }
        else { Set-JsonProperty -Object $node -Name $leaf -Value $entry.Value }
    }
    Backup-File -Path $Path
    Write-JsonFile -Path $Path -Object $doc
    Write-Status "${Label}: previous settings restored" -Level Detail
}

function Restore-InstallChanges {
    <#
    .SYNOPSIS
        Undoes what the installers recorded in install-changes.json: user environment variables,
        Desktop Commander and VS Code telemetry switches, PowerToys module switches. Returns $false
        when there is no record (older installs), in which case those settings are left alone.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    [OutputType([bool])]
    param()
    $ledgerPath = Get-LedgerPath
    $ledger = $null
    try { $ledger = Read-JsonFile -Path $ledgerPath } catch { Write-Status "$ledgerPath is unreadable" -Level Warn }
    if ($null -eq $ledger) { return $false }
    $section = { param($name) if ($ledger.PSObject.Properties[$name]) { $ledger.$name } else { $null } }
    $userEnv = & $section 'user_env'
    if ($userEnv) {
        foreach ($entry in @($userEnv.PSObject.Properties)) {
            if (-not $PSCmdlet.ShouldProcess($entry.Name, 'Restore user environment variable')) { continue }
            if ($null -eq $entry.Value) {
                [Environment]::SetEnvironmentVariable($entry.Name, $null, 'User'); Write-Status "Removed user variable $($entry.Name)" -Level Detail
            } else {
                [Environment]::SetEnvironmentVariable($entry.Name, [string]$entry.Value, 'User'); Write-Status "Restored user variable $($entry.Name)" -Level Detail
            }
        }
    }
    Restore-JsonSection -Path (Join-Path $env:USERPROFILE '.claude-server-commander\config.json') -Values (& $section 'desktop_commander') -Label 'Desktop Commander'
    Restore-JsonSection -Path (Join-Path $env:APPDATA 'Code\User\settings.json') -Values (& $section 'vscode') -Label 'VS Code'
    # A server on this machine keeps settings of its own: what a Remote-SSH window reads from here.
    foreach ($serverRoot in '.vscode-server', '.vscode-server-insiders') {
        $serverSettings = Join-Path $env:USERPROFILE ($serverRoot + '\data\Machine\settings.json')
        if (Test-Path -LiteralPath $serverSettings) {
            Restore-JsonSection -Path $serverSettings -Values (& $section 'vscode_server') -Label 'VS Code Server'
        }
    }
    Restore-JsonSection -Path (Join-Path $env:LOCALAPPDATA 'Microsoft\PowerToys\settings.json') -Values (& $section 'powertoys') -Label 'PowerToys'
    $otel = & $section 'otel_removed'
    if ($otel) {
        Write-Status ("OpenTelemetry variables removed at install (values were not kept): " + (@($otel.PSObject.Properties.Name) -join ', ') + '. Re-add them by hand if you still need them.') -Level Detail
    }
    # Print Screen opening Snipping Tool (2026-10-12): back to what it was, or the setting gone.
    $keyboard = & $section 'windows_keyboard'
    if ($keyboard -and $keyboard.PSObject.Properties['PrintScreenKeyForSnippingEnabled'] -and
        $PSCmdlet.ShouldProcess('HKCU:\Control Panel\Keyboard', 'Put the Print Screen key back')) {
        $was = $keyboard.PrintScreenKeyForSnippingEnabled
        if ($null -eq $was) { if ((Get-ItemProperty -LiteralPath 'HKCU:\Control Panel\Keyboard').PSObject.Properties['PrintScreenKeyForSnippingEnabled']) { Remove-ItemProperty -LiteralPath 'HKCU:\Control Panel\Keyboard' -Name 'PrintScreenKeyForSnippingEnabled' } }
        else { Set-ItemProperty -LiteralPath 'HKCU:\Control Panel\Keyboard' -Name 'PrintScreenKeyForSnippingEnabled' -Value ([int]$was) -Type DWord }
        Write-Status 'Print Screen key put back the way it was' -Level Detail
    }
    $tesseract = & $section 'tesseract_install'
    if ($tesseract -and $tesseract.PSObject.Properties['installed_by_aicleaner'] -and [bool]$tesseract.installed_by_aicleaner) {
        Write-Status 'Tesseract was installed by aicleaner and is left in place; remove it with: winget uninstall UB-Mannheim.TesseractOCR' -Level Detail
    }
    $install = & $section 'powertoys_install'
    if ($install -and $install.PSObject.Properties['installed_by_aicleaner'] -and [bool]$install.installed_by_aicleaner -and -not $RemovePowerToys) {
        Write-Status 'PowerToys was installed by aicleaner; run again with -RemovePowerToys to uninstall it' -Level Detail
    }
    if ($PSCmdlet.ShouldProcess($ledgerPath, 'Remove the change record')) { Remove-Item -LiteralPath $ledgerPath -Force }
    return $true
}

function Stop-AiCleanerProcess {
    <#
    .SYNOPSIS
        Stops the proxy: the python process listening on the port and any python running from an
        aicleaner venv (a venv's python.exe is a launcher with a child interpreter). Waits up to 10
        seconds; returns $true when none is left running.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    [OutputType([bool])]
    param([Parameter(Mandatory)][int]$ProxyPort, [string[]]$VenvFolders = @())
    $targets = @{}
    foreach ($conn in @(Get-NetTCPConnection -LocalPort $ProxyPort -State Listen -ErrorAction SilentlyContinue)) {
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -like 'python*') { $targets[$proc.Id] = $proc }
    }
    foreach ($proc in @(Get-Process -Name 'python*' -ErrorAction SilentlyContinue)) {
        $exePath = $null
        try { $exePath = $proc.Path } catch { $exePath = $null }
        foreach ($folder in $VenvFolders) {
            if ($exePath -and $folder -and $exePath.StartsWith($folder.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { $targets[$proc.Id] = $proc }
        }
    }
    if ($targets.Count -eq 0) { return $true }
    $ids = @($targets.Keys)
    if (-not $PSCmdlet.ShouldProcess("pid $($ids -join ', ')", 'Stop the proxy')) { return $true }
    foreach ($id in $ids) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    Wait-Process -Id $ids -Timeout 10 -ErrorAction SilentlyContinue
    $left = @($ids | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
    if ($left.Count -gt 0) { Write-Status "Proxy process(es) still running: $($left -join ', ')" -Level Warn; return $false }
    Write-Status 'Proxy stopped' -Level Detail
    return $true
}

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
    param([Parameter(Mandatory)][string]$Message, [ValidateSet('Yes', 'No')][string]$Default = 'No')
    $suffix = if ($Default -eq 'Yes') { '[Y/n]' } else { '[y/N]' }
    # Nobody there to answer - run from a script, stdin at end of file - is the default, not a
    # crash and not a hang. This gates deleting things in somebody's Cloudflare account, so an
    # unattended run has to land somewhere predictable rather than throwing halfway through.
    try { $response = Read-Host "$Message $suffix" }
    catch { Write-Status "  (nothing to read an answer from; taking $Default)" -Level Detail; return ($Default -eq 'Yes') }
    if ([string]::IsNullOrWhiteSpace($response)) { return ($Default -eq 'Yes') }
    return $response -match '^(y|yes)$'
}

function Backup-File {
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $backup = "$Path.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    if ($PSCmdlet.ShouldProcess($Path, "Back up to $backup")) { Copy-Item -LiteralPath $Path -Destination $backup; Write-Status "Backed up to $backup" -Level Detail }
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

function Read-JsonFile { param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $raw = [string](Get-Content -LiteralPath $Path -Raw -Encoding UTF8)
    if ([string]::IsNullOrWhiteSpace($raw)) { return [pscustomobject]@{} }
    if (Test-JsonComment -Text $raw) { throw "$Path contains comments (JSONC); it is left unchanged so they are not lost" }
    return ($raw | ConvertFrom-Json)
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

function Test-SourceCheckout {
    <#
    .SYNOPSIS
        True when Folder is a git working copy: somewhere code is written, not somewhere it is installed.
    .DESCRIPTION
        A deployment is copied files with no .git. A checkout is where someone develops, and its
        .venv is their development environment, not something an installer put there. Run from a
        checkout, this script used to offer "Remove the repo-local venv" with Yes as the default,
        and the owner's development venv went with it.
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param([Parameter(Mandatory)][string]$Folder)
    return [bool](Test-Path -LiteralPath (Join-Path $Folder '.git'))
}

function Get-OldInstallVenv {
    <#
    .SYNOPSIS
        The venv an early install kept beside its code, or '' when there is none this script may touch.
    .DESCRIPTION
        Installs now keep the venv in %USERPROFILE%\.aicleaner\venv. An install from before that
        kept one in the app folder, and that is the only .venv this looks for - never one inside a
        source checkout.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][string]$Root)
    if (Test-SourceCheckout -Folder $Root) { return '' }
    $candidate = Join-Path $Root '.venv'
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    return ''
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

function Set-JsonProperty {
    # PSCustomObject-friendly upsert (Add-Member fails on existing names).
    param([Parameter(Mandatory)]$Object, [Parameter(Mandatory)][string]$Name, $Value)
    if ($Object.PSObject.Properties[$Name]) { $Object.$Name = $Value }
    else { $Object | Add-Member -MemberType NoteProperty -Name $Name -Value $Value }
}

function Write-JsonFile {
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)]$Object)
    if ($PSCmdlet.ShouldProcess($Path, 'Write JSON')) {
        [IO.File]::WriteAllText($Path, ($Object | ConvertTo-Json -Depth 20), (New-Object System.Text.UTF8Encoding($false)))
    }
}

$transcriptStarted = $false
if ($LogPath) {
    try { Start-Transcript -Path $LogPath -Force | Out-Null; $transcriptStarted = $true }
    catch { Write-Status "Could not start transcript at ${LogPath}: $($_.Exception.Message)" -Level Warn }
}

try {
    $root = $script:AppFolder
    if ($RemoteOnly) {
        # Remote access alone (a reinstall's "replace", or by hand): aicleaner stays as it is.
        Invoke-RemoteRemoval -Root $root -Asked
        Write-Status 'Remote access handled; nothing else was changed' -Level Done
        return
    }
    Write-Status "aicleaner uninstall (data files are kept) from $root"
    $isCheckout = Test-SourceCheckout -Folder $root
    if ($isCheckout) {
        # Everything this removes lives outside this folder. Nothing inside a checkout is ours.
        Write-Status "$root is a source checkout: this removes what was installed on this machine, and nothing in this folder is touched" -Level Warn
    }

    # --- 1. Point both base-URL settings back at the public endpoint -------------------
    # First, before anything is stopped or torn down. A running session follows a value that
    # CHANGES the moment it changes, so flipping these two puts every session back on the real
    # API immediately - and the rest of the uninstall can then take as long as it likes.
    #
    # It used to run after the proxy was stopped and after the Cloudflare teardown, which makes
    # Access and tunnel calls with up to six retries. Everything between those two points was
    # dead air: the proxy gone, the settings still pointed at it. Now it is a blip.

    # --- 1a. Claude Code settings.json -----------------------------------------------
    $settingsPath = Join-Path $env:USERPROFILE '.claude\settings.json'
    $settings = Read-JsonFile -Path $settingsPath
    if ($settings -and $settings.PSObject.Properties['env']) {
        $ours = @('ANTHROPIC_BASE_URL', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC', 'DISABLE_TELEMETRY', 'DISABLE_ERROR_REPORTING', 'ENABLE_TOOL_SEARCH') | Where-Object { $settings.env.PSObject.Properties[$_] }
        if ($ours -and $PSCmdlet.ShouldProcess($settingsPath, "Remove env $($ours -join ', ')")) {
            Backup-File -Path $settingsPath
            # Pointed home rather than taken away. A session already running follows a value that
            # changes - that is why installing stops work the instant a faulty proxy is in the
            # path - and does not notice a key that disappears, which is why an uninstall used to
            # mean restarting everything first. Setting it back is the whole fix, and it is one
            # write with nothing to observe in passing.
            #
            # Safe to name: Remote Control is refused for a host OTHER than api.anthropic.com, and
            # this is where Claude Code goes when nothing is in the way. It is still a value we
            # left behind, so the summary says so rather than leaving it to be discovered.
            $homeAgain = $false
            if ($settings.env.PSObject.Properties['ANTHROPIC_BASE_URL']) {
                Set-JsonProperty -Object $settings.env -Name 'ANTHROPIC_BASE_URL' -Value $PUBLIC_ANTHROPIC
                $homeAgain = $true
            }
            # These go entirely: the traffic switch turns off the feature-flag evaluation Remote
            # Control needs, and the telemetry switches are aicleaner's choice, not the owner's -
            # an uninstall hands Claude Code back with its own defaults.
            foreach ($k in @($ours | Where-Object { $_ -ne 'ANTHROPIC_BASE_URL' })) {
                $settings.env.PSObject.Properties.Remove($k)
            }
            Write-JsonFile -Path $settingsPath -Object $settings
            if ($homeAgain) {
                Write-Status "Claude Code settings: ANTHROPIC_BASE_URL now $PUBLIC_ANTHROPIC (sessions recover without a restart)" -Level Detail
            }
            $gone = @($ours | Where-Object { $_ -ne 'ANTHROPIC_BASE_URL' })
            if ($gone) { Write-Status "Claude Code settings: removed $($gone -join ', ')" -Level Detail }
        }
    }

    # --- 1a-ii. The terminal-question hook (feature 79) ------------------------------
    # Added from the Session page when asked for. Left behind it would run a Python that is no
    # longer there before every permission prompt. Only our entry goes; anyone else's stay.
    $settings = Read-JsonFile -Path $settingsPath
    if ($settings -and $settings.PSObject.Properties['hooks'] -and $settings.hooks.PSObject.Properties['PermissionRequest']) {
        $entries = @($settings.hooks.PermissionRequest)
        $kept = @($entries | Where-Object { -not (@($_.hooks) | Where-Object { [string]$_.command -like '*hook_ask.py*' }) })
        if ($kept.Count -lt $entries.Count -and $PSCmdlet.ShouldProcess($settingsPath, 'Remove the aicleaner terminal-question hook')) {
            Backup-File -Path $settingsPath
            if ($kept.Count -gt 0) { $settings.hooks.PermissionRequest = $kept }
            else { $settings.hooks.PSObject.Properties.Remove('PermissionRequest') }
            if (@($settings.hooks.PSObject.Properties).Count -eq 0) { $settings.PSObject.Properties.Remove('hooks') }
            Write-JsonFile -Path $settingsPath -Object $settings
            Write-Status 'Claude Code settings: removed the terminal-question hook' -Level Detail
        }
    }

    # --- 1a-iii. The Code tab's settings and the Bash guard's hook (features 117, 98) -
    # Put there by the proxy's switches, so taken out by the same code (`aicleaner unwire-claude`),
    # which knows from its own record what it added and what was there before: the Code tab's proxy
    # settings, its SessionStart hook and MCP entries, its certificate authority, and the Bash guard's
    # PreToolUse hook. Left behind, Claude Code would send everything to a proxy that is gone, and run
    # hooks with a Python that is gone.
    $venvPython = Join-Path $env:USERPROFILE '.aicleaner\venv\Scripts\python.exe'
    $configFile = Join-Path $root 'aicleaner-config.json'
    if ((Test-Path -LiteralPath $venvPython) -and (Test-Path -LiteralPath $configFile) -and
        $PSCmdlet.ShouldProcess($settingsPath, 'Remove the Code tab settings and the Bash guard hook')) {
        $savedEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        Push-Location -LiteralPath $root
        try {
            & $venvPython -m aicleaner unwire-claude --config $configFile 2>&1 | ForEach-Object { Write-Status "Claude Code settings: $_" -Level Detail }
            if ($LASTEXITCODE -ne 0) { Write-Status 'Some Claude Code settings could not be put back; see the lines above' -Level Warn }
        } finally {
            Pop-Location
            $ErrorActionPreference = $savedEap
        }
    }

    # --- 1a-iv. GitHub Copilot in VS Code (Copilot release 6) ------------------------
    # VS Code's proxy setting put back from the proxy's own record, the Copilot certificate taken out of
    # the Windows certificates (Windows asks to confirm), and its files removed: `aicleaner copilot-off
    # --remove-cert`. Left behind, VS Code would send everything to a proxy that is gone.
    if ((Test-Path -LiteralPath $venvPython) -and (Test-Path -LiteralPath $configFile) -and
        $PSCmdlet.ShouldProcess('VS Code settings and the Copilot certificate', 'Remove what the Copilot switch added')) {
        $savedEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        Push-Location -LiteralPath $root
        try {
            Write-Status 'If Windows asks whether to delete the "aicleaner Copilot" certificate, choose Yes.' -Level Detail
            & $venvPython -m aicleaner copilot-off --remove-cert --config $configFile 2>&1 | ForEach-Object { Write-Status "Copilot: $_" -Level Detail }
            if ($LASTEXITCODE -ne 0) { Write-Status 'Copilot''s settings could not all be put back; see the lines above' -Level Warn }
        } finally {
            Pop-Location
            $ErrorActionPreference = $savedEap
        }
    }

    # --- 1a-v. Claude Desktop's chat (route B) -----------------------------------------
    # The Windows proxy setting put back from the proxy's own record, the claude.ai certificate taken out of the
    # Windows certificates (Windows asks to confirm), and its files removed: `aicleaner claudeai-off
    # --remove-cert`. Left behind, claude.ai would point at a PAC file that is gone.
    if ((Test-Path -LiteralPath $venvPython) -and (Test-Path -LiteralPath $configFile) -and
        $PSCmdlet.ShouldProcess('the Windows proxy setting and the claude.ai certificate', 'Remove what route B added')) {
        $savedEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        Push-Location -LiteralPath $root
        try {
            Write-Status 'If Windows asks whether to delete the "aicleaner claude.ai" certificate, choose Yes.' -Level Detail
            & $venvPython -m aicleaner claudeai-off --remove-cert --config $configFile 2>&1 | ForEach-Object { Write-Status "Claude Desktop's chat: $_" -Level Detail }
            if ($LASTEXITCODE -ne 0) { Write-Status 'The Windows proxy setting could not be put back; see the lines above' -Level Warn }
        } finally {
            Pop-Location
            $ErrorActionPreference = $savedEap
        }
    }

    # --- 1b. VS Code environmentVariables ---------------------------------------------
    # Every settings file the installer can write: the locally installed VS Code, and the servers a
    # Remote-SSH session runs from. Missing the server ones left Claude Code pointed at a proxy that
    # the uninstall had just removed, and a base URL other than api.anthropic.com is also what stops
    # Claude Code offering Remote Control - so the leftover disabled that too, on a machine with no
    # aicleaner on it. The installer's own Get-VSCodeServerSettingsPath looks in the same folders.
    $vsPaths = @(
        (Join-Path $env:APPDATA 'Code\User\settings.json'),
        (Join-Path $env:APPDATA 'Code - Insiders\User\settings.json'),
        (Join-Path $env:USERPROFILE '.vscode-server\data\Machine\settings.json'),
        (Join-Path $env:USERPROFILE '.vscode-server-insiders\data\Machine\settings.json')
    )
    foreach ($vsPath in $vsPaths) {
        if (-not (Test-Path -LiteralPath $vsPath)) { continue }
        try {
            $vs = Read-JsonFile -Path $vsPath
            if ($vs.PSObject.Properties['claudeCode.environmentVariables']) {
                $kept = @($vs.'claudeCode.environmentVariables' | Where-Object { $_.name -ne 'ANTHROPIC_BASE_URL' })
                if ($kept.Count -ne @($vs.'claudeCode.environmentVariables').Count -and $PSCmdlet.ShouldProcess($vsPath, 'Remove ANTHROPIC_BASE_URL')) {
                    Backup-File -Path $vsPath
                    # Pointed home, for the same reason as the settings above: the extension
                    # replaces a value it already has, and does not notice one that vanishes.
                    $vs.'claudeCode.environmentVariables' = @($kept) +
                        @([pscustomobject]@{ name = 'ANTHROPIC_BASE_URL'; value = $PUBLIC_ANTHROPIC })
                    Write-JsonFile -Path $vsPath -Object $vs
                    Write-Status "VS Code: ANTHROPIC_BASE_URL now $PUBLIC_ANTHROPIC in $vsPath" -Level Detail
                }
            }
        } catch { Write-Status "$vsPath is not plain JSON; remove ANTHROPIC_BASE_URL from claudeCode.environmentVariables by hand" -Level Warn }
    }


    # --- 2. Stop the proxy and remove the tasks ------------------------------------
    Write-Status 'Stopping the proxy and removing scheduled tasks'
    # Feature 107: the start-up tasks (as you, from boot). Their launcher loop is told to stop first; removing them
    # needs an administrator, so without one the way to do it is said.
    $stoppedMarker = Join-Path $env:USERPROFILE '.aicleaner\stopped-on-purpose'
    foreach ($task in 'aicleaner-service', 'aicleaner-tunnel-service') {
        if (-not (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue)) { continue }
        if ($PSCmdlet.ShouldProcess($task, 'Stop and unregister the start-up task')) {
            Set-Content -LiteralPath $stoppedMarker -Value (Get-Date -Format o) -ErrorAction SilentlyContinue
            try {
                Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
                Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction Stop
                Write-Status "Removed task $task" -Level Detail
            } catch {
                Write-Status "Could not remove the start-up task $task ($($_.Exception.Message)); from an elevated PowerShell: Unregister-ScheduledTask -TaskName $task -Confirm:`$false" -Level Warn
            }
        }
    }
    foreach ($task in 'aicleaner-proxy', 'aicleaner-claude-backup', 'aicleaner-claude-update') {
        if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
            if ($PSCmdlet.ShouldProcess($task, 'Unregister scheduled task')) {
                Unregister-ScheduledTask -TaskName $task -Confirm:$false; Write-Status "Removed task $task" -Level Detail
            }
        }
    }
    if (-not $PSBoundParameters.ContainsKey('Port')) {
        try {
            $cfgDoc = Read-JsonFile -Path (Join-Path $root 'aicleaner-config.json')
            if ($cfgDoc -and $cfgDoc.PSObject.Properties['port']) { $Port = [int]$cfgDoc.port }
        } catch { Write-Status "aicleaner-config.json unreadable; using port $Port" -Level Warn }
    }
    # Processes are stopped by the venv they run from. A checkout's .venv is a development
    # environment - its python may be running tests, not the proxy - so it is never on this list.
    $venvFolders = @(Join-Path $env:USERPROFILE '.aicleaner\venv')
    $oldVenv = Get-OldInstallVenv -Root $root
    if ($oldVenv) { $venvFolders += $oldVenv }
    $proxyStopped = Stop-AiCleanerProcess -ProxyPort $Port -VenvFolders $venvFolders

    # --- 2b. Remote control: the connector here, and the tunnel in the account --------
    # Only when asked (-RemoveRemote), and offered then: otherwise it is left for a later install to
    # keep or replace.
    Invoke-RemoteRemoval -Root $root -Asked:($RemoveRemote -or $RemoteOnly)

    # --- 4. CLAUDE.md block -------------------------------------------------------------
    $claudeMd = Join-Path $env:USERPROFILE '.claude\CLAUDE.md'
    if (Test-Path -LiteralPath $claudeMd) {
        $text = [string](Get-Content -LiteralPath $claudeMd -Raw -Encoding UTF8)
        $i = $text.IndexOf('<!-- aicleaner:start -->'); $j = $text.IndexOf('<!-- aicleaner:end -->')
        if ($i -ge 0 -and $j -gt $i -and $PSCmdlet.ShouldProcess($claudeMd, 'Remove aicleaner block')) {
            Backup-File -Path $claudeMd
            $new = ($text.Substring(0, $i) + $text.Substring($j + '<!-- aicleaner:end -->'.Length)).Trim()
            [IO.File]::WriteAllText($claudeMd, $(if ($new) { $new + "`n" } else { '' }), (New-Object System.Text.UTF8Encoding($false)))
            Write-Status 'CLAUDE.md: aicleaner block removed' -Level Detail
        }
    }

    # --- 5. Claude Desktop policy --------------------------------------------------------
    $policyKey = 'HKCU:\SOFTWARE\Policies\Claude'
    $policyLeft = @()
    if (Test-Path $policyKey) {
        $ourValues = 'inferenceProvider', 'inferenceGatewayBaseUrl', 'inferenceGatewayApiKey', 'inferenceGatewayAuthScheme',
                     'skipWebFetchPreflight', 'chatAdvancedFileAnalysisEnabled', 'organizationInstructions', 'claudeAiImport'
        $current = Get-PolicyValues -KeyPath $policyKey
        $present = @($ourValues | Where-Object { $current.Contains($_) })
        if ($present -and -not (Test-PolicyWritable)) {
            $policyLeft = $present
            Write-Status "Claude Desktop policy NOT removed: $policyKey is read-only without elevation." -Level Warn
            Write-Status 'Re-run this script from an elevated PowerShell signed in as the same user to remove it.' -Level Detail
        } elseif ($present -and $PSCmdlet.ShouldProcess($policyKey, "Back up and remove $($present.Count) values")) {
            $backup = Backup-PolicyKey -KeyPath $policyKey
            $policyLeft = @(Remove-PolicyValues -KeyPath $policyKey -Names $present)
            if ($policyLeft.Count -gt 0) { Write-Status "Claude Desktop policy values still present: $($policyLeft -join ', ')" -Level Warn }
            else { Write-Status "Claude Desktop policy removed (backup $backup); fully quit and relaunch Claude Desktop" -Level Detail }
        }
    }

    if ($isCheckout) {
        Write-Status 'Old policy exports in this checkout (if any) are left for you to delete' -Level Detail
    } else {
        Remove-LegacyPolicyExports -Root $root
    }

    # --- 6. What the installers changed (recorded in install-changes.json) -----------------------
    $restored = Restore-InstallChanges

    # The copy of Snipping Tool's engine DLLs the image reader made for this user (feature 113).
    $engineCopy = Join-Path $env:LOCALAPPDATA 'aicleaner\oneocr'
    if ((Test-Path -LiteralPath $engineCopy) -and $PSCmdlet.ShouldProcess($engineCopy, 'Remove the copy of Snipping Tool''s text engine')) {
        Remove-Item -LiteralPath $engineCopy -Recurse -Force
        Write-Status 'Removed the copy of Snipping Tool''s text engine' -Level Detail
    }

    $captureShortcut = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\aicleaner text from screen.lnk'
    if ((Test-Path -LiteralPath $captureShortcut) -and $PSCmdlet.ShouldProcess($captureShortcut, 'Remove the screen-text shortcut')) {
        Remove-Item -LiteralPath $captureShortcut -Force
        Write-Status 'Screen-text shortcut removed' -Level Detail
    }

    # --- 6b. PowerToys remap (PowerToys stays unless -RemovePowerToys) ---------------------------
    $kbm = Join-Path $env:LOCALAPPDATA 'Microsoft\PowerToys\Keyboard Manager\default.json'
    if (Test-Path -LiteralPath $kbm) {
        $doc = Read-JsonFile -Path $kbm
        if ($doc.PSObject.Properties['remapShortcuts'] -and $doc.remapShortcuts.PSObject.Properties['global']) {
            # Which keys were taken is in the ledger: the person chose them at install time.
            $remapSection = Get-LedgerSection -Name 'powertoys_remap'
            $taken = @()
            if ($remapSection) { $taken = @($remapSection.PSObject.Properties | ForEach-Object { $_.Name }) }
            # Ours either way: the capture script started by a run-program remap (2026-10-12), or the older remap to Win+Shift+T.
            $ours = @($doc.remapShortcuts.global | Where-Object { ($_.PSObject.Properties['newRemapKeys'] -and [string]$_.newRemapKeys -eq '91;16;84') -or
                ($_.PSObject.Properties['runProgramArgs'] -and [string]$_.runProgramArgs -like '*Capture-TextFromScreen.ps1*') })
            if ($ours.Count -eq 0) {
                Write-Status 'PowerToys: nothing is remapped to the screen capture or the text extractor; left alone' -Level Detail
            } elseif ($taken.Count -eq 0) {
                Write-Status 'PowerToys: a shortcut points at Win+Shift+T, but nothing recorded what it did before.' -Level Warn
                Write-Status 'Left in place, in case that remap was yours; remove it in PowerToys if it was not.' -Level Detail
            } elseif ($PSCmdlet.ShouldProcess($kbm, 'Put the screenshot shortcut back')) {
                Backup-File -Path $kbm
                $restored = New-Object System.Collections.Generic.List[object]
                foreach ($entry in @($doc.remapShortcuts.global)) {
                    $keys = [string]$entry.originalKeys
                    if ($taken -notcontains $keys) { $restored.Add($entry); continue }
                    $was = [string]$remapSection.$keys
                    if ($was) { $restored.Add([pscustomobject]@{ originalKeys = $keys; newRemapKeys = $was }) }
                }
                # ToArray, not @(): the adapter refuses a List here with "Argument types do not match".
                $doc.remapShortcuts.global = $restored.ToArray()
                Write-JsonFile -Path $kbm -Object $doc
                $pt = Get-Process PowerToys -ErrorAction SilentlyContinue
                if ($pt) { $pt | Stop-Process -Force; Start-Sleep 2; Start-Process -FilePath $pt[0].Path | Out-Null }
                Write-Status 'PowerToys: your screenshot shortcut is back the way it was' -Level Detail
            }
        }
    }
    if ($RemovePowerToys) {
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if ($winget -and $PSCmdlet.ShouldProcess('Microsoft.PowerToys', 'winget uninstall')) {
            $savedEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
            try { & $winget.Source uninstall -e --id Microsoft.PowerToys --silent --disable-interactivity | Out-Null } finally { $ErrorActionPreference = $savedEap }
            Write-Status 'PowerToys uninstalled' -Level Detail
        }
    }

    # --- 7. User-scope variables ---------------------------------------------------------------
    # The change record restores what was there before, when it survived. Whether it did or not,
    # these two never outlive the install: each switches off the feature-flag evaluation Claude
    # Code's Remote Control depends on, so leaving them behind disabled an unrelated feature on a
    # machine that no longer runs aicleaner at all. Only our own value ('1') is taken away.
    foreach ($name in 'DO_NOT_TRACK', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC') {
        $cur = [Environment]::GetEnvironmentVariable($name, 'User')
        if ([string]::IsNullOrEmpty($cur)) { continue }
        if ($cur -ne '1') {
            Write-Status "$name=$cur was not set by us; left alone" -Level Detail
        } elseif ($PSCmdlet.ShouldProcess($name, 'Remove user environment variable')) {
            [Environment]::SetEnvironmentVariable($name, $null, 'User')
            Write-Status "$name removed (new terminals only; a running VS Code server keeps the old value)" -Level Detail
        }
    }
    if (-not $restored) {
        Write-Status 'No record of what the installers changed (install-changes.json): other telemetry switches are left as they are' -Level Detail
    }

    # --- 8. venv (and secrets only on request) --------------------------------------------------
    $local = Join-Path $env:USERPROFILE '.aicleaner'
    $venv = Join-Path $local 'venv'
    if ((Test-Path -LiteralPath $venv) -and $PSCmdlet.ShouldProcess($venv, 'Remove venv')) {
        if (-not $proxyStopped) { Write-Status "Venv kept at ${venv}: a proxy process is still running; remove it after a restart" -Level Warn }
        else {
            try { Remove-Item -LiteralPath $venv -Recurse -Force; Write-Status 'Shared venv removed' -Level Detail }
            catch { Write-Status "Venv at $venv could not be removed (files in use: $($_.Exception.Message)); remove it after a restart" -Level Warn }
        }
    }
    # Only an early install's venv, kept beside its code; never a checkout's. Asked, defaulting to
    # No, with the full path, because deleting a Python environment cannot be undone from here.
    if ($oldVenv -and (Confirm-Action -Message "Delete the Python environment an earlier install kept at ${oldVenv}?" -Default 'No')) {
        if ($PSCmdlet.ShouldProcess($oldVenv, 'Remove venv')) { Remove-Item -LiteralPath $oldVenv -Recurse -Force }
    } elseif ($isCheckout -and (Test-Path -LiteralPath (Join-Path $root '.venv'))) {
        Write-Status "Left $(Join-Path $root '.venv') alone: it is this checkout's development environment" -Level Detail
    }
    $secrets = Join-Path $local 'secrets'
    if (Test-Path -LiteralPath $secrets) {
        if ($RemoveSecrets) {
            if ($PSCmdlet.ShouldProcess($secrets, 'Remove secrets (gateway token, API key, redact list)')) { Remove-Item -LiteralPath $secrets -Recurse -Force; Write-Status 'Secrets removed' -Level Detail }
        } else { Write-Status "Secrets kept at $secrets (pass -RemoveSecrets to delete)" -Level Detail }
    }
    if ((Test-Path -LiteralPath $local) -and -not (Get-ChildItem -LiteralPath $local -Force)) { Remove-Item -LiteralPath $local }

    # --- 9. Feedback identity (2026-10-16): kept, so a reinstall reconnects to the same tickets ----
    $feedbackHome = Join-Path $env:LOCALAPPDATA 'aicleaner-feedback'
    if (Test-Path -LiteralPath $feedbackHome) {
        if ($RemoveFeedbackIdentity) {
            if ($PSCmdlet.ShouldProcess($feedbackHome, 'Remove the feedback connection')) {
                Remove-Item -LiteralPath $feedbackHome -Recurse -Force
                Write-Status 'Feedback connection removed (a reinstall will ask again)' -Level Detail
            }
        } else {
            Write-Status "Feedback connection kept at $feedbackHome, so a reinstall finds your tickets (pass -RemoveFeedbackIdentity to delete it)" -Level Detail
        }
    }

    if ($policyLeft.Count -gt 0) {
        Write-Status "Uninstall finished EXCEPT the Claude Desktop policy ($($policyLeft -join ', ')); re-run elevated." -Level Warn
    } else {
        Write-Status 'Uninstall complete. Kept: mapping (client-map.json, clients\, _global.json), ignore lists, candidates, logs, aicleaner-config.json.' -Level Done
    }
    # Both base-URL settings were pointed back at the public endpoint in the first step, before
    # anything was stopped, which a running session follows the same way it followed the proxy
    # going in. So a restart should no longer be the price of getting back to work - but it is
    # offered rather than promised, because what picks the change up is Claude Code's business.
    Write-Status ("Sessions were pointed back at $PUBLIC_ANTHROPIC first, before anything was stopped, so " +
                  'the break in service was the moment that took rather than the whole uninstall. They ' +
                  'should reach the API again without restarting; if one does not, start a new Claude ' +
                  'Code session. Claude Desktop reads its configuration at launch, so it needs a restart ' +
                  'either way.') -Level Detail
}
catch {
    Write-Status "Uninstall failed: $($_.Exception.Message)" -Level Error
    Write-Error $_ -ErrorAction Continue
    exit 1
}
finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
