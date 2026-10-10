<#
.SYNOPSIS
    Checks that the filter is ready, then routes Claude Code and VS Code through it.

.DESCRIPTION
    Switching live AI traffic through a filter is the one step of an install that can stop you
    working, so it is the last step and it is deliberate. Five checks run first and every one of
    them blocks:

      1. the proxy answers /healthz            - routing at something not listening is an outage
      2. the mapping loads, no broken client   - a broken client file stops model traffic anyway
      3. a client has at least one entry       - an empty filter protects nothing and fills Review
      4. a real round-trips and the canary is clean
      5. a request survives the whole path     - everything else can pass while nothing works

    Check 5 sends a request shaped like the ones that have actually broken this: a tool definition
    carrying "$schema": "https://json-schema.org/draft/2020-12/schema", with Accept-Encoding
    br, gzip. A Brotli reply the proxy cannot decode, and a placeholder substituted into a
    meta-schema URI, each made every request fail while the proxy was healthy, the mapping valid
    and the canary clean.

    It cannot prove credentials: a subscription authenticates with a token Claude Code holds and
    this script does not. So a 401 or 403 from the provider PASSES - the request got there
    well-formed, which is what is being tested - while a 502, a connection failure, or a 400 naming
    input_schema or a decode error fails.

    Only after all five pass are the two settings written. A session already running follows a
    value that changes, so it takes effect without restarting anything.

.PARAMETER Port
    Proxy port. Default: the port in aicleaner-config.json, else 8787.

.PARAMETER Force
    Write the settings without running the checks. For somebody who knows better.

.EXAMPLE
    .\Enable-AiCleaner.ps1
    Check, then switch Claude Code and VS Code over.

.NOTES
    Contact:       see contact-*.json stored alongside this script
    Requires:      Windows PowerShell 5.1 or PowerShell 7+
#>

#Requires -Version 5.1

[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateRange(1024, 65535)][int]$Port = 0,
    [switch]$Force,
    [AllowEmptyString()][string]$LogPath = $(
        $dir = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }
        Join-Path $dir 'Enable-AiCleaner.log'
    )
)
# The app folder: this script's own, or its parent when it sits in installer\ (feature 102).
$script:AppFolder = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

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
        if (Test-Path -LiteralPath $Path) {
            Copy-Item -LiteralPath $Path -Destination "$Path.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')" -ErrorAction SilentlyContinue
        }
        [IO.File]::WriteAllText($Path, ($Object | ConvertTo-Json -Depth 20),
                                (New-Object System.Text.UTF8Encoding($false)))
    }
}

function Set-JsonProperty {
    param([Parameter(Mandatory)]$Object, [Parameter(Mandatory)][string]$Name, $Value)
    if ($Object.PSObject.Properties[$Name]) { $Object.$Name = $Value }
    else { $Object | Add-Member -MemberType NoteProperty -Name $Name -Value $Value }
}

# ---- the five checks -----------------------------------------------------------------------

function Test-ProxyUp {
    param([int]$ProxyPort)
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ProxyPort/healthz" -TimeoutSec 5
        return [pscustomobject]@{ Ok = $true; Why = "clients=$($health.clients -join ',') forms=$($health.forms)"; Health = $health }
    } catch {
        return [pscustomobject]@{ Ok = $false; Health = $null
            Why = "nothing answered http://127.0.0.1:$ProxyPort/healthz. Start it with $(Join-Path $PSScriptRoot 'Start-AiCleaner.ps1') -Detach" }
    }
}

function Test-MappingUsable {
    <# Checks 2 and 3 together: the status page knows both, and it is the same view the UI shows. #>
    param([int]$ProxyPort)
    try {
        $status = Invoke-RestMethod -Uri "http://127.0.0.1:$ProxyPort/api/status" -TimeoutSec 10
    } catch {
        return [pscustomobject]@{ Ok = $false; Why = "could not read the mapping status: $($_.Exception.Message)" }
    }
    $rows = @($status.clients)
    if ($status.PSObject.Properties['global'] -and $status.global) { $rows += $status.global }
    $broken = @($rows | Where-Object { $_.state -contains 'broken' -or $_.state -contains 'fallback' })
    if ($broken.Count -gt 0) {
        return [pscustomobject]@{ Ok = $false
            Why = "these client files are not usable: $(($broken | ForEach-Object { $_.id }) -join ', '). Fix them on the Clients page first" }
    }
    $withEntries = @($rows | Where-Object { [int]$_.entries -gt 0 })
    if ($withEntries.Count -eq 0) {
        return [pscustomobject]@{ Ok = $false
            Why = 'no client has any entries yet, so nothing would be filtered. Create a client and give it the names that mean them (the short one, the full one, their domains) before switching over' }
    }
    $total = ($withEntries | Measure-Object -Property entries -Sum).Sum
    return [pscustomobject]@{ Ok = $true; Why = "$($withEntries.Count) client(s), $total entries" }
}

