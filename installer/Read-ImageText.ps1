<#
.SYNOPSIS
    Reads the text out of an image with Windows' own OCR engine, rows and columns intact.

.DESCRIPTION
    Two callers need this and they need the same answer, so it lives in one file.

      - the proxy, which reads an image that arrived in a request so its words can be filtered
        (aicleaner/convert.py runs this script)
      - Capture-TextFromScreen.ps1, which reads part of the screen so it can be pasted as text

    The engine returns a box for every word, and those boxes are used to rebuild rows and columns:
    a three-column grid comes back as three columns rather than one long list, which is what
    Snipping Tool's extractor gets wrong. Without it a timesheet reads as a pile of entries with
    no day attached to any of them.

    Run with a -Path to read an image and print the text. Dot-source it with no arguments to get
    the functions and nothing else.

    Windows' OCR is reached through WindowsRuntime, which only Windows PowerShell 5.1 can load:
    run it with powershell.exe, not pwsh.

.PARAMETER Path
    The image to read. Without it the script defines its functions and does nothing, which is how
    another script dot-sources it.

.PARAMETER ColumnGap
    How wide a gap between words counts as a column break, as a multiple of the average character
    width. Default 2.5: lower splits more eagerly, higher keeps columns together.

.EXAMPLE
    powershell.exe -File .\Read-ImageText.ps1 -Path .\screenshot.png

.NOTES
    Contact:       see contact-*.json stored alongside this script
    Last-modified: 2026-09-23
    Requires:      Windows PowerShell 5.1 (not PowerShell 7) and an OCR language pack
#>

#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$Path = '',
    [double]$ColumnGap = 2.5
)
# The app folder: this script's own, or its parent when it sits in installer\ (feature 102).
$script:AppFolder = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-WindowsPowerShell {
    <# PowerShell 7 cannot reach WindowsRuntime types, so this script needs powershell.exe. #>
    if ($PSVersionTable.PSEdition -eq 'Core') {
        throw ('This needs Windows PowerShell 5.1 (Windows'' OCR engine is a WindowsRuntime API). ' +
               'Run: powershell.exe -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"')
    }
}

function Get-AsTaskMethod {
    <# The AsTask overload that turns an IAsyncOperation<T> into a Task<T> we can wait on. #>
    Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction SilentlyContinue
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' }
    if (-not $method) { throw 'WindowsRuntime interop is not available in this PowerShell' }
    return $method
}

function Wait-WinRt {
    <# Runs an IAsyncOperation to completion and returns its result. #>
    param([Parameter(Mandatory)]$Operation, [Parameter(Mandatory)][type]$ResultType)
    $task = (Get-AsTaskMethod).MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $task.Wait(-1) | Out-Null
    return $task.Result
}

function Get-OcrWords {
    <#
    .SYNOPSIS
        Every word the OCR engine finds in an image file, with its box: @{Text; X; Y; Width; Height}.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$ImagePath)
    [void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    [void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
    [void][Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if (-not $engine) {
        throw ('No OCR language pack for your display languages. Settings > Time & language > ' +
               'Language & region > (your language) > Language options > Optional features: add Optical character recognition.')
    }
    $file = Wait-WinRt ([Windows.Storage.StorageFile]::GetFileFromPathAsync((Resolve-Path -LiteralPath $ImagePath).Path)) ([Windows.Storage.StorageFile])
    $stream = Wait-WinRt ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Wait-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Wait-WinRt ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Wait-WinRt ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $words = New-Object System.Collections.Generic.List[object]
    foreach ($line in $result.Lines) {
        foreach ($word in $line.Words) {
            $words.Add([pscustomobject]@{
                Text   = [string]$word.Text
                X      = [double]$word.BoundingRect.X
                Y      = [double]$word.BoundingRect.Y
                Width  = [double]$word.BoundingRect.Width
                Height = [double]$word.BoundingRect.Height
            })
        }
    }
    return $words
}

function ConvertTo-LayoutText {
    <#
    .SYNOPSIS
        Words with boxes become text with its rows and columns intact.
    .DESCRIPTION
        Words that overlap vertically are one row (a row of a table, or a line of prose). Within a
        row, a gap wider than ColumnGap average characters is a column break and becomes two
        spaces; anything narrower is a single space. This is what Snipping Tool's extractor does
        not do: it reads a three-column grid as one long list.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Words, [double]$ColumnGap = 2.5)
    if ($Words.Count -eq 0) { return '' }
    $averageChar = ($Words | ForEach-Object { $_.Width / [Math]::Max(1, $_.Text.Length) } |
        Measure-Object -Average).Average
    if (-not $averageChar -or $averageChar -le 0) { $averageChar = 8 }
    $rows = New-Object System.Collections.Generic.List[object]
    foreach ($word in ($Words | Sort-Object Y, X)) {
        $middle = $word.Y + ($word.Height / 2)
        $row = $rows | Where-Object {
            $middle -ge $_.Top -and $middle -le $_.Bottom } | Select-Object -First 1
        if ($row) {
            $row.Words.Add($word)
            $row.Top = [Math]::Min($row.Top, $word.Y + ($word.Height * 0.25))
            $row.Bottom = [Math]::Max($row.Bottom, $word.Y + ($word.Height * 0.75))
        } else {
            $list = New-Object System.Collections.Generic.List[object]
            $list.Add($word)
            $rows.Add([pscustomobject]@{
                Top    = $word.Y + ($word.Height * 0.25)
                Bottom = $word.Y + ($word.Height * 0.75)
                Words  = $list
            })
        }
    }
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($row in ($rows | Sort-Object Top)) {
        $text = ''
        $previous = $null
        foreach ($word in ($row.Words | Sort-Object X)) {
            if ($null -eq $previous) {
                $text = $word.Text
            } else {
                $gap = $word.X - ($previous.X + $previous.Width)
                $text += $(if ($gap -gt ($averageChar * $ColumnGap)) { '  ' } else { ' ' }) + $word.Text
            }
            $previous = $word
        }
        $lines.Add($text)
    }
    return ($lines -join "`r`n")
}

# Dot-sourced with no -Path: the functions are what was wanted, so stop here.
if ($Path) {
    Assert-WindowsPowerShell
    if (-not (Test-Path -LiteralPath $Path)) { throw "No such image: $Path" }
    ConvertTo-LayoutText -Words (Get-OcrWords -ImagePath $Path) -ColumnGap $ColumnGap
}
