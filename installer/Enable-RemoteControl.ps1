<#
.SYNOPSIS
    Puts the aicleaner UI behind a Cloudflare tunnel so a session can be driven from a phone.

.DESCRIPTION
    Installs cloudflared, creates a named tunnel from this machine to Cloudflare, points a hostname
    at it, and runs it as a service. Nothing is opened on this machine: the tunnel dials out, and
    the proxy keeps listening on 127.0.0.1 only.

    In front of the hostname you put Cloudflare Access, which does the sign-in. There are two ways
    to set that up and the script asks which you want:

      - Hand it a scoped API token (-ApiToken, or it asks) and it does the whole Cloudflare side
        without a browser: tunnel, DNS record, Access application, allow policy, and the AUD tag
        into the config. It prints the four permissions the token needs, which are the smallest set
        that can do the job; the token is never written to disk or logged, and can be deleted the
        moment the run finishes.
      - Or do it in the dashboard yourself, with the click path the script prints. Nothing is
        handed over; it takes a few more minutes.

    It also asks whether you already have a Cloudflare account with Zero Trust turned on, and only
    prints the one-time account steps for someone who says they do not.

    Access then puts a signed token on every request, and aicleaner verifies it before serving
    anything: see the remote block in aicleaner-config.json.

    Be clear about the trade: Access terminates TLS at Cloudflare's edge, so Cloudflare could see
    the rehydrated session. That is an accepted risk for this deployment, in the same class as the
    other proxies a request already crosses.

    Only the UI and the session API are reachable through the tunnel. Model traffic stays on this
    machine: a request for /anthropic arriving from outside is refused.

.PARAMETER Hostname
    The public name for the UI, e.g. claude.example.com (asked when not given).

.PARAMETER TunnelName
    The tunnel's name in Cloudflare. Default aicleaner-<computer name>.

.PARAMETER Port
    The port the proxy listens on. Default: the port in aicleaner-config.json, else 8787.

.PARAMETER ConfigPath
    aicleaner-config.json to update. Default: beside this script.

.PARAMETER Remove
    Take it all away: stop and remove the service, delete the tunnel, and turn remote access off in
    the config.

.PARAMETER ApiToken
    A Cloudflare API token, to have the script do the Cloudflare side. Asked for when -UseApi is
    chosen and this is not given. Never stored.

.PARAMETER Email
    The address allowed to sign in, and where the one-time PIN goes. Asked for when needed.

.PARAMETER Setup
    Skip the question: 'api' (the default when asked) to have the script do it with a token,
    'manual' for the dashboard.

.PARAMETER Rename
    Give this machine's existing address a new name, given with -Hostname (asked when not given):
    the same tunnel and the same Access application, so the sign-in and the proxy's settings carry
    over. The new name answers before the old one is removed. Uses the API token kept at setup, or
    asks for one. Browsers sign in again at the new name, and with the encrypted tunnel on each
    device enters the code again.

.EXAMPLE
    .\Enable-RemoteControl.ps1 -Rename -Hostname bsdevq8.example.com

.EXAMPLE
    .\Enable-RemoteControl.ps1 -Hostname claude.example.com

.EXAMPLE
    .\Enable-RemoteControl.ps1 -Remove

.NOTES
    Contact:       see contact-*.json stored alongside this script
    Last-modified: 2026-10-04
    Requires:      Windows PowerShell 5.1 or PowerShell 7+; a Cloudflare account with a zone; winget
#>

#Requires -Version 5.1

[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [string]$Hostname = '',
    [string]$TunnelName = '',
    [int]$Port = 0,
    [string]$ConfigPath = '',
    [System.Security.SecureString]$ApiToken,
    [string]$Email = '',
    [ValidateSet('', 'api', 'manual')][string]$Setup = '',
    # How you prove who you are at Cloudflare's door. OneTimePin needs nothing set up anywhere and
    # is what this has always done: Cloudflare emails a code to an address on the policy. Google and
    # Microsoft each need an OAuth client created in their own console first - browser work this
    # script cannot do - so it prints the steps, waits, and does the Cloudflare side over the API.
    [ValidateSet('OneTimePin', 'Google', 'Microsoft')][string]$Identity = 'OneTimePin',
    # Go straight to the provider instead of showing a sign-in button. Saves a click and tells
    # anyone who opens the address which account guards it, because the provider names it.
    [switch]$SkipTheChooser,
    # Give the existing address a new name (with -Hostname): same tunnel, same Access application.
    [switch]$Rename,
    [switch]$Remove
)
# The app folder: this script's own, or its parent when it sits in installer\ (feature 102).
$script:AppFolder = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- Contact attribution (inlined per script so each script stays self-contained) ---------
function Get-ContactInfo {
    <# The first contact-*.json beside this script, or $null. #>
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
    $contact = Get-ContactInfo
    if (-not $contact) { return }
    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($field in @('organisation', 'name', 'email', 'issues_url')) {
        if ($contact.PSObject.Properties[$field] -and $contact.$field) { $parts.Add([string]$contact.$field) }
    }
    if ($parts.Count -gt 0) { Write-Host ('aicleaner | ' + ($parts -join ' | ')) }
}

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
    $response = Read-Host "$Message $suffix"
    if ([string]::IsNullOrWhiteSpace($response)) { return ($Default -eq 'Yes') }
    return ($response.Trim() -match '^(y|yes)$')
}

function Read-Value {
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][string]$Message, [AllowEmptyString()][string]$Default = '')
    $prompt = if ($Default) { "$Message [$Default]" } else { $Message }
    $response = Read-Host $prompt
    if ([string]::IsNullOrWhiteSpace($response)) { return $Default }
    return $response.Trim()
}

function Read-JsonFile {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $raw = [string](Get-Content -LiteralPath $Path -Raw -Encoding UTF8)
    if ([string]::IsNullOrWhiteSpace($raw)) { return [pscustomobject]@{} }
    return ($raw | ConvertFrom-Json)
}

function Write-JsonFile {
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)]$Object)
    if ($PSCmdlet.ShouldProcess($Path, 'Write JSON')) {
        $json = $Object | ConvertTo-Json -Depth 20
        [IO.File]::WriteAllText($Path, $json, (New-Object System.Text.UTF8Encoding($false)))
    }
}

function Set-JsonProperty {
    param([Parameter(Mandatory)]$Object, [Parameter(Mandatory)][string]$Name, $Value)
    if ($Object.PSObject.Properties[$Name]) { $Object.$Name = $Value }
    else { $Object | Add-Member -MemberType NoteProperty -Name $Name -Value $Value }
}

function Get-Cloudflared {
    <#
    .SYNOPSIS
        The cloudflared executable, installing it with winget when it is not here yet.
    .DESCRIPTION
        Returns exactly one path. winget's progress goes to the console, not to the output: left in
        the output it came back as an array - winget's lines plus the path - and the first thing to
        use it (-Exe [string]) refused it, after the whole Cloudflare side had been set up. Only a
        machine without cloudflared took that path, which is why it went unnoticed.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    [OutputType([string])]
    param()
    # -First 1: a cloudflared on PATH twice (a winget copy and a manual one) is two results.
    $found = Get-Command cloudflared -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return [string]$found.Source }
    $winget = Get-Command winget -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $winget) { throw 'cloudflared is not installed and winget is not available to install it' }
    if (-not $PSCmdlet.ShouldProcess('Cloudflare.cloudflared', 'winget install')) { return '' }
    Write-Status 'Installing cloudflared'
    & $winget.Source install --id Cloudflare.cloudflared -e --silent --accept-package-agreements --accept-source-agreements |
        Out-Host
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    $found = Get-Command cloudflared -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $found) { throw 'cloudflared was installed but is not on PATH yet; open a new PowerShell and re-run' }
    return [string]$found.Source
}

function Invoke-Cloudflared {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Exe, [Parameter(Mandatory)][string[]]$Arguments, [switch]$AllowFailure)
    $savedEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $output = & $Exe @Arguments 2>&1 } finally { $ErrorActionPreference = $savedEap }
    $text = ($output | ForEach-Object { "$_" }) -join "`n"
    if ($LASTEXITCODE -ne 0 -and -not $AllowFailure) {
        throw "cloudflared $($Arguments[0]) failed: $($text.Trim())"
    }
    return $text
}

function Read-PublicHostname {
    <#
    .SYNOPSIS
        The name this machine's pages will answer on, asked once there is something to suggest.
    .DESCRIPTION
        "Public name for the UI" with an empty default asks somebody to invent a hostname with no
        idea what would be accepted. With a token in hand the zones are known, so the question can
        offer one and explain what it is for: a NEW name in a zone Cloudflare runs for you, created
        as a proxied CNAME to the tunnel. Nothing existing is taken over.
    .PARAMETER Zones
        Zone names the token can see. Empty when the name is being chosen without one.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([string[]]$Zones = @())
    $machine = $env:COMPUTERNAME.ToLower()
    Write-Host ''
    Write-Status 'The name these pages will answer on' -Level Step
    Write-Status 'It is a NEW name in a domain Cloudflare runs your DNS for, created as a record' -Level Detail
    Write-Status 'pointing at this tunnel. An existing name is never taken over.' -Level Detail
    if ($Zones.Count -gt 0) {
        Write-Status ("Domains this token can write to: {0}" -f ($Zones -join ', ')) -Level Detail
    }
    # A hostname is public: Cloudflare issues a certificate for it, and certificate transparency
    # makes that name searchable by anyone. A client's name does not belong in it.
    Write-Status 'Name it after the machine, not a client: the name ends up in public certificate' -Level Warn
    Write-Status 'transparency logs, so "acme-project.example.com" tells the world who you work for.' -Level Detail
    $suggested = if ($Zones.Count -gt 0) { "claude-$machine.$($Zones[0])" } else { "claude-$machine.example.com" }
    $answer = Read-Value -Message 'Public name for these pages' -Default $suggested
    if (-not $answer) { throw 'A hostname is needed' }
    return ($answer -replace '^https?://', '').Trim('/')
}

