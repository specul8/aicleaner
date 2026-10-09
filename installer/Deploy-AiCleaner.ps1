<#
.SYNOPSIS
    Installs, updates or removes aicleaner on this machine from its git repository.

.DESCRIPTION
    -Action GetSource puts the whole repository on disk first, for reading it or installing from
    a working copy; the install itself never needs it.

    Copy this script to the machine with your contact-*.json beside it, then run it with no
    parameters: it asks what the machine is and works the rest out (which installer to run, what to
    skip, where the code and the client files go). Every answer can be given as a parameter instead,
    for scripted runs.

    A file it needs but cannot find (an older deployment, or a working copy from before that file
    existed) is fetched from the repository rather than stopping the run, so a newer copy of this
    script can always finish the job. If the folder it is run from is synced by OneDrive, the files
    it holds in the cloud are fetched first, so nothing is copied as an empty placeholder. It:
      - Install: fetches the newest released version (the highest vX.Y.Z tag) from the repository
        named in the contact file, copies the code to the install folder (default
        %USERPROFILE%\.aicleaner\app, outside any synced folder), points aicleaner-config.json at
        the client files folder you choose (OneDrive is suggested, and pinned to this device), and
        runs the VS Code and/or Desktop installer from there.
      - GetSource: clones the repository the contact file names (or brings an existing clone up
        to date) so you have the code to read or to install from. Nothing is installed.
      - Update: installs a newer release over the same folder; data files are never touched and
        files dropped from the release are removed.
      - Uninstall: undoes the wiring (Uninstall-AiCleaner.ps1), removes the code and keeps the data.
        With -RemoveData it also deletes the data, after you type REMOVE; a client files folder
        outside the install folder (possibly shared with colleagues) needs its own typed
        confirmation.

    What was installed is recorded in %USERPROFILE%\.aicleaner\deployment.json (the release, the
    folders and the list of code files), which Update and Uninstall read.

    Self-contained: every helper is inside this file, so it can be pasted into a new file and run.

.PARAMETER Action
    Install (default), Update, Uninstall, GetSource or Rollback.

    Rollback puts the previous build back. Each Update keeps the build it is about to overwrite
    under %USERPROFILE%\.aicleaner
ollback\, so a code drop that will not start can be undone
    without the proxy running - which is the point, because a broken proxy takes the AI tooling
    with it. Data is never rolled back: config, client files and last-good copies are untouched
    by a deploy and stay untouched by this.

.PARAMETER Target
    Install only: which wiring installer to run.
      VSCode      Claude Code on this machine (the VS Code server VM); the default when asked
      Desktop     Claude Desktop; needs an elevated PowerShell
      Both        VSCode and Desktop
      Workstation the client PC that only shows the VS Code window over SSH: screenshot-to-text and
                  VS Code telemetry, no proxy, no client files and no Claude Code settings
      None        files only; wire up later

.PARAMETER InstallPath
    Where the code goes. Default %USERPROFILE%\.aicleaner\app, used without asking: the venv sits
    at %USERPROFILE%\.aicleaner\venv whatever this says, so a different answer splits one install
    across two places. Give it only when the code genuinely has to live somewhere else.

.PARAMETER ClientsPath
    Where the client files live. Suggested: <OneDrive>\aicleaner\clients when OneDrive is set up,
    otherwise <InstallPath>\clients (asked when not given). Not used by -Target Workstation, which
    holds no client files.

.PARAMETER AllowUnsigned
    Install a release that is not signed - a branch (-Ref main) or a tag from before signing began.
    Releases are signed (RELEASE-MANIFEST, checked with OpenSSH against the key this install trusts)
    and anything unsigned, signed by another key, or altered is refused without this (H16). A
    working copy (-FromWorkingCopy) is local code and is not checked.

.PARAMETER FromWorkingCopy
    Deploy this working copy instead of a release from the repository: the tracked files as they are
    on disk, copied to the install folder. For a development machine, so what runs is still a
    deployment (its own folder, a record, Update and Uninstall) rather than the repo itself.

.PARAMETER RepoUrl
    The repository (https). Default: repo_url from the contact-*.json beside this script.

.PARAMETER SourcePath
    -Action GetSource only: where to put the clone. Default: a folder named after the repository,
    in the current directory.

.PARAMETER Ref
    A tag or branch to deploy instead of the newest vX.Y.Z tag.

.PARAMETER WithTesseract
    Install Tesseract as well, which the proxy prefers for reading an image that arrives in a
    request. Without it the OCR engine built into Windows is used, which needs no install.
    Forwarded to whichever installers run.

.PARAMETER VSCodeArguments
    Extra arguments for Install-AiCleaner-VSCode.ps1, e.g. @('-SkipVSCode') on a VS Code server VM.

.PARAMETER DesktopArguments
    Extra arguments for Install-AiCleaner-Desktop.ps1, e.g. @('-RegisterBackupTask').

.PARAMETER WorkstationArguments
    Extra arguments for Install-AiCleaner-Workstation.ps1, e.g. @('-SkipPowerToys').

.PARAMETER RemoteHostname
    Set up remote control for this hostname as part of the install (Enable-RemoteControl.ps1). Asked
    for on a machine where an agent runs; never offered on a client PC, which has nothing to drive.

.PARAMETER Identity
    How the remote page is signed in to: OneTimePin (an emailed PIN), Google or Microsoft. Asked
    when remote control is set up and this is not given; the PIN is the default, and the weakest.

.PARAMETER RemoveData
    Uninstall only: also delete the data (after typed confirmation) and the stored secrets.

.PARAMETER AssumeDefaults
    Take the default answer at every yes/no question (unattended runs). Typed confirmations for
    deleting data are still asked.

.PARAMETER StateRoot
    Folder for the deployment record. Default %USERPROFILE%\.aicleaner.

.PARAMETER LogPath
    Transcript path. Default %TEMP%\Deploy-AiCleaner.log. Pass '' to disable.

.EXAMPLE
    .\Deploy-AiCleaner.ps1
    Install the newest release, asking for the folders and which client to wire up.

.EXAMPLE
    .\Deploy-AiCleaner.ps1 -Target VSCode -VSCodeArguments '-SkipVSCode'
    On the VS Code server VM.

.EXAMPLE
    .\Deploy-AiCleaner.ps1 -Target Workstation
    On the client PC: screenshot-to-text only, since Claude Code and the proxy run on the VM.

.EXAMPLE
    .\Deploy-AiCleaner.ps1 -FromWorkingCopy
    On a development machine: deploy the code in this folder, tracked files as they are on disk.

.EXAMPLE
    .\Deploy-AiCleaner.ps1 -Action Update
    Move to the newest release (or, for a working-copy deployment, to the files as they are now).

.EXAMPLE
    .\Deploy-AiCleaner.ps1 -Action Uninstall -WhatIf
    Show what removal would do.

.NOTES
    Contact:       see contact-*.json stored alongside this script
    Last-modified: 2026-09-20
    Requires:      Windows PowerShell 5.1 or PowerShell 7+; git (offered through winget when missing)
#>

#Requires -Version 5.1

[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [ValidateSet('Install', 'Update', 'Uninstall', 'GetSource', 'Rollback')][string]$Action = 'Install',
    [ValidateSet('VSCode', 'Desktop', 'Both', 'Workstation', 'None')][string]$Target,
    [string]$InstallPath = '',
    [string]$ClientsPath = '',
    [switch]$FromWorkingCopy,
    [switch]$AllowUnsigned,
    [string]$SourcePath = '',
    [string]$RepoUrl = '',
    [string]$Ref = '',
    [switch]$WithTesseract,
    [string[]]$VSCodeArguments = @(),
    [string[]]$DesktopArguments = @(),
    [string[]]$WorkstationArguments = @(),
    [string]$RemoteHostname = '',
    [ValidateSet('', 'OneTimePin', 'Google', 'Microsoft')][string]$Identity = '',
    [switch]$RemoveData,
    [switch]$AssumeDefaults,
    [string]$StateRoot = (Join-Path $env:USERPROFILE '.aicleaner'),
    [AllowEmptyString()][string]$LogPath = (Join-Path ([IO.Path]::GetTempPath()) 'Deploy-AiCleaner.log')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:RecordPath = Join-Path $StateRoot 'deployment.json'
# How the remote page is signed in to (D44). Script scope, read as $script:Identity, so the one
# function that needs it reaches for it visibly rather than through a bare name (D17).
$script:Identity = $Identity
# Where a replaced build is kept (#61). Script scope for the same reason RecordPath is: the
# functions that need it are several calls deep, and threading a folder through every one of
# them to reach two call sites is worse than naming it once.
$script:StateFolder = $StateRoot
# The repository (or install) root: this script's folder, or its parent when it sits in installer\
# (feature 102). Pasted or extracted alone - the release zip - it is the folder it is in.
$script:ScriptFolder = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }
$script:Stage = ''
# Read by Get-Release (H16): an unsigned release is installed only when this was asked for.
$script:AllowUnsignedRelease = [bool]$AllowUnsigned
# -AssumeDefaults, for functions that would otherwise reach for the parameter (feature 104).
$script:Unattended = [bool]$AssumeDefaults
$script:PinnedSigningKey = ''
$script:Fetched = New-Object System.Collections.Generic.List[string]  # files pulled in to fill a gap

# --- Contact attribution (inlined per script so each script stays self-contained) ---------
function Get-ContactFile {
    <# The first contact-*.json beside this script (ordinal order of the lower-cased name), or $null. #>
    param([string]$Folder = $script:ScriptFolder)
    $candidates = @(Get-ChildItem -LiteralPath $Folder -Filter 'contact-*.json' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like 'contact-*.json' })
    # The repository keeps its own in examples\ since feature 102; one beside the script wins.
    if ($candidates.Count -eq 0) {
        $candidates = @(Get-ChildItem -LiteralPath (Join-Path $Folder 'examples') -Filter 'contact-*.json' -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like 'contact-*.json' })
    }
    if ($candidates.Count -eq 0) { return $null }
    $sortKeys = [string[]]@($candidates | ForEach-Object { $_.Name.ToLowerInvariant() })
    $sorted = [System.IO.FileInfo[]]$candidates
    [Array]::Sort($sortKeys, $sorted, [System.StringComparer]::Ordinal)
    return $sorted[0]
}

function Get-ContactInfo {
    <# The contact file as an object, or $null when there is none or it does not parse. #>
    $file = Get-ContactFile
    if (-not $file) { return $null }
    try { return (Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
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
# Helpers
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

function Write-StepBanner {
    <#
    .SYNOPSIS
        A framed heading for a major step of the install: "Step 3 of 6: Remote access" (feature 104).
    .DESCRIPTION
        The screen is cleared first when a person is watching, so each step starts on its own page;
        never with -AssumeDefaults, over SSH or with output redirected, where the run is a log that
        must stay readable from the top. ASCII only, in cyan.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][int]$Number, [Parameter(Mandatory)][int]$Of, [Parameter(Mandatory)][string]$Title,
          [switch]$Unattended)
    $interactive = -not $Unattended -and -not $env:SSH_CONNECTION -and [Environment]::UserInteractive
    try { $redirected = [Console]::IsOutputRedirected } catch { $redirected = $true }
    if ($interactive -and -not $redirected) { try { Clear-Host } catch { Write-Verbose 'no console to clear' } }
    $text = "  Step $Number of ${Of}: $Title  "
    $rule = '#' * ($text.Length + 4)
    Write-Host ''
    Write-Host $rule -ForegroundColor Cyan
    Write-Host "##$text##" -ForegroundColor Cyan
    Write-Host $rule -ForegroundColor Cyan
    Write-Host ''
}

function Write-SummaryBox {
    <# The framed summary at the end of an install: what was done and what to do next (feature 104). #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Title, [Parameter(Mandatory)][AllowEmptyCollection()][AllowEmptyString()][string[]]$Lines)
    $longest = $Title.Length
    foreach ($line in $Lines) { if ($line.Length -gt $longest) { $longest = $line.Length } }
    $width = $longest + 8
    $rule = '#' * $width
    Write-Host ''
    Write-Host $rule -ForegroundColor Green
    Write-Host ("##  {0}" -f $Title).PadRight($width - 2) -ForegroundColor Green -NoNewline; Write-Host '##' -ForegroundColor Green
    Write-Host ('##' + (' ' * ($width - 4)) + '##') -ForegroundColor Green
    foreach ($line in $Lines) {
        Write-Host ("##  {0}" -f $line).PadRight($width - 2) -NoNewline; Write-Host '##' -ForegroundColor Green
    }
    Write-Host $rule -ForegroundColor Green
    Write-Host ''
}

function Confirm-Action {
    <# Yes/no question; -AssumeDefaults answers with the default. #>
    [CmdletBinding()]
    [OutputType([bool])]
    param(
        [Parameter(Mandatory)][string]$Message,
        [ValidateSet('Yes', 'No')][string]$Default = 'No'
    )
    if ($AssumeDefaults) {
        Write-Status "$Message -> $Default (default)" -Level Detail
        return ($Default -eq 'Yes')
    }
    $suffix = if ($Default -eq 'Yes') { '[Y/n]' } else { '[y/N]' }
    $response = Read-Host "$Message $suffix"
    if ([string]::IsNullOrWhiteSpace($response)) { return ($Default -eq 'Yes') }
    return ($response.Trim() -match '^(y|yes)$')
}

function Expand-InputPath {
    <#
    .SYNOPSIS
        A path as somebody would paste it, turned into one the script can use.
    .DESCRIPTION
        A path with a space in it is written in quotes, and Explorer's "Copy as path" adds them
        whether it needs to or not. Passed as -InstallPath they never reach the script: PowerShell's
        parameter binder takes them off. Typed at a prompt they do, because Read-Host returns the
        line as it was typed, and the quotes then become part of the folder name.

        Both kinds of quote are taken off, repeatedly, so a doubly quoted paste works too, and
        %USERPROFILE%-style variables are expanded for the same reason: it is what people type.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value)
    $text = $Value.Trim()
    while ($text.Length -ge 2 -and
           (($text[0] -eq '"' -and $text[$text.Length - 1] -eq '"') -or
            ($text[0] -eq "'" -and $text[$text.Length - 1] -eq "'"))) {
        $text = $text.Substring(1, $text.Length - 2).Trim()
    }
    if ($text) { $text = [Environment]::ExpandEnvironmentVariables($text) }
    return $text
}

function Read-Value {
    <# Free-text question with a default; -AssumeDefaults answers with the default. #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][string]$Message,
          [Parameter(Mandatory)][AllowEmptyString()][string]$Default,
          [switch]$AsPath)
    if ($AssumeDefaults) {
        Write-Status "$Message -> $Default (default)" -Level Detail
        return $Default
    }
    $response = Read-Host "$Message [$Default]"
    if ([string]::IsNullOrWhiteSpace($response)) { return $Default }
    if ($AsPath) { return (Expand-InputPath -Value $response) }
    return $response.Trim()
}

function Read-Typed {
    <# A typed confirmation for deleting data. Never assumed: the answer must match exactly. #>
    [CmdletBinding()]
    [OutputType([bool])]
    param([Parameter(Mandatory)][string]$Message, [Parameter(Mandatory)][string]$Expected)
    $response = Read-Host $Message
    return ($null -ne $response -and $response.Trim() -ceq $Expected)
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
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
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

function Test-PolicyWritable {
    <#
    .SYNOPSIS
        True when this process can write HKCU\SOFTWARE\Policies (Claude Desktop's policy). A standard
        (non-elevated) token has read access only.
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

function Test-UnderFolder {
    <# True when Path is Folder or inside it (case-insensitive, full paths). #>
    [OutputType([bool])]
    param([Parameter(Mandatory)][string]$Path, [string]$Folder)
    if (-not $Folder) { return $false }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\') + '\'
    $root = [IO.Path]::GetFullPath($Folder).TrimEnd('\') + '\'
    return $full.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)
}

function Get-OneDriveRoot {
    <# The OneDrive folder whose sync client covers Path, or '' when Path is not under OneDrive. #>
    [OutputType([string])]
    param([string]$Path = '')
    foreach ($candidate in @($env:OneDriveCommercial, $env:OneDriveConsumer, $env:OneDrive)) {
        if (-not $candidate) { continue }
        if (-not $Path -or (Test-UnderFolder -Path $Path -Folder $candidate)) { return $candidate }
    }
    return ''
}

# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

function Invoke-Git {
    <# Runs git; returns stdout lines, throws with the last stderr lines on failure. #>
    [CmdletBinding()]
    [OutputType([string[]])]
    param([Parameter(Mandatory)][string[]]$Arguments)
    $savedEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& git @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $savedEap }
    $stdout = @($output | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } | ForEach-Object { "$_" })
    $stderr = @($output | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object { "$_" })
    if ($exitCode -ne 0) {
        throw ("git {0} failed (exit {1}): {2}" -f $Arguments[0], $exitCode, (($stderr | Select-Object -Last 3) -join ' '))
    }
    return $stdout
}

function Test-RepoAccess {
    <#
    .SYNOPSIS
        Can this repository be read, and if not, why - without cloning and without a prompt (64).
    .DESCRIPTION
        One `git ls-remote`. Prompts are switched off for it: on an unattended run Git Credential
        Manager would otherwise wait for a browser nobody is watching. State is ok, auth (no or
        wrong credentials), missing (not found - which is also what GitHub says of a private repo
        to someone not signed in), or network.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Url)
    $saved = @{ GIT_TERMINAL_PROMPT = $env:GIT_TERMINAL_PROMPT; GCM_INTERACTIVE = $env:GCM_INTERACTIVE }
    $env:GIT_TERMINAL_PROMPT = '0'
    $env:GCM_INTERACTIVE = 'Never'
    try {
        [void](Invoke-Git -Arguments @('ls-remote', '--heads', $Url))
        return [pscustomobject]@{ State = 'ok'; Detail = '' }
    } catch {
        $why = "$_"
        $state = if ($why -match 'could not read Username|terminal prompts disabled|Authentication failed|Invalid username|403') { 'auth' }
                 elseif ($why -match 'not found|does not exist') { 'missing' }
                 else { 'network' }
        return [pscustomobject]@{ State = $state; Detail = $why }
    } finally {
        foreach ($name in $saved.Keys) { Set-Item -Path "env:$name" -Value $saved[$name] }
    }
}

