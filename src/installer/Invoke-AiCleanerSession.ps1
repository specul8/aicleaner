<# Invoke-AiCleanerSession.ps1
.SYNOPSIS
    Send a prompt to an AI Cleaner session on a schedule, raise or dismiss an AI Cleaner alert, or register the
    scheduled task that does the first (feature 161).

.DESCRIPTION
    Talks to the AI Cleaner running on this machine (http://127.0.0.1:8787 by default) through its local API.

    Run a prompt (-Session, -Prompt):
      - the session is not running: it is started (a new conversation, titled -Title) and sent the prompt;
      - it is running and in use - a turn running, a question waiting, prompts queued, or anything within the last
        -IdleMinutes - the prompt is queued in it, so a conversation Brad has open is never switched away;
      - it is running and idle: with -NewConversation a new conversation is started in it first; without, the prompt
        goes into the open conversation.
      A run that cannot start the session (the cap on sessions running at once, memory nearly full, AI Cleaner not
      answering) raises an amber alert saying so, unless -NoFailureAlert.
    Raise an alert (-Alert): a persistent alert on every AI Cleaner page and device until dismissed.
    Dismiss an alert (-Dismiss): by -Key or -Id.
    Register the task (-RegisterTask): one Task Scheduler task for this user that runs this script with the run
    parameters given, daily at -At (local time); a missed run starts when the machine is next available. No
    administrator rights are needed.

    The title may contain {date}, replaced with the run's date (yyyy-MM-dd).

.PARAMETER Session
    The AI Cleaner session (profile) name, e.g. "BIOS - Accounting".
.PARAMETER Prompt
    The prompt to send.
.PARAMETER Title
    Title of a new conversation; {date} becomes today's date.
.PARAMETER NewConversation
    Start a new conversation when the session is running but idle (a stopped session always starts a new one).
.PARAMETER IdleMinutes
    A running session with anything in the last this-many minutes counts as in use. Default 30.
.PARAMETER NoFailureAlert
    Do not raise an alert when the run cannot reach or start the session.
.PARAMETER Alert
    Raise an alert (with -AlertTitle, -Summary or -SummaryFile, -Level, -Key, -Session).
.PARAMETER AlertTitle
    The alert's title (up to 120 characters).
.PARAMETER Summary
    The alert's summary, plain text (up to 4000 characters).
.PARAMETER SummaryFile
    A UTF-8 text file holding the summary (instead of -Summary).
.PARAMETER Level
    amber (default), red or green.
.PARAMETER Key
    Raise: an open alert with the same key is replaced. Dismiss: the alert to dismiss.
.PARAMETER Dismiss
    Dismiss an alert by -Key or -Id.
.PARAMETER Id
    The alert id to dismiss.
.PARAMETER RegisterTask
    Register (or update) the daily scheduled task that runs this script with -Session, -Prompt, -Title,
    -NewConversation and -IdleMinutes.
.PARAMETER UnregisterTask
    Remove the scheduled task named -TaskName.
.PARAMETER TaskName
    The scheduled task's name. Default "AI Cleaner - <Session> daily".
.PARAMETER At
    The time of day for the task, HH:mm, local time. Default 18:00.
.PARAMETER Force
    Replace a scheduled task that differs without asking.
.PARAMETER BaseUrl
    AI Cleaner's address. Default http://127.0.0.1:8787.
.PARAMETER LogPath
    Transcript file. Default logs\Invoke-AiCleanerSession-<yyyyMMdd-HHmmss>.log in the AI Cleaner folder (one per run,
    the newest 30 kept); '' turns logging off.

.EXAMPLE
    .\Invoke-AiCleanerSession.ps1 -Session "BIOS - Accounting" -Prompt "run the due jobs" -Title "Scheduled - due jobs {date}" -NewConversation

.EXAMPLE
    .\Invoke-AiCleanerSession.ps1 -RegisterTask -Session "BIOS - Accounting" -Prompt "run the due jobs" -Title "Scheduled - due jobs {date}" -NewConversation -At 18:00

.EXAMPLE
    .\Invoke-AiCleanerSession.ps1 -Alert -AlertTitle "Daily close needs you" -SummaryFile .\summary.txt -Session "BIOS - Accounting" -Key daily-close-2026-11-04

.EXAMPLE
    .\Invoke-AiCleanerSession.ps1 -Dismiss -Key daily-close-2026-11-04

.NOTES
    AI Cleaner feature 161, 2026-11-04. Windows PowerShell 5.1 or PowerShell 7. Runs as the user AI Cleaner runs as.
    Exit codes: 0 done, 1 AI Cleaner not answering, 2 refused (the reason is printed and logged), 3 wrong parameters.
#>
#Requires -Version 5.1
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '', Justification = 'Status lines for the person running it, coloured by outcome; the transcript keeps them.')]
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$Session = '',
    [string]$Prompt = '',
    [string]$Title = '',
    [switch]$NewConversation,
    [ValidateRange(0, 1440)][int]$IdleMinutes = 30,
    [switch]$NoFailureAlert,
    [switch]$Alert,
    [string]$AlertTitle = '',
    [string]$Summary = '',
    [string]$SummaryFile = '',
    [ValidateSet('amber', 'red', 'green')][string]$Level = 'amber',
    [string]$Key = '',
    [switch]$Dismiss,
    [string]$Id = '',
    [switch]$RegisterTask,
    [switch]$UnregisterTask,
    [string]$TaskName = '',
    [ValidatePattern('^([01][0-9]|2[0-3]):[0-5][0-9]$')][string]$At = '18:00',
    [switch]$Force,
    [string]$BaseUrl = 'http://127.0.0.1:8787',
    [AllowEmptyString()][string]$LogPath = $(
        $base = if ($PSCommandPath) { [IO.Path]::GetFileNameWithoutExtension($PSCommandPath) } else { 'Invoke-AiCleanerSession' }
        $dir = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }
        # One file per run, stamped, in logs\ (D219): a fixed name was overwritten by the next run.
        Join-Path $dir ("logs\{0}-{1}.log" -f $base, (Get-Date -Format 'yyyyMMdd-HHmmss'))
    )
)

