<#
.SYNOPSIS
    Exports the identifiers that are the same in every tenant, from a tenancy you can sign in to.

.DESCRIPTION
    aicleaner keeps a list of identifiers that name nobody: Microsoft's own application IDs, Graph
    permission IDs, Entra role template IDs, and the built-in Azure role and policy definition IDs.
    They are identical in every tenant, so tokenising them hides a fact the model already knows, and
    a value that turns up in two clients is usually one of these.

    tools/build_public_identifiers.py collects most of them from public downloads. Two sets have no
    download and need a tenant: the built-in policy definitions, and the full list of Microsoft
    first-party applications as your tenant sees them. This script collects those and merges them
    into aicleaner/data/public-identifiers.json.

    SAFETY: only identifiers owned by Microsoft's own tenant are exported. Your tenant's own
    application registrations, service principals, groups and users identify the client and are
    never written. Review the summary (and `git diff` on the data file) before committing.

    Sign in is handled here: an existing az session is used, and when there is none the browser
    opens for you to sign in. A subscription is needed for the role and policy lists; without one,
    those steps are skipped and the rest still runs.

.PARAMETER OutputPath
    The file to merge into. The default is aicleaner\data\public-identifiers.json in the repository
    when this script is run from its tools folder, and public-identifiers.json beside the script
    otherwise, so a copy downloaded on its own writes where it is standing.

.PARAMETER IncludePermissions
    Also export the application and delegated permission IDs of every Microsoft API (appRoles and
    oauth2PermissionScopes). Thousands of GUIDs, all tenant-independent, and they turn up in any
    repo that automates consent.

.PARAMETER SkipPolicies
    Skip the built-in policy and policy set definitions (the slowest step).

.PARAMETER TenantId
    The tenant to sign in to. Asked for only if sign-in is needed and this is not given.

.EXAMPLE
    .\tools\Export-PublicIdentifiers.ps1
    Uses the az session if there is one, and opens the browser to sign in if there is not.

.EXAMPLE
    .\tools\Export-PublicIdentifiers.ps1 -IncludePermissions -WhatIf

.NOTES
    Contact:       see contact-*.json stored alongside the repository root
    Last-modified: 2026-09-20
    Requires:      Windows PowerShell 5.1 or PowerShell 7+; the az CLI, signed in
#>

#Requires -Version 5.1

[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Low')]
param(
    [string]$OutputPath = '',
    [switch]$IncludePermissions,
    [switch]$SkipPolicies,
    [string]$TenantId = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Microsoft's own tenant: anything owned by it is the same in every other tenant.
$MicrosoftTenantId = 'f8cdef31-a31e-4b4a-93e4-5f571e91255a'
$GuidPattern = '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'

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

function Get-AzPath {
    <# Where az is, with the one instruction that fixes it when it is nowhere. #>
    [OutputType([string])]
    param()
    foreach ($name in 'az', 'az.cmd') {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if ($found) { return [string]$found.Source }
    }
    throw 'The az CLI is not installed. winget install -e --id Microsoft.AzureCLI, then run this again'
}

function Invoke-Az {
    <# Runs az and returns the parsed JSON, or $null when the command fails. #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string[]]$Arguments, [switch]$Quiet)
    $exe = Get-AzPath
    $savedEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $output = & $exe @Arguments -o json 2>&1 } finally { $ErrorActionPreference = $savedEap }
    if ($LASTEXITCODE -ne 0) {
        if (-not $Quiet) { Write-Status ("az {0} failed: {1}" -f $Arguments[0], (($output | Select-Object -Last 1) -join ' ')) -Level Warn }
        return $null
    }
    $text = ($output | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) -join "`n"
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    return ($text | ConvertFrom-Json)
}

function Get-OutputPath {
    <#
    .SYNOPSIS
        Where to write, and whether this is a copy running on its own: @{ Path; Standalone }.
    .DESCRIPTION
        In a checkout this script sits in tools\, so the data file is two folders up. A copy
        downloaded by itself has no such folder, and writing to it fails with "could not find part
        of the path" - so it writes beside the script instead, where it is standing.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Here, [string]$Requested = '')
    if ($Requested) { $path = $Requested; $standalone = $false }
    else {
        $inRepo = Join-Path (Split-Path -Parent $Here) 'aicleaner\data\public-identifiers.json'
        if (Test-Path -LiteralPath (Split-Path -Parent $inRepo)) {
            $path = $inRepo; $standalone = $false
        } else {
            $path = Join-Path $Here 'public-identifiers.json'; $standalone = $true
        }
    }
    $folder = Split-Path -Parent $path
    if ($folder -and -not (Test-Path -LiteralPath $folder)) {
        New-Item -ItemType Directory -Path $folder -Force | Out-Null
    }
    return @{ Path = $path; Standalone = $standalone }
}

function Connect-Tenant {
    <#
    .SYNOPSIS
        The signed-in account, signing in through the browser when there is not one yet.
    .DESCRIPTION
        az login is run without capturing its output, so the sign-in prompt, and the device-code
        message on a machine with no browser, both reach the screen.
    #>
    [CmdletBinding()]
    param([string]$Tenant = '')
    $account = Invoke-Az -Arguments @('account', 'show') -Quiet
    if ($account -and (-not $Tenant -or [string]$account.tenantId -eq $Tenant)) { return $account }

    Write-Status 'Signing in (a browser window opens; sign in as an account that can read the directory)'
    $exe = Get-AzPath
    $arguments = @('login', '--only-show-errors')
    if ($Tenant) { $arguments += @('--tenant', $Tenant) }
    $savedEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $exe @arguments | Out-Null } finally { $ErrorActionPreference = $savedEap }

    $account = Invoke-Az -Arguments @('account', 'show')
    if (-not $account) { throw 'Sign-in did not complete; nothing was exported' }
    return $account
}