function Wait-ForCloudflare {
    <#
    .SYNOPSIS
        Holds here while the Cloudflare side is set up. $true to carry on, $false to stop for now.
    .DESCRIPTION
        The alternative is sending someone away to run the whole thing again, which wastes the
        answers they have already given. The nameserver change can take a while, though, so
        stopping stays available and says how to pick up where this left off.
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param([string]$Resume = '')
    Write-Host ''
    Write-Status 'Waiting here: nothing has been changed on this machine yet.' -Level Detail
    $answer = Read-Value -Message "Press Enter when that is done, or type 'stop' to finish this later" -Default ''
    if ($answer -and $answer.Trim().ToLowerInvariant() -in @('stop', 'q', 'quit', 'exit', 'n', 'no')) {
        Write-Status 'Stopped; nothing was changed.' -Level Done
        if ($Resume) { Write-Status "Pick it up with:  $Resume" -Level Detail }
        return $false
    }
    return $true
}

function Invoke-WithRetry {
    <#
    .SYNOPSIS
        Runs a step, and on failure says what is wrong and offers to wait and try again.
    .DESCRIPTION
        The failures here are nearly all "Cloudflare is not ready yet": a zone still pending its
        nameserver change, Zero Trust without a team name, a token missing a permission. Every one
        of them is fixed in the dashboard in a minute, so the useful thing is to hold the run open
        rather than throw the setup away.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][scriptblock]$Step,
        [Parameter(Mandatory)][string]$What,
        [int]$Attempts = 3
    )
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try { return & $Step }
        catch {
            Write-Status "$What`: $($_.Exception.Message)" -Level Warn
            if ($attempt -eq $Attempts) { throw }
            if (-not (Confirm-Action -Message 'Fix that in the dashboard, then try again?' -Default 'Yes')) { throw }
        }
    }
}

function Restart-AiCleanerProxy {
    <#
    .SYNOPSIS
        Restarts the proxy so it reads the remote block just written, and checks it answers again.
    .DESCRIPTION
        The proxy reads its config once, at start: the guard that lets the tunnel's hostname in is
        built then. Leaving "restart the proxy" as a closing line meant the owner signed in through
        Cloudflare, typed the PIN, and was refused by aicleaner with "use a loopback host name" -
        the tunnel, DNS and Access all working, the running proxy simply not knowing about any of it.

        Uses the Stop and Start scripts beside this one (they are deployed together) and the logon
        task when there is one, so the proxy comes back the way it normally starts. Returns $true
        when /healthz answers afterwards.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    [OutputType([bool])]
    param([Parameter(Mandatory)][int]$Port, [string]$Folder = $script:AppFolder, [int]$WaitSeconds = 20)
    # In installer\ since feature 102; the root of an install from before the move.
    $scripts = if (Test-Path -LiteralPath (Join-Path $Folder 'installer\Stop-AiCleaner.ps1')) { Join-Path $Folder 'installer' } else { $Folder }
    $stop = Join-Path $scripts 'Stop-AiCleaner.ps1'
    $start = Join-Path $scripts 'Start-AiCleaner.ps1'
    if (-not (Test-Path -LiteralPath $stop) -or -not (Test-Path -LiteralPath $start)) {
        Write-Status 'Restart the proxy so it reads the new config (Stop-AiCleaner.ps1, then Start-AiCleaner.ps1 -Detach)' -Level Warn
        return $false
    }
    if (-not $PSCmdlet.ShouldProcess('aicleaner proxy', 'Restart to read the remote settings')) { return $false }
    Write-Status 'Restarting the proxy so it reads the remote settings'
    $shell = (Get-Process -Id $PID).Path
    # Statements, never captured: a captured child hands its pipe to the proxy it starts, and the
    # caller then waits for as long as the proxy runs (see Deploy-AiCleaner.ps1, Update-Deployment).
    & $shell -NoProfile -ExecutionPolicy Bypass -File $stop -Port $Port
    $task = Get-ScheduledTask -TaskName 'aicleaner-proxy' -ErrorAction SilentlyContinue
    if ($task) { Start-ScheduledTask -TaskName 'aicleaner-proxy' }
    else { & $shell -NoProfile -ExecutionPolicy Bypass -File $start -Port $Port -Detach }
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    do {
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 2 | Out-Null
            Write-Status 'Proxy restarted with remote access on' -Level Done
            return $true
        } catch { Start-Sleep -Milliseconds 500 }
    } while ((Get-Date) -lt $deadline)
    Write-Status "The proxy did not answer on port $Port after the restart; run Start-AiCleaner.ps1 -Detach and read what it says" -Level Warn
    return $false
}