function Test-RoundTrip {
    param([int]$ProxyPort)
    try {
        $mapping = Invoke-RestMethod -Uri "http://127.0.0.1:$ProxyPort/api/mapping" -TimeoutSec 10
        $form = @($mapping.clients | ForEach-Object { $_.forms } | Where-Object { $_.real }) | Select-Object -First 1
        if (-not $form) { return [pscustomobject]@{ Ok = $false; Why = 'no entry to test with' } }
        $real = [string]$form.real
        $tokenised = Invoke-RestMethod -Uri "http://127.0.0.1:$ProxyPort/api/tokenise" -Method POST `
            -Body $real -ContentType 'text/plain' -TimeoutSec 10
        $back = Invoke-RestMethod -Uri "http://127.0.0.1:$ProxyPort/api/rehydrate" -Method POST `
            -Body $tokenised -ContentType 'text/plain' -TimeoutSec 10
        if ([string]$tokenised -eq $real) {
            return [pscustomobject]@{ Ok = $false; Why = "'$real' was not replaced at all" }
        }
        if ([string]$back -ne $real) {
            return [pscustomobject]@{ Ok = $false; Why = "'$real' came back as '$back'" }
        }
        return [pscustomobject]@{ Ok = $true; Why = "'$real' -> '$tokenised' -> back again" }
    } catch {
        return [pscustomobject]@{ Ok = $false; Why = "the round trip failed: $($_.Exception.Message)" }
    }
}

function Test-WholePath {
    <#
    A request through the proxy to the provider, shaped like the ones that have broken it: a tool
    definition naming its meta-schema, and an Accept-Encoding the proxy has to strip. What is being
    tested is the path, not the credentials, so an auth refusal is a pass.
    #>
    param([int]$ProxyPort)
    $body = @{
        model      = 'claude-haiku-4-5-20251001'
        max_tokens = 1
        messages   = @(@{ role = 'user'; content = 'ping' })
        tools      = @(@{
            name          = 'aicleaner_readiness'
            description   = 'A tool definition whose meta-schema URI must survive the filter.'
            input_schema  = @{
                '$schema'  = 'https://json-schema.org/draft/2020-12/schema'
                type       = 'object'
                properties = @{ path = @{ type = 'string'; format = 'uri' } }
            }
        })
    } | ConvertTo-Json -Depth 10 -Compress

    try {
        $answer = Invoke-WebRequest -Uri "http://127.0.0.1:$ProxyPort/anthropic/v1/messages" -Method POST `
            -Body $body -ContentType 'application/json' `
            -Headers @{ 'Accept-Encoding' = 'br, gzip, deflate' } -TimeoutSec 60 -ErrorAction Stop
        return [pscustomobject]@{ Ok = $true; Why = "the provider answered $($answer.StatusCode)" }
    } catch {
        $status = 0
        $text = ''
        if ($_.Exception.PSObject.Properties['Response'] -and $_.Exception.Response) {
            try { $status = [int]$_.Exception.Response.StatusCode } catch { $status = 0 }
            try {
                $stream = $_.Exception.Response.GetResponseStream()
                $text = (New-Object IO.StreamReader($stream)).ReadToEnd()
            } catch { $text = '' }
        }
        if (-not $text) { $text = [string]$_.ErrorDetails.Message }
        # Auth is not what this tests: reaching the provider well-formed is.
        if ($status -eq 401 -or $status -eq 403) {
            return [pscustomobject]@{ Ok = $true
                Why = "the provider answered $status (auth), so the request reached it well-formed" }
        }
        if ($status -eq 502 -or $status -eq 0) {
            return [pscustomobject]@{ Ok = $false
                Why = "the proxy could not complete the round trip ($(if ($status) { $status } else { 'no answer' })): $($text.Trim() -replace '\s+', ' ')" }
        }
        if ($text -match 'input_schema|tools\.|content-encoding|Can not decode') {
            return [pscustomobject]@{ Ok = $false
                Why = "the proxy changed the request into one the provider refused: $($text.Trim() -replace '\s+', ' ')" }
        }
        return [pscustomobject]@{ Ok = $false
            Why = "the provider answered $status : $($text.Trim() -replace '\s+', ' ')" }
    }
}

# ---- switching it on ------------------------------------------------------------------------

function Get-VsCodeSettingsFiles {
    $paths = @(
        (Join-Path $env:APPDATA 'Code\User\settings.json'),
        (Join-Path $env:APPDATA 'Code - Insiders\User\settings.json')
    )
    foreach ($server in '.vscode-server', '.vscode-server-insiders') {
        foreach ($scope in 'Machine', 'User') {
            $p = Join-Path $env:USERPROFILE "$server\data\$scope\settings.json"
            if (Test-Path -LiteralPath $p) { $paths += $p }
        }
    }
    return $paths | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -Unique
}