function Get-GraphToken {
    <# A Graph access token from the az session, so Graph is called directly rather than through az. #>
    [OutputType([string])]
    param()
    $token = Invoke-Az -Arguments @('account', 'get-access-token', '--resource-type', 'ms-graph')
    if (-not $token -or -not $token.PSObject.Properties['accessToken']) {
        throw 'Could not get a Microsoft Graph token from the az session'
    }
    return [string]$token.accessToken
}

function Invoke-GraphRequest {
    <#
    .SYNOPSIS
        One Graph GET, or $null when it fails.
    .DESCRIPTION
        Deliberately not az rest: az is a .cmd on Windows, PowerShell only quotes arguments that
        contain spaces, and cmd.exe then treats the & between query parameters as a command
        separator. Invoke-RestMethod has no command line to mangle.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][string]$Token,
        [hashtable]$Headers = @{},
        [switch]$Quiet
    )
    $all = @{ Authorization = "Bearer $Token" }
    foreach ($key in $Headers.Keys) { $all[$key] = $Headers[$key] }
    try {
        return Invoke-RestMethod -Uri $Url -Headers $all -Method Get -ErrorAction Stop
    } catch {
        if (-not $Quiet) { Write-Status ("Graph request failed: {0}" -f $_.Exception.Message) -Level Warn }
        return $null
    }
}

function Get-GraphPages {
    <#
    .SYNOPSIS
        Every page of a Graph collection, following @odata.nextLink.
    .DESCRIPTION
        Emits the items, nothing more: every call site wraps the call in @(), which is what keeps
        an empty result an empty array instead of $null with no .Count on it.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][string]$Token,
        [hashtable]$Headers = @{},
        [switch]$Quiet
    )
    $items = New-Object System.Collections.Generic.List[object]
    $next = $Url
    while ($next) {
        # The header travels with every page: nextLink carries $count, not ConsistencyLevel.
        $page = Invoke-GraphRequest -Url $next -Token $Token -Headers $Headers -Quiet:$Quiet
        if ($null -eq $page) { return @() }
        if ($page.PSObject.Properties['value']) { foreach ($item in $page.value) { $items.Add($item) } }
        $next = if ($page.PSObject.Properties['@odata.nextLink']) { [string]$page.'@odata.nextLink' } else { '' }
    }
    return $items.ToArray()
}