function Invoke-ApiSetup {
    <#
    .SYNOPSIS
        The whole Cloudflare side with a scoped token: tunnel, DNS, Access application, config.
    .DESCRIPTION
        No browser and no dashboard. The token is held as a SecureString, turned into text only for
        the moment each call needs it, and never written anywhere.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param()
    if (-not $script:ApiTokenSecure) {
        Show-TokenSteps -PublicHostname $script:Hostname -SignInWith $Identity
        $script:ApiTokenSecure = Read-Host -Prompt 'Paste the API token (hidden)' -AsSecureString
    }
    if (-not $script:ApiTokenSecure -or $script:ApiTokenSecure.Length -eq 0) { throw 'No token given' }
    $emails = @($script:Emails | Where-Object { $_ })
    if ($emails.Count -eq 0) {
        $address = Read-Value -Message 'Which email address may sign in (the one-time PIN goes there)' -Default ''
        if (-not $address) { throw 'An email address is needed: without one, nobody could sign in' }
        $emails = @($address)
    }
    $token = ConvertFrom-SecureToken -Secure $script:ApiTokenSecure
    try {
        # The name is asked for here, not at the top: with the token in hand the zones are known,
        # so the question can suggest one instead of expecting somebody to invent it.
        if (-not $script:Hostname) {
            $zones = @(Invoke-WithRetry -What 'Could not list the domains this token can see' -Step {
                Invoke-CloudflareApi -Token $token -Path '/zones?per_page=50' -Needs 'Zone | DNS | Edit'
            } | ForEach-Object { [string]$_.name } | Where-Object { $_ })
            $script:Hostname = Read-PublicHostname -Zones $zones
        }
        $targets = Invoke-WithRetry -What 'Could not find the account and zone' -Step {
            Get-CloudflareTargets -Token $token -PublicHostname $script:Hostname
        }
        Write-Status "Account $($targets.AccountName), zone $($targets.ZoneName)" -Level Detail
        $teamDomain = Invoke-WithRetry -What 'Could not read the team domain' -Step {
            Get-TeamDomain -Token $token -AccountId $targets.AccountId
        }
        Write-Status "Team domain $teamDomain" -Level Detail

        $tunnel = New-ApiTunnel -Token $token -AccountId $targets.AccountId -Name $script:TunnelName `
            -PublicHostname $script:Hostname -LocalPort $script:ProxyPort
        if (-not $tunnel) { throw 'The tunnel was not created' }
        Set-TunnelDns -Token $token -ZoneId $targets.ZoneId -PublicHostname $script:Hostname -TunnelId $tunnel.Id
        $audience = New-AccessApplication -Token $token -AccountId $targets.AccountId `
            -PublicHostname $script:Hostname -Emails $emails
        # After the application exists, because pinning needs something to pin to. The allow policy
        # is unchanged either way: it matches the address the provider asserts, whoever asserts it.
        $idp = New-IdentityProvider -Token $token -AccountId $targets.AccountId `
            -Provider $Identity -TeamDomain $teamDomain
        if ($idp) {
            Set-ApplicationIdentity -Token $token -AccountId $targets.AccountId `
                -AppId $script:AccessAppId -IdpId $idp -SkipTheChooser:$SkipTheChooser
        }

        # A task at sign-in, as this user, rather than a Windows service: no administrator rights, and
        # the tunnel runs exactly while the proxy it serves does (feature 104, part 2). It dials out;
        # nothing is opened on this machine.
        Write-Status 'Starting the tunnel at every sign-in (it dials out; nothing is opened on this machine)'
        Install-TunnelTask -RunToken $tunnel.RunToken -AppRoot (Split-Path -Parent $PSScriptRoot)
        Save-RemoteConfig -TeamDomain $teamDomain -Audience $audience -Emails $emails -Enabled ([bool]$audience) `
            -AccountId $targets.AccountId -ZoneId $targets.ZoneId -TunnelId $tunnel.Id -SignInWith $Identity
        # Kept so Uninstall-AiCleaner.ps1 can take the tunnel, the DNS record and the Access
        # application down by itself. It cannot delete itself - that needs a far broader permission
        # - so the uninstaller prints the two clicks for that.
        Save-CloudflareToken -Secure $script:ApiTokenSecure
    } finally {
        # The plain text lives as briefly as this.
        $token = $null
        [GC]::Collect()
    }
    # The guard that lets the tunnel's hostname in is built when the proxy starts, so it has to
    # start again to see what was just written. Done here, not left as a line to act on.
    $restarted = Restart-AiCleanerProxy -Port $script:ProxyPort
    Write-Status "Remote control is on: https://$($script:Hostname) -> 127.0.0.1:$($script:ProxyPort)" -Level Done
    if ($script:IdpId) {
        Write-Status "Sign in with $Identity as $($emails -join ', '). Nobody else gets past Cloudflare, and the address is refused before anything reaches this machine." -Level Detail
    } else {
        Write-Status "Sign in with $($emails -join ', '): Cloudflare emails a one-time PIN when you open the address." -Level Detail
        Write-Status "Prefer your Microsoft or Google account? Re-run with -Identity Microsoft or -Identity Google; it prints what to set up first." -Level Detail
    }
    Write-Status 'Through the tunnel you can drive a session and sort the queue. Exports, imports, secrets, rehydrate and the settings that weaken the filter are refused there and stay on this machine.' -Level Detail
    if (-not $restarted) { Write-Status 'Until the proxy restarts, the address answers "use a loopback host name".' -Level Warn }
    Write-Status 'The token is kept, encrypted to this Windows account, so Uninstall-AiCleaner.ps1 can take the tunnel down.' -Level Detail
    Write-Status "Delete it yourself at https://dash.cloudflare.com/profile/api-tokens if you would rather not keep it; the tunnel keeps working, and the uninstaller then prints the steps to undo it by hand." -Level Detail
}

function Set-OwnedIds {
    <#
    .SYNOPSIS
        Seeds the ids the uninstaller is allowed to act on from what a previous run recorded.
    .DESCRIPTION
        Only the paths that CREATE a DNS record or an Access application assign these. A run that
        finds a tunnel, a record and an application already in place creates nothing, so it assigns
        neither - and under Set-StrictMode the save at the end then failed on a variable that had
        never been set, taking the whole install with it.

        Seeding from the config is what makes that safe rather than merely quiet: a record this
        deployment made keeps its id across a re-run and stays removable, instead of being orphaned
        because the run that found it was not the run that made it. Empty still means "not ours",
        which is what a name somebody else already owned reads as.
    #>
    param([psobject]$Config)
    $remote = if ($Config -and $Config.PSObject.Properties['remote']) { $Config.remote } else { $null }
    foreach ($pair in @(@('dns_record_id', 'DnsRecordId'), @('access_app_id', 'AccessAppId'),
                        @('idp_id', 'IdpId'))) {
        $value = if ($remote -and $remote.PSObject.Properties[$pair[0]]) { [string]$remote.($pair[0]) } else { '' }
        Set-Variable -Name $pair[1] -Value $value -Scope Script
    }
    # Whether THIS deployment made each object, as far as anyone has recorded. It is a claim, not
    # proof: the uninstaller checks the object's own stamp as well, so a run that lost the flag
    # still works out what it is looking at rather than orphaning it.
    foreach ($pair in @(@('dns_record_mine', 'DnsRecordMine'), @('access_app_mine', 'AccessAppMine'),
                        @('idp_mine', 'IdpMine'))) {
        $claim = if ($remote -and $remote.PSObject.Properties[$pair[0]]) { [bool]$remote.($pair[0]) } else { $false }
        Set-Variable -Name $pair[1] -Value $claim -Scope Script
    }
}

function Save-RemoteConfig {
    <#
    .SYNOPSIS
        The remote block, the same shape whichever path filled it in.
    .DESCRIPTION
        The ids are here for the uninstaller: without them it would have to guess which tunnel, DNS
        record and Access application belong to this machine, and guessing wrong takes down somebody
        else's. Only what this script created is recorded, so only that is ever removed. They name
        objects in your own Cloudflare account; none of it is a secret.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [string]$TeamDomain = '',
        [string]$Audience = '',
        [string[]]$Emails = @(),
        [bool]$Enabled = $false,
        [string]$AccountId = '',
        [string]$ZoneId = '',
        [string]$TunnelId = '',
        # A parameter, not the script's -Identity read from here: this function is lifted out and
        # run on its own by the tests, and reaching for a variable that only exists when the whole
        # script does is how the last three StrictMode faults happened.
        [string]$SignInWith = 'OneTimePin'
    )
    $config = $script:Config
    if (-not $config.PSObject.Properties['remote']) {
        Set-JsonProperty -Object $config -Name 'remote' -Value ([pscustomobject]@{})
    }
    Set-JsonProperty -Object $config.remote -Name 'enabled' -Value $Enabled
    Set-JsonProperty -Object $config.remote -Name 'hostname' -Value $script:Hostname
    Set-JsonProperty -Object $config.remote -Name 'team_domain' -Value $TeamDomain
    Set-JsonProperty -Object $config.remote -Name 'audience' -Value $Audience
    Set-JsonProperty -Object $config.remote -Name 'allowed_emails' -Value @($Emails)
    # What the uninstaller takes down, and nothing else (empty means "not ours; leave it").
    Set-JsonProperty -Object $config.remote -Name 'account_id' -Value $AccountId
    Set-JsonProperty -Object $config.remote -Name 'zone_id' -Value $ZoneId
    Set-JsonProperty -Object $config.remote -Name 'tunnel_id' -Value $TunnelId
    Set-JsonProperty -Object $config.remote -Name 'tunnel_name' -Value $script:TunnelName
    Set-JsonProperty -Object $config.remote -Name 'dns_record_id' -Value ([string]$script:DnsRecordId)
    Set-JsonProperty -Object $config.remote -Name 'access_app_id' -Value ([string]$script:AccessAppId)
    # Recorded separately from the id: the id says what the object is, the flag says whether this
    # deployment made it. An id with the flag false is something found and left alone, which the
    # uninstaller offers to remove rather than deletes.
    Set-JsonProperty -Object $config.remote -Name 'dns_record_mine' -Value ([bool]$script:DnsRecordMine)
    Set-JsonProperty -Object $config.remote -Name 'access_app_mine' -Value ([bool]$script:AccessAppMine)
    # Which identity provider signs people in, and whether this deployment registered it. Empty
    # means the one-time PIN, which needs nothing and leaves nothing to take down.
    Set-JsonProperty -Object $config.remote -Name 'identity' -Value $SignInWith
    Set-JsonProperty -Object $config.remote -Name 'idp_id' -Value ([string]$script:IdpId)
    Set-JsonProperty -Object $config.remote -Name 'idp_mine' -Value ([bool]$script:IdpMine)
    # Driving a session from a phone is the reason the tunnel exists, so setting one up switches the
    # Session page on. Only on the way up, and only when it is off: turning it back off afterwards
    # is a decision, and re-running this must not undo it.
    if ($Enabled) {
        if (-not $config.PSObject.Properties['session']) {
            Set-JsonProperty -Object $config -Name 'session' -Value ([pscustomobject]@{})
        }
        $already = $config.session.PSObject.Properties['enabled'] -and [bool]$config.session.enabled
        $decided = $config.session.PSObject.Properties['enabled']
        if (-not $already -and -not $decided) {
            Set-JsonProperty -Object $config.session -Name 'enabled' -Value $true
            Write-Status 'Session page switched on: driving a session from your phone is what the tunnel is for. Turn it off under Settings if you would rather it stayed off.' -Level Detail
        }
    }
    Write-JsonFile -Path $script:ConfigFile -Object $config
}

function Show-CloudflarePrerequisites {
    <#
    .SYNOPSIS
        The one-time account steps, printed only for someone who says they need them.
    .DESCRIPTION
        Anyone already running Zero Trust knows all of this, and printing it at them buries the
        steps that matter. The click paths name what to look for as well as where it sits, because
        the dashboard's wording moves.
    #>
    param([string]$PublicHostname = '')
    # The domain is asked for later, once a token can list the zones; until then, say so rather
    # than print an invented one for somebody to type into Cloudflare.
    $zone = if ($PublicHostname) { ($PublicHostname -split '\.' | Select-Object -Last 2) -join '.' } else { 'your domain' }
    Write-Status 'The Cloudflare side, once per account (a browser, about ten minutes)' -Level Done
    Write-Status '1. Sign up at https://dash.cloudflare.com/sign-up - the free plan is enough for this.' -Level Detail
    Write-Status "2. Add the domain: dash.cloudflare.com > Add a site > $zone > Free." -Level Detail
    Write-Status '   Cloudflare gives you two nameservers; set them at whoever you registered the' -Level Detail
    Write-Status '   domain with. The zone stays "Pending" until that propagates, often under an hour.' -Level Detail
    Write-Status '   You need a domain you control: this cannot be done on a domain someone else runs.' -Level Detail
    Write-Status '3. Turn on Zero Trust: pick Zero Trust in the left-hand menu, choose a team name' -Level Detail
    Write-Status '   (it becomes <team>.cloudflareaccess.com and is the team domain below), and take' -Level Detail
    Write-Status '   the Free plan. A card may be asked for even on Free; nothing here is charged.' -Level Detail
    Write-Status '4. Come back here: this run is waiting for you, and will carry on where it is.' -Level Detail
}

function Show-TokenSteps {
    <# How to make a token that can do this job and nothing else. #>
    param([string]$PublicHostname = '',
          # A parameter, not the script's -Identity: this is lifted out and run on its own by
          # the tests, and reaching into script scope is how five StrictMode faults happened.
          [string]$SignInWith = 'OneTimePin')
    # The name is asked for after the token, once the zones can be listed, so until then say "your
    # domain" rather than print an invented one to select in the dashboard.
    $zone = if ($PublicHostname) { ($PublicHostname -split '\.' | Select-Object -Last 2) -join '.' } else { 'your domain' }
    Write-Status 'Create a token that can do this and nothing else (about two minutes)' -Level Done
    Write-Status '1. dash.cloudflare.com > your profile (top right) > API Tokens > Create Token.' -Level Detail
    # Registering an identity provider WRITES one, so that row is Edit rather than Read when a
    # provider was asked for. Printing Read regardless meant following these instructions exactly
    # and then failing at the last step of the run, with the token already made.
    $idpAccess = if ($SignInWith -eq 'OneTimePin') { 'Read' } else { 'Edit' }
    Write-Status '2. Take "Create Custom Token" (the last option, Get started), and add exactly these' -Level Detail
    Write-Status '   four permissions:' -Level Detail
    Write-Status '     Account | Cloudflare Tunnel                                    | Edit' -Level Detail
    Write-Status '     Account | Access: Apps and Policies                            | Edit' -Level Detail
    Write-Status "     Account | Access: Organizations, Identity Providers and Groups | $idpAccess" -Level Detail
    Write-Status '     Zone    | DNS                                                  | Edit' -Level Detail
    if ($SignInWith -ne 'OneTimePin') {
        Write-Status "   (that third one is Edit, not Read, because -Identity $SignInWith registers a provider)" -Level Detail
    }
    Write-Status "3. Account Resources: your account. Zone Resources: Include > Specific zone > $zone." -Level Detail
    Write-Status '4. Continue to summary > Create Token, then copy it. It is shown once.' -Level Detail
    Write-Status 'It cannot read your traffic, touch another zone, or change account settings. Delete it' -Level Detail
    Write-Status 'at the same screen as soon as this finishes; nothing here keeps it.' -Level Detail
}

function Install-TunnelTask {
    <#
    .SYNOPSIS
        Runs the tunnel at every sign-in as this user - no Windows service, no administrator rights.
    .DESCRIPTION
        The tunnel's run token is stored encrypted to this Windows account (DPAPI, CurrentUser) in
        %USERPROFILE%\.aicleaner\cloudflare-tunnel.dpapi; the aicleaner-tunnel task runs
        installer\Start-Tunnel.ps1, which hands it to cloudflared in its environment, never on a
        command line. A Cloudflared service left by an earlier setup is stopped when this account may
        (it would run the same tunnel twice); removing it needs administrator rights once, and the
        message says how. Started now as well, so remote access works without signing out.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$RunToken, [Parameter(Mandatory)][string]$AppRoot)
    $tokenPath = Join-Path $env:USERPROFILE '.aicleaner\cloudflare-tunnel.dpapi'
    $launcher = Join-Path $AppRoot 'installer\Start-Tunnel.ps1'
    if (-not $PSCmdlet.ShouldProcess('aicleaner-tunnel', 'store the tunnel token and register the sign-in task')) { return }
    Add-Type -AssemblyName System.Security
    $bytes = [Text.Encoding]::UTF8.GetBytes($RunToken)
    try {
        [IO.File]::WriteAllBytes($tokenPath, [Security.Cryptography.ProtectedData]::Protect($bytes, $null, 'CurrentUser'))
    } finally { [Array]::Clear($bytes, 0, $bytes.Length) }
    & icacls $tokenPath /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
    $action = New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`"" -WorkingDirectory $AppRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName 'aicleaner-tunnel' -Action $action -Trigger $trigger -Settings $settings `
        -Description 'aicleaner remote access: the Cloudflare tunnel, as this user' -Force | Out-Null
    $service = Get-Service -Name 'Cloudflared' -ErrorAction SilentlyContinue
    if ($service) {
        try { Stop-Service -Name 'Cloudflared' -Force -ErrorAction Stop; Set-Service -Name 'Cloudflared' -StartupType Disabled -ErrorAction Stop }
        catch {
            Write-Status 'A Cloudflared Windows service from an earlier setup is still installed and would run this tunnel twice.' -Level Warn
            Write-Status 'Remove it once from an elevated PowerShell:  cloudflared service uninstall' -Level Detail
        }
    }
    Start-ScheduledTask -TaskName 'aicleaner-tunnel'
    Write-Status 'Tunnel task aicleaner-tunnel registered and started' -Level Detail
}

function Get-TokenStorePath {
    <# Where the API token is kept between setup and teardown, beside the rest of the local state. #>
    [OutputType([string])]
    param()
    return (Join-Path $env:USERPROFILE '.aicleaner\cloudflare-token.dpapi')
}

function Save-CloudflareToken {
    <#
    .SYNOPSIS
        Keeps the API token on this machine, encrypted to this Windows account, so the uninstaller
        can take the tunnel down without asking for a new one.
    .DESCRIPTION
        DPAPI with CurrentUser scope: the key comes from the logon secret, so another account on
        this machine - an administrator included - cannot read it back. The file's ACL is narrowed
        as well, which stops casual reading; DPAPI is what actually protects the bytes.

        The token stays narrowly scoped on purpose (zone DNS, tunnels, Access apps, account read).
        It deliberately cannot delete itself: that needs User > API Tokens > Edit, which would let
        anyone holding this file mint and revoke tokens across the whole account. The uninstaller
        prints the two clicks for deleting it instead.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][System.Security.SecureString]$Secure)
    $path = Get-TokenStorePath
    if (-not $PSCmdlet.ShouldProcess($path, 'Store the Cloudflare API token (encrypted to this account)')) { return }
    $folder = Split-Path -Parent $path
    if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
    $plain = ConvertFrom-SecureToken -Secure $Secure
    try {
        Add-Type -AssemblyName System.Security
        $bytes = [Text.Encoding]::UTF8.GetBytes($plain)
        $protected = [Security.Cryptography.ProtectedData]::Protect($bytes, $null, 'CurrentUser')
        [IO.File]::WriteAllBytes($path, $protected)
        [Array]::Clear($bytes, 0, $bytes.Length)
    } finally { $plain = $null }
    # Owner only: inheritance off, then this account alone.
    & icacls $path /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
    Write-Status "API token stored for teardown at $path (encrypted to this Windows account)" -Level Detail
}

function Read-StoredCloudflareToken {
    <# The token Save-CloudflareToken kept, as a SecureString; $null when there is none or it cannot be read. #>
    [OutputType([System.Security.SecureString])]
    param()
    $path = Get-TokenStorePath
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try {
        Add-Type -AssemblyName System.Security
        $bytes = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($path), $null, 'CurrentUser')
        $secure = New-Object System.Security.SecureString
        foreach ($c in [Text.Encoding]::UTF8.GetChars($bytes)) { $secure.AppendChar($c) }
        [Array]::Clear($bytes, 0, $bytes.Length)
        $secure.MakeReadOnly()
        return $secure
    } catch {
        return $null
    }
}

function ConvertFrom-SecureToken {
    <# The token as plain text for the Authorization header, freed straight after. #>
    [OutputType([string])]
    param([Parameter(Mandatory)][System.Security.SecureString]$Secure)
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
}

function Invoke-CloudflareApi {
    <#
    .SYNOPSIS
        One Cloudflare API call, returning the result, and naming the missing permission when the
        token is not allowed to make it.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Token,
        [Parameter(Mandatory)][string]$Path,
        [string]$Method = 'GET',
        $Body = $null,
        [string]$Needs = ''
    )
    $arguments = @{
        Uri         = "https://api.cloudflare.com/client/v4$Path"
        Method      = $Method
        Headers     = @{ Authorization = "Bearer $Token" }
        ContentType = 'application/json'
        ErrorAction = 'Stop'
    }
    if ($null -ne $Body) { $arguments['Body'] = ($Body | ConvertTo-Json -Depth 8 -Compress) }
    try {
        $response = Invoke-RestMethod @arguments
    } catch {
        $status = 0
        if ($_.Exception.PSObject.Properties['Response'] -and $_.Exception.Response) {
            try { $status = [int]$_.Exception.Response.StatusCode } catch { $status = 0 }
        }
        if ($status -eq 403 -and $Needs) {
            throw "The token is not allowed to do this. Add the permission: $Needs"
        }
        if ($status -eq 401) { throw 'Cloudflare refused the token (401): check it was copied whole and has not been deleted' }
        throw ("Cloudflare API {0} {1} failed: {2}" -f $Method, $Path, $_.Exception.Message)
    }
    if ($response.PSObject.Properties['success'] -and -not $response.success) {
        $messages = @($response.errors | ForEach-Object { "$($_.code): $($_.message)" })
        throw ("Cloudflare refused: {0}" -f (($messages | Select-Object -First 2) -join '; '))
    }
    return $response.result
}

function Get-CloudflareTargets {
    <#
    .SYNOPSIS
        The zone this hostname belongs to, and the account that owns it.
    .DESCRIPTION
        The zone comes first and the account is the zone's own. Listing /accounts needs "Account
        Settings: Read", which the token deliberately does not have - so a token built exactly as
        instructed got an empty list and the run stopped at "That token can see no account". Every
        zone names the account it belongs to, and that is the right account even when a token can
        see several.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Token, [Parameter(Mandatory)][string]$PublicHostname)
    # The zone is the longest registered name that this hostname sits under.
    $zones = @(Invoke-CloudflareApi -Token $Token -Path '/zones?per_page=50' -Needs 'Zone | DNS | Edit')
    $match = @($zones | Where-Object { $PublicHostname -eq $_.name -or $PublicHostname.EndsWith("." + $_.name) } |
        Sort-Object { $_.name.Length } -Descending | Select-Object -First 1)
    if ($match.Count -eq 0) {
        throw "No zone in this account covers $PublicHostname; add the domain to Cloudflare, or give the token access to that zone, and this run picks it up"
    }
    $zone = $match[0]
    $accountId = ''
    $accountName = ''
    if ($zone.PSObject.Properties['account'] -and $zone.account) {
        $accountId = [string]$zone.account.id
        $accountName = [string]$zone.account.name
    }
    if (-not $accountId) {
        # Only when the zone did not say: a token that can list accounts still works this way.
        $accounts = @(Invoke-CloudflareApi -Token $Token -Path '/accounts' -Needs 'Account | Cloudflare Tunnel | Edit')
        if ($accounts.Count -eq 0) {
            throw "Could not tell which account owns $($zone.name); check the token's Account Resources is set to your account"
        }
        $accountId = [string]$accounts[0].id
        $accountName = [string]$accounts[0].name
    }
    return [pscustomobject]@{ AccountId = $accountId; AccountName = $accountName
                              ZoneId = $zone.id; ZoneName = $zone.name }
}

function Get-TeamDomain {
    <# <team>.cloudflareaccess.com, which the proxy needs to fetch the signing keys. #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Token, [Parameter(Mandatory)][string]$AccountId)
    $org = Invoke-CloudflareApi -Token $Token -Path "/accounts/$AccountId/access/organizations" `
        -Needs 'Account | Access: Organizations, Identity Providers and Groups | Read'
    if (-not $org -or -not $org.PSObject.Properties['auth_domain'] -or -not $org.auth_domain) {
        throw 'Zero Trust is not set up on this account yet: open Zero Trust in the dashboard and choose a team name (this run waits for you)'
    }
    return [string]$org.auth_domain
}