Set-StrictMode -Version Latest

function Get-AiCleanerApiHeader {
    <#
    .SYNOPSIS
        The header that carries this install's API key (H2, AI Cleaner 1.1): a change through the API from this machine
        is refused without it. Read from the DPAPI-protected file AI Cleaner makes at its first start; none yet (an
        older AI Cleaner) gives no header, as before.
    #>
    [OutputType([hashtable])]
    param([string]$StateFolder = (Join-Path $env:USERPROFILE '.aicleaner'))
    # Given outright (a test, or a script started by AI Cleaner itself): whoever sets this already has the key.
    if ($env:AICLEANER_API_KEY) { return @{ 'X-AI-Cleaner-Key' = [string]$env:AICLEANER_API_KEY } }
    $file = Join-Path $StateFolder 'secrets\api-key.dpapi'
    if (-not (Test-Path -LiteralPath $file)) { return @{} }
    try {
        Add-Type -AssemblyName System.Security
        $bytes = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($file), $null, 'CurrentUser')
        try { return @{ 'X-AI-Cleaner-Key' = [Text.Encoding]::UTF8.GetString($bytes).Trim() } }
        finally { [Array]::Clear($bytes, 0, $bytes.Length) }
    } catch {
        Write-Verbose "The API key could not be read: $($_.Exception.Message)"
        return @{}
    }
}
$ErrorActionPreference = 'Stop'

function Initialize-LogFile {
    <#
    .SYNOPSIS
        Makes the log's folder, and keeps only the newest logs of its kind beside it (D219). Never throws.
    .DESCRIPTION
        A log of the stamped shape, <name>-<yyyyMMdd>[-HHmmss].log, gets one file per run (or per day), so a run
        never overwrites the last one's - a fixed name was, and the evidence of why AI Cleaner stopped went with it
        (2026-10-29). Of the files of the same name and shape, the newest -Keep stay. A path of any other shape (one
        passed in) is left as it is, and nothing beside it is touched. The same function is in every script that
        writes a transcript; a test keeps the copies identical.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$Path, [int]$Keep = 30)
    try {
        $folder = Split-Path -Parent $Path
        if ($folder -and -not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
        $shape = [regex]::Match((Split-Path -Leaf $Path), '^(?<kind>.+)-\d{8}(-\d{6})?\.log$')
        if (-not $shape.Success -or -not $folder) { return }
        $kind = $shape.Groups['kind'].Value
        $same = '^' + [regex]::Escape($kind) + '-\d{8}(-\d{6})?\.log$'
        if (-not $PSCmdlet.ShouldProcess($folder, "Keep the newest $Keep $kind logs")) { return }
        Get-ChildItem -LiteralPath $folder -Filter "$kind-*.log" -File -ErrorAction Stop |
            Where-Object { $_.Name -match $same } | Sort-Object Name -Descending | Select-Object -Skip $Keep |
            Remove-Item -Force -ErrorAction SilentlyContinue
    } catch { Write-Verbose "log folder not prepared: $($_.Exception.Message)" }
}

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Done([string]$Text) { Write-Host "==> $Text" -ForegroundColor Green }
function Write-Fail([string]$Text) { Write-Host "==> $Text" -ForegroundColor Red }
function Write-Detail([string]$Text) { Write-Host "    $Text" }