function Get-RepoVisibility {
    <# 'public', 'private, or not found', or 'could not tell' - GitHub only, asked anonymously. #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][string]$Url)
    $found = [regex]::Match($Url, 'github\.com[:/](?<owner>[^/]+)/(?<repo>[^/]+?)(\.git)?/?$')
    if (-not $found.Success) { return 'could not tell' }
    try {
        $answer = Invoke-RestMethod -Uri ("https://api.github.com/repos/{0}/{1}" -f $found.Groups['owner'].Value, $found.Groups['repo'].Value) -TimeoutSec 10
        return $(if ($answer.private) { 'private' } else { 'public' })
    } catch {
        $code = try { [int]$_.Exception.Response.StatusCode } catch { 0 }
        return $(if ($code -eq 404) { 'private, or not found' } else { 'could not tell' })
    }
}

function Save-GitToken {
    <# A GitHub token, encrypted to this Windows account (DPAPI), owner-only - as the Cloudflare token is. #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][System.Security.SecureString]$Secure, [Parameter(Mandatory)][string]$Path)
    $folder = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure))
    try {
        Add-Type -AssemblyName System.Security
        $bytes = [Text.Encoding]::UTF8.GetBytes($plain)
        [IO.File]::WriteAllBytes($Path, [Security.Cryptography.ProtectedData]::Protect($bytes, $null, 'CurrentUser'))
        [Array]::Clear($bytes, 0, $bytes.Length)
    } finally { $plain = $null }
    & icacls $Path /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
}

function Read-GitToken {
    <# The stored token as plain text, or '' when there is none or it cannot be decrypted here. #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    try {
        Add-Type -AssemblyName System.Security
        return [Text.Encoding]::UTF8.GetString(
            [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($Path), $null, 'CurrentUser'))
    } catch { return '' }
}

function Use-GitToken {
    <#
    .SYNOPSIS
        Hand git a token for this process only (64).
    .DESCRIPTION
        Through GIT_CONFIG_COUNT / KEY / VALUE, which git reads as configuration for the commands
        this process runs. Never `git config --global`, never a credential in the URL - either
        would leave it on disk, and the URL is written into the deployment record.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Token, [Parameter(Mandatory)][string]$Url)
    $origin = [regex]::Match($Url, '^https://[^/]+/').Value
    if (-not $origin) { return }
    $basic = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("x-access-token:$Token"))
    $env:GIT_CONFIG_COUNT = '1'
    $env:GIT_CONFIG_KEY_0 = "http.$origin.extraheader"
    $env:GIT_CONFIG_VALUE_0 = "AUTHORIZATION: basic $basic"
}

function Resolve-RepoAccess {
    <#
    .SYNOPSIS
        Make sure the repository can be read before anything relies on it, or stop and say why (64).
    .DESCRIPTION
        A token already stored is used first. When the repository still cannot be read: its URL
        and whether it is private are shown, and both ways in are offered (DECISIONS 32) - Git's
        own sign-in, which leaves aicleaner holding nothing, or a fine-grained read-only token kept
        with DPAPI. Either is checked again before it is believed. A local copy is never switched to
        silently: stopping says how to choose one.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Url, [Parameter(Mandatory)][string]$TokenPath)
    $stored = Read-GitToken -Path $TokenPath
    if ($stored) { Use-GitToken -Token $stored -Url $Url }
    $access = Test-RepoAccess -Url $Url
    if ($access.State -eq 'ok') { return }

    $visibility = Get-RepoVisibility -Url $Url
    $what = switch ($access.State) {
        'auth'    { 'no credentials that can read it' }
        'missing' { 'not found - which is also what GitHub answers for a private repository when you are not signed in' }
        default   { 'the network did not reach it' }
    }
    Write-Status "Cannot read the repository $Url" -Level Warn
    Write-Status "  It is: $visibility. What git said: $what." -Level Detail
    $local = 'Stopped. To deploy from a copy on this machine instead, re-run with -FromWorkingCopy (from the copy) or -SourcePath <folder>.'
    if ($access.State -eq 'network') { throw "Cannot reach $Url. $local" }
    $choice = Read-Choice -Message "How should this machine read $Url ?" -Default 'stop' -Options ([ordered]@{
        gcm   = 'Sign in with Git Credential Manager: a browser opens, Windows keeps the sign-in, and aicleaner stores nothing'
        token = 'Store a GitHub token for aicleaner, encrypted to this Windows account (DPAPI) - use a fine-grained, read-only, single repository token'
        stop  = 'Stop, and deploy from a local copy instead' })
    switch ($choice) {
        'gcm' {
            Write-Status 'Signing in through Git Credential Manager (a browser window may open)' -Level Detail
            try { [void](Invoke-Git -Arguments @('ls-remote', '--heads', $Url)) } catch { Write-Status "$_" -Level Detail }
        }
        'token' {
            Write-Status 'Create it at github.com > Settings > Developer settings > Fine-grained tokens: this repository only, Contents: Read-only. A classic token with "repo" can read every private repository you can.' -Level Detail
            $secure = Read-Host -Prompt 'Paste the token (not shown)' -AsSecureString
            Save-GitToken -Secure $secure -Path $TokenPath
            Use-GitToken -Token (Read-GitToken -Path $TokenPath) -Url $Url
            Write-Status "Token stored, encrypted to this account, at $TokenPath" -Level Detail
        }
        default { throw "Cannot read $Url. $local" }
    }
    $again = Test-RepoAccess -Url $Url
    if ($again.State -ne 'ok') { throw "Still cannot read $Url ($($again.State)). $local" }
    Write-Status "The repository can be read now" -Level Detail
}

function Assert-Git {
    <# Makes sure git is available, offering Git for Windows (with Git Credential Manager) via winget. #>
    [CmdletBinding(SupportsShouldProcess)]
    param()
    if (Get-Command git -ErrorAction SilentlyContinue) { return }
    Write-Status 'git is not installed' -Level Warn
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) { throw 'git is required: install Git for Windows (https://git-scm.com/download/win) and re-run' }
    if (-not (Confirm-Action -Message 'Install Git for Windows with winget?' -Default 'Yes')) { throw 'git is required' }
    if (-not $PSCmdlet.ShouldProcess('Git.Git', 'winget install')) { throw 'git is required to read the repository' }
    & $winget.Source install --id Git.Git -e --silent --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'git was installed but is not on PATH yet; open a new PowerShell and re-run' }
}

function Resolve-RepoUrl {
    <# The repository to deploy from: -RepoUrl, else repo_url in the contact file. https only. #>
    [CmdletBinding()]
    [OutputType([string])]
    param([string]$Explicit)
    $url = $Explicit
    if (-not $url) {
        $contact = Get-ContactInfo
        if ($contact -and $contact.PSObject.Properties['repo_url'] -and $contact.repo_url) { $url = [string]$contact.repo_url }
    }
    if (-not $url) {
        Write-Status 'No contact-*.json beside this script names a repository' -Level Warn
        $url = Read-Value -Message 'Repository to deploy from (https://...)' -Default ''
    }
    if (-not $url) { throw 'No repository: pass -RepoUrl, or put repo_url in the contact-*.json beside this script' }
    if ($url -match '^https://[^\s/]+/\S+$') { return $url }
    if ($url -match '^file://' -or (Test-Path -LiteralPath $url -PathType Container)) {
        Write-Status "Using a local repository ($url); intended for offline mirrors and tests" -Level Warn
        return $url
    }
    throw "Refusing the repository '$url': only https:// addresses (or a local folder) are accepted"
}

function Resolve-Release {
    <#
    .SYNOPSIS
        The ref to deploy and its commit: -Ref when given, else the highest vX.Y.Z tag, else main.
        Annotated tags are peeled to their commit.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Url, [string]$Wanted = '')
    $pattern = [regex]'^(?<sha>[0-9a-f]{40,64})\s+refs/(?<kind>tags|heads)/(?<name>[^\s^]+)(?<peeled>\^\{\})?$'
    $refs = @{}
    foreach ($line in (Invoke-Git -Arguments @('ls-remote', '--tags', '--heads', $Url))) {
        $found = $pattern.Match($line)
        if (-not $found.Success) { continue }
        $key = $found.Groups['kind'].Value + '/' + $found.Groups['name'].Value
        if ($found.Groups['peeled'].Success -or -not $refs.ContainsKey($key)) { $refs[$key] = $found.Groups['sha'].Value }
    }
    if ($Wanted) {
        # The hashtable ignores case and git does not: -Ref Main found heads/main here, then git was
        # asked for a branch called Main and refused. Return the name as the repository spells it,
        # preferring an exact match where two refs differ only by case.
        foreach ($key in @("tags/$Wanted", "heads/$Wanted")) {
            $exact = @($refs.Keys | Where-Object { $_ -ceq $key })
            $found = @(if ($exact.Count -gt 0) { $exact } else { $refs.Keys | Where-Object { $_ -ieq $key } })
            if ($found.Count -gt 0) {
                $name = $found[0].Substring($found[0].IndexOf('/') + 1)
                if ($name -cne $Wanted) { Write-Status "Using '$name' (the repository's spelling of '$Wanted')" -Level Detail }
                return [pscustomobject]@{ Ref = $name; Commit = $refs[$found[0]] }
            }
        }
        throw "The repository has no tag or branch named '$Wanted'"
    }
    $versions = @($refs.Keys | Where-Object { $_ -match '^tags/v\d+\.\d+\.\d+$' } | ForEach-Object {
        [pscustomobject]@{ Name = $_.Substring(5); Version = [version]$_.Substring(6) } } | Sort-Object Version)
    if ($versions.Count -gt 0) {
        $newest = $versions[$versions.Count - 1].Name
        return [pscustomobject]@{ Ref = $newest; Commit = $refs["tags/$newest"] }
    }
    if ($refs.ContainsKey('heads/main')) {
        Write-Status 'The repository has no release tags (vX.Y.Z); using main, which may be unreleased work' -Level Warn
        return [pscustomobject]@{ Ref = 'main'; Commit = $refs['heads/main'] }
    }
    throw 'The repository has no release tags and no main branch'
}

# What a copy of the source never contains: build leftovers, someone else's editor and tool state,
# and the data a deployment makes for itself. Everything else is the source.
$script:CopyExcludedFolders = @('.git', '.venv', 'venv', '__pycache__', '.pytest_cache', '.mypy_cache',
                                '.ruff_cache', 'node_modules', '.serena', '.claude', '.vscode', '.idea',
                                'logs', 'clients', 'lastgood', 'stage', '.aicleaner')
$script:CopyExcludedFiles = @('aicleaner-config.json', 'deployment.json', '.gitignore', '.gitattributes')
$script:CopyExcludedSuffixes = @('.pyc', '.pyo', '.log', '.bak', '.tmp', '.zip')

function Get-PlainCopyFiles {
    <#
    .SYNOPSIS
        The files of a source folder that is not a git clone, relative to it.
    .DESCRIPTION
        Copying the repository through OneDrive or a file share leaves no .git, so there is no
        tracked-file list to read. Everything is taken except build leftovers, editor state and the
        data a deployment writes for itself; a clone is still exact, and this says so.
    #>
    [CmdletBinding()]
    [OutputType([string[]])]
    param([Parameter(Mandatory)][string]$Folder)
    $root = [IO.Path]::GetFullPath($Folder).TrimEnd('\')
    $found = New-Object System.Collections.Generic.List[string]
    foreach ($item in @(Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction SilentlyContinue)) {
        $relative = $item.FullName.Substring($root.Length).TrimStart('\')
        $parts = $relative.Split('\')
        $skip = $false
        foreach ($part in $parts[0..([Math]::Max(0, $parts.Count - 2))]) {
            if ($parts.Count -gt 1 -and $script:CopyExcludedFolders -contains $part) { $skip = $true; break }
        }
        if ($skip) { continue }
        if ($script:CopyExcludedFiles -contains $item.Name) { continue }
        if ($script:CopyExcludedSuffixes -contains $item.Extension.ToLowerInvariant()) { continue }
        $found.Add($relative.Replace('\', '/'))
    }
    return $found.ToArray()
}

function Get-WorkingCopy {
    <#
    .SYNOPSIS
        This working copy as a release: the tracked files as they are on disk, no fetch.
    .DESCRIPTION
        For deploying what you are working on to the machine you are working on, without tagging
        anything. The code still lands in the install folder rather than running from the repo, so
        a branch switch does not change what is running, and Update and Uninstall still know what
        was installed.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Folder)
    Restore-CloudFiles -Folder $Folder | Out-Null
    $isClone = Test-Path -LiteralPath (Join-Path $Folder '.git')
    $head = ''
    if ($isClone) {
        Assert-Git
        $files = @(Invoke-Git -Arguments @('-C', $Folder, '-c', 'core.quotepath=off', 'ls-files') | Where-Object { $_ })
        try { $head = @(Invoke-Git -Arguments @('-C', $Folder, 'rev-parse', 'HEAD'))[0] } catch { $head = '' }
    } else {
        # A copy of the source - through OneDrive, a file share, a zip - has no tracked-file list.
        if (-not (Test-Path -LiteralPath (Join-Path $Folder 'aicleaner\__init__.py'))) {
            throw "$Folder does not hold the aicleaner source (no aicleaner\__init__.py). Point -SourcePath at the copy, or use -Action GetSource to clone it"
        }
        $files = @(Get-PlainCopyFiles -Folder $Folder)
        Write-Status "No .git here, so this is a copy rather than a clone: taking every file except build leftovers and deployment data" -Level Warn
        Write-Status 'A clone deploys exactly what is tracked; -Action GetSource makes one.' -Level Detail
    }
    if ($files.Count -eq 0) { throw "$Folder has no tracked files" }
    $version = ''
    $init = Join-Path $Folder 'aicleaner\__init__.py'
    if (Test-Path -LiteralPath $init) {
        $declared = [regex]::Match((Get-Content -LiteralPath $init -Raw), '__version__\s*=\s*"(?<v>[^"]+)"')
        if ($declared.Success) { $version = $declared.Groups['v'].Value }
    }
    if ($isClone) {
        $dirty = @(Invoke-Git -Arguments @('-C', $Folder, 'status', '--porcelain') | Where-Object { $_ })
        if ($dirty.Count -gt 0) {
            Write-Status "$($dirty.Count) uncommitted change(s) in the working copy; they are deployed as they are" -Level Warn
        }
    }
    return [pscustomobject]@{ Folder = $Folder; Files = $files; Version = $version; Commit = $head }
}

function Get-Release {
    <#
    .SYNOPSIS
        Clones the release into a temporary folder, checks it is the expected commit and returns
        the folder, the tracked files and the version it declares.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Url, [Parameter(Mandatory)]$Release, [switch]$AlreadyInstalled)
    $script:Stage = Join-Path ([IO.Path]::GetTempPath()) ('aicleaner-deploy-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    Write-Status "Fetching $($Release.Ref)"
    Invoke-Git -Arguments @('-c', 'advice.detachedHead=false', 'clone', '--quiet', '--depth', '1',
        '--branch', $Release.Ref, $Url, $script:Stage) | Out-Null
    $head = @(Invoke-Git -Arguments @('-C', $script:Stage, 'rev-parse', 'HEAD'))[0]
    if ($head -ne $Release.Commit) { throw "Fetched commit $head is not the expected $($Release.Commit); stopping" }
    $files = @(Invoke-Git -Arguments @('-C', $script:Stage, '-c', 'core.quotepath=off', 'ls-files') | Where-Object { $_ })
    # Before anything is copied (H16): signed by the pinned key, every file as the manifest says.
    # Re-fetching the commit already installed (a repair, an added role) on an install that has no
    # key pinned yet - it predates signing - takes it as it is: refusing would only break the repair.
    $pinned = [string]$script:PinnedSigningKey
    $unsignedOk = $script:AllowUnsignedRelease -or ($AlreadyInstalled -and -not $pinned)
    $signingKey = Assert-ReleaseSigned -Folder $script:Stage -Files $files -Pinned $pinned -AllowUnsigned:$unsignedOk
    $version = ''
    $init = Join-Path $script:Stage 'aicleaner\__init__.py'
    if (Test-Path -LiteralPath $init) {
        $declared = [regex]::Match((Get-Content -LiteralPath $init -Raw), '__version__\s*=\s*"(?<v>[^"]+)"')
        if ($declared.Success) { $version = $declared.Groups['v'].Value }
    }
    return [pscustomobject]@{ Folder = $script:Stage; Files = $files; Version = $version; SigningKey = $signingKey }
}

# ---------------------------------------------------------------------------
# Signed releases (H16, DECISIONS 54)
# ---------------------------------------------------------------------------

function Get-NormalisedHash {
    <# SHA-256 of a file with CRLF read as LF, lowercase hex - the same as tools/sign_release.py. A
       Windows checkout converts line endings, and a genuine release must not look altered. #>
    [OutputType([string])]
    param([Parameter(Mandatory)][string]$Path)
    # Latin-1 maps every byte to one character and back, so any file - text or not - survives the
    # round trip; only the CRLF pairs change.
    $latin1 = [Text.Encoding]::GetEncoding(28591)
    $bytes = $latin1.GetBytes($latin1.GetString([IO.File]::ReadAllBytes($Path)).Replace("`r`n", "`n"))
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $sha.Dispose() }
}

function Get-AppFile {
    <#
    .SYNOPSIS
        A file of the install by its name: in its folder since the layout (feature 102 - installer\,
        examples\, requirements\, release\), or at the root in an install from before the move.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][string]$AppRoot, [Parameter(Mandatory)][string]$Name)
    $folders = @{ '.ps1' = 'installer'; '.cmd' = 'installer'; '.lock' = 'requirements'; '.txt' = 'requirements'; '.json' = 'examples' }
    $folder = $folders[[IO.Path]::GetExtension($Name).ToLowerInvariant()]
    if ($folder) {
        $moved = Join-Path (Join-Path $AppRoot $folder) $Name
        if (Test-Path -LiteralPath $moved) { return $moved }
    }
    return (Join-Path $AppRoot $Name)
}

function Find-SshKeygen {
    <# OpenSSH's ssh-keygen, which Windows 10 and 11 include; '' when it is not there. #>
    [OutputType([string])]
    param()
    $found = Get-Command ssh-keygen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Source }
    $inbox = Join-Path $env:WINDIR 'System32\OpenSSH\ssh-keygen.exe'
    if (Test-Path -LiteralPath $inbox) { return $inbox }
    return ''
}