function Show-IdentityPrerequisites {
    <#
    .SYNOPSIS
        The browser work this script cannot do, printed so it can be followed and come back.
    .DESCRIPTION
        Cloudflare has exactly one sign-in method that needs nothing configured anywhere, and that
        is the one-time PIN. Everything else is an OAuth client in somebody else's console, which
        means a browser and a human. So this does what the API-token instructions already do: says
        what to make, what to tick, and what to bring back.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateSet('Google', 'Microsoft')][string]$Provider,
          [Parameter(Mandatory)][string]$TeamDomain)
    # Get-TeamDomain returns the organisation's auth_domain, which is ALREADY
    # <team>.cloudflareaccess.com - appending the suffix again produced
    # <team>.cloudflareaccess.com.cloudflareaccess.com, and Google refused the sign-in with
    # redirect_uri_mismatch at the very end, after everything else had worked. Accepts a bare team
    # name too, because that is what somebody reading the dashboard is likely to type.
    $auth = ($TeamDomain -replace '^https?://', '').Trim().TrimEnd('/')
    if ($auth -notlike '*.cloudflareaccess.com') { $auth = "$auth.cloudflareaccess.com" }
    $callback = "https://$auth/cdn-cgi/access/callback"
    Write-Host ''
    Write-Status "Setting up $Provider sign-in needs one thing made in $Provider's own console first"
    Write-Host ''
    if ($Provider -eq 'Google') {
        Write-Host '   1. Open https://console.cloud.google.com/apis/credentials'
        Write-Host '   2. Create credentials > OAuth client ID > Web application'
        Write-Host '   3. Under "Authorised redirect URIs" add exactly:'
        Write-Host "         $callback" -ForegroundColor Cyan
        Write-Host '   4. Create, then copy the Client ID and Client secret'
        Write-Host ''
        Write-Host '   If it asks you to configure a consent screen first, "External" with your own'
        Write-Host '   address as the only test user is enough: nobody else ever signs in to this.'
    } else {
        Write-Host '   A personal account (outlook.com, hotmail.com, live.com) works here - this is'
        Write-Host '   not work-accounts-only. The one thing nobody mentions: an app registration is'
        Write-Host '   made INSIDE an Entra directory even for a personal sign-in. Signing in to'
        Write-Host '   entra.microsoft.com with a personal account gives you a free one, and'
        Write-Host '   registrations cost nothing - a hurdle, not a blocker. Google needs no'
        Write-Host '   directory at all if you would rather skip it.'
        Write-Host ''
        Write-Host '   1. Open https://entra.microsoft.com > Applications > App registrations > New'
        Write-Host '   2. Supported account types decides whether a personal account can sign in:'
        Write-Host '         work or school only        -> "Accounts in this organizational directory only"'
        Write-Host '         a personal account as well -> "Accounts in any organizational directory'
        Write-Host '                                       and personal Microsoft accounts"'
        Write-Host '      An outlook.com or hotmail.com address needs the second one.' -ForegroundColor Cyan
        Write-Host '   3. Redirect URI (Web), exactly:'
        Write-Host "         $callback" -ForegroundColor Cyan
        Write-Host '   4. Register, then Certificates & secrets > New client secret, and copy it'
        Write-Host '   5. From the Overview page copy the Application (client) ID'
        Write-Host '   6. API permissions > add Microsoft Graph delegated: openid, email, profile,'
        Write-Host '      offline_access - then Grant admin consent if the button is offered'
    }
    Write-Host ''
}