function Get-Field {
    # A property of a JSON answer, or a default when it is not there (StrictMode refuses missing properties).
    param($Object, [string]$Name, $Default = $null)
    if ($null -ne $Object -and $Object.PSObject.Properties.Name -contains $Name) { return $Object.$Name }
    return $Default
}

function Invoke-AiCleanerApi {
    # One call to AI Cleaner's local API. Returns Ok, Status and the parsed Body - never throws for an HTTP refusal,
    # because a refusal (the cap reached, memory nearly full) is an answer the caller acts on. The body goes as UTF-8
    # bytes: Windows PowerShell 5.1 would otherwise send a string as ISO-8859-1 and mangle any non-ASCII text.
    param([string]$Api, [string]$Method, [string]$Path, $Body = $null)
    $request = @{ Uri = "$Api$Path"; Method = $Method; UseBasicParsing = $true; TimeoutSec = 120 }
    if ($Method -ne 'GET') { $request.Headers = Get-AiCleanerApiHeader }      # H2: a change needs the install's key
    if ($null -ne $Body) {
        $request.Body = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 5 -Compress))
        $request.ContentType = 'application/json; charset=utf-8'
    }
    try {
        $response = Invoke-WebRequest @request
        $text = [Text.Encoding]::UTF8.GetString($response.RawContentStream.ToArray())
        return [pscustomobject]@{ Ok = $true; Status = [int]$response.StatusCode; Body = ($text | ConvertFrom-Json) }
    } catch {
        $status = 0
        $exceptionResponse = Get-Field -Object $_.Exception -Name 'Response'
        if ($null -ne $exceptionResponse) { $status = [int]$exceptionResponse.StatusCode }
        $detail = if ($_.ErrorDetails -and $_.ErrorDetails.Message) { $_.ErrorDetails.Message } else { $_.Exception.Message }
        $parsed = $null
        try { $parsed = $detail | ConvertFrom-Json } catch { $parsed = [pscustomobject]@{ error = $detail } }
        return [pscustomobject]@{ Ok = $false; Status = $status; Body = $parsed }
    }
}

function Get-ErrorText($Answer) {
    $message = Get-Field -Object $Answer.Body -Name 'error' -Default ''
    if (-not $message) { $message = "HTTP $($Answer.Status)" }
    $why = Get-Field -Object $Answer.Body -Name 'why' -Default ''
    if ($why) { return "$message $why" }
    return $message
}

function Test-AiCleaner([string]$Api) {
    $health = Invoke-AiCleanerApi -Api $Api -Method GET -Path '/healthz'
    return ($health.Ok -and (Get-Field -Object $health.Body -Name 'ok' -Default $false))
}

function Send-Alert {
    # Raise an alert; used for -Alert and for a scheduled run that could not reach its session.
    param([string]$Api, [string]$AlertText, [string]$Text, [string]$ForSession, [string]$AlertLevel, [string]$AlertKey)
    $answer = Invoke-AiCleanerApi -Api $Api -Method POST -Path '/api/alerts/raise' -Body @{
        title = $AlertText; summary = $Text; session = $ForSession; level = $AlertLevel; key = $AlertKey }
    if (-not $answer.Ok) {
        Write-Fail "The alert was refused: $(Get-ErrorText $answer)"
        return $false
    }
    Write-Done "Alert raised: $AlertText (id $($answer.Body.alert.id))"
    return $true
}