function Get-MicrosoftPrincipals {
    <#
    .SYNOPSIS
        Microsoft's own service principals as this tenant sees them.
    .DESCRIPTION
        appOwnerOrganizationId only takes a filter as an advanced query ($count=true plus
        ConsistencyLevel: eventual), and some tenants refuse it outright. When the filtered call
        comes back empty, every service principal is read instead and the owner is checked here.
        Either way the owner check below is what decides, so a filter that silently stops working
        cannot widen the export to this tenant's own registrations.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$TenantId, [Parameter(Mandatory)][string]$Token)
    $select = 'appId,displayName,appOwnerOrganizationId,appRoles,oauth2PermissionScopes'
    $filter = [uri]::EscapeDataString("appOwnerOrganizationId eq $TenantId")
    $url = "https://graph.microsoft.com/v1.0/servicePrincipals?`$select=$select&`$filter=$filter&`$count=true&`$top=999"
    $principals = @(Get-GraphPages -Url $url -Token $Token -Headers @{ ConsistencyLevel = 'eventual' } -Quiet)
    if ($principals.Count -eq 0) {
        Write-Status 'The tenant would not filter on appOwnerOrganizationId; reading every service principal instead' -Level Detail
        $url = "https://graph.microsoft.com/v1.0/servicePrincipals?`$select=$select&`$top=999"
        $principals = @(Get-GraphPages -Url $url -Token $Token)
        if ($principals.Count -eq 0) {
            throw 'Graph returned no service principals at all; check the sign-in and that directory reads are allowed'
        }
        Write-Status ("{0} service principal(s) in the directory" -f $principals.Count) -Level Detail
    }
    # The one rule that keeps this tenant's own identifiers out, wherever the list came from.
    return @($principals | Where-Object { [string]$_.appOwnerOrganizationId -eq $TenantId })
}