function New-IdentityProvider {
    <#
    .SYNOPSIS
        Registers the identity provider at Cloudflare and returns its id, or '' for the PIN.
    .DESCRIPTION
        The allow policy needs no change for any of this: it matches the address the provider
        asserts, whoever asserts it. So your-address@example.com on the policy plus Google configured
        is the whole of it.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)][string]$Token,
        [Parameter(Mandatory)][string]$AccountId,
        [Parameter(Mandatory)][string]$Provider,
        [Parameter(Mandatory)][string]$TeamDomain,
        # Supplied, this runs without asking anything, which is what makes it testable and what an
        # unattended re-run needs. Absent, it prints the console steps and prompts, as before.
        [string]$ClientId = '',
        [System.Security.SecureString]$ClientSecret,
        [ValidateSet('', 'personal', 'work')][string]$AccountKind = ''
    )
    if ($Provider -eq 'OneTimePin') { return '' }
    $needs = 'Account | Access: Organizations, Identity Providers and Groups | Edit'
    $wanted = if ($Provider -eq 'Google') { 'google' } else { 'azureAD' }
    $existing = @(Invoke-CloudflareApi -Token $Token -Path "/accounts/$AccountId/access/identity_providers" -Needs $needs |
        Where-Object { $_.type -eq $wanted })
    if ($existing.Count -gt 0) {
        Write-Status "A $Provider identity provider already exists at Cloudflare; using it" -Level Detail
        $script:IdpId = [string]$existing[0].id
        return $script:IdpId
    }

    if (-not $ClientId -or -not $ClientSecret) {
        Show-IdentityPrerequisites -Provider $Provider -TeamDomain $TeamDomain
        if (-not (Confirm-Action -Message "Done? Continue and register $Provider with Cloudflare" -Default 'Yes')) {
            # Half-configured is the state worth never reaching: a provider with no OAuth client
            # behind it refuses every sign-in, from a page that no longer offers the PIN.
            Write-Status "Left on one-time PIN. Re-run with -Identity $Provider when the console side is ready." -Level Warn
            return ''
        }
    }
    if (-not $ClientId) { $ClientId = Read-Value -Message "$Provider client ID" -Default '' }
    if (-not $ClientId) { throw "A client ID is needed to register $Provider" }
    if (-not $ClientSecret) { $ClientSecret = Read-Host -AsSecureString -Prompt "$Provider client secret" }
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ClientSecret))
    if (-not $plain) { throw "A client secret is needed to register $Provider" }

    $config = @{ client_id = $ClientId; client_secret = $plain }
    if ($Provider -eq 'Microsoft') {
        # "common" lets a personal Microsoft account in as well as a work one, and only works if the
        # app registration was made for both. A tenant id restricts it to that directory. Getting
        # this wrong fails at sign-in with a message that does not explain itself, so it is asked
        # rather than guessed.
        if (-not $AccountKind) {
            $AccountKind = if (Confirm-Action -Message 'Will you sign in with a personal Microsoft account (outlook.com, hotmail.com)?' -Default 'Yes') { 'personal' } else { 'work' }
        }
        $directory = if ($AccountKind -eq 'personal') { 'common' } else { Read-Value -Message 'Entra directory (tenant) ID' -Default '' }
        if (-not $directory) { throw 'A directory ID is needed for a work-account-only registration' }
        $config['directory_id'] = $directory
        $config['support_groups'] = $false
    }
    if (-not $PSCmdlet.ShouldProcess($Provider, 'Register the identity provider')) { return '' }
    $idp = Invoke-CloudflareApi -Token $Token -Path "/accounts/$AccountId/access/identity_providers" `
        -Method 'POST' -Needs $needs -Body @{ name = "aicleaner ($Provider)"; type = $wanted; config = $config }
    $script:IdpId = [string]$idp.id
    $script:IdpMine = [bool]$script:IdpId
    Write-Status "$Provider registered with Cloudflare" -Level Done
    return $script:IdpId
}


function Set-ApplicationIdentity {
    <#
    .SYNOPSIS
        Pin the application to one provider, so the PIN route is not left open beside it.
    .DESCRIPTION
        PUT, not PATCH: the Access applications endpoint answers PATCH with 405 Method Not Allowed.
        And PUT there REPLACES the application rather than merging into it, so the current one is
        read first and the two fields are set on top of what it already says. Sending only the two
        would blank the name, the domain and the type, which is a worse failure than the 405 -
        the hostname would stop being guarded at all while appearing to succeed.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Token, [Parameter(Mandatory)][string]$AccountId,
          [Parameter(Mandatory)][string]$AppId, [Parameter(Mandatory)][string]$IdpId,
          # Going straight to the provider saves a click and tells an unauthenticated visitor which
          # account guards the address: Google names it on its own sign-in page. With the one-time
          # PIN you had to know the address already. Off by default, because a click is cheaper than
          # a disclosure and nobody asked for the click to be saved.
          [switch]$SkipTheChooser)
    if (-not $AppId -or -not $IdpId) { return }
    if (-not $PSCmdlet.ShouldProcess($AppId, 'Pin the application to the identity provider')) { return }
    $needs = 'Account | Access: Apps and Policies | Edit'
    $app = Invoke-CloudflareApi -Token $Token -Path "/accounts/$AccountId/access/apps/$AppId" -Needs $needs
    if (-not $app) { throw "Could not read the Access application $AppId to pin it" }
    $body = @{
        name                      = [string]$app.name
        domain                    = [string]$app.domain
        type                      = [string]$app.type
        allowed_idps              = @($IdpId)
        auto_redirect_to_identity = [bool]$SkipTheChooser
    }
    # Carried over only when the application has them, so a PUT never invents a value.
    foreach ($field in 'session_duration', 'app_launcher_visible', 'skip_interstitial') {
        if ($app.PSObject.Properties[$field] -and $null -ne $app.$field) { $body[$field] = $app.$field }
    }
    Invoke-CloudflareApi -Token $Token -Path "/accounts/$AccountId/access/apps/$AppId" -Method 'PUT' `
        -Needs $needs -Body $body | Out-Null
    if ($SkipTheChooser) {
        Write-Status 'Sign-in is that provider only, and goes straight to it' -Level Detail
        Write-Status 'Anyone opening the address sees which account guards it; -SkipTheChooser is what does that.' -Level Warn
    } else {
        Write-Status 'Sign-in is that provider only. Somebody opening the address gets a sign-in button and learns nothing from it.' -Level Detail
    }
}