function Set-RoutingOn {
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$BaseUrl)
    $changed = 0

    $settingsPath = Join-Path $env:USERPROFILE '.claude\settings.json'
    $settings = Read-JsonFile -Path $settingsPath
    if (-not $settings) { $settings = [pscustomobject]@{} }
    if (-not $settings.PSObject.Properties['env']) {
        Set-JsonProperty -Object $settings -Name 'env' -Value ([pscustomobject]@{})
    }
    # Under StrictMode a missing property throws rather than returning $null, and missing is now the
    # normal case: the installers stopped writing this, so the first Enable on a machine finds no key.
    $currentBase = if ($settings.env.PSObject.Properties['ANTHROPIC_BASE_URL']) {
        [string]$settings.env.ANTHROPIC_BASE_URL
    } else { '' }
    if ($currentBase -eq $BaseUrl) {
        Write-Status "Claude Code already routes through $BaseUrl" -Level Detail
    } elseif ($PSCmdlet.ShouldProcess($settingsPath, "Point ANTHROPIC_BASE_URL at $BaseUrl")) {
        $parent = Split-Path -Parent $settingsPath
        if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Set-JsonProperty -Object $settings.env -Name 'ANTHROPIC_BASE_URL' -Value $BaseUrl
        Write-JsonFile -Path $settingsPath -Object $settings
        Write-Status "Claude Code: ANTHROPIC_BASE_URL now $BaseUrl" -Level Detail
        $changed++
    }

    foreach ($vsPath in Get-VsCodeSettingsFiles) {
        $vs = Read-JsonFile -Path $vsPath
        if (-not $vs) { continue }
        $entries = @()
        if ($vs.PSObject.Properties['claudeCode.environmentVariables']) {
            $entries = @($vs.'claudeCode.environmentVariables')
        }
        $current = $entries | Where-Object { $_.name -eq 'ANTHROPIC_BASE_URL' } | Select-Object -First 1
        if ($current -and [string]$current.value -eq $BaseUrl) {
            Write-Status "$vsPath already routes through $BaseUrl" -Level Detail
            continue
        }
        if ($PSCmdlet.ShouldProcess($vsPath, "Point ANTHROPIC_BASE_URL at $BaseUrl")) {
            Set-JsonProperty -Object $vs -Name 'claudeCode.environmentVariables' -Value (
                @($entries | Where-Object { $_.name -ne 'ANTHROPIC_BASE_URL' }) +
                @([pscustomobject]@{ name = 'ANTHROPIC_BASE_URL'; value = $BaseUrl }))
            Write-JsonFile -Path $vsPath -Object $vs
            Write-Status "VS Code: ANTHROPIC_BASE_URL now $BaseUrl in $vsPath" -Level Detail
            $changed++
        }
    }
    return $changed
}

# ---- main ------------------------------------------------------------------------------------

$transcriptStarted = $false
if ($LogPath) { try { Start-Transcript -Path $LogPath -Append | Out-Null; $transcriptStarted = $true } catch { } }

try {
    $root = $script:AppFolder
    if ($Port -le 0) {
        $config = Read-JsonFile -Path (Join-Path $root 'aicleaner-config.json')
        $Port = if ($config -and $config.PSObject.Properties['port']) { [int]$config.port } else { 8787 }
    }
    $baseUrl = "http://127.0.0.1:$Port/anthropic"

    if (-not $Force) {
        Write-Status 'Checking the filter is ready before switching anything over'
        $checks = [ordered]@{
            'the proxy is up'                    = { Test-ProxyUp -ProxyPort $Port }
            'the mapping is usable and not empty' = { Test-MappingUsable -ProxyPort $Port }
            'a name round-trips'                 = { Test-RoundTrip -ProxyPort $Port }
            'a request survives the whole path'  = { Test-WholePath -ProxyPort $Port }
        }
        $failed = @()
        foreach ($name in $checks.Keys) {
            $result = & $checks[$name]
            if ($result.Ok) {
                Write-Status "OK    $name - $($result.Why)" -Level Detail
            } else {
                Write-Status "FAIL  $name" -Level Warn
                Write-Status "      $($result.Why)" -Level Detail
                $failed += $name
            }
        }
        if ($failed.Count -gt 0) {
            Write-Status "Not switched over: $($failed.Count) check(s) failed. Nothing was changed, so Claude Code is still working." -Level Error
            Write-Status 'Fix what is named above and run this again, or use -Force if you know better.' -Level Detail
            exit 1
        }
        Write-Status 'All checks passed' -Level Done
    } else {
        Write-Status 'Checks skipped (-Force)' -Level Warn
    }

    $changed = Set-RoutingOn -BaseUrl $baseUrl
    if ($changed -gt 0) {
        Write-Status "Routing is on: Claude Code and VS Code now go through $baseUrl" -Level Done
        Write-Status 'A session already running follows a value that changes, so it should pick this up without a restart.' -Level Detail
    } else {
        Write-Status "Routing was already on ($baseUrl); nothing to change." -Level Done
    }
    # Fully pathed (feature 109), so it can be pasted into any window, from any folder.
    Write-Status "Switch it off again at any time with $(Join-Path $PSScriptRoot 'Disable-AiCleaner.ps1') - it takes a few seconds and leaves everything installed." -Level Detail
}
catch {
    Write-Status "Could not switch routing on: $($_.Exception.Message)" -Level Error
    Write-Error $_ -ErrorAction Continue
    exit 1
}
finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