function Add-Guid {
    <# Adds a GUID to the set, ignoring anything that is not one. #>
    # AllowEmptyCollection: a mandatory collection parameter refuses an empty one, and the set is
    # empty until the first GUID goes in.
    param([Parameter(Mandatory)][AllowEmptyCollection()][System.Collections.Generic.HashSet[string]]$Set, $Value)
    $text = [string]$Value
    if ($text -and $text -match $GuidPattern) { [void]$Set.Add($text.ToLowerInvariant()) }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

try {
    $here = if ($PSScriptRoot) { $PSScriptRoot } else { $PWD.Path }
    $chosen = Get-OutputPath -Here $here -Requested $OutputPath
    $OutputPath = $chosen.Path
    $standalone = $chosen.Standalone
    $account = Connect-Tenant -Tenant $TenantId
    Write-Status ("Signed in to tenant {0} as {1}" -f $account.tenantId, $account.user.name)
    if ([string]$account.tenantId -eq $MicrosoftTenantId) {
        throw 'This is Microsoft''s own tenant; the owner filter would not separate anything'
    }

    $found = New-Object 'System.Collections.Generic.HashSet[string]'
    $sources = New-Object System.Collections.Generic.List[string]

    # --- 1. Microsoft's own applications, as this tenant sees them ---------------
    Write-Status 'Microsoft first-party applications (owned by Microsoft''s tenant only)'
    $graphToken = Get-GraphToken
    $principals = @(Get-MicrosoftPrincipals -TenantId $MicrosoftTenantId -Token $graphToken)
    if ($principals.Count -eq 0) {
        throw 'No Microsoft-owned service principals found; nothing this script exports would be safe to trust'
    }
    $before = $found.Count
    foreach ($principal in $principals) { Add-Guid -Set $found -Value $principal.appId }
    Write-Status ("{0} Microsoft-owned applications, {1} new application id(s)" -f $principals.Count, ($found.Count - $before)) -Level Detail
    $sources.Add('graph servicePrincipals (appOwnerOrganizationId = Microsoft)')

    # --- 2. Permission ids on those applications -----------------------------------
    if ($IncludePermissions) {
        Write-Status 'Permission ids (appRoles and delegated scopes) on those applications'
        $before = $found.Count
        foreach ($principal in $principals) {
            if ($principal.PSObject.Properties['appRoles']) {
                foreach ($role in @($principal.appRoles)) { Add-Guid -Set $found -Value $role.id }
            }
            if ($principal.PSObject.Properties['oauth2PermissionScopes']) {
                foreach ($scope in @($principal.oauth2PermissionScopes)) { Add-Guid -Set $found -Value $scope.id }
            }
        }
        Write-Status ("{0} new permission id(s)" -f ($found.Count - $before)) -Level Detail
        $sources.Add('graph servicePrincipals appRoles and oauth2PermissionScopes (Microsoft-owned)')
    }

    # --- 3. Entra role templates ----------------------------------------------------
    Write-Status 'Entra directory role templates'
    $before = $found.Count
    foreach ($template in @(Get-GraphPages -Url 'https://graph.microsoft.com/v1.0/directoryRoleTemplates' -Token $graphToken)) {
        Add-Guid -Set $found -Value $template.id
    }
    Write-Status ("{0} new role template id(s)" -f ($found.Count - $before)) -Level Detail
    $sources.Add('graph directoryRoleTemplates')

    # --- 4. Built-in Azure roles ------------------------------------------------------
    Write-Status 'Azure built-in role definitions'
    $before = $found.Count
    $roles = Invoke-Az -Arguments @('role', 'definition', 'list', '--custom-role-only', 'false',
                                    '--query', "[?roleType=='BuiltInRole'].name")
    if ($null -eq $roles) {
        Write-Status 'No subscription in context; skipped (az account set --subscription <id> to include them)' -Level Warn
    } else {
        foreach ($role in @($roles)) { Add-Guid -Set $found -Value $role }
        Write-Status ("{0} new built-in role id(s)" -f ($found.Count - $before)) -Level Detail
        $sources.Add('az role definition list (BuiltInRole)')
    }

    # --- 5. Built-in policy definitions -------------------------------------------------
    if (-not $SkipPolicies -and $null -ne $roles) {
        foreach ($pair in @(@{ Label = 'policy definitions'; Args = @('policy', 'definition', 'list') },
                            @{ Label = 'policy set definitions'; Args = @('policy', 'set-definition', 'list') })) {
            Write-Status "Built-in $($pair.Label)"
            $before = $found.Count
            $definitions = Invoke-Az -Arguments ($pair.Args + @('--query', "[?policyType=='BuiltIn'].name"))
            foreach ($definition in @($definitions)) { Add-Guid -Set $found -Value $definition }
            Write-Status ("{0} new id(s)" -f ($found.Count - $before)) -Level Detail
            if ($definitions) { $sources.Add("az $($pair.Args -join ' ') (BuiltIn)") }
        }
    }

    # --- 6. Merge ------------------------------------------------------------------------
    $existing = $null
    if (Test-Path -LiteralPath $OutputPath) {
        $existing = Get-Content -LiteralPath $OutputPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    $all = New-Object 'System.Collections.Generic.HashSet[string]'
    if ($existing -and $existing.PSObject.Properties['guids']) {
        foreach ($guid in @($existing.guids)) { [void]$all.Add(([string]$guid).ToLowerInvariant()) }
    }
    $knownBefore = $all.Count
    foreach ($guid in $found) { [void]$all.Add($guid) }
    $added = $all.Count - $knownBefore
    Write-Status ("{0} identifier(s) collected, {1} of them new; {2} in the file after the merge" -f
        $found.Count, $added, $all.Count) -Level Done
    if ($added -eq 0) { Write-Status 'Nothing to write' -Level Done; return }

    $domains = @()
    if ($existing -and $existing.PSObject.Properties['domains']) { $domains = @($existing.domains) }
    $mergedSources = New-Object System.Collections.Generic.List[string]
    if ($existing -and $existing.PSObject.Properties['sources']) {
        foreach ($source in @($existing.sources)) { $mergedSources.Add([string]$source) }
    }
    foreach ($source in $sources) { if (-not $mergedSources.Contains($source)) { $mergedSources.Add($source) } }
    $document = [ordered]@{
        generated = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        sources   = @($mergedSources)
        guids     = @($all | Sort-Object)
        domains   = @($domains | Sort-Object)
    }
    if ($PSCmdlet.ShouldProcess($OutputPath, "Write $($all.Count) public identifiers")) {
        $json = ([pscustomobject]$document | ConvertTo-Json -Depth 5)
        [IO.File]::WriteAllText($OutputPath, $json, (New-Object System.Text.UTF8Encoding($false)))
        Write-Status "Written to $OutputPath" -Level Done
        Write-Status 'Only Microsoft-owned identifiers are exported; nothing from your own tenant.' -Level Detail
        if ($standalone -and -not $existing) {
            # Nothing was merged, so this file holds the tenant half only: the vendor domains and
            # the identifiers that come from public downloads are not in it.
            Write-Status 'This file holds only what the tenancy gave: copy it beside the script into a' -Level Warn
            Write-Status 'checkout and re-run there, or pass -OutputPath <repo>\aicleaner\data\public-identifiers.json,' -Level Detail
            Write-Status 'so it merges instead of replacing what ships with aicleaner.' -Level Detail
        } else {
            Write-Status 'Review it before committing: git diff -- aicleaner/data/public-identifiers.json' -Level Detail
        }
    }
}
catch {
    Write-Status "Export failed: $($_.Exception.Message)" -Level Error
    Write-Error $_ -ErrorAction Continue
    exit 1
}