function New-AccessApplication {
    <#
    .SYNOPSIS
        The Access application in front of the hostname, and the allow policy, returning the AUD tag.
    .DESCRIPTION
        One-time PIN by default: Cloudflare emails a code to the addresses on the policy, so no
        identity provider, app registration or tenant is needed and this can finish without a
        browser. Only the listed addresses get in.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)][string]$Token,
        [Parameter(Mandatory)][string]$AccountId,
        [Parameter(Mandatory)][string]$PublicHostname,
        [Parameter(Mandatory)][string[]]$Emails
    )
    $needs = 'Account | Access: Apps and Policies | Edit'
    $existing = @(Invoke-CloudflareApi -Token $Token -Path "/accounts/$AccountId/access/apps" -Needs $needs |
        Where-Object { $_.domain -eq $PublicHostname })
    if ($existing.Count -gt 0) {
        Write-Status "An Access application already covers $PublicHostname; leaving it as it is" -Level Warn
        # Nothing was made here, so nothing is claimed: whatever the last run recorded stands. The
        # id is recorded regardless, because the uninstaller cannot offer to remove an application
        # it cannot name, and one it never learned is left guarding a hostname that has gone.
        $script:AccessAppId = [string]$existing[0].id
        return [string]$existing[0].aud
    }
    if (-not $PSCmdlet.ShouldProcess($PublicHostname, 'Create the Access application')) { return '' }
    $app = Invoke-CloudflareApi -Token $Token -Path "/accounts/$AccountId/access/apps" -Method 'POST' -Needs $needs -Body @{
        name             = "aicleaner ($PublicHostname)"
        domain           = $PublicHostname
        type             = 'self_hosted'
        session_duration = '24h'
    }
    Write-Status "Access application created for $PublicHostname" -Level Detail
    $include = @($Emails | ForEach-Object { @{ email = @{ email = $_ } } })
    Invoke-CloudflareApi -Token $Token -Path "/accounts/$AccountId/access/apps/$($app.id)/policies" `
        -Method 'POST' -Needs $needs -Body @{
            name     = 'aicleaner: the addresses allowed in'
            decision = 'allow'
            include  = $include
        } | Out-Null
    Write-Status ("Allow policy: {0}" -f ($Emails -join ', ')) -Level Detail
    # Recorded so the uninstaller removes this application and no other.
    $script:AccessAppId = [string]$app.id
    $script:AccessAppMine = [bool]$script:AccessAppId
    return [string]$app.aud
}

function New-ApiTunnel {
    <# A tunnel Cloudflare holds the configuration for, returning its id and run token. #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)][string]$Token,
        [Parameter(Mandatory)][string]$AccountId,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$PublicHostname,
        [Parameter(Mandatory)][int]$LocalPort
    )
    $needs = 'Account | Cloudflare Tunnel | Edit'
    $tunnel = $null
    $existing = @(Invoke-CloudflareApi -Token $Token -Path "/accounts/$AccountId/cfd_tunnel?name=$Name&is_deleted=false" -Needs $needs)
    if ($existing.Count -gt 0) {
        $tunnel = $existing[0]
        Write-Status "Reusing the tunnel $Name" -Level Detail
    } else {
        if (-not $PSCmdlet.ShouldProcess($Name, 'Create the tunnel')) { return $null }
        $tunnel = Invoke-CloudflareApi -Token $Token -Path "/accounts/$AccountId/cfd_tunnel" -Method 'POST' -Needs $needs -Body @{
            name = $Name; config_src = 'cloudflare'
        }
        Write-Status "Tunnel $Name created" -Level Detail
    }
    Set-TunnelRoute -Token $Token -AccountId $AccountId -TunnelId ([string]$tunnel.id) `
        -PublicHostname $PublicHostname -LocalPort $LocalPort
    $runToken = Invoke-CloudflareApi -Token $Token -Path "/accounts/$AccountId/cfd_tunnel/$($tunnel.id)/token" -Needs $needs
    return [pscustomobject]@{ Id = [string]$tunnel.id; RunToken = [string]$runToken }
}