function Get-SessionActivity {
    # This session's entry in the status poll; $null when it has not run since AI Cleaner started (so: not running).
    param([string]$Api, [string]$Name)
    $status = Invoke-AiCleanerApi -Api $Api -Method GET -Path '/api/status'
    if (-not $status.Ok) { return $null }
    $activity = Get-Field -Object $status.Body -Name 'activity'
    foreach ($entry in @(Get-Field -Object $activity -Name 'sessions' -Default @())) {
        if ((Get-Field -Object $entry -Name 'name' -Default '') -eq $Name) { return $entry }
    }
    return $null
}

function Test-SessionInUse {
    # In use: a turn running, a question waiting, prompts queued, or anything happened within the last IdleMinutes.
    param($Activity, [int]$Minutes)
    if ((Get-Field -Object $Activity -Name 'busy' -Default $false) -or ((Get-Field -Object $Activity -Name 'waiting' -Default 0) -gt 0) -or
        ((Get-Field -Object $Activity -Name 'queued' -Default 0) -gt 0)) {
        return $true
    }
    $lastAt = [double](Get-Field -Object $Activity -Name 'last_at' -Default 0)
    if ($lastAt -le 0) { return $false }
    $now = ([DateTimeOffset]::UtcNow).ToUnixTimeMilliseconds() / 1000.0
    return (($now - $lastAt) -lt ($Minutes * 60))
}

function Send-Prompt {
    # A freshly started session may take a moment before it takes input: tried for up to a minute.
    param([string]$Api, [string]$Name, [string]$Text)
    $answer = $null
    for ($attempt = 1; $attempt -le 12; $attempt++) {
        $answer = Invoke-AiCleanerApi -Api $Api -Method POST -Path '/api/session/prompt' -Body @{ name = $Name; text = $Text }
        if ($answer.Ok) { return $answer }
        if ((Get-ErrorText $answer) -notmatch 'no session is running') { return $answer }
        Start-Sleep -Seconds 5
    }
    return $answer
}

function Invoke-ScheduledRun {
    param([string]$Api, [string]$Name, [string]$Text, [string]$RunTitle, [bool]$Fresh, [int]$Minutes, [bool]$AlertOnFailure)
    $today = Get-Date -Format 'yyyy-MM-dd'
    Write-Step "Scheduled run for '$Name'"
    if (-not (Test-AiCleaner -Api $Api)) {
        Write-Fail "AI Cleaner is not answering at $Api"
        return 1                                   # no alert possible: there is nothing to raise it in
    }
    $activity = Get-SessionActivity -Api $Api -Name $Name
    $running = ($null -ne $activity) -and (Get-Field -Object $activity -Name 'running' -Default $false)
    if (-not $running) {
        Write-Detail 'Not running: starting it on a new conversation'
        $started = Invoke-AiCleanerApi -Api $Api -Method POST -Path '/api/session/start' -Body @{ name = $Name; title = $RunTitle }
        if (-not $started.Ok) {
            $why = Get-ErrorText $started
            Write-Fail "Could not start '$Name': $why"
            if ($AlertOnFailure) {
                $null = Send-Alert -Api $Api -AlertText "Scheduled run could not start $Name" -ForSession $Name -AlertLevel 'amber' `
                    -AlertKey "scheduled-start-$today" `
                    -Text "The scheduled prompt was not sent.`n`nPrompt: $Text`nReason: $why`n`nStart the session and send it yourself, or wait for the next run."
            }
            return 2
        }
    } elseif (Test-SessionInUse -Activity $activity -Minutes $Minutes) {
        Write-Detail 'Running and in use: the prompt is queued in the open conversation'
    } elseif ($Fresh) {
        Write-Detail 'Running and idle: a new conversation first'
        $switched = Invoke-AiCleanerApi -Api $Api -Method POST -Path '/api/session/switch' -Body @{ name = $Name; title = $RunTitle }
        if (-not $switched.Ok) {
            # Refused when work started in the meantime: the prompt then queues in the open conversation.
            Write-Warning "No new conversation ($(Get-ErrorText $switched)); the prompt goes to the open one"
        }
    } else {
        Write-Detail 'Running and idle: the prompt goes to the open conversation'
    }
    $sent = Send-Prompt -Api $Api -Name $Name -Text $Text
    if (-not $sent.Ok) {
        $why = Get-ErrorText $sent
        Write-Fail "The prompt was refused: $why"
        if ($AlertOnFailure) {
            $null = Send-Alert -Api $Api -AlertText "Scheduled prompt not sent to $Name" -ForSession $Name -AlertLevel 'amber' `
                -AlertKey "scheduled-prompt-$today" -Text "Prompt: $Text`nReason: $why"
        }
        return 2
    }
    Write-Done "Prompt $(Get-Field -Object $sent.Body -Name 'outcome' -Default 'sent') to '$Name'"
    return 0
}