function Assert-ReleaseSigned {
    <#
    .SYNOPSIS
        Refuses a release unless RELEASE-MANIFEST is signed by the pinned key and every file matches it.
    .DESCRIPTION
        The release names its key in release-signing.pub. With nothing pinned yet (a first install, or
        the first update after signing began) that key is pinned and returned; afterwards a release
        naming a different key is refused - a new key means someone has the repository but not the
        old key. Unsigned releases (a branch, an older tag) need -AllowUnsigned, said loudly.
        Returns the key ("ssh-ed25519 AAAA...") to store as the deployment's signing_key.
        A refusal throws: installing code nobody vouched for is not a condition to recover from here.
    #>
    [OutputType([string])]
    param([Parameter(Mandatory)][string]$Folder, [Parameter(Mandatory)][string[]]$Files,
          [string]$Pinned = '', [switch]$AllowUnsigned)
    # The signer identity and the namespace, both: tools/sign_release.py signs with the same.
    $identity = 'aicleaner-release'
    # In release\ since feature 102; a release tagged before the move keeps them at its root.
    $releaseDir = if (Test-Path -LiteralPath (Join-Path $Folder 'release\RELEASE-MANIFEST')) { Join-Path $Folder 'release' } else { $Folder }
    $manifest = Join-Path $releaseDir 'RELEASE-MANIFEST'
    $signature = Join-Path $releaseDir 'RELEASE-MANIFEST.sig'
    $named = Join-Path $releaseDir 'release-signing.pub'
    if (-not (Test-Path -LiteralPath $manifest) -or -not (Test-Path -LiteralPath $signature)) {
        if ($AllowUnsigned) {
            Write-Warning 'This release is NOT signed. Installing it anyway, because -AllowUnsigned was given.'
            return ''
        }
        throw 'This release is not signed (no RELEASE-MANIFEST): refusing it. A branch or a release from before signing began needs -AllowUnsigned.'
    }
    $offered = ''
    if (Test-Path -LiteralPath $named) {
        $parts = @(((Get-Content -LiteralPath $named -Raw) -split '\s+') | Where-Object { $_ })
        if ($parts.Count -ge 3) { $offered = "$($parts[1]) $($parts[2])" }
    }
    $key = if ($Pinned) { $Pinned } else { $offered }
    if (-not $key) { throw 'The release names no signing key (release-signing.pub): refusing it.' }
    if ($Pinned -and $offered -and $offered -ne $Pinned) {
        throw 'The release is signed with a different key from the one this install trusts: refusing it. If the key was really changed, uninstall and install again to trust the new one.'
    }
    $keygen = Find-SshKeygen
    if (-not $keygen) { throw 'ssh-keygen (OpenSSH) was not found, so the release cannot be checked. Add the Windows "OpenSSH Client" feature and run this again.' }
    $allowed = [IO.Path]::GetTempFileName()
    $check = $null
    try {
        Set-Content -LiteralPath $allowed -Value "$identity $key" -Encoding Ascii
        # The manifest is written to ssh-keygen's input and the input closed, by hand, with a time
        # limit (D96). Start-Process -RedirectStandardInput hung forever in a hosted PowerShell
        # (no console, a transcript running): ssh-keygen sat waiting for an end of input that
        # never came, with the update stopped part way and nothing said.
        $info = New-Object System.Diagnostics.ProcessStartInfo
        $info.FileName = $keygen
        $info.Arguments = "-Y verify -f `"$allowed`" -I $identity -n $identity -s `"$signature`""
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.RedirectStandardInput = $true
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $check = [System.Diagnostics.Process]::Start($info)
        $bytes = [IO.File]::ReadAllBytes($manifest)
        $check.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
        $check.StandardInput.Close()
        # Read both before waiting, so a full pipe can never stall the child.
        $said = $check.StandardOutput.ReadToEndAsync()
        $complained = $check.StandardError.ReadToEndAsync()
        if (-not $check.WaitForExit(60000)) {
            try { $check.Kill() } catch { Write-Verbose "ssh-keygen had already ended: $($_.Exception.Message)" }
            throw 'Checking the release signature did not finish within a minute: refusing the release.'
        }
        $null = $said.Result, $complained.Result
        if ($check.ExitCode -ne 0) { throw 'The release signature does not verify with the trusted key: refusing it.' }
    } finally {
        if ($check) { $check.Dispose() }
        Remove-Item -LiteralPath $allowed -Force -ErrorAction SilentlyContinue
    }
    $listed = @{}
    foreach ($line in (Get-Content -LiteralPath $manifest)) {
        $entry = [regex]::Match($line, '^(?<hash>[0-9a-f]{64})  (?<path>.+)$')
        if ($entry.Success) { $listed[$entry.Groups['path'].Value] = $entry.Groups['hash'].Value }
    }
    $unlisted = @('RELEASE-MANIFEST', 'RELEASE-MANIFEST.sig', 'release/RELEASE-MANIFEST', 'release/RELEASE-MANIFEST.sig')
    $shipped = @($Files | Where-Object { $unlisted -notcontains $_ })
    foreach ($relative in $shipped) {
        if (-not $listed.ContainsKey($relative)) { throw "$relative is in the release but not in its signed manifest: refusing it." }
        $actual = Get-NormalisedHash -Path (Join-Path $Folder $relative.Replace('/', '\'))
        if ($actual -ne $listed[$relative]) { throw "$relative does not match its signed manifest: refusing the release." }
    }
    if ($listed.Count -ne $shipped.Count) { throw 'The signed manifest lists files the release does not have: refusing it.' }
    if (-not $Pinned) { Write-Status "Trusting the release signing key it names (pinned from now on): $($key.Substring(0, [Math]::Min(40, $key.Length)))..." -Level Detail }
    return $key
}

function Remove-Stage {
    if ($script:Stage -and (Test-Path -LiteralPath $script:Stage)) {
        # git marks pack files read-only; clear that so the folder can be deleted.
        Get-ChildItem -LiteralPath $script:Stage -Recurse -Force -File | ForEach-Object { $_.IsReadOnly = $false }
        Remove-Item -LiteralPath $script:Stage -Recurse -Force
    }
    $script:Stage = ''
}

# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

function Restore-RepoFile {
    <#
    .SYNOPSIS
        Makes sure one file is in the app folder, fetching it from the repository when it is not.
    .DESCRIPTION
        A deployment made before a file existed does not have it, and neither does an older working
        copy; this script may be newer than both. Rather than failing with "not found", the file is
        fetched from the release being deployed (or the one the record names). Returns true when the
        file is there afterwards.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    [OutputType([bool])]
    param([Parameter(Mandatory)][string]$AppRoot, [Parameter(Mandatory)][string]$Relative,
          [string]$Url = '', [string]$Ref = '')
    # A bare script name is wherever the layout puts it (feature 102): installer\ since the move, the
    # root before it - in the app folder and in the release alike.
    $choices = @($Relative)
    if ($Relative -notmatch '[\\/]') {
        $folder =@{ '.ps1' = 'installer'; '.cmd' = 'installer'; '.lock' = 'requirements'; '.txt' = 'requirements'; '.json' = 'examples' }[[IO.Path]::GetExtension($Relative).ToLowerInvariant()]
        $choices = @(if ($folder) { "$folder/$Relative" }) + @($Relative)
    }
    foreach ($choice in $choices) {
        if (Test-Path -LiteralPath (Join-Path $AppRoot $choice.Replace('/', '\'))) { return $true }
    }
    $destination = Join-Path $AppRoot $Relative.Replace('/', '\')
    if (-not $Url) {
        Write-Status "$Relative is missing from $AppRoot and there is no repository to fetch it from" -Level Warn
        return $false
    }
    Write-Status "$Relative is missing from $AppRoot; fetching it from the repository" -Level Warn
    if (-not $PSCmdlet.ShouldProcess($destination, "Fetch $Relative")) { return $false }
    try {
        Assert-Git
        if (-not $script:Stage -or -not (Test-Path -LiteralPath $script:Stage)) {
            $release = Resolve-Release -Url $Url -Wanted $Ref
            # A repair of the install's own release: pinned key if there is one (H16).
            Get-Release -Url $Url -Release $release -AlreadyInstalled | Out-Null
        }
        $found = @($choices | Where-Object { Test-Path -LiteralPath (Join-Path $script:Stage $_.Replace('/', '\')) } | Select-Object -First 1)
        if (-not $found) {
            Write-Status "$Relative is not in that release either; the repository may be older than this script" -Level Warn
            return $false
        }
        $source = Join-Path $script:Stage $found[0].Replace('/', '\')
        $destination = Join-Path $AppRoot $found[0].Replace('/', '\')
        $folder = Split-Path -Parent $destination
        if ($folder -and -not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
        Copy-Item -LiteralPath $source -Destination $destination -Force
        $script:Fetched.Add($destination) | Out-Null
        Write-Status "Fetched $Relative" -Level Done
        return $true
    } catch {
        Write-Status "Could not fetch ${Relative}: $($_.Exception.Message)" -Level Warn
        return $false
    }
}

# What Windows sets on a file whose contents live in the cloud: offline, recall on open, recall on
# data access. Reading the attributes does not fetch anything, which is what makes the check cheap.
$script:CloudOnlyAttributes = 0x441000

function Test-CopyExcluded {
    <#
    .SYNOPSIS
        Whether a path relative to the source root is one a copy would never take.
    .DESCRIPTION
        The same three lists the copy itself uses, in one place, so nothing can wait on a file it
        is not going to read.
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param([Parameter(Mandatory)][string]$Relative)
    $parts = $Relative.Split('\')
    if ($parts.Count -gt 1) {
        foreach ($part in $parts[0..($parts.Count - 2)]) {
            if ($script:CopyExcludedFolders -contains $part) { return $true }
        }
    }
    $leaf = $parts[$parts.Count - 1]
    if ($script:CopyExcludedFiles -contains $leaf) { return $true }
    return ($script:CopyExcludedSuffixes -contains ([IO.Path]::GetExtension($leaf).ToLowerInvariant()))
}

function Get-CloudOnlyFile {
    <#
    .SYNOPSIS
        Every file under a folder whose contents are not on this machine yet, and that a copy
        would actually take.
    .DESCRIPTION
        The exclusions are the point, not a tidy-up. Without them this scans .git, .venv, logs and
        node_modules - none of which are ever copied - and then waits ten minutes for them. Seen
        2026-09-27: a zero-length logs\proxy-stdout.log carrying a stale offline attribute stopped
        a deployment dead, on a folder OneDrive does not even sync. A deploy must not block on a
        file it is not going to read.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Folder)
    $root = [IO.Path]::GetFullPath($Folder).TrimEnd('\')
    return @(Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object { ([int]$_.Attributes -band $script:CloudOnlyAttributes) -ne 0 } |
        Where-Object { -not (Test-CopyExcluded -Relative $_.FullName.Substring($root.Length).TrimStart('\')) })
}

function Restore-CloudFiles {
    <#
    .SYNOPSIS
        Fetches the files in a folder that OneDrive is keeping in the cloud, and waits for them.
    .DESCRIPTION
        Files On-Demand leaves a placeholder on disk: the name is there, the contents are not. Copying
        one of those to the install folder gives you an empty file or an error, so before anything is
        read or copied the whole tree is pinned (Always keep on this device) and the placeholders are
        waited out. Returns true when everything is here.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    [OutputType([bool])]
    param([Parameter(Mandatory)][string]$Folder, [int]$TimeoutSeconds = 600)
    if (-not (Test-Path -LiteralPath $Folder)) { return $true }
    $pending = @(Get-CloudOnlyFile -Folder $Folder)
    if ($pending.Count -eq 0) { return $true }
    Write-Status "$($pending.Count) file(s) in $Folder are in the cloud only; fetching them first" -Level Warn
    if (-not $PSCmdlet.ShouldProcess($Folder, 'Fetch the files OneDrive is holding in the cloud')) { return $false }
    # +P is "always keep on this device", which is what starts the download; -U clears "free up space".
    # Named file by file rather than /S over the whole tree: a recursive pin would hydrate .venv,
    # .git and node_modules as well - thousands of files nothing is going to copy - and on a
    # metered or slow link that is the difference between a deploy and an afternoon.
    foreach ($file in $pending) {
        & attrib.exe +P -U $file.FullName 2>&1 | Out-Null
    }
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $announced = 0
    while ((Get-Date) -lt $deadline) {
        $pending = @(Get-CloudOnlyFile -Folder $Folder)
        if ($pending.Count -eq 0) {
            Write-Status 'Everything is on this machine now' -Level Done
            return $true
        }
        if ($pending.Count -ne $announced) {
            Write-Status "waiting for $($pending.Count) file(s)" -Level Detail
            $announced = $pending.Count
        }
        Start-Sleep -Seconds 2
    }
    Write-Status ("Gave up waiting for {0} file(s), the first being {1}. Is OneDrive running and signed in?" -f
        $pending.Count, $pending[0].FullName) -Level Warn
    return $false
}

# What a client PC actually runs. Everything else - the proxy, its package, the UI, the public
# identifier list, the other installers - belongs on a machine with an agent on it.
$script:WorkstationFiles = @(
    'Install-AiCleaner-Workstation.ps1',   # the wiring
    'Capture-TextFromScreen.ps1',          # what the shortcut points at, so it needs a settled home
    'Read-ImageText.ps1',                  # the OCR and the column rebuild - Capture cannot read without it
    # Capture-TextFromScreen.ps1 is the one script here that is not standalone: the OCR and the
    # column rebuild live in the reader, shared so the proxy and the shortcut cannot give
    # different answers for the same image. Leaving it out is D51 - the deploy reported success
    # and the first press of Win+Shift+T failed with the missing-companion message the capture
    # script raises itself, on the machine whose entire purpose is that shortcut.
    'Read-ImageText.ps1',
    'Uninstall-AiCleaner.ps1',             # and a way back out
    'Remove-WorkstationSetup.ps1'
)

function Select-RoleFiles {
    <#
    .SYNOPSIS
        The files a set of targets needs, out of everything the release holds.
    .DESCRIPTION
        Only the client PC gets a short list. Anything running a proxy needs the package, the UI
        and the data beside it, and "just put the files in place" means all of them on purpose.
    #>
    [CmdletBinding()]
    [OutputType([string[]])]
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Files,
          [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Targets)
    $needsEverything = @($Targets | Where-Object { @('VSCode', 'Desktop') -contains $_ }).Count -gt 0
    if ($needsEverything -or $Targets.Count -eq 0) { return $Files }
    return @($Files | Where-Object { $script:WorkstationFiles -contains (Split-Path -Leaf $_) })
}

function Copy-Release {
    <# Copies the release's tracked files into the app folder (data files are untracked, so never touched). #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)]$Fetched, [Parameter(Mandatory)][string]$AppRoot)
    if (-not $PSCmdlet.ShouldProcess($AppRoot, "Copy $($Fetched.Files.Count) files")) { return }
    foreach ($relative in $Fetched.Files) {
        $native = $relative.Replace('/', '\')
        $destination = Join-Path $AppRoot $native
        $folder = Split-Path -Parent $destination
        if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
        Copy-Item -LiteralPath (Join-Path $Fetched.Folder $native) -Destination $destination -Force
    }
    Write-Status "Copied $($Fetched.Files.Count) files to $AppRoot" -Level Detail
}

function Get-RollbackRoot {
    <# Where previous builds are kept. Beside the state, never inside the app folder: a rollback
       that lives in the thing being replaced is no rollback at all. #>
    param([Parameter(Mandatory)][string]$StateFolder)
    return (Join-Path $StateFolder 'rollback')
}

function Save-Rollback {
    <#
    .SYNOPSIS
        Copies the current build aside before it is overwritten, so a bad deploy can be undone
        (FUTURE-FEATURES #61).
    .DESCRIPTION
        Routing was pointed straight at the provider on purpose while work was landing, because
        a failing code drop would otherwise have removed the tool needed to fix it. This is the
        answer to that: a deliberate, discoverable way back that does not depend on the proxy
        running, since it is a script run at the keyboard.

        Only the tracked code files listed in the deployment record are copied. Data is not:
        aicleaner-config.json, the clients folder and the last-good copies are deliberately
        untouched by a deploy, so rolling the code back must not roll them back either - a
        client file added this afternoon is not part of the build.

        Two snapshots are kept. One is enough to undo the deploy that just broke; the second
        covers noticing one deploy late.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$AppRoot,
          [Parameter(Mandatory)][string]$StateFolder,
          [Parameter(Mandatory)]$Record)
    $rollbackRoot = Get-RollbackRoot -StateFolder $StateFolder
    $stampText = (Get-Date).ToString('yyyyMMddTHHmmss')
    $snapshot = Join-Path $rollbackRoot $stampText
    $recordedFiles = @($Record.files)
    if ($recordedFiles.Count -eq 0) {
        Write-Status 'No file list in the deployment record; nothing to keep for a rollback' -Level Warn
        return ''
    }
    if (-not $PSCmdlet.ShouldProcess($snapshot, "Keep $($recordedFiles.Count) files for rollback")) { return '' }
    $copied = 0
    foreach ($relative in $recordedFiles) {
        $native = ([string]$relative).Replace('/', '\')
        $source = Join-Path $AppRoot $native
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { continue }
        $destination = Join-Path $snapshot $native
        $folder = Split-Path -Parent $destination
        if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
        Copy-Item -LiteralPath $source -Destination $destination -Force
        $copied++
    }
    if ($copied -eq 0) {
        Write-Status 'Nothing was found to keep for a rollback' -Level Warn
        return ''
    }
    # The record goes in with it: putting the files back without the ref, commit and file list
    # would leave the record claiming a version that is no longer on disk, and the next Update
    # would compare against the wrong thing.
    Write-JsonFile -Path (Join-Path $snapshot 'rollback-record.json') -Object $Record
    $kept = @(Get-ChildItem -LiteralPath $rollbackRoot -Directory -ErrorAction SilentlyContinue |
              Sort-Object -Property Name -Descending)
    foreach ($old in @($kept | Select-Object -Skip 2)) {
        Remove-Item -LiteralPath $old.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Status "Kept the current build ($copied files) at $snapshot" -Level Detail
    return $snapshot
}

function Get-RollbackSnapshots {
    <# Newest first. Each is a folder of files plus the deployment record that went with them. #>
    param([Parameter(Mandatory)][string]$StateFolder)
    $rollbackRoot = Get-RollbackRoot -StateFolder $StateFolder
    if (-not (Test-Path -LiteralPath $rollbackRoot)) { return @() }
    return @(Get-ChildItem -LiteralPath $rollbackRoot -Directory -ErrorAction SilentlyContinue |
             Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'rollback-record.json') } |
             Sort-Object -Property Name -Descending)
}

function Invoke-Rollback {
    <#
    .SYNOPSIS
        Puts the previous build back. The way out that does not need the proxy running (#61).
    .DESCRIPTION
        Stops the proxy, copies the kept files over the app folder, restores the deployment
        record that went with them, and leaves starting it again to the caller - so a rollback
        that itself cannot start the proxy still leaves working code on disk, which is the whole
        point.

        Data is untouched, exactly as Save-Rollback did not copy it.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$AppRoot, [Parameter(Mandatory)][string]$StateFolder)
    $snapshots = @(Get-RollbackSnapshots -StateFolder $StateFolder)
    if ($snapshots.Count -eq 0) {
        Write-Status "No kept build to roll back to under $(Get-RollbackRoot -StateFolder $StateFolder)" -Level Error
        Write-Status 'A rollback point is written by the deploy that replaces the code, so the first deploy has none.' -Level Detail
        return $false
    }
    $snapshot = $snapshots[0]
    $recordPath = Join-Path $snapshot.FullName 'rollback-record.json'
    $keptRecord = Read-JsonFile -Path $recordPath
    $keptRef = if ($keptRecord.PSObject.Properties.Name -contains 'ref') { [string]$keptRecord.ref } else { 'an earlier build' }
    if (-not (Confirm-Action -Message "Put the build from $($snapshot.Name) ($keptRef) back into ${AppRoot}?" -Default 'Yes')) {
        Write-Status 'Rollback declined' -Level Detail
        return $false
    }
    if (-not $PSCmdlet.ShouldProcess($AppRoot, "Roll back to $($snapshot.Name)")) { return $false }
    $stopScript = Get-AppFile -AppRoot $AppRoot -Name 'Stop-AiCleaner.ps1'
    if (Test-Path -LiteralPath $stopScript) {
        Invoke-AppScript -Path $stopScript -Arguments @('-Port', [string](Get-ConfiguredPort -AppRoot $AppRoot))
    }
    $restored = 0
    foreach ($item in @(Get-ChildItem -LiteralPath $snapshot.FullName -File -Recurse)) {
        $relative = $item.FullName.Substring($snapshot.FullName.Length).TrimStart('\')
        if ($relative -eq 'rollback-record.json') { continue }
        $destination = Join-Path $AppRoot $relative
        $folder = Split-Path -Parent $destination
        if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
        Copy-Item -LiteralPath $item.FullName -Destination $destination -Force
        $restored++
    }
    Save-Deployment -Record $keptRecord
    Write-Status "Rolled back to $keptRef ($restored files). Start the proxy with Start-AiCleaner.ps1 -Detach" -Level Done
    return $true
}

function Remove-RecordedFiles {
    <#
    .SYNOPSIS
        Deletes the listed files (paths relative to AppRoot), Python caches, then any folder left
        empty. Nothing outside AppRoot is touched, and AppRoot itself stays.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$AppRoot, [string[]]$Files = @())
    $rootFull = [IO.Path]::GetFullPath($AppRoot).TrimEnd('\')
    $folders = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($relative in $Files) {
        $full = [IO.Path]::GetFullPath((Join-Path $rootFull $relative.Replace('/', '\')))
        if (-not (Test-UnderFolder -Path $full -Folder $rootFull) -or $full -eq $rootFull) {
            Write-Status "Skipping $relative (outside $rootFull)" -Level Warn
            continue
        }
        if ((Test-Path -LiteralPath $full -PathType Leaf) -and $PSCmdlet.ShouldProcess($full, 'Remove')) {
            Remove-Item -LiteralPath $full -Force
        }
        $parent = Split-Path -Parent $full
        while ($parent.Length -gt $rootFull.Length) { [void]$folders.Add($parent); $parent = Split-Path -Parent $parent }
    }
    foreach ($cache in @(Get-ChildItem -LiteralPath $rootFull -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue)) {
        if ($PSCmdlet.ShouldProcess($cache.FullName, 'Remove Python cache')) { Remove-Item -LiteralPath $cache.FullName -Recurse -Force }
    }
    foreach ($folder in @($folders | Sort-Object Length -Descending)) {
        if ((Test-Path -LiteralPath $folder) -and -not (Get-ChildItem -LiteralPath $folder -Force | Select-Object -First 1)) {
            if ($PSCmdlet.ShouldProcess($folder, 'Remove empty folder')) { Remove-Item -LiteralPath $folder -Force }
        }
    }
}

function Set-DataLocation {
    <#
    .SYNOPSIS
        Seeds aicleaner-config.json when absent, points clients_dir at the clients folder, creates the
        folder and, under OneDrive, pins it to this device so the proxy never meets a cloud-only file.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$AppRoot, [Parameter(Mandatory)][string]$Clients)
    $cfgPath = Join-Path $AppRoot 'aicleaner-config.json'
    if (-not (Test-Path -LiteralPath $cfgPath) -and $PSCmdlet.ShouldProcess($cfgPath, 'Seed from the example')) {
        Copy-Item -LiteralPath (Get-AppFile -AppRoot $AppRoot -Name 'aicleaner-config.example.json') -Destination $cfgPath
        Write-Status 'Created aicleaner-config.json from the example' -Level Detail
    }
    $cfg = Read-JsonFile -Path $cfgPath
    if ($cfg) {
        $current = if ($cfg.PSObject.Properties['clients_dir']) { [string]$cfg.clients_dir } else { '' }
        $change = ($current -ne $Clients) -and (-not $current -or
            (Confirm-Action -Message "aicleaner-config.json uses $current for client files; change it to ${Clients}?" -Default 'Yes'))
        if ($change) {
            Set-JsonProperty -Object $cfg -Name 'clients_dir' -Value $Clients
            Write-JsonFile -Path $cfgPath -Object $cfg
            Write-Status "Client files: $Clients" -Level Detail
        }
    }
    if (-not (Test-Path -LiteralPath $Clients) -and $PSCmdlet.ShouldProcess($Clients, 'Create the clients folder')) {
        New-Item -ItemType Directory -Path $Clients -Force | Out-Null
    }
    if ((Get-OneDriveRoot -Path $Clients) -and (Test-Path -LiteralPath $Clients) -and $PSCmdlet.ShouldProcess($Clients, 'Pin to this device')) {
        & attrib.exe +P -U $Clients | Out-Null
        & attrib.exe +P -U (Join-Path $Clients '*') /S /D | Out-Null
        Write-Status 'Pinned to this device (OneDrive: Always keep on this device)' -Level Detail
    }
}

function Copy-ContactFile {
    <# Copies the contact file beside this script into the app folder (the UI and scripts read it there). #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$AppRoot)
    $contact = Get-ContactFile
    if (-not $contact) { Write-Status 'No contact-*.json beside this script; the UI will show no contact' -Level Warn; return }
    $destination = Join-Path $AppRoot $contact.Name
    if ([IO.Path]::GetFullPath($destination) -eq $contact.FullName) { return }
    if ($PSCmdlet.ShouldProcess($destination, "Copy $($contact.Name)")) {
        Copy-Item -LiteralPath $contact.FullName -Destination $destination -Force
        Write-Status "Contact file $($contact.Name) copied" -Level Detail
    }
}

# ---------------------------------------------------------------------------
# Wiring (the existing installers, run as child processes of the same PowerShell)
# ---------------------------------------------------------------------------

function Invoke-AppScript {
    <#
    .SYNOPSIS
        Runs one of the install's scripts in a child PowerShell; -Elevated runs it as administrator.
    .DESCRIPTION
        -Elevated (feature 104) is for the one step that needs it - Claude Desktop's policy keys -
        so the rest of the install stays a normal user's: Windows shows a single UAC prompt for that
        script, in the same user's session, and the run waits for it. Its output goes to its own log
        beside it (an elevated window cannot write into this one).
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path, [string[]]$Arguments = @(), [switch]$Elevated)
    $shell = (Get-Process -Id $PID).Path
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Path) + $Arguments
    if ($WhatIfPreference) { $argList += '-WhatIf' }
    Write-Status "Running $(Split-Path -Leaf $Path) $($Arguments -join ' ')$(if ($Elevated) { ' (as administrator)' })"
    if ($Elevated) {
        $quoted = @($argList | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } })
        try {
            $child = Start-Process -FilePath $shell -ArgumentList $quoted -Verb RunAs -Wait -PassThru -ErrorAction Stop
        } catch {
            throw "$(Split-Path -Leaf $Path) needs administrator rights and the prompt was declined: $($_.Exception.Message)"
        }
        if ($child.ExitCode -ne 0) { throw "$(Split-Path -Leaf $Path) failed (exit $($child.ExitCode)); see its log beside it" }
        return
    }
    & $shell @argList
    if ($LASTEXITCODE -ne 0) { throw "$(Split-Path -Leaf $Path) failed (exit $LASTEXITCODE)" }
}

function Invoke-Wiring {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$AppRoot, [string[]]$Targets = @(), $Role = $null,
          [string]$Url = '', [string]$Ref = '')
    $needed = @{
        VSCode      = @('Install-AiCleaner-VSCode.ps1')
        Desktop     = @('Install-AiCleaner-Desktop.ps1')
        Workstation = @('Install-AiCleaner-Workstation.ps1', 'Capture-TextFromScreen.ps1')
    }
    foreach ($target in $Targets) {
        foreach ($file in $needed[$target]) {
            Restore-RepoFile -AppRoot $AppRoot -Relative $file -Url $Url -Ref $Ref | Out-Null
        }
    }
    $vscodeArgs = if ($Role) { @($Role.VSCode) } else { $VSCodeArguments }
    $desktopArgs = if ($Role) { @($Role.Desktop) } else { $DesktopArguments }
    # Asked for once here rather than in each role: what reads an image is the proxy's business,
    # and it is the same answer wherever the agent happens to be wired.
    if ($WithTesseract) {
        if ($vscodeArgs -notcontains '-WithTesseract') { $vscodeArgs += '-WithTesseract' }
        if ($desktopArgs -notcontains '-WithTesseract') { $desktopArgs += '-WithTesseract' }
    }
    $workstationArgs = if ($Role) { @($Role.Workstation) } else { $WorkstationArguments }
    if ($Targets -contains 'VSCode') {
        Invoke-AppScript -Path (Get-AppFile -AppRoot $AppRoot -Name 'Install-AiCleaner-VSCode.ps1') -Arguments (@('-RegisterStartupTask') + $vscodeArgs)
    }
    if ($Targets -contains 'Desktop') {
        Invoke-AppScript -Path (Get-AppFile -AppRoot $AppRoot -Name 'Install-AiCleaner-Desktop.ps1') -Arguments (@('-RegisterStartupTask') + $desktopArgs) `
            -Elevated:(-not (Test-PolicyWritable))
    }
    if ($Targets -contains 'Workstation') {
        Invoke-AppScript -Path (Get-AppFile -AppRoot $AppRoot -Name 'Install-AiCleaner-Workstation.ps1') -Arguments $workstationArgs
    }
}

function Get-ConfiguredPort {
    param([Parameter(Mandatory)][string]$AppRoot)
    $cfg = Read-JsonFile -Path (Join-Path $AppRoot 'aicleaner-config.json')
    if ($cfg -and $cfg.PSObject.Properties['port']) { return [int]$cfg.port }
    return 8787
}

function Update-Runtime {
    <# After new code is in place on a wired machine: requirements, validation, proxy start. #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$AppRoot)
    $venvPython = Join-Path $env:USERPROFILE '.aicleaner\venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        # Nothing to start the proxy with. Saying so beats a start that fails with a path nobody chose.
        Write-Status "The Python environment ($venvPython) is missing, so the proxy was not started" -Level Warn
        Write-Status 'Run .\Deploy-AiCleaner.ps1 again (the install path rebuilds it), then it starts as normal' -Level Detail
        return
    }
    if ($PSCmdlet.ShouldProcess('requirements.lock', 'pip install')) {
        # Exactly the wheels whose SHA-256 is in the lock, or nothing (H17): a malicious release of a
        # dependency would otherwise run inside the process holding every client mapping.
        & $venvPython -m pip install --quiet --disable-pip-version-check --require-hashes --only-binary :all: -r (Get-AppFile -AppRoot $AppRoot -Name 'requirements.lock')
        if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
        # From the app folder: the venv holds aiohttp, and aicleaner is imported from there. Run
        # from wherever this script was launched, it reports "No module named aicleaner".
        $savedEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        Push-Location -LiteralPath $AppRoot
        try { & $venvPython -m aicleaner check --config (Join-Path $AppRoot 'aicleaner-config.json') 2>&1 | ForEach-Object { Write-Status "$_" -Level Detail } }
        finally { Pop-Location; $ErrorActionPreference = $savedEap }
        if ($LASTEXITCODE -ne 0) { Write-Status 'The mapping check failed; the proxy may refuse model traffic until it is fixed' -Level Warn }
    }
    if (-not $PSCmdlet.ShouldProcess('aicleaner proxy', 'Start')) { return }
    $port = Get-ConfiguredPort -AppRoot $AppRoot
    if (Get-ScheduledTask -TaskName 'aicleaner-proxy' -ErrorAction SilentlyContinue) {
        Start-ScheduledTask -TaskName 'aicleaner-proxy'
        # Starting the task is not the proxy answering (D173: the task named a script an update had
        # just moved, and "Proxy started" was said over a proxy that never came up). If it is not
        # up shortly, start the script directly, which waits for /healthz and says so either way.
        if (Wait-ProxyHealth -Port $port -Seconds 20) { Write-Status 'Proxy started' -Level Detail; return }
        Write-Status 'The proxy did not come up from its scheduled task; starting it directly' -Level Warn
    }
    Invoke-AppScript -Path (Get-AppFile -AppRoot $AppRoot -Name 'Start-AiCleaner.ps1') -Arguments @('-Detach')
    if (Wait-ProxyHealth -Port $port -Seconds 5) { Write-Status 'Proxy started' -Level Detail }
    else { Write-Status "The proxy is not answering on port $port; see logs\proxy-stderr.log" -Level Warn }
}

function Wait-ProxyHealth {
    <# True once http://127.0.0.1:<port>/healthz answers ok, polling once a second up to Seconds. #>
    [CmdletBinding()]
    [OutputType([bool])]
    param([Parameter(Mandatory)][int]$Port, [int]$Seconds = 20)
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try {
            $answer = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 2 -ErrorAction Stop
            if ($answer.ok) { return $true }
        } catch { Write-Verbose "not yet: $($_.Exception.Message)" }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    return $false
}

# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------

function Read-Choice {
    <#
    .SYNOPSIS
        A numbered question. Returns the chosen key, or with -Multiple every chosen key.
    .DESCRIPTION
        -Multiple takes several numbers in one answer ("1 3", or "1,3"), because a machine is
        often more than one of the things on the list.
    .PARAMETER Options
        Ordered pairs of key and description, e.g. [ordered]@{ vm = 'The machine where...' }.
    #>
    [CmdletBinding()]
    [OutputType([string[]])]
    param([Parameter(Mandatory)][string]$Message, [Parameter(Mandatory)][System.Collections.IDictionary]$Options,
          [Parameter(Mandatory)][string]$Default, [switch]$Multiple)
    $keys = @($Options.Keys)
    if ($AssumeDefaults) {
        Write-Status "$Message -> $Default (default)" -Level Detail
        return $Default
    }
    Write-Host ''
    Write-Host $Message -ForegroundColor Cyan
    for ($i = 0; $i -lt $keys.Count; $i++) {
        $marker = if ($keys[$i] -eq $Default) { '*' } else { ' ' }
        Write-Host ("  {0}{1}. {2}" -f $marker, ($i + 1), $Options[$keys[$i]])
    }
    if ($Multiple) { Write-Host '   Several apply? Give every number, e.g. 1 3' -ForegroundColor DarkGray }
    $answer = Read-Host ("Number [{0}]" -f (1 + [Array]::IndexOf($keys, $Default)))
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    $chosen = New-Object System.Collections.Generic.List[string]
    foreach ($part in ($answer -split '[\s,;]+' | Where-Object { $_ })) {
        $number = 0
        $key = ''
        if ([int]::TryParse($part.Trim(), [ref]$number) -and $number -ge 1 -and $number -le $keys.Count) {
            $key = $keys[$number - 1]
        } elseif ($keys -contains $part.Trim()) {
            $key = $part.Trim()
        } else {
            throw "Not one of the choices: $part"
        }
        if (-not $chosen.Contains($key)) { $chosen.Add($key) }
    }
    if (-not $Multiple -and $chosen.Count -gt 1) { throw "Only one choice here: $answer" }
    if ($Multiple) { return $chosen.ToArray() }
    return $chosen[0]
}

function Test-ClaudeCode {
    <# Claude Code on this machine, and where to get it when it is not. #>
    [CmdletBinding()]
    [OutputType([bool])]
    param()
    $found = Get-Command claude -ErrorAction SilentlyContinue
    if ($found) {
        Write-Status "Claude Code found at $($found.Source)" -Level Detail
        return $true
    }
    Write-Status 'Claude Code is not on PATH on this machine.' -Level Warn
    Write-Status 'Install it first: winget install Anthropic.ClaudeCode (or npm install -g @anthropic-ai/claude-code)' -Level Detail
    Write-Status 'aicleaner filters what an agent sends, so there is nothing for it to do until the agent is here.' -Level Detail
    return $false
}

function Test-ClaudeDesktop {
    <# Claude Desktop on this machine, and where to get it when it is not. #>
    [CmdletBinding()]
    [OutputType([bool])]
    param()
    $places = @(
        (Join-Path $env:LOCALAPPDATA 'AnthropicClaude\claude.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Claude\Claude.exe'),
        (Join-Path $env:ProgramFiles 'Claude\Claude.exe')
    )
    foreach ($place in $places) {
        if (Test-Path -LiteralPath $place) {
            Write-Status "Claude Desktop found at $place" -Level Detail
            return $true
        }
    }
    if (Get-ChildItem -LiteralPath (Join-Path $env:LOCALAPPDATA 'AnthropicClaude') -Filter 'claude.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1) {
        return $true
    }
    # The Store build is an MSIX package: nothing under AnthropicClaude, but Windows publishes its
    # execution aliases in a Claude_<publisher hash> folder, which is readable without admin.
    $aliases = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\Claude_*') `
        -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($aliases) {
        Write-Status "Claude Desktop found as a packaged (Store) install: $($aliases.FullName)" -Level Detail
        return $true
    }
    Write-Status 'Claude Desktop is not installed on this machine.' -Level Warn
    Write-Status 'Install it first: winget install Anthropic.Claude (or claude.ai/download)' -Level Detail
    return $false
}

function Confirm-Agent {
    <# Nothing to filter without an agent: say so, and let the person carry on anyway if they mean to. #>
    [CmdletBinding()]
    [OutputType([bool])]
    param([Parameter(Mandatory)][string[]]$Agents)
    $missing = @()
    if ($Agents -contains 'code' -and -not (Test-ClaudeCode)) { $missing += 'Claude Code' }
    if ($Agents -contains 'desktop' -and -not (Test-ClaudeDesktop)) { $missing += 'Claude Desktop' }
    if ($missing.Count -eq 0) { return $true }
    return (Confirm-Action -Message "$($missing -join ' and ') not found. Set aicleaner up anyway (install the agent afterwards)?" -Default 'No')
}

function Write-NextSteps {
    <#
    .SYNOPSIS
        The work the install cannot do for you, printed at the end where it cannot be missed.
    .DESCRIPTION
        A deployed proxy with no clients seeded and no repositories swept protects nothing: it
        forwards everything untouched and looks like it is working. These are the steps that turn
        it into something that filters, in the order worth doing them, each one a command to paste.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$AppRoot,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Targets,
        [string]$ClientsPath = '',
        [string]$RemoteHostname = ''
    )
    $cli = Get-AppFile -AppRoot $AppRoot -Name 'aicleaner.cmd'
    $port = Get-ConfiguredPort -AppRoot $AppRoot
    $step = 0
    $seedStep = 0          # which number the seeding step ends up as, and 0 where there is none
    $steps = New-Object System.Collections.Generic.List[string]

    if ($Targets -contains 'Workstation') {
        $steps.Add("Take a screenshot with CTRL+ALT+T: the text lands on the clipboard, ready to paste.")
        $steps.Add("Run this script on the machine where the agent runs, if you have not already.")
    }
    if ($Targets -contains 'Desktop') {
        # The filter is a local proxy. It covers the app on this machine and nothing else.
        $steps.Add("Use Claude Desktop for client work ONLY on this machine. The same account on a" +
                   " phone, a tablet or another PC has no aicleaner in front of it, and anything" +
                   " typed there goes out with the real names in it - including claude.ai in a browser.")
    }
    if (@($Targets | Where-Object { $_ -in @('VSCode', 'Desktop') }).Count -gt 0) {
        $steps.Add("Open a NEW terminal (and restart VS Code): a running one still has the environment it started with.")
        $steps.Add("Seed the clients you work with, one per client:  $cli seed <client-id> --resources")
        $seedStep = $steps.Count
        $steps.Add("Sweep the repositories and transcripts you already have, and work through what it finds in Review:  $cli preflight <path to a repo>")
        $steps.Add("Open the UI and confirm the mappings look right:  http://127.0.0.1:$port (Start-AiCleaner.ps1 runs the proxy if it is not up)")
        # Last, and on purpose: routing is the only step here that can stop you working, and it is
        # worth nothing until the steps above have told the filter who it is protecting.
        $steps.Add("Switch Claude Code and VS Code over, once the filter knows your clients:  $AppRoot\Enable-AiCleaner.ps1  (it checks the filter is ready first; /status then shows the proxy as the Anthropic base URL)")
        $steps.Add("Teach it which identifiers name nobody, from a tenancy you can sign in to (Microsoft-owned only):  az login --tenant <tenant>  then  $AppRoot\tools\Export-PublicIdentifiers.ps1 -IncludePermissions")
        if (-not $RemoteHostname) {
            $steps.Add("Optional - reach the UI from a phone through a Cloudflare tunnel:  $AppRoot\Enable-RemoteControl.ps1 -Hostname <public name>")
        }
        $steps.Add("Claude Code keeps itself up to date; its telemetry and error reporting are off (DISABLE_TELEMETRY and DISABLE_ERROR_REPORTING in ~\.claude\settings.json; the VS Code installer's -KeepTelemetry leaves them on)")
    }
    if ($Targets.Count -eq 0) {
        $steps.Add("Nothing is wired up yet: re-run this script with -Target VSCode, Desktop, Both or Workstation when you are ready.")
    }
    if ($ClientsPath) {
        $steps.Add("Client files live in $ClientsPath; _global.json is yours alone and stays in $AppRoot.")
    }
    if ($steps.Count -eq 0) { return }

    Write-Host ''
    Write-Status 'What to do next' -Level Step
    foreach ($item in $steps) {
        $step++
        Write-Host ("    {0}. {1}" -f $step, $item)
    }
    # Only where there is a proxy to seed: a client PC has no clients and no step to skip.
    if ($seedStep -gt 0) {
        Write-Host ''
        Write-Status "Until clients are seeded the proxy forwards everything untouched, so do not skip step $seedStep." -Level Warn
    }
}

function Invoke-Activation {
    <#
    .SYNOPSIS
        The pause before routing: prepare the filter, confirm, verify, then switch over.
    .DESCRIPTION
        Everything before this point is safe to re-run and cannot stop anyone working, because
        Claude Code and VS Code are still talking to the real API. Routing is the one step that can,
        so it comes last, it is asked for, and it happens only after Enable-AiCleaner.ps1 has proved
        the filter is ready and that a request survives the whole path.

        The pause matters as much as the checks. A filter switched on before it knows a single
        client forwards everything untouched while looking like it works, and queues everything it
        sees for review - which is how one client's list reached 1,478 forms, seven in eight of
        which identified nobody. There is no clock on the pause: the proxy is running and out of the
        path, so the person setting it up can take as long as the job needs.

        A failed check is not a failed deployment. Everything else is installed and working, so this
        reports what to fix and returns rather than throwing.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$AppRoot,
          [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Targets)

    if (@($Targets | Where-Object { @('VSCode', 'Desktop') -contains $_ }).Count -eq 0) { return }
    $enable = Get-AppFile -AppRoot $AppRoot -Name 'Enable-AiCleaner.ps1'
    if (-not (Test-Path -LiteralPath $enable)) {
        Write-Status "Enable-AiCleaner.ps1 is not in $AppRoot, so nothing was switched over" -Level Warn
        return
    }
    # Nobody is at the keyboard to do the preparation, so an unattended run provisions and stops.
    # Answering this one with the default would switch routing on over an empty filter.
    if ($AssumeDefaults) {
        Write-Status 'Unattended run: provisioned but not switched over. Set a client up, then run Enable-AiCleaner.ps1.' -Level Warn
        return
    }
    $port = Get-ConfiguredPort -AppRoot $AppRoot
    $cli = Get-AppFile -AppRoot $AppRoot -Name 'aicleaner.cmd'

    Write-Host ''
    Write-Status 'Ready to switch over' -Level Step
    Write-Host '    Claude Code and VS Code are still talking to the real API, so nothing is filtered yet.'
    Write-Host '    Set the filter up first - it can only replace what it has been told about:'
    Write-Host ''
    Write-Host "      1. Open the UI:  http://127.0.0.1:$port"
    Write-Host '      2. Add a client, and give it the phrases that name them: the acronym, the full'
    Write-Host '         name, and any house spelling (ACME, ACME Corporation, ACMECorp).'
    Write-Host '      3. Add the identifiers you already know - tenant, domains, the people you deal with.'
    Write-Host "      4. Scan a repository of theirs:  $cli preflight <path to a repo>"
    Write-Host '      5. Work through Review: keep what names the client, ignore the rest.'
    Write-Host ''
    Write-Host '    Take as long as you need. The proxy is up and out of the path, so nothing here is' -ForegroundColor DarkGray
    Write-Host '    on a clock, and you can stop now and come back to it with Enable-AiCleaner.ps1.' -ForegroundColor DarkGray
    Write-Host ''

    if (-not (Confirm-Action -Message 'Ready to switch Claude Code and VS Code over?' -Default 'Yes')) {
        Write-Status "Left switched off. Run this when the filter is ready:  $enable" -Level Done
        return
    }

    # Not through Invoke-AppScript: that throws on a non-zero exit, and a check that says "no client
    # has any forms yet" is an answer, not a deployment failure.
    $shell = (Get-Process -Id $PID).Path
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $enable)
    if ($WhatIfPreference) { $argList += '-WhatIf' }
    & $shell @argList
    if ($LASTEXITCODE -eq 0) {
        Write-Status 'Filtering is on. A session already running follows a value that changes, so it takes effect without restarting anything.' -Level Done
    } else {
        Write-Status "The readiness checks did not pass, so nothing was switched over (exit $LASTEXITCODE)." -Level Warn
        Write-Status "Everything else is installed and running. Fix what it named above, then run:  $enable" -Level Detail
    }
}

function Resolve-Roles {
    <#
    .SYNOPSIS
        Settles a set of chosen scenarios into the ones that still mean something together.
    .DESCRIPTION
        A machine is often several at once - you work in VS Code here and others reach it over SSH -
        and some combinations say the same thing twice. Rather than refuse them, the ones that add
        nothing are dropped, with a line saying why, so what follows is what will actually happen.
    #>
    [CmdletBinding()]
    [OutputType([string[]])]
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Chosen)
    $roles = @($Chosen | Where-Object { $_ } | Select-Object -Unique)
    if ($roles.Count -le 1) { return $roles }
    $agents = @($roles | Where-Object { @('code-local', 'code-remote', 'code-headless', 'desktop') -contains $_ })
    if ($roles -contains 'none') {
        Write-Status "'just put the files in place' does not add to the rest; dropping it" -Level Detail
        $roles = @($roles | Where-Object { $_ -ne 'none' })
    }
    if ($agents.Count -gt 0 -and $roles -contains 'workstation') {
        # The agent installs bring the screenshot tooling with them; workstation is for a PC with
        # no agent at all, and running it as well would only turn the proxy wiring back off.
        Write-Status 'An agent runs here, so the screenshot tooling comes with it; dropping the client-PC role' -Level Detail
        $roles = @($roles | Where-Object { $_ -ne 'workstation' })
    }
    if ($roles -contains 'code-headless' -and @($roles | Where-Object { @('code-local', 'code-remote') -contains $_ }).Count -gt 0) {
        Write-Status 'VS Code is used here after all, so the headless answer adds nothing; dropping it' -Level Detail
        $roles = @($roles | Where-Object { $_ -ne 'code-headless' })
    }
    return $roles
}

function Test-VSCodeServer {
    <#
    .SYNOPSIS
        Where VS Code Server keeps its data on this machine, or '' when it is not here.
    .DESCRIPTION
        This is the whole difference between "reached by VS Code Remote-SSH" and "driven from a
        plain SSH terminal": with a server here, the Claude Code extension runs here too and reads
        the server's own settings rather than a local VS Code's. Asking someone to know that is
        asking them to know our implementation - the folder either exists or it does not.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param()
    foreach ($folder in '.vscode-server', '.vscode-server-insiders') {
        $path = Join-Path $env:USERPROFILE $folder
        if (Test-Path -LiteralPath $path) { return $path }
    }
    return ''
}

function Show-ViewerHelp {
    <#
    .SYNOPSIS
        What a device that only views another machine needs, which is almost nothing.
    .DESCRIPTION
        Two ways of looking at the same situation - a VS Code window over SSH, or the session page
        in a browser - and the same answer to both: the proxy, the mapping and the session all stay
        on the machine running the agent. This one either takes screenshots or it does not.
    #>
    [CmdletBinding()]
    param()
    Write-Status 'The agent runs somewhere else, so almost nothing belongs here.' -Level Done
    Write-Status 'Reaching a session through a browser needs nothing installed on this device: the' -Level Detail
    Write-Status 'tunnel is set up on the machine the agent runs on, and you open the address it' -Level Detail
    Write-Status 'prints. Cloudflare terminates TLS there, so it can see what crosses it.' -Level Detail
    Write-Status 'The one thing worth having here is the screenshot tooling, so a console picture' -Level Detail
    Write-Status 'becomes text the proxy can filter rather than an image it cannot read.' -Level Detail
}

function Test-InferenceCredential {
    <#
    .SYNOPSIS
        Is there already something for the proxy to authenticate to a model provider with?
    .DESCRIPTION
        Claude Desktop cannot be filtered on a subscription. It reaches a model provider either
        directly, or - in third-party mode, which is what lets a local proxy sit in front - through
        a gateway whose credential is billed to whoever owns it. So this looks for a key we have
        already been given, or a gateway somebody has already configured.
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param()
    $keyFile = Join-Path $env:USERPROFILE '.aicleaner\secrets\anthropic.key.dpapi'
    if (Test-Path -LiteralPath $keyFile) {
        Write-Status 'An API key is already stored for the proxy to use' -Level Detail
        return $true
    }
    if ($env:AICLEANER_ANTHROPIC_API_KEY) {
        Write-Status 'AICLEANER_ANTHROPIC_API_KEY is set in this environment' -Level Detail
        return $true
    }
    $policy = 'HKCU:\SOFTWARE\Policies\Claude'
    if (Test-Path -LiteralPath $policy) {
        $values = Get-ItemProperty -LiteralPath $policy -ErrorAction SilentlyContinue
        if ($values -and $values.PSObject.Properties['inferenceGatewayApiKey'] -and $values.inferenceGatewayApiKey) {
            Write-Status 'Claude Desktop already has an inference gateway configured on this machine' -Level Detail
            return $true
        }
    }
    return $false
}

function Confirm-DesktopCost {
    <#
    .SYNOPSIS
        Explains what filtering Claude Desktop needs, and lets someone drop it and carry on.
    .DESCRIPTION
        Claude Code keeps its subscription: pointing it at a proxy with no gateway credential
        leaves the claude.ai login as the active credential, and Anthropic documents that. Claude
        Desktop has no equivalent - the only way a local proxy gets in front of it is third-party
        inference mode, whose credential is billed per token to whoever owns it. Finding that out
        after an install, from a bill, would be a poor way to learn it.
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param()
    if (Test-InferenceCredential) { return $true }
    Write-Host ''
    Write-Status 'Claude Desktop needs a paid inference credential; a Claude subscription will not do it.' -Level Warn
    Write-Status 'Claude Code keeps using your subscription through the proxy. Claude Desktop cannot:' -Level Detail
    Write-Status 'the only way a local filter gets in front of it is its third-party inference mode,' -Level Detail
    Write-Status 'and that mode bills per token to whichever credential the gateway forwards:' -Level Detail
    Write-Status '  - an Anthropic API key from the Console:  https://console.anthropic.com/' -Level Detail
    Write-Status '  - or a cloud provider you already pay:    Microsoft Foundry, Amazon Bedrock,' -Level Detail
    Write-Status '    Google Cloud (Claude models through an account you already have)' -Level Detail
    Write-Status 'Anthropic states it plainly: a paid Claude subscription "does not include access to' -Level Detail
    Write-Status 'the Claude API or Console".' -Level Detail
    Write-Host ''
    Write-Status 'Nothing has been changed on this machine yet.' -Level Detail
    if (Confirm-Action -Message 'Set Claude Desktop up anyway (you will be asked for the key)?' -Default 'No') {
        return $true
    }
    Write-Status 'Leaving Claude Desktop out. Use it for work with no client data in it, or come back' -Level Done
    Write-Status 'once you have a key; running this again adds the role to what is already here.' -Level Detail
    return $false
}

function Get-MachineRole {
    <#
    .SYNOPSIS
        What is this machine? Everything else (which installer runs, which parts to skip) follows
        from the answer, so running this script with no parameters asks for what it needs.
    #>
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param()
    if ($Target) {
        # Given explicitly (scripted runs): keep the arguments the caller passed.
        $targets = switch ($Target) {
            'None' { @() }
            'Both' { @('VSCode', 'Desktop') }
            default { @($Target) }
        }
        return [pscustomobject]@{ Targets = $targets; VSCode = $VSCodeArguments; Desktop = $DesktopArguments
                                  Workstation = $WorkstationArguments; Advice = ''
                                  RemoteHostname = $RemoteHostname; NothingToDo = $false }
    }
    # One question for the whole situation: which agent, and where it runs. The old pair (which
    # agent, then "do you use the VS Code window here") described one thing in two answers, and the
    # second was easy to get wrong. Run it again to add another role to this machine.
    # The two SSH answers differ by one fact the machine already knows, so let it say which it is.
    $vscodeServer = Test-VSCodeServer
    $remoteNote = if ($vscodeServer) { " - VS Code Server IS here ($vscodeServer)" } else { '' }
    $headlessNote = if ($vscodeServer) { '' } else { ' - no VS Code Server on this machine' }
    $roles = @(Read-Choice -Multiple -Message 'Which AI agent is this for, and where does it run?' -Default 'code-local' -Options ([ordered]@{
        'code-local'    = 'Claude Code in VS Code, on this machine (you work at this PC)'
        'desktop'       = 'Claude Desktop, on this machine'
        'code-remote'   = "Claude Code here, reached by VS Code Remote-SSH from another PC$remoteNote"
        'code-headless' = "Claude Code here, driven from a plain SSH terminal$headlessNote"
        'workstation'   = 'None here: this device views or drives an agent running somewhere else'
        'none'          = 'None yet: just put the files in place'
    }))
    $roles = @(Resolve-Roles -Chosen $roles)
    $agentHere = @('code-local', 'code-remote', 'code-headless', 'desktop')
    $nothingToDo = $false
    if ($roles -contains 'workstation' -and @($roles | Where-Object { $_ -ne 'workstation' }).Count -eq 0) {
        # A viewing device is a viewing device whether you look through VS Code over SSH or a
        # browser: neither runs the agent, and both want the same one thing if anything.
        Show-ViewerHelp
        # Screenshots are that one thing, since pasting a picture of a console sends the client
        # names the proxy never sees.
        if (-not (Confirm-Action -Message 'Do you take screenshots on this device (so they can be pasted as text)?' -Default 'Yes')) {
            $roles = @()
            $nothingToDo = $true
        }
    }
    $wanted = @($roles | Where-Object { $agentHere -contains $_ })
    if ($wanted.Count -gt 0) {
        $agents = @()
        if (@($wanted | Where-Object { $_ -like 'code-*' }).Count -gt 0) { $agents += 'code' }
        if ($wanted -contains 'desktop') { $agents += 'desktop' }
        if (-not (Confirm-Agent -Agents $agents)) { throw 'Stopped: install the agent first, then run this again' }
    }
    # Said before anything is installed, because the answer may be "not on this machine, then".
    if ($roles -contains 'desktop' -and -not (Confirm-DesktopCost)) {
        $roles = @($roles | Where-Object { $_ -ne 'desktop' })
        if ($roles.Count -eq 0) { $nothingToDo = $true }
    }
    $vscodeArgs = @()
    $desktopArgs = @()
    $advice = ''
    # Which VS Code settings belong to this machine follows from the situation. With more than one
    # chosen, a file is skipped only when every chosen scenario skips it: a machine that is both
    # worked at locally and reached over SSH wants both.
    $codeRoles = @($roles | Where-Object { $_ -like 'code-*' })
    if ($codeRoles.Count -gt 0) {
        $skipsLocal = @($codeRoles | Where-Object { @('code-remote', 'code-headless') -contains $_ })
        $skipsServer = @($codeRoles | Where-Object { @('code-local', 'code-headless') -contains $_ })
        if ($skipsLocal.Count -eq $codeRoles.Count) { $vscodeArgs += '-SkipVSCode' }
        if ($skipsServer.Count -eq $codeRoles.Count) { $vscodeArgs += '-SkipVSCodeServer' }
    }
    $screenHere = $roles -contains 'code-local' -or $roles -contains 'desktop'
    if ($screenHere) {
        if (-not (Confirm-Action -Message 'Do you take screenshots on this machine?' -Default 'Yes')) {
            $vscodeArgs += @('-SkipPowerToys', '-SkipSnippingTool')
            $advice = 'Screenshots are taken elsewhere: run this script on that PC and answer "None here".'
        }
    } elseif ($codeRoles.Count -gt 0) {
        # Nobody looks at a screen on a machine reached only over SSH.
        $vscodeArgs += @('-SkipPowerToys', '-SkipSnippingTool')
        $advice = 'Screenshots are taken on the PC you drive this from: run this there and answer "None here".'
    }
    if ($roles -contains 'desktop') {
        if (Confirm-Action -Message 'Register the nightly backup of the Claude Desktop profile?' -Default 'Yes') {
            $desktopArgs += '-RegisterBackupTask'
        }
    }
    $remoteHostname = $RemoteHostname
    # Offered only where there is a session to drive. What the tunnel reaches is aicleaner's own
    # pages and the session page, which runs the Claude Code CLI; it does not reach Claude Desktop,
    # because that is a local window rather than something served. On a machine with only the
    # desktop app there is nothing on the other end worth opening a tunnel for, so it is not asked.
    # -RemoteHostname still works for anyone who wants the pages reachable anyway.
    if (@($roles | Where-Object { $_ -like 'code-*' }).Count -gt 0 -and -not $remoteHostname) {
        Write-Status 'Remote control, through a Cloudflare tunnel:' -Level Detail
        Write-Status '  reach aicleaner''s pages on THIS device (mappings, review, evidence) from a phone' -Level Detail
        Write-Status '  or another PC, and drive a Claude Code session running here.' -Level Detail
        Write-Status 'You need a domain you own, with its DNS run by Cloudflare: the address is a new' -Level Detail
        Write-Status 'name under it (claude-thismachine.yourdomain.com), and only the domain owner can' -Level Detail
        Write-Status 'create that. Without one, skip this and set it up later.' -Level Detail
        Write-Status 'Cloudflare terminates TLS at its edge, so it could see what crosses it in clear.' -Level Detail
        # The name is not asked for here. Enable-RemoteControl.ps1 asks it once it has a token and
        # can list the domains Cloudflare runs for you, so the question comes with an answer in it.
        if (Confirm-Action -Message 'Set that up now (it will ask for the details)?' -Default 'No') {
            $remoteHostname = 'ask'
        }
    }
    $targets = New-Object System.Collections.Generic.List[string]
    if ($codeRoles.Count -gt 0) { $targets.Add('VSCode') }
    if ($roles -contains 'desktop') { $targets.Add('Desktop') }
    if ($roles -contains 'workstation') { $targets.Add('Workstation') }
    $targets = @($targets)
    if ($nothingToDo) { $targets = @() }
    return [pscustomobject]@{ Targets = $targets; VSCode = $vscodeArgs; Desktop = $desktopArgs
                              Workstation = $WorkstationArguments; Advice = $advice
                              RemoteHostname = $remoteHostname; NothingToDo = $nothingToDo }
}

function Resolve-Folders {
    <#
    .SYNOPSIS
        Where the code goes and where the client files go.
    .DESCRIPTION
        The code folder is not asked about. Part of the install is pinned to %USERPROFILE% either
        way - the venv lives at %USERPROFILE%\.aicleaner\venv so it never syncs through OneDrive
        and is shared by however many installs - so offering a free choice for the rest of it
        invites an answer that splits one install across two places for no gain. -InstallPath is
        still honoured for the machine that genuinely needs the code somewhere else.

        The clients folder is a real choice and is still asked about: it is shared with a team
        through OneDrive, which is exactly what the venv must not be.
    #>
    [CmdletBinding()]
    param([switch]$SkipClients)
    $appRoot = $InstallPath
    if (-not $appRoot) { $appRoot = Join-Path $env:USERPROFILE '.aicleaner\app' }
    $appRoot = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($appRoot))
    Write-Status "Install folder: $appRoot" -Level Detail
    if (Get-OneDriveRoot -Path $appRoot) { Write-Status "The install folder is inside OneDrive; code should not sync between machines" -Level Warn }
    if ($SkipClients) { return [pscustomobject]@{ App = $appRoot; Clients = "" } }
    # A folder that is already deployed keeps its client files where they are. Asking again would
    # collect an answer nothing uses: moving shared client files is not something a re-run does.
    $known = @(Get-DeploymentRecords | Where-Object { Test-SameFolder -Left ([string]$_.install_path) -Right $appRoot })
    if ($known.Count -gt 0 -and [string]$known[0].clients_path) {
        $recorded = [string]$known[0].clients_path
        if ($ClientsPath -and -not (Test-SameFolder -Left ([IO.Path]::GetFullPath($ClientsPath)) -Right $recorded)) {
            Write-Status "This deployment keeps its client files at $recorded; -ClientsPath $ClientsPath was not applied" -Level Warn
            Write-Status 'To move them, uninstall (the data stays) and install again with the new folder.' -Level Detail
        } else {
            Write-Status "Client files stay at $recorded (already deployed here)" -Level Detail
        }
        return [pscustomobject]@{ App = $appRoot; Clients = $recorded }
    }
    $clients = $ClientsPath
    if (-not $clients) {
        $oneDrive = Get-OneDriveRoot
        $suggested = if ($oneDrive) { Join-Path $oneDrive 'aicleaner\clients' } else { Join-Path $appRoot 'clients' }
        $clients = Read-Value -Message 'Folder for the client files (share it through OneDrive to work as a team)' -Default $suggested -AsPath
    }
    $clients = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($clients))
    # Each client gets its own folder under this one, and that folder is what is shared with the
    # engagement's people in OneDrive (DECISIONS 51). Outside OneDrive nothing syncs: fine for one
    # person or a test box, so it is warned about and confirmed, never refused. No re-asks a folder.
    while (-not (Get-OneDriveRoot -Path $clients)) {
        Write-Status "$clients is not inside OneDrive: client folders there will not sync to colleagues" -Level Warn
        if (Confirm-Action -Message 'Use it anyway?' -Default 'Yes') { break }
        $oneDrive = Get-OneDriveRoot
        $suggested = if ($oneDrive) { Join-Path $oneDrive 'aicleaner\clients' } else { $clients }
        $clients = Read-Value -Message 'Folder for the client files' -Default $suggested -AsPath
        $clients = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($clients))
    }
    return [pscustomobject]@{ App = $appRoot; Clients = $clients }
}

# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

function Invoke-GetSource {
    <#
    .SYNOPSIS
        Clones the repository the contact file names, or brings an existing clone up to date.
    .DESCRIPTION
        The install does not need a clone - it fetches what it needs by itself - but reading the
        code, or installing with -FromWorkingCopy, does. Rather than have someone hand-write a git
        command around a URL they have to go and look up, this reads repo_url from the contact file
        beside this script, clones it, checks out -Ref when one is given, and copies the contact
        file in: that file is gitignored, so a fresh clone has no copy, and the working-copy
        install wants one.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Assert-Git
    $url = Resolve-RepoUrl -Explicit $RepoUrl
    $destination = $SourcePath
    if (-not $destination) {
        $leaf = (($url.TrimEnd('/') -split '/')[-1]) -replace '\.git$', ''
        if (-not $leaf) { $leaf = 'aicleaner' }
        $destination = Join-Path $PWD.Path $leaf
    }
    $destination = [IO.Path]::GetFullPath($destination)

    if (Test-Path -LiteralPath (Join-Path $destination '.git')) {
        $origin = ''
        try { $origin = (@(Invoke-Git -Arguments @('-C', $destination, 'remote', 'get-url', 'origin')) | Select-Object -First 1) } catch { $origin = '' }
        if ($origin) { $origin = ([string]$origin).Trim() }
        if ($origin -and $origin -ne $url) {
            throw "$destination is a clone of $origin, not $url; pass -SourcePath to use another folder"
        }
        Write-Status "Bringing the clone at $destination up to date"
        if ($PSCmdlet.ShouldProcess($destination, 'git fetch and fast-forward')) {
            Invoke-Git -Arguments @('-C', $destination, 'fetch', '--tags', '--prune', '--quiet', 'origin') | Out-Null
            $dirty = @(Invoke-Git -Arguments @('-C', $destination, 'status', '--porcelain'))
            if ($dirty.Count -gt 0) {
                Write-Status "$($dirty.Count) local change(s) here: the new commits were fetched, the working copy left alone" -Level Warn
            } else {
                # Fast-forward only: a clone someone has committed in is theirs to sort out.
                try { Invoke-Git -Arguments @('-C', $destination, 'merge', '--ff-only', '--quiet', '@{u}') | Out-Null }
                catch { Write-Status "Fetched, but could not fast-forward ($($_.Exception.Message))" -Level Warn }
            }
        }
    } else {
        if ((Test-Path -LiteralPath $destination) -and @(Get-ChildItem -LiteralPath $destination -Force -ErrorAction SilentlyContinue).Count -gt 0) {
            throw "$destination already exists and is not a clone; pass -SourcePath to use another folder"
        }
        Write-Status "Cloning $url into $destination"
        if ($PSCmdlet.ShouldProcess($destination, "git clone $url")) {
            Invoke-Git -Arguments @('clone', '--quiet', $url, $destination) | Out-Null
        }
    }
    if ($Ref -and $PSCmdlet.ShouldProcess($destination, "git checkout $Ref")) {
        Invoke-Git -Arguments @('-C', $destination, 'checkout', '--quiet', $Ref) | Out-Null
        Write-Status "Checked out $Ref" -Level Detail
    }
    if (Test-Path -LiteralPath $destination) {
        if (-not (Get-ContactFile -Folder $destination)) { Copy-ContactFile -AppRoot $destination }
        $version = ''
        try { $version = (@(Invoke-Git -Arguments @('-C', $destination, 'describe', '--tags', '--always')) | Select-Object -First 1) } catch { $version = '' }
        Write-Status ("Source ready at {0}{1}" -f $destination, $(if ($version) { " ($version)" } else { '' })) -Level Done
    }
    Write-Host ''
    Write-Status 'What to do next' -Level Step
    Write-Host ("    1. Install this code:  cd `"{0}`"  then  .\Deploy-AiCleaner.ps1 -FromWorkingCopy" -f $destination)
    Write-Host ("    2. Or install the newest release instead, from where you are:  .\Deploy-AiCleaner.ps1")
    Write-Host ''
}

function Get-DeploymentRecords {
    <#
    .SYNOPSIS
        Every deployment this script has made on this machine, oldest first.
    .DESCRIPTION
        One machine can hold several: the PC that shows the VS Code window, the one running Claude
        Desktop, a second copy somewhere else for a different job. The file used to hold a single
        record, and one of those is still read as a list of one.
    #>
    [CmdletBinding()]
    [OutputType([object[]])]
    param()
    $document = Read-JsonFile -Path $script:RecordPath
    if (-not $document) { return @() }
    if ($document.PSObject.Properties['deployments']) { return @($document.deployments | Where-Object { $_ }) }
    if ($document.PSObject.Properties['install_path']) { return @($document) }   # the old single record
    return @()
}

function Save-DeploymentRecords {
    <# Writes the list back, newest shape. #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Records)
    $document = [pscustomobject][ordered]@{
        schema      = 2
        updated_at  = (Get-Date).ToString('s')
        deployments = @($Records)
    }
    Write-JsonFile -Path $script:RecordPath -Object $document
}

function Save-Deployment {
    <# Adds or replaces the record for one install folder, leaving the others alone. #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)]$Record)
    $path = [string]$Record.install_path
    $others = @(Get-DeploymentRecords | Where-Object { -not (Test-SameFolder -Left ([string]$_.install_path) -Right $path) })
    Save-DeploymentRecords -Records @($others + @($Record))
}

function Remove-DeploymentRecord {
    <# Drops one deployment's record; the file goes when the last one does. #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$AppRoot)
    $left = @(Get-DeploymentRecords | Where-Object { -not (Test-SameFolder -Left ([string]$_.install_path) -Right $AppRoot) })
    if ($left.Count -eq 0) {
        if (Test-Path -LiteralPath $script:RecordPath) {
            if ($PSCmdlet.ShouldProcess($script:RecordPath, 'Remove the deployment record')) {
                Remove-Item -LiteralPath $script:RecordPath -Force
            }
        }
        return
    }
    Save-DeploymentRecords -Records $left
    Write-Status ("{0} other deployment(s) left on this machine" -f $left.Count) -Level Detail
}

function Test-SameFolder {
    <# Two paths naming the same folder, whatever the trailing slash or case. #>
    [OutputType([bool])]
    param([string]$Left, [string]$Right)
    if (-not $Left -or -not $Right) { return $false }
    try {
        $a = [IO.Path]::GetFullPath($Left).TrimEnd('\')
        $b = [IO.Path]::GetFullPath($Right).TrimEnd('\')
    } catch { return $false }
    return [string]::Equals($a, $b, [StringComparison]::OrdinalIgnoreCase)
}

function Select-Deployment {
    <#
    .SYNOPSIS
        Which deployment to act on: the one named by -InstallPath, the only one, or the one chosen.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Verb)
    $records = @(Get-DeploymentRecords)
    if ($records.Count -eq 0) { return $null }
    if ($InstallPath) {
        $match = @($records | Where-Object { Test-SameFolder -Left ([string]$_.install_path) -Right $InstallPath })
        if ($match.Count -eq 0) {
            throw ("No deployment at {0}. This machine has: {1}" -f $InstallPath, (@($records | ForEach-Object { $_.install_path }) -join ', '))
        }
        return $match[0]
    }
    if ($records.Count -eq 1) { return $records[0] }
    $options = [ordered]@{}
    foreach ($record in $records) {
        $options[[string]$record.install_path] = ("{0} - {1}" -f ([string]$record.ref), ((@($record.targets) -join ' + ')))
    }
    Write-Status "This machine has $($records.Count) deployments; -InstallPath picks one without asking." -Level Detail
    $chosen = Read-Choice -Message "Which one to $Verb" -Options $options -Default ([string]$records[0].install_path)
    return @($records | Where-Object { Test-SameFolder -Left ([string]$_.install_path) -Right $chosen })[0]
}

function Assert-TargetsFree {
    <#
    .SYNOPSIS
        Stops a second deployment from wiring a target another one already owns.
    .DESCRIPTION
        The wiring is machine-wide - environment variables, policy keys, the PowerToys remap,
        scheduled tasks - so two deployments wiring the same target would fight, and the loser
        would look installed while doing nothing. Different targets in different folders are fine.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Targets, [string]$AppRoot = '')
    foreach ($record in @(Get-DeploymentRecords)) {
        if ($AppRoot -and (Test-SameFolder -Left ([string]$record.install_path) -Right $AppRoot)) { continue }
        $clash = @(@($record.targets) | Where-Object { $Targets -contains $_ })
        if ($clash.Count -gt 0) {
            throw ("{0} is already wired from {1}. Uninstall that one first, or install this for a different role." -f ($clash -join ' and '), [string]$record.install_path)
        }
    }
}

function Invoke-RemoteSetup {
    <#
    .SYNOPSIS
        Runs Enable-RemoteControl.ps1 when this run was asked to, from a first install or a later one.
    .DESCRIPTION
        "Set that up now?" is asked before the script knows whether this folder is already deployed,
        so a yes has to be honoured on every path, not only the first install's.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$AppRoot, [Parameter(Mandatory)]$Role,
          [string]$Url = '', [string]$Ref = '')
    if (-not $Role.RemoteHostname) { return }
    if (Restore-RepoFile -AppRoot $AppRoot -Relative 'Enable-RemoteControl.ps1' -Url $Url -Ref $Ref) {
        # 'ask' means nobody has named it yet: the tunnel script asks, with the zones in front of it.
        $remoteArgs = if ($Role.RemoteHostname -eq 'ask') { @() } else { @('-Hostname', $Role.RemoteHostname) }
        # Asked here, beside the hostname, rather than left to the tunnel script's default (D44):
        # that default is the one-time PIN, the weakest of the three and the only one that
        # authenticates an address rather than an account. The PIN is still what answering
        # nothing gives, so an unattended run behaves as it always did - but now it is said.
        $identity = [string]$script:Identity
        if (-not $identity) {
            $identity = Read-Choice -Message 'How should the remote page be signed in to?' -Default 'OneTimePin' -Options ([ordered]@{
                OneTimePin = 'Email me a one-time PIN (anyone who reads that mailbox is in)'
                Google     = 'A Google account (Google sign-in and its MFA in front)'
                Microsoft  = 'A Microsoft account or work ID (Microsoft sign-in and its MFA in front)' })
        }
        Write-Status "Remote sign-in: $identity" -Level Detail
        $remoteArgs += @('-Identity', $identity)
        Invoke-AppScript -Path (Get-AppFile -AppRoot $AppRoot -Name 'Enable-RemoteControl.ps1') -Arguments $remoteArgs
    }
}

function Add-Target {
    <#
    .SYNOPSIS
        Wires an already-deployed machine for another target, and records it.
    .DESCRIPTION
        A machine is often two things in turn: the PC that shows the VS Code window over SSH, and
        later the one Claude Desktop runs on. The code is already here, so this only runs the
        wiring for what is new and merges the targets in the record. Nothing is fetched, nothing is
        overwritten, and the data stays where it is.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)]$Record,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Wanted,
        [Parameter(Mandatory)]$Role
    )
    $appRoot = [string]$Record.install_path
    if (-not (Test-Path -LiteralPath $appRoot)) {
        throw "The record says aicleaner is at $appRoot, which is not there. Use -Action Uninstall, then install again."
    }
    $already = @($Record.targets)
    $new = @($Wanted | Where-Object { $already -notcontains $_ })
    Assert-TargetsFree -Targets $new -AppRoot $appRoot
    # Before any wiring runs: re-running an installer that is itself out of date repairs nothing,
    # and reports its own old bugs as problems with this machine.
    # Called as a statement, never captured: see Update-Deployment for why. The wiring below runs
    # the installers, which own the runtime, so the update only brings the code up to date.
    Update-Deployment -Record $Record -SkipRuntime -StateFolder $script:StateFolder
    if ($script:UpdateOutcome -eq 'declined') {
        Write-Status 'Keeping the installed code; the checks below run that older version.' -Level Warn
    }
    if ($new.Count -eq 0) {
        # The record says this is wired, which is a claim about a file. Whether the machine still
        # matches it is another question: shortcuts get deleted, a remap gets reset, a policy value
        # gets cleared. The installers are idempotent and report what they change, so re-running
        # them is the check - and the repair.
        Write-Status ("The record says this machine is wired for {0}, from {1}" -f (($already -join ', ')), $appRoot) -Level Done
        if (-not (Confirm-Action -Message 'Check it is all still in place, and put back anything missing?' -Default 'Yes')) {
            Write-Status 'Left as it is. -Action Update takes newer code; -Action Uninstall removes it.' -Level Detail
            return
        }
        # The answers from this run decide it: "do you use the VS Code window on this machine"
        # gives different wiring for a VS Code server and the PC in front of you.
        $checkRole = [pscustomobject]@{ Targets = $already; VSCode = $Role.VSCode; Desktop = $Role.Desktop
                                        Workstation = $Role.Workstation; Advice = ''
                                        RemoteHostname = $Role.RemoteHostname }
        Invoke-Wiring -AppRoot $appRoot -Targets $already -Role $checkRole -Url ([string]$Record.repo_url) -Ref ([string]$Record.ref)
        Write-Status ("Checked: {0} wiring is in place" -f ($already -join ' + ')) -Level Done
        Write-Status 'Anything that had drifted was put back; what was already right was left alone.' -Level Detail
        Invoke-RemoteSetup -AppRoot $appRoot -Role $Role -Url ([string]$Record.repo_url) -Ref ([string]$Record.ref)
        return
    }
    Write-Status ("Already deployed here for {0}; adding {1}" -f (($already -join ', ')), (($new -join ', ')))
    if (-not (Confirm-Action -Message ("Wire this machine for {0} as well?" -f ($new -join ', ')) -Default 'Yes')) {
        Write-Status 'Nothing changed' -Level Done
        return
    }
    # A client PC holds four files. Adding a role that runs the proxy means fetching the rest.
    $needsEverything = @($new | Where-Object { @('VSCode', 'Desktop') -contains $_ }).Count -gt 0
    if ($needsEverything -and -not (Test-Path -LiteralPath (Join-Path $appRoot 'aicleaner\__init__.py'))) {
        Write-Status 'This folder holds the client-PC files only; fetching the rest' -Level Detail
        $sourceUrl = [string]$Record.repo_url
        $sourceRef = [string]$Record.ref
        $fetched = if ($sourceRef -eq 'working-copy') {
            Get-WorkingCopy -Folder $sourceUrl
        } else {
            $script:PinnedSigningKey = if ($Record.PSObject.Properties['signing_key']) { [string]$Record.signing_key } else { '' }
            Get-Release -Url $sourceUrl -Release ([pscustomobject]@{ Ref = $sourceRef; Commit = [string]$Record.commit }) -AlreadyInstalled
        }
        Copy-Release -Fetched $fetched -AppRoot $appRoot
        Remove-Stage
        $Record | Add-Member -NotePropertyName 'files' -NotePropertyValue @($fetched.Files) -Force
    }
    if ($new -contains 'Desktop' -and -not (Test-PolicyWritable)) {
        Write-Status 'Claude Desktop is set up through a Windows policy, which needs administrator rights: Windows will ask once, for that step only.' -Level Detail
    }
    # A proxy target needs somewhere for the client files; a workstation-only deployment has none.
    $clients = [string]$Record.clients_path
    if (-not $clients -and @($new | Where-Object { @('VSCode', 'Desktop') -contains $_ }).Count -gt 0) {
        $clients = (Resolve-Folders).Clients
        Set-DataLocation -AppRoot $appRoot -ClientsPath $clients
    }
    $addRole = [pscustomobject]@{ Targets = $new; VSCode = $Role.VSCode; Desktop = $Role.Desktop
                                  Workstation = $Role.Workstation; Advice = ''; RemoteHostname = $Role.RemoteHostname }
    Invoke-Wiring -AppRoot $appRoot -Targets $new -Role $addRole -Url ([string]$Record.repo_url) -Ref ([string]$Record.ref)
    $merged = @($already + $new | Select-Object -Unique)
    Save-Deployment -Record ([pscustomobject][ordered]@{
        schema = 2; repo_url = [string]$Record.repo_url; ref = [string]$Record.ref
        commit = [string]$Record.commit; version = [string]$Record.version
        install_path = $appRoot; clients_path = $clients; targets = $merged
        files = @($Record.files); installed_at = [string]$Record.installed_at
        updated_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    })
    Write-Status ("This machine is now wired for {0}" -f ($merged -join ', ')) -Level Done
    Invoke-RemoteSetup -AppRoot $appRoot -Role $Role -Url ([string]$Record.repo_url) -Ref ([string]$Record.ref)
    Write-NextSteps -AppRoot $appRoot -Targets $merged -ClientsPath $clients -RemoteHostname ([string]$Role.RemoteHostname)
}

function Invoke-Install {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    # Six framed steps a person can follow (feature 104); the questions inside them are unchanged.
    Write-StepBanner -Number 1 -Of 6 -Title 'What this device is for' -Unattended:$script:Unattended
    $role = Get-MachineRole
    $targets = @($role.Targets)
    if ($role.PSObject.Properties['NothingToDo'] -and $role.NothingToDo) {
        Write-Status 'Nothing to install on this device; no files were copied.' -Level Done
        return
    }
    # The client PC holds no client files: it runs no proxy and reads no mapping.
    $workstationOnly = $targets.Count -eq 1 -and $targets[0] -eq 'Workstation'
    Write-StepBanner -Number 2 -Of 6 -Title 'Where to put it' -Unattended:$script:Unattended
    $folders = Resolve-Folders -SkipClients:$workstationOnly
    $existing = @(Get-DeploymentRecords | Where-Object { Test-SameFolder -Left ([string]$_.install_path) -Right $folders.App })
    if ($existing.Count -gt 0) {
        # The code is already in this folder: this run is about wiring another role. Said, because
        # the heading above reads "Install" and what follows is not one (D52).
        Write-Status "Already deployed to $($folders.App): checking and adding wiring, not installing again. -Action Update changes the code." -Level Detail
        Add-Target -Record $existing[0] -Wanted $targets -Role $role
        return
    }
    # A second deployment elsewhere is fine; wiring the same role twice is not.
    Assert-TargetsFree -Targets $targets -AppRoot $folders.App
    # Claude Desktop's policy keys need administrator rights; that one step asks for them itself
    # (a UAC prompt for the Desktop installer alone) when it is reached (feature 104).
    if ($targets -contains 'Desktop' -and -not (Test-PolicyWritable)) {
        Write-Status 'Claude Desktop is set up through a Windows policy, which needs administrator rights: Windows will ask once, for that step only.' -Level Detail
    }
    $url = ''
    $release = $null
    Write-StepBanner -Number 3 -Of 6 -Title 'Getting aicleaner' -Unattended:$script:Unattended
    if ($FromWorkingCopy) {
        $url = $script:ScriptFolder
        $release = [pscustomobject]@{ Ref = 'working-copy'; Commit = '' }
        Write-Status "Deploying this working copy ($url)" -Level Detail
    } else {
        Assert-Git
        $url = Resolve-RepoUrl -Explicit $RepoUrl
        if (-not (Confirm-Action -Message "Deploy aicleaner from ${url}?" -Default 'Yes')) { throw 'Stopped at your request' }
        # Before anything relies on it (64): can it be read, and if not, how should it be.
        Resolve-RepoAccess -Url $url -TokenPath (Join-Path $script:StateFolder 'secrets\github-token.dpapi')
        $release = Resolve-Release -Url $url -Wanted $Ref
        Write-Status "Release $($release.Ref) (commit $($release.Commit.Substring(0, 12)))" -Level Detail
    }
    $plan = $(if ($workstationOnly) {
            "Put the screenshot tool in $($folders.App) (no proxy and no client files), wiring: Workstation"
        } else {
            "Install $($release.Ref) to $($folders.App), client files in $($folders.Clients), wiring: " +
                $(if ($targets) { $targets -join ' + ' } else { 'none' })
        })
    if (-not (Confirm-Action -Message "$plan. Continue?" -Default 'Yes')) { throw 'Stopped at your request' }
    if ($WhatIfPreference) { Write-Status "WhatIf: $plan" -Level Done; return }

    $fetched = if ($FromWorkingCopy) { Get-WorkingCopy -Folder $url } else { Get-Release -Url $url -Release $release }
    $wanted = @(Select-RoleFiles -Files $fetched.Files -Targets $targets)
    if ($wanted.Count -lt @($fetched.Files).Count) {
        Write-Status ("A client PC runs {0} of the {1} files; the rest is for machines with an agent on them" -f
                      $wanted.Count, @($fetched.Files).Count) -Level Detail
        $fetched | Add-Member -NotePropertyName 'Files' -NotePropertyValue $wanted -Force
    }
    Copy-Release -Fetched $fetched -AppRoot $folders.App
    Remove-Stage
    if (-not $workstationOnly) { Set-DataLocation -AppRoot $folders.App -Clients $folders.Clients }
    Copy-ContactFile -AppRoot $folders.App
    $now = (Get-Date).ToString('s')
    # Written before the wiring runs, so a failed wiring step can still be updated or uninstalled.
    Save-Deployment -Record ([pscustomobject][ordered]@{
        schema = 2; repo_url = $url; ref = $release.Ref
        commit = if ($FromWorkingCopy) { [string]$fetched.Commit } else { $release.Commit }
        version = $fetched.Version
        install_path = $folders.App; clients_path = $folders.Clients; targets = $targets
        files = $fetched.Files; installed_at = $now; updated_at = $now
        # The release key this install trusts from now on (H16); '' for an unsigned or working-copy install.
        signing_key = if ($fetched.PSObject.Properties['SigningKey']) { [string]$fetched.SigningKey } else { '' }
    })
    Write-StepBanner -Number 4 -Of 6 -Title 'Connecting your AI tools' -Unattended:$script:Unattended
    Invoke-Wiring -AppRoot $folders.App -Targets $targets -Role $role -Url $url -Ref $release.Ref
    Write-StepBanner -Number 5 -Of 6 -Title 'Remote access (optional)' -Unattended:$script:Unattended
    Invoke-RemoteSetup -AppRoot $folders.App -Role $role -Url $url -Ref $release.Ref
    Write-StepBanner -Number 6 -Of 6 -Title 'Ready' -Unattended:$script:Unattended
    Write-Status "aicleaner $($release.Ref) deployed to $($folders.App)" -Level Done
    if ($role.Advice) { Write-Status $role.Advice -Level Detail }
    Write-NextSteps -AppRoot $folders.App -Targets $targets -ClientsPath $folders.Clients `
        -RemoteHostname $role.RemoteHostname
    Invoke-Activation -AppRoot $folders.App -Targets $targets
    Write-SummaryBox -Title "aicleaner $($release.Ref) is installed" -Lines @(
        "Program:      $($folders.App)",
        $(if ($workstationOnly) { 'Client files: none on this device' } else { "Client files: $($folders.Clients)" }),
        "AI tools:     $(if ($targets) { $targets -join ', ' } else { 'none yet' })",
        '',
        'Next: the setup page, http://127.0.0.1:8787/#/setup')
    # The rest of the setup happens in the browser (feature 106) - only when a person is here and a
    # proxy runs on this machine to show it.
    if (-not $script:Unattended -and -not $env:SSH_CONNECTION -and -not $workstationOnly -and -not $WhatIfPreference) {
        $port = Get-ConfiguredPort -AppRoot $folders.App
        if (Wait-ProxyHealth -Port $port -Seconds 20) { Start-Process "http://127.0.0.1:$port/#/setup" }
    }
}

function Resolve-UpdateSource {
    <#
    .SYNOPSIS
        Where an update takes its code from, decided from the flags given - never pinned (D52).
    .DESCRIPTION
        A deployment seeded from a working copy used to go back to that folder for ever: -RepoUrl
        was read as another folder, so there was no way to say "use the repository from now on",
        and the recorded folder silently beat the folder the script was run from.

        -RepoUrl         the repository, whatever the record says; a working-copy deployment
                         switches back to releases.
        -FromWorkingCopy the working copy the script is being run from, whatever the record says.
        neither          what the record says. When that is a working copy other than the one being
                         run, the note says so, because an update could install older code than the
                         copy in hand.

        Everything arrives as a parameter (D17): nothing here reads the script's own options.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Record, [switch]$FromWorkingCopy, [string]$RepoUrl = '',
          [string]$RunFolder = '')
    $recorded = [string]$Record.repo_url
    $wasCopy = [string]$Record.ref -eq 'working-copy'
    if ($RepoUrl) {
        $note = if ($wasCopy) { "Switching from the working copy at $recorded to the repository $RepoUrl; later updates follow the repository" } else { '' }
        return [pscustomobject]@{ Kind = 'repo'; Source = $RepoUrl; Note = $note }
    }
    if ($FromWorkingCopy) {
        $from = if ($RunFolder) { $RunFolder } else { $recorded }
        $note = if (-not $wasCopy) { "Switching from releases to the working copy at $from" }
                elseif ($recorded -and $from -and ($recorded.TrimEnd('\') -ne $from.TrimEnd('\'))) { "Taking the working copy at $from, not the one recorded ($recorded)" }
                else { '' }
        return [pscustomobject]@{ Kind = 'working-copy'; Source = $from; Note = $note }
    }
    if ($wasCopy) {
        $note = ''
        $runIsSource = $RunFolder -and (Test-Path -LiteralPath (Join-Path $RunFolder 'aicleaner\__init__.py'))
        if ($runIsSource -and ($recorded.TrimEnd('\') -ne $RunFolder.TrimEnd('\'))) {
            $note = "This deployment follows the working copy at $recorded, not the folder this script was run from ($RunFolder). Add -FromWorkingCopy to take this one, or -RepoUrl to follow the repository."
        }
        return [pscustomobject]@{ Kind = 'working-copy'; Source = $recorded; Note = $note }
    }
    return [pscustomobject]@{ Kind = 'repo'; Source = $recorded; Note = '' }
}

function Update-Deployment {
    <#
    .SYNOPSIS
        Brings a deployment's code up to the newest release (or the working copy it came from).
    .DESCRIPTION
        Sets $script:UpdateOutcome to what happened: 'current', 'updated', 'declined' or 'whatif'.
        It does NOT return it: this function runs child scripts, and capturing its output
        ($x = Update-Deployment) turns their console into a pipe. The proxy it starts inherits that
        pipe's handle and holds it open for as long as it runs, so the caller waited for ever on a
        proxy that had started in seconds. It is shared by
        -Action Update and by a second install run, because re-running an installer that is
        itself out of date repairs nothing: the wiring check has to run the code that is current,
        or it reports yesterday's bugs as today's problems.

        Files are chosen for the deployment's targets, as an install chooses them, so a client PC
        that holds four files is not handed sixty by an update.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)]$Record, [switch]$SkipRuntime,
          [string]$StateFolder = '')
    $script:UpdateOutcome = ''
    $appRoot = [string]$Record.install_path
    $targets = @($Record.targets)
    $runsProxy = @($targets | Where-Object { @('VSCode', 'Desktop') -contains $_ }).Count -gt 0
    $source = Resolve-UpdateSource -Record $Record -FromWorkingCopy:$FromWorkingCopy -RepoUrl $RepoUrl `
        -RunFolder $script:ScriptFolder
    # Said before anything is asked, so "update from where?" is answered without reading the record.
    Write-Status "Update source: $($source.Kind) $($source.Source)" -Level Detail
    if ($source.Note) { Write-Status $source.Note -Level Warn }
    if ($source.Kind -eq 'working-copy') {
        $folder = [string]$source.Source
        $fetched = Get-WorkingCopy -Folder $folder
        if ($fetched.Commit -and [string]$fetched.Commit -eq [string]$Record.commit) {
            Write-Status "Up to date: working copy ($(([string]$fetched.Commit).Substring(0, 12)))" -Level Done
            $script:UpdateOutcome = 'current'; return
        }
        if (-not (Confirm-Action -Message "Update $appRoot from the working copy at ${folder}?" -Default 'Yes')) {
            $script:UpdateOutcome = 'declined'; return
        }
        if ($WhatIfPreference) { Write-Status "WhatIf: would copy $folder over $appRoot" -Level Done; $script:UpdateOutcome = 'whatif'; return }
        $newRef = 'working-copy'
        $newCommit = [string]$fetched.Commit
        $newUrl = $folder
    } else {
        Assert-Git
        $url = if ($RepoUrl) { Resolve-RepoUrl -Explicit $RepoUrl } else { [string]$source.Source }
        # An install follows the channel it came from. One taken from a branch (-Ref main) stays on
        # that branch; left to "newest release" it would be offered an older tag as an update, and
        # taken backwards onto code it was installed to get away from. A working copy is not a
        # channel: leaving one for the repository means the newest release (D52).
        $follow = $Ref
        if (-not $follow -and [string]$Record.ref -and [string]$Record.ref -ne 'working-copy' -and
            [string]$Record.ref -notmatch '^v\d+\.\d+\.\d+$') {
            $follow = [string]$Record.ref
        }
        Resolve-RepoAccess -Url $url -TokenPath (Join-Path $script:StateFolder 'secrets\github-token.dpapi')
        $release = Resolve-Release -Url $url -Wanted $follow
        $short = $release.Commit.Substring(0, 12)
        # The same commit from a different source is still a change of source: the record has to
        # move, or the next update goes back to the working copy.
        if ($release.Commit -eq $Record.commit -and [string]$Record.ref -ne 'working-copy') { Write-Status "Up to date: $($Record.ref) ($short)" -Level Done; $script:UpdateOutcome = 'current'; return }
        $installed = if ([string]$Record.commit) { ([string]$Record.commit).Substring(0, 12) } else { 'unknown' }
        if (-not (Confirm-Action -Message "Newer code is available: update $appRoot from $($Record.ref) ($installed) to $($release.Ref) ($short)?" -Default 'Yes')) {
            $script:UpdateOutcome = 'declined'; return
        }
        if ($WhatIfPreference) { Write-Status "WhatIf: would update $appRoot to $($release.Ref)" -Level Done; $script:UpdateOutcome = 'whatif'; return }
        # The key this install trusts (H16); none yet means the first signed release pins one.
        $script:PinnedSigningKey = if ($Record.PSObject.Properties['signing_key']) { [string]$Record.signing_key } else { '' }
        $fetched = Get-Release -Url $url -Release $release
        $newRef = $release.Ref
        $newCommit = $release.Commit
        $newUrl = $url
    }
    $wanted = @(Select-RoleFiles -Files $fetched.Files -Targets $targets)
    $fetched | Add-Member -NotePropertyName 'Files' -NotePropertyValue $wanted -Force
    if ($runsProxy) { Invoke-AppScript -Path (Get-AppFile -AppRoot $appRoot -Name 'Stop-AiCleaner.ps1') -Arguments @('-Port', [string](Get-ConfiguredPort -AppRoot $appRoot)) }
    # Keep what is about to be overwritten, before overwriting it (#61). A failing code drop
    # takes the AI tooling down with it, and the remedy has to be something that does not depend
    # on the thing that broke.
    if ($StateFolder) { [void](Save-Rollback -AppRoot $appRoot -StateFolder $StateFolder -Record $Record) }
    Copy-Release -Fetched $fetched -AppRoot $appRoot
    Remove-Stage
    $dropped = @(@($Record.files) | Where-Object { $wanted -notcontains $_ })
    if ($dropped.Count -gt 0) {
        Remove-RecordedFiles -AppRoot $appRoot -Files $dropped
        Write-Status "Removed $($dropped.Count) files no longer in the release" -Level Detail
    }
    Update-TaskPath -AppRoot $appRoot
    $Record.ref = $newRef; $Record.commit = $newCommit; $Record.version = $fetched.Version
    $Record.repo_url = $newUrl; $Record.files = $wanted
    # Pinned from the first signed release on; an unsigned one (-AllowUnsigned) never unpins it.
    if ($fetched.PSObject.Properties['SigningKey'] -and [string]$fetched.SigningKey) {
        $Record | Add-Member -NotePropertyName 'signing_key' -NotePropertyValue ([string]$fetched.SigningKey) -Force
    }
    Save-Deployment -Record $Record
    # No data migrations exist yet; a versioned migration step belongs here (FUTURE-FEATURES #14).
    # A re-run leaves the runtime to the installer that follows: it builds the venv if it has gone,
    # installs the requirements, checks the mapping and starts the proxy, in that order. Starting the
    # proxy here first failed whenever the venv was missing - which is exactly when a repair is run.
    if ($runsProxy -and -not $SkipRuntime) { Update-Runtime -AppRoot $appRoot }
    Update-AgentTool -AppRoot $appRoot -Targets @($Record.targets)
    Write-Status "aicleaner updated to $newRef ($($wanted.Count) files)" -Level Done
    $script:UpdateOutcome = 'updated'
}

function Update-TaskPath {
    <#
    .SYNOPSIS
        Points this install's scheduled tasks at the scripts' folder after the layout (feature 102).
    .DESCRIPTION
        The proxy's sign-in task, the health check and the backup were registered with a script at
        the app folder's root. An update to the layout removes those copies, so a task still naming
        one would start nothing at the next sign-in. Each aicleaner task whose action names a script
        at the root that now sits in installer\ is changed to name the new path; nothing else is
        touched, and a task that cannot be changed is reported, not fatal.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$AppRoot)
    $folder = Join-Path $AppRoot 'installer'
    if (-not (Test-Path -LiteralPath $folder) -or -not (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)) { return }
    $moved = @(Get-ChildItem -LiteralPath $folder -Filter '*.ps1' | ForEach-Object Name)
    foreach ($task in @(Get-ScheduledTask -TaskName 'aicleaner*' -ErrorAction SilentlyContinue)) {
        # The scripts the task names at the root, found before anything is rewritten: the rewrite
        # below changes the action in memory, and the fallback needs the old path (D173).
        $named = @(foreach ($name in $moved) {
            $old = Join-Path $AppRoot $name
            if (@($task.Actions | Where-Object { $_.Arguments -and $_.Arguments.Contains($old) }).Count) { $name }
        })
        if (-not $named.Count -or -not $PSCmdlet.ShouldProcess($task.TaskName, 'point at installer\')) { continue }
        foreach ($action in @($task.Actions)) {
            foreach ($name in $named) {
                if ($action.Arguments) { $action.Arguments = $action.Arguments.Replace((Join-Path $AppRoot $name), (Join-Path $folder $name)) }
            }
        }
        if ($named.Count) {
            try {
                Set-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Action $task.Actions -ErrorAction Stop | Out-Null
                Write-Status "Scheduled task $($task.TaskName) now runs the script from installer\" -Level Detail
            } catch {
                # A task registered elevated cannot be changed from a normal session (Access is
                # denied). It keeps working through a forwarder at the path it names.
                foreach ($name in $named) { Write-TaskForwarder -AppRoot $AppRoot -Name $name }
                Write-Status "Scheduled task $($task.TaskName) could not be changed ($($_.Exception.Message)); it runs the script through a forwarder at its old path. Re-run Deploy from an elevated PowerShell to point it at installer\ directly." -Level Warn
            }
        }
    }
}

function Write-TaskForwarder {
    <#
    .SYNOPSIS
        A script at a moved script's old place in the app folder that runs the one in installer\.
    .DESCRIPTION
        For a scheduled task that still names the old path and cannot be changed without
        administrator rights (feature 102). The arguments are handed to a child PowerShell, so a
        switch such as -Detach arrives as a switch. Not part of the release, so an update never
        removes it; an elevated re-run that points the task at installer\ makes it unnecessary.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$AppRoot, [Parameter(Mandatory)][string]$Name)
    $path = Join-Path $AppRoot $Name
    if (-not $PSCmdlet.ShouldProcess($path, 'write a forwarder to installer\')) { return }
    $text = @(
        "# Written by Deploy-AiCleaner.ps1 (feature 102): a scheduled task still names this path and could not",
        "# be changed without administrator rights. It runs installer\$Name with the same arguments.",
        "`$target = Join-Path (Join-Path `$PSScriptRoot 'installer') '$Name'",
        "`$shell = [Diagnostics.Process]::GetCurrentProcess().Path",
        "& `$shell -NoProfile -ExecutionPolicy Bypass -File `$target @args",
        "exit `$LASTEXITCODE"
    ) -join "`r`n"
    Set-Content -LiteralPath $path -Value $text -Encoding UTF8
}

function Update-AgentTool {
    <#
    .SYNOPSIS
        The tools a Claude Code machine gives its agent, kept current on Update as well as Install.
    .DESCRIPTION
        Today PSScriptAnalyzer, so the agent can lint the PowerShell it writes (the user,
        2026-10-02). Install gets it through Install-AiCleaner-VSCode.ps1's own step; Update copies
        files and runs no installer, so it asks that same script for that one step alone rather than
        keeping a second copy of the logic here. Claude Code roles only (in VS Code, over
        Remote-SSH, headless): Claude Desktop and a viewing device run no agent scripts. A failure
        is a warning - the proxy does not need it.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$AppRoot, [string[]]$Targets = @())
    if ($Targets -notcontains 'VSCode') { return }
    if (-not $PSCmdlet.ShouldProcess('PSScriptAnalyzer', 'Check, and install when missing')) { return }
    $installer = Get-AppFile -AppRoot $AppRoot -Name 'Install-AiCleaner-VSCode.ps1'
    if (-not (Test-Path -LiteralPath $installer)) {
        Write-Status "PSScriptAnalyzer not checked: $installer is missing" -Level Warn
        return
    }
    try { Invoke-AppScript -Path $installer -Arguments @('-ScriptAnalyzerOnly', '-LogPath', '') }
    catch { Write-Status "PSScriptAnalyzer not checked: $($_.Exception.Message)" -Level Warn }
    # Claude Code's telemetry and error reporting off (the user, 2026-10-06), kept so on every
    # update in the same way: the installer's own step, alone.
    try { Invoke-AppScript -Path $installer -Arguments @('-TelemetryOnly', '-LogPath', '') }
    catch { Write-Status "Telemetry setting not checked: $($_.Exception.Message)" -Level Warn }
}

function Invoke-Update {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    $record = Select-Deployment -Verb 'update'
    if (-not $record) { throw "No deployment record at $script:RecordPath; use -Action Install" }
    Update-Deployment -Record $record -StateFolder $script:StateFolder
    if ($script:UpdateOutcome -eq 'declined') { throw 'Stopped at your request' }
}

function Remove-DeploymentData {
    <# -RemoveData: the app folder's data and last-good copies after REMOVE; an outside clients folder separately. #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$AppRoot, [string]$Clients = '')
    if ($WhatIfPreference) { Write-Status "WhatIf: would ask before deleting $AppRoot and $Clients" -Level Detail; return }
    $inside = $Clients -and (Test-UnderFolder -Path $Clients -Folder $AppRoot)
    $what = "everything left in $AppRoot (config, Global, contact, logs, candidates" + $(if ($inside) { ', AND the client files' } else { '' }) + ") and $StateRoot\lastgood"
    if (Read-Typed -Message "Type REMOVE to delete $what" -Expected 'REMOVE') {
        foreach ($path in @($AppRoot, (Join-Path $StateRoot 'lastgood'))) {
            if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
        }
        Write-Status 'Data deleted' -Level Detail
    } else { Write-Status "Data kept in $AppRoot" -Level Detail }
    if ($Clients -and -not $inside -and (Test-Path -LiteralPath $Clients)) {
        $leaf = Split-Path -Leaf $Clients
        $sharing = if (Get-OneDriveRoot -Path $Clients) { ' through OneDrive' } else { '' }
        Write-Status "The client files at $Clients may be shared with colleagues$sharing; deleting them deletes them for everyone." -Level Warn
        if (Read-Typed -Message "Type the folder name ($leaf) to delete it too, or press Enter to keep it" -Expected $leaf) {
            Remove-Item -LiteralPath $Clients -Recurse -Force
            Write-Status "Deleted $Clients" -Level Detail
        } else { Write-Status "Client files kept at $Clients" -Level Detail }
    }
}

function Invoke-RollbackAction {
    <# Put the previous build back (#61). Reads the same record the other actions read, so it
       rolls back the install this script knows about rather than guessing at a folder. #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$StateFolder)
    $record = Select-Deployment -Verb 'roll back'
    $appRoot = if ($record) { [string]$record.install_path } else { Join-Path $env:USERPROFILE '.aicleanerpp' }
    if (-not $record) {
        Write-Status "No deployment record at $script:RecordPath; trying $appRoot anyway" -Level Warn
    }
    [void](Invoke-Rollback -AppRoot $appRoot -StateFolder $StateFolder)
}

function Invoke-Uninstall {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    $record = Select-Deployment -Verb 'uninstall'
    if (-not $record) { throw "No deployment record at $script:RecordPath; nothing was deployed by this script" }
    $appRoot = [string]$record.install_path
    if (-not (Confirm-Action -Message "Remove aicleaner from ${appRoot}?" -Default 'Yes')) { throw 'Stopped at your request' }
    if (@($record.targets).Count -gt 0) {
        $uninstallArgs = @('-Port', [string](Get-ConfiguredPort -AppRoot $appRoot))
        if ($RemoveData) { $uninstallArgs += '-RemoveSecrets' }
        $have = Restore-RepoFile -AppRoot $appRoot -Relative 'Uninstall-AiCleaner.ps1' `
            -Url ([string]$record.repo_url) -Ref ([string]$record.ref)
        if ($have) { Invoke-AppScript -Path (Get-AppFile -AppRoot $appRoot -Name 'Uninstall-AiCleaner.ps1') -Arguments $uninstallArgs }
        else { Write-Status 'The uninstaller is not available; the wiring was not removed' -Level Warn }
    }
    Remove-RecordedFiles -AppRoot $appRoot -Files @($record.files)
    foreach ($fetched in $script:Fetched) {
        if ((Test-Path -LiteralPath $fetched) -and $PSCmdlet.ShouldProcess($fetched, 'Remove the file fetched to run this')) {
            Remove-Item -LiteralPath $fetched -Force
        }
    }
    Write-Status "Code removed from $appRoot" -Level Detail
    if ($RemoveData) { Remove-DeploymentData -AppRoot $appRoot -Clients ([string]$record.clients_path) }
    else { Write-Status "Data kept: $appRoot (config, Global, contact, logs) and $($record.clients_path) (client files)" -Level Detail }
    Remove-DeploymentRecord -AppRoot $appRoot
    if ((Test-Path -LiteralPath $StateRoot) -and -not (Get-ChildItem -LiteralPath $StateRoot -Force | Select-Object -First 1)) {
        if ($PSCmdlet.ShouldProcess($StateRoot, 'Remove the empty state folder')) {
            Remove-Item -LiteralPath $StateRoot -Force
        }
    }
    Write-Status "aicleaner removed from $appRoot" -Level Done
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

Write-ContactAttribution

$transcriptStarted = $false
if ($LogPath -and -not $WhatIfPreference) {
    try { Start-Transcript -Path $LogPath -Force | Out-Null; $transcriptStarted = $true }
    catch { Write-Status "Could not start transcript at ${LogPath}: $($_.Exception.Message)" -Level Warn }
}

try {
    Write-Status "aicleaner deployment: $Action"
    # Whatever is beside this script (a contact file, a copied-out tree, a working copy) may be a
    # OneDrive placeholder; fetch it all before anything reads or copies it.
    Restore-CloudFiles -Folder $script:ScriptFolder | Out-Null
    switch ($Action) {
        'Install'   { Invoke-Install }
        'Update'    { Invoke-Update }
        'Uninstall' { Invoke-Uninstall }
        'GetSource' { Invoke-GetSource }
        'Rollback'  { Invoke-RollbackAction -StateFolder $script:StateFolder }
    }
}
catch {
    Write-Status "$Action failed: $($_.Exception.Message)" -Level Error
    Write-Error $_ -ErrorAction Continue
    exit 1
}
finally {
    Remove-Stage
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