function Set-TunnelRoute {
    <# Which name the tunnel answers to, and where it sends it: the proxy, on this machine only. #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)][string]$Token,
        [Parameter(Mandatory)][string]$AccountId,
        [Parameter(Mandatory)][string]$TunnelId,
        [Parameter(Mandatory)][string]$PublicHostname,
        [Parameter(Mandatory)][int]$LocalPort
    )
    if (-not $PSCmdlet.ShouldProcess($PublicHostname, 'Route the hostname to the proxy')) { return }
    Invoke-CloudflareApi -Token $Token -Path "/accounts/$AccountId/cfd_tunnel/$TunnelId/configurations" `
        -Method 'PUT' -Needs 'Account | Cloudflare Tunnel | Edit' -Body @{
            config = @{ ingress = @(
                @{ hostname = $PublicHostname; service = "http://127.0.0.1:$LocalPort" },
                @{ service = 'http_status:404' }
            ) }
        } | Out-Null
}

function Rename-AccessApplication {
    <#
    .SYNOPSIS
        Moves the Access application to a new hostname, keeping everything else about it.
    .DESCRIPTION
        The same application, so the same AUD tag: the proxy's config needs no new audience, and
        the allow policy and sign-in method come along untouched. Cloudflare replaces the whole
        application on a PUT, so what it already has is read first and sent back with the domain
        and name changed.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)][string]$Token,
        [Parameter(Mandatory)][string]$AccountId,
        [Parameter(Mandatory)][string]$AppId,
        [Parameter(Mandatory)][string]$PublicHostname
    )
    $needs = 'Account | Access: Apps and Policies | Edit'
    $app = Invoke-CloudflareApi -Token $Token -Path "/accounts/$AccountId/access/apps/$AppId" -Needs $needs
    if (-not $app) { throw "The Access application $AppId was not found; rename it in the Zero Trust dashboard" }
    $body = @{ name = "aicleaner ($PublicHostname)"; domain = $PublicHostname }
    foreach ($keep in 'type', 'session_duration', 'allowed_idps', 'auto_redirect_to_identity', 'app_launcher_visible') {
        if ($app.PSObject.Properties[$keep]) { $body[$keep] = $app.$keep }
    }
    if (-not $PSCmdlet.ShouldProcess($PublicHostname, 'Move the Access application')) { return }
    Invoke-CloudflareApi -Token $Token -Path "/accounts/$AccountId/access/apps/$AppId" -Method 'PUT' -Needs $needs -Body $body | Out-Null
    Write-Status "Access application now guards $PublicHostname" -Level Detail
}

function Test-PublicName {
    <#
    Whether the new name answers from the outside yet - any HTTP answer at all, Cloudflare Access's
    sign-in included, means DNS and the tunnel reach it. Tried for up to a minute: a new record can
    take a moment to be served.
    #>
    [OutputType([bool])]
    param([Parameter(Mandatory)][string]$PublicHostname, [int]$Seconds = 60)
    $until = (Get-Date).AddSeconds($Seconds)
    do {
        try {
            Invoke-WebRequest -Uri "https://$PublicHostname/" -Method Head -MaximumRedirection 0 -UseBasicParsing -TimeoutSec 10 | Out-Null
            return $true
        } catch {
            $response = $_.Exception.PSObject.Properties['Response']
            if ($response -and $response.Value) { return $true }       # a 3xx/4xx is an answer
        }
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $until)
    return $false
}

function Get-RemoteValue {
    <# One field of the config's remote block as text; empty when the block or the field is missing. #>
    [OutputType([string])]
    param($Remote, [Parameter(Mandatory)][string]$Name)
    if ($Remote -and $Remote.PSObject.Properties[$Name]) { return [string]$Remote.$Name }
    return ''
}

function Invoke-Rename {
    <#
    .SYNOPSIS
        Gives this machine's remote-control address a new name: the same tunnel, the same Access
        application, the same sign-in.
    .DESCRIPTION
        In an order that never leaves the address dead: the new DNS record first, then the tunnel's
        route and the Access application, then the config and a proxy restart. The old record goes
        last - only once the new name answers, and only if this deployment made it; otherwise it says
        what to remove by hand. Browsers sign in to Access again at the new name, and with the
        encrypted tunnel on each device enters the code again (browsers keep it per address).
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Token, [Parameter(Mandatory)][string]$NewHostname)
    $remote = if ($script:Config.PSObject.Properties['remote']) { $script:Config.remote } else { $null }
    $oldHostname = Get-RemoteValue -Remote $remote -Name 'hostname'
    $accountId = Get-RemoteValue -Remote $remote -Name 'account_id'
    $tunnelId = Get-RemoteValue -Remote $remote -Name 'tunnel_id'
    $appId = Get-RemoteValue -Remote $remote -Name 'access_app_id'
    if (-not $oldHostname -or -not $accountId -or -not $tunnelId) {
        throw ('This deployment was set up in the dashboard, so there is nothing recorded to rename with. ' +
               'In the Cloudflare dashboard: add a public hostname to the tunnel, change the Access ' +
               'application''s domain, then set remote.hostname in aicleaner-config.json and restart.')
    }
    $NewHostname = $NewHostname.Trim().ToLower()
    if ($NewHostname -eq $oldHostname.ToLower()) { throw "The address is already $oldHostname" }
    $oldRecord = Get-RemoteValue -Remote $remote -Name 'dns_record_id'
    $oldMine = $remote.PSObject.Properties['dns_record_mine'] -and [bool]$remote.dns_record_mine
    $oldZone = Get-RemoteValue -Remote $remote -Name 'zone_id'
    $port = if ($script:ProxyPort) { [int]$script:ProxyPort } else { 8787 }

    $targets = Get-CloudflareTargets -Token $Token -PublicHostname $NewHostname
    Write-Status "Renaming $oldHostname to $NewHostname" -Level Detail
    Set-TunnelDns -Token $Token -ZoneId $targets.ZoneId -PublicHostname $NewHostname -TunnelId $tunnelId
    Set-TunnelRoute -Token $Token -AccountId $accountId -TunnelId $tunnelId -PublicHostname $NewHostname -LocalPort $port
    if ($appId) {
        Rename-AccessApplication -Token $Token -AccountId $accountId -AppId $appId -PublicHostname $NewHostname
    } else {
        Write-Status "No Access application is recorded: change its domain to $NewHostname in the Zero Trust dashboard before using the new address" -Level Warn
    }
    if (-not $PSCmdlet.ShouldProcess($script:ConfigFile, "Set remote.hostname to $NewHostname")) { return }
    Set-JsonProperty -Object $remote -Name 'hostname' -Value $NewHostname
    Set-JsonProperty -Object $remote -Name 'zone_id' -Value ([string]$targets.ZoneId)
    Set-JsonProperty -Object $remote -Name 'dns_record_id' -Value ([string]$script:DnsRecordId)
    Set-JsonProperty -Object $remote -Name 'dns_record_mine' -Value ([bool]$script:DnsRecordMine)
    Write-JsonFile -Path $script:ConfigFile -Object $script:Config
    # The guard that lets the tunnel's name in is built when the proxy starts.
    Restart-AiCleanerProxy -Port $port | Out-Null

    if (-not (Test-PublicName -PublicHostname $NewHostname)) {
        Write-Status "$NewHostname does not answer yet, so the old record for $oldHostname was left in place. Remove it in the Cloudflare dashboard (DNS) once the new name works." -Level Warn
    } elseif ($oldRecord -and $oldMine -and $oldZone -and $oldRecord -ne [string]$script:DnsRecordId) {
        if ($PSCmdlet.ShouldProcess($oldHostname, 'Delete the old DNS record')) {
            Invoke-CloudflareApi -Token $Token -Path "/zones/$oldZone/dns_records/$oldRecord" -Method 'DELETE' -Needs 'Zone | DNS | Edit' | Out-Null
            Write-Status "Removed the old record for $oldHostname" -Level Detail
        }
    } else {
        Write-Status "The record for $oldHostname was not made by this deployment, so it was left alone; remove it in the Cloudflare dashboard if nothing else uses it." -Level Detail
    }
    Write-Status "Remote control is now at https://$NewHostname - sign in again there; with the encrypted tunnel on, enter the code again on each device." -Level Done
}

function Set-TunnelDns {
    <# The CNAME that sends the hostname into the tunnel, proxied so Access can sit in front. #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)][string]$Token,
        [Parameter(Mandatory)][string]$ZoneId,
        [Parameter(Mandatory)][string]$PublicHostname,
        [Parameter(Mandatory)][string]$TunnelId
    )
    $needs = 'Zone | DNS | Edit'
    $content = "$TunnelId.cfargotunnel.com"
    $record = @{ type = 'CNAME'; name = $PublicHostname; content = $content; proxied = $true; comment = 'aicleaner remote control' }
    $existing = @(Invoke-CloudflareApi -Token $Token -Path "/zones/$ZoneId/dns_records?name=$PublicHostname" -Needs $needs)
    if ($existing.Count -gt 0) {
        if ($existing[0].content -eq $content) {
            Write-Status "DNS already points $PublicHostname at the tunnel" -Level Detail
            # Recorded even though nothing was made here: the uninstaller cannot offer to remove
            # what it cannot name, and an id it never learned is an object left behind for good.
            # Whether it is ours is left as whatever a previous run claimed.
            $script:DnsRecordId = [string]$existing[0].id
            return
        }
        if ([string]$existing[0].content -match '\.cfargotunnel\.com$') {
            # Another machine's deployment owns this name. Taking it would silently break that one.
            throw ("$PublicHostname already serves another tunnel ($($existing[0].content)). " +
                   'Give this machine its own name (for example claude-vm.' + $PublicHostname.Substring($PublicHostname.IndexOf('.') + 1) + ') and run this again.')
        }
        if (-not $PSCmdlet.ShouldProcess($PublicHostname, "Repoint the existing $($existing[0].type) record")) { return }
        Invoke-CloudflareApi -Token $Token -Path "/zones/$ZoneId/dns_records/$($existing[0].id)" -Method 'PUT' -Needs $needs -Body $record | Out-Null
        Write-Status "Repointed $PublicHostname at the tunnel" -Level Detail
        # The name existed before this ran, serving something else, so it is not ours - but it is
        # recorded anyway so the uninstaller can offer to put it beyond use rather than silently
        # leaving a record aimed at a tunnel that will not exist.
        $script:DnsRecordId = [string]$existing[0].id
        $script:DnsRecordMine = $false
        return
    }
    if (-not $PSCmdlet.ShouldProcess($PublicHostname, 'Create the DNS record')) { return }
    $created = Invoke-CloudflareApi -Token $Token -Path "/zones/$ZoneId/dns_records" -Method 'POST' -Needs $needs -Body $record
    # Ours, so the uninstaller can take it away again. An answer without an id (a stub, or an API
    # that changed shape) leaves it empty, which the uninstaller reads as "not ours to delete".
    $script:DnsRecordId = if ($created -and $created.PSObject.Properties['id']) { [string]$created.id } else { '' }
    $script:DnsRecordMine = [bool]$script:DnsRecordId
    Write-Status "$PublicHostname now points at the tunnel" -Level Detail
}

function Show-AccessSteps {
    <# What has to be done in the Cloudflare dashboard, which has no CLI worth scripting. #>
    param([Parameter(Mandatory)][string]$PublicHostname, [Parameter(Mandatory)][string]$Provider)
    $idp = if ($Provider -eq 'entra') {
        'Azure AD / Microsoft Entra ID (a work tenant: your normal sign-in and Conditional Access apply)'
    } else {
        'Microsoft Account (a personal account: no tenant needed)'
    }
    Write-Status 'Now put Access in front of it (Cloudflare dashboard, once)' -Level Done
    Write-Status "1. Zero Trust > Settings > Authentication > Login methods: add $idp" -Level Detail
    Write-Status '   Skip this step to sign in with a one-time PIN instead: Cloudflare emails a code' -Level Detail
    Write-Status '   to the addresses on the policy below, and it needs no setup at all.' -Level Detail
    Write-Status '2. Zero Trust > Access > Applications > Add an application > Self-hosted' -Level Detail
    Write-Status "   Application domain: $PublicHostname" -Level Detail
    Write-Status '   Identity providers: the one you just added' -Level Detail
    Write-Status '3. Add a policy: Allow, with Emails = your address (only what you list gets in)' -Level Detail
    Write-Status '4. Open the application''s Overview and copy its Application Audience (AUD) tag' -Level Detail
    Write-Status '5. Put the AUD tag, your team domain and your address in aicleaner-config.json:' -Level Detail
    Write-Status '     "remote": { "enabled": true, "hostname": "...", "team_domain": "<team>.cloudflareaccess.com",' -Level Detail
    Write-Status '                 "audience": "<AUD tag>", "allowed_emails": ["you@example.com"] }' -Level Detail
    Write-Status '   then restart the proxy. Until those are set, the hostname serves nothing.' -Level Detail
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

Write-ContactAttribution

try {
    $root = $script:AppFolder
    if (-not $ConfigPath) { $ConfigPath = Join-Path $root 'aicleaner-config.json' }
    $config = Read-JsonFile -Path $ConfigPath
    if (-not $config) { throw "No aicleaner-config.json at $ConfigPath" }
    if ($Port -le 0) {
        $Port = if ($config.PSObject.Properties['port']) { [int]$config.port } else { 8787 }
    }
    if (-not $TunnelName) { $TunnelName = "aicleaner-$($env:COMPUTERNAME.ToLower())" }
    $exe = Get-Cloudflared
    # What both paths read. One machine, one tunnel, one hostname: another machine gets its own.
    $script:Config = $config
    $script:ConfigFile = $ConfigPath
    $script:ProxyPort = $Port
    $script:TunnelName = $TunnelName
    $script:CloudflaredExe = $exe
    $script:ApiTokenSecure = $ApiToken
    # From the setup wizard (feature 106): the token arrives in this process's environment, never on
    # its command line, and leaves the environment at once.
    if (-not $script:ApiTokenSecure -and $env:AICLEANER_CLOUDFLARE_TOKEN) {
        $script:ApiTokenSecure = ConvertTo-SecureString -String $env:AICLEANER_CLOUDFLARE_TOKEN -AsPlainText -Force
        Remove-Item Env:\AICLEANER_CLOUDFLARE_TOKEN -ErrorAction SilentlyContinue
    }
    $script:Hostname = $Hostname
    $script:Emails = @($Email | Where-Object { $_ })
    Set-OwnedIds -Config $config      # before anything can reuse instead of create

    # --- Rename ---------------------------------------------------------------
    if ($Rename) {
        $current = if ($config.PSObject.Properties['remote'] -and $config.remote.PSObject.Properties['hostname']) { [string]$config.remote.hostname } else { '' }
        if (-not $current) { throw 'There is no remote-control address to rename yet: run this without -Rename to set one up' }
        $newName = if ($Hostname) { $Hostname } else { Read-Value -Message "New name for $current" -Default '' }
        if (-not $newName) { throw 'A new name is needed' }
        $secure = if ($ApiToken) { $ApiToken } else { Read-StoredCloudflareToken }
        if (-not $secure) {
            Show-TokenSteps -PublicHostname $newName -SignInWith $Identity
            $secure = Read-Host -Prompt 'Paste the API token (hidden)' -AsSecureString
        }
        $token = ConvertFrom-SecureToken -Secure $secure
        try { Invoke-Rename -Token $token -NewHostname $newName }
        finally { $token = $null; [GC]::Collect() }
        return
    }

    # --- Remove ---------------------------------------------------------------
    if ($Remove) {
        Write-Status 'Removing the tunnel'
        if ($PSCmdlet.ShouldProcess('cloudflared service', 'Uninstall')) {
            Invoke-Cloudflared -Exe $exe -Arguments @('service', 'uninstall') -AllowFailure | Out-Null
        }
        if ($PSCmdlet.ShouldProcess($TunnelName, 'Delete the tunnel')) {
            Invoke-Cloudflared -Exe $exe -Arguments @('tunnel', 'delete', '-f', $TunnelName) -AllowFailure | Out-Null
        }
        if ($config.PSObject.Properties['remote']) {
            Set-JsonProperty -Object $config.remote -Name 'enabled' -Value $false
            Write-JsonFile -Path $ConfigPath -Object $config
        }
        Write-Status 'Tunnel removed; remote access is off (restart the proxy)' -Level Done
        return
    }

    # --- Create ---------------------------------------------------------------
    Write-Status "Cloudflare terminates TLS at its edge: it can see the session in clear. That is the accepted trade for reaching this from a phone." -Level Warn
    # One tunnel and one hostname per machine: a desktop and a VS Code server are two deployments,
    # and pointing both at one name would leave whichever connected last serving everything.
    Write-Status "This machine gets its own tunnel ($TunnelName); another machine needs its own name." -Level Detail
    Write-Status 'This needs a domain you own whose DNS Cloudflare runs. The address will be a new name' -Level Detail
    Write-Status 'under it, created here; a domain someone else controls cannot be used.' -Level Detail
    if (-not (Confirm-Action -Message "Reach this machine's pages (127.0.0.1:${Port}) through a Cloudflare tunnel?" -Default 'Yes')) {
        Write-Status 'Nothing changed' -Level Done
        return
    }
    # Nobody can answer this without knowing where to look, so say where, and what each answer does.
    Write-Status 'How to tell whether Zero Trust is on: at https://dash.cloudflare.com open Zero Trust in the' -Level Detail
    Write-Status 'left-hand menu. On: it opens a dashboard, and Settings shows a team domain' -Level Detail
    Write-Status '(<team>.cloudflareaccess.com). Off: it asks you to choose a team name or a plan.' -Level Detail
    Write-Status 'Answer n if it is off, or you have no account: the steps are printed, and this waits here' -Level Detail
    Write-Status 'while you do them, then carries on. Nothing is asked twice.' -Level Detail
    if (-not (Confirm-Action -Message 'Do you already have a Cloudflare account with Zero Trust turned on?' -Default 'Yes')) {
        Show-CloudflarePrerequisites -PublicHostname $Hostname
        if (-not (Wait-ForCloudflare -Resume '.\Enable-RemoteControl.ps1')) { return }
    }
    $choice = $Setup
    if (-not $choice) {
        Write-Status 'Two ways to do the Cloudflare side:' -Level Detail
        Write-Status '  api    - hand this script a scoped API token and it does all of it, no browser needed' -Level Detail
        Write-Status '  manual - you do it in the dashboard; the script prints the steps and hands over nothing' -Level Detail
        # API by default (the user, 2026-10-08): the script then does every step, nothing to click.
        $choice = Read-Value -Message 'Which? [api/manual]' -Default 'api'
    }
    if (@('api', 'manual') -notcontains $choice) { throw "Unknown choice '$choice'" }

    if ($choice -eq 'api') {
        Invoke-ApiSetup
        return
    }

    # Nothing here can list the zones, so the question explains itself instead.
    if (-not $script:Hostname) { $script:Hostname = Read-PublicHostname }
    $Hostname = $script:Hostname
    $provider = Read-Value -Message 'Sign in with Entra ID (work) or a Microsoft account (personal)? [entra/microsoft]' -Default 'entra'
    if (@('entra', 'microsoft') -notcontains $provider) { throw "Unknown choice '$provider'" }

    Write-Status 'Signing in to Cloudflare (a browser window opens; pick the zone for this hostname)'
    if ($PSCmdlet.ShouldProcess('cloudflared', 'tunnel login')) {
        Invoke-Cloudflared -Exe $exe -Arguments @('tunnel', 'login') | Out-Null
    }
    Write-Status "Creating the tunnel $TunnelName"
    if ($PSCmdlet.ShouldProcess($TunnelName, 'Create the tunnel')) {
        Invoke-Cloudflared -Exe $exe -Arguments @('tunnel', 'create', $TunnelName) -AllowFailure | Out-Null
    }
    Write-Status "Pointing $Hostname at it"
    if ($PSCmdlet.ShouldProcess($Hostname, 'Route DNS to the tunnel')) {
        # The step that fails when the zone is not on Cloudflare yet: hold the run open for it.
        Invoke-WithRetry -What "Could not point $Hostname at the tunnel" -Step {
            Invoke-Cloudflared -Exe $exe -Arguments @('tunnel', 'route', 'dns', '-f', $TunnelName, $Hostname) | Out-Null
        }
    }

    $folder = Join-Path $env:USERPROFILE '.cloudflared'
    $tunnelConfig = Join-Path $folder 'config.yml'
    $credentials = @(Get-ChildItem -LiteralPath $folder -Filter '*.json' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.BaseName -match '^[0-9a-f-]{36}$' } | Sort-Object LastWriteTime -Descending)
    if ($credentials.Count -eq 0) { throw "No tunnel credentials in $folder; did 'tunnel create' run?" }
    $yaml = @(
        "tunnel: $TunnelName",
        "credentials-file: $($credentials[0].FullName)",
        'ingress:',
        "  - hostname: $Hostname",
        "    service: http://127.0.0.1:$Port",
        '  - service: http_status:404'
    ) -join "`r`n"
    if ($PSCmdlet.ShouldProcess($tunnelConfig, 'Write the tunnel configuration')) {
        [IO.File]::WriteAllText($tunnelConfig, $yaml + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
        Write-Status "Wrote $tunnelConfig" -Level Detail
    }
    Write-Status 'Installing the tunnel as a service (it dials out; nothing is opened on this machine)'
    if ($PSCmdlet.ShouldProcess('cloudflared', 'service install')) {
        Invoke-Cloudflared -Exe $exe -Arguments @('--config', $tunnelConfig, 'service', 'install') -AllowFailure | Out-Null
    }

    # The config gets everything the script knows; the AUD tag comes from the dashboard.
    if (-not $config.PSObject.Properties['remote']) {
        Set-JsonProperty -Object $config -Name 'remote' -Value ([pscustomobject]@{})
    }
    Set-JsonProperty -Object $config.remote -Name 'enabled' -Value $false
    Set-JsonProperty -Object $config.remote -Name 'hostname' -Value $Hostname
    if (-not $config.remote.PSObject.Properties['team_domain']) { Set-JsonProperty -Object $config.remote -Name 'team_domain' -Value '' }
    if (-not $config.remote.PSObject.Properties['audience']) { Set-JsonProperty -Object $config.remote -Name 'audience' -Value '' }
    if (-not $config.remote.PSObject.Properties['allowed_emails']) { Set-JsonProperty -Object $config.remote -Name 'allowed_emails' -Value @() }
    Write-JsonFile -Path $ConfigPath -Object $config
    Write-Status "Tunnel up: $Hostname -> 127.0.0.1:$Port" -Level Done
    Show-AccessSteps -PublicHostname $Hostname -Provider $provider
}
catch {
    Write-Status "Could not set up remote control: $($_.Exception.Message)" -Level Error
    Write-Error $_ -ErrorAction Continue
    exit 1
}