function Format-Quoted([string]$Text) {
    # One argument for powershell.exe's command line: in double quotes, an inner double quote escaped.
    return '"' + $Text.Replace('"', '\"') + '"'
}

function Get-TaskArgument {
    # What the scheduled task passes back to this script: only the run parameters, quoted for the command line.
    param([string]$ScriptPath, [string]$Api, [string]$Name, [string]$Text, [string]$RunTitle, [bool]$Fresh, [int]$Minutes)
    $parts = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', (Format-Quoted $ScriptPath),
        '-Session', (Format-Quoted $Name), '-Prompt', (Format-Quoted $Text), '-IdleMinutes', $Minutes)
    if ($RunTitle) { $parts += @('-Title', (Format-Quoted $RunTitle)) }
    if ($Fresh) { $parts += '-NewConversation' }
    if ($Api -ne 'http://127.0.0.1:8787') { $parts += @('-BaseUrl', (Format-Quoted $Api)) }
    return ($parts -join ' ')
}

function Read-YesNo([string]$Question, [bool]$Default) {
    if (-not [Environment]::UserInteractive) { return $Default }
    $hint = if ($Default) { '[Y/n]' } else { '[y/N]' }
    $reply = Read-Host "$Question $hint"
    if ([string]::IsNullOrWhiteSpace($reply)) { return $Default }
    return ($reply.Trim().ToLower().StartsWith('y'))
}

function Register-RunTask {
    # One task for this user, run while signed in (AI Cleaner runs then too): no stored password, no elevation.
    # StartWhenAvailable: a machine asleep or off at the hour runs it when it is next up.
    [CmdletBinding(SupportsShouldProcess = $true)]
    [OutputType([int])]
    param([string]$Name, [string]$Argument, [string]$WorkFolder, [string]$Time, [bool]$Replace, [string]$Description)
    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        $action = @($existing.Actions)[0]
        $trigger = @($existing.Triggers)[0]
        $hasTime = if ($trigger) { ([datetime]$trigger.StartBoundary).ToString('HH:mm') } else { '' }
        if ($action -and $action.Arguments -eq $Argument -and $hasTime -eq $Time) {
            Write-Done "Task '$Name' is already registered as asked - nothing changed"
            return 0
        }
        Write-Warning "Task '$Name' exists with other settings"
        Write-Detail "Now:  $($action.Arguments) at $hasTime"
        Write-Detail "Want: $Argument at $Time"
        if (-not $Replace -and -not (Read-YesNo -Question 'Replace it?' -Default $false)) {
            Write-Warning 'Left as it was'
            return 2
        }
    }
    $taskAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $Argument -WorkingDirectory $WorkFolder
    $taskTrigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($Time, 'HH:mm', $null))
    $taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive -RunLevel Limited
    if ($PSCmdlet.ShouldProcess($Name, "Register a daily task at $Time")) {
        $null = Register-ScheduledTask -TaskName $Name -Action $taskAction -Trigger $taskTrigger -Settings $taskSettings `
            -Principal $taskPrincipal -Description $Description -Force
        Write-Done "Task '$Name' registered: daily at $Time"
        Write-Detail "Runs: powershell.exe $Argument"
        Write-Detail "Undo: .\Invoke-AiCleanerSession.ps1 -UnregisterTask -TaskName `"$Name`""
    }
    return 0
}

function Remove-RunTask {
    [CmdletBinding(SupportsShouldProcess = $true)]
    [OutputType([int])]
    param([string]$Name)
    if (-not (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue)) {
        Write-Done "No task '$Name' - nothing to remove"
    } elseif ($PSCmdlet.ShouldProcess($Name, 'Unregister the task')) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Done "Task '$Name' removed"
    }
    return 0
}

function Invoke-Alert {
    param([string]$Api, [string]$AlertText, [string]$Text, [string]$TextFile, [string]$ForSession, [string]$AlertLevel, [string]$AlertKey)
    if ($TextFile) {
        if (-not (Test-Path -LiteralPath $TextFile)) { Write-Fail "No summary file at $TextFile"; return 3 }
        $Text = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $TextFile).Path, [Text.Encoding]::UTF8)
    }
    if (-not $AlertText) { Write-Fail '-Alert needs -AlertTitle'; return 3 }
    if (-not (Test-AiCleaner -Api $Api)) { Write-Fail "AI Cleaner is not answering at $Api"; return 1 }
    if (-not (Send-Alert -Api $Api -AlertText $AlertText -Text $Text -ForSession $ForSession -AlertLevel $AlertLevel -AlertKey $AlertKey)) {
        return 2
    }
    return 0
}

function Invoke-Dismiss {
    param([string]$Api, [string]$AlertId, [string]$AlertKey)
    if (-not $AlertKey -and -not $AlertId) { Write-Fail '-Dismiss needs -Key or -Id'; return 3 }
    $answer = Invoke-AiCleanerApi -Api $Api -Method POST -Path '/api/alerts/dismiss' -Body @{ id = $AlertId; key = $AlertKey }
    if (-not $answer.Ok) { Write-Fail (Get-ErrorText $answer); return 2 }
    if ($answer.Body.dismissed) { Write-Done 'Alert dismissed' } else { Write-Done 'No open alert by that key or id - nothing to dismiss' }
    return 0
}

Write-Host "==> Started $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))"
$transcribing = $false
$exitCode = 0
try {
    if ($LogPath) {
        Initialize-LogFile -Path $LogPath -WhatIf:$false
        Start-Transcript -Path $LogPath -Force -WhatIf:$false | Out-Null
        $transcribing = $true
    }
    if (-not $TaskName -and $Session) { $TaskName = "AI Cleaner - $Session daily" }
    if ($UnregisterTask) {
        if (-not $TaskName) { Write-Fail '-UnregisterTask needs -TaskName or -Session'; $exitCode = 3 }
        else { $exitCode = Remove-RunTask -Name $TaskName }
    } elseif ($Dismiss) {
        $exitCode = Invoke-Dismiss -Api $BaseUrl -AlertId $Id -AlertKey $Key
    } elseif ($Alert) {
        $exitCode = Invoke-Alert -Api $BaseUrl -AlertText $AlertTitle -Text $Summary -TextFile $SummaryFile -ForSession $Session `
            -AlertLevel $Level -AlertKey $Key
    } elseif ($RegisterTask) {
        if (-not $Session -or -not $Prompt) { Write-Fail '-RegisterTask needs -Session and -Prompt'; $exitCode = 3 }
        else {
            $argument = Get-TaskArgument -ScriptPath $PSCommandPath -Api $BaseUrl -Name $Session -Text $Prompt -RunTitle $Title `
                -Fresh $NewConversation.IsPresent -Minutes $IdleMinutes
            $exitCode = Register-RunTask -Name $TaskName -Argument $argument -WorkFolder (Split-Path -Parent $PSCommandPath) -Time $At `
                -Replace $Force.IsPresent -Description "AI Cleaner feature 161: sends '$Prompt' to the session '$Session'."
        }
    } elseif ($Session -and $Prompt) {
        $exitCode = Invoke-ScheduledRun -Api $BaseUrl -Name $Session -Text $Prompt -RunTitle $Title.Replace('{date}', (Get-Date -Format 'yyyy-MM-dd')) `
            -Fresh $NewConversation.IsPresent -Minutes $IdleMinutes -AlertOnFailure (-not $NoFailureAlert)
    } else {
        Write-Fail 'Give -Session and -Prompt, or -Alert, -Dismiss, -RegisterTask or -UnregisterTask (Get-Help .\Invoke-AiCleanerSession.ps1 -Full)'
        $exitCode = 3
    }
} catch {
    Write-Fail "Failed: $($_.Exception.Message)"
    $exitCode = 2
} finally {
    Write-Host "==> Finished $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) (exit $exitCode)"
    if ($transcribing) { Stop-Transcript | Out-Null }
}
exit $exitCode
