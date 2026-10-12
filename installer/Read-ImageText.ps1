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
    .DESCRIPTION
        The image is read twice, as it is and enlarged (Scale, default 2), and the two readings
        merged by Merge-OcrReading. Neither size wins everywhere on console text: as it is, the
        engine read "==>" as "=>"; enlarged, it kept "C:" and dropped the rest of the path after
        it (the user's capture, 2026-10-12). Scale 1 reads once. The boxes come back in the
        original image's pixels whatever the scale.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$ImagePath, [ValidateRange(1, 4)][double]$Scale = 2)
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
    # Never past the largest image the engine accepts: a big capture is enlarged less, or not at all.
    $longest = [Math]::Max($decoder.PixelWidth, $decoder.PixelHeight)
    $factor = [Math]::Max(1.0, [Math]::Min($Scale, [double][Windows.Media.Ocr.OcrEngine]::MaxImageDimension / [Math]::Max(1, $longest)))
    $words = Read-OcrPass -Engine $engine -Decoder $decoder -Factor 1
    if ($factor -gt 1) {
        $words = Merge-OcrReading -First $words -Second (Read-OcrPass -Engine $engine -Decoder $decoder -Factor $factor)
    }
    return $words
}

function Read-OcrPass {
    <# One reading of the decoded image at a size, its boxes divided back to the image's own pixels. #>
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Engine, [Parameter(Mandatory)]$Decoder, [Parameter(Mandatory)][double]$Factor)
    $transform = New-Object Windows.Graphics.Imaging.BitmapTransform
    $transform.ScaledWidth = [uint32][Math]::Floor($Decoder.PixelWidth * $Factor)
    $transform.ScaledHeight = [uint32][Math]::Floor($Decoder.PixelHeight * $Factor)
    $transform.InterpolationMode = [Windows.Graphics.Imaging.BitmapInterpolationMode]::Cubic
    $bitmap = Wait-WinRt ($Decoder.GetSoftwareBitmapAsync([Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
        [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied, $transform,
        [Windows.Graphics.Imaging.ExifOrientationMode]::IgnoreExifOrientation,
        [Windows.Graphics.Imaging.ColorManagementMode]::DoNotColorManage)) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Wait-WinRt ($Engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $words = New-Object System.Collections.Generic.List[object]
    foreach ($line in $result.Lines) {
        foreach ($word in $line.Words) {
            $words.Add([pscustomobject]@{
                Text   = Repair-OcrWord -Text ([string]$word.Text)
                X      = [double]$word.BoundingRect.X / $Factor
                Y      = [double]$word.BoundingRect.Y / $Factor
                Width  = [double]$word.BoundingRect.Width / $Factor
                Height = [double]$word.BoundingRect.Height / $Factor
            })
        }
    }
    return $words
}

function Repair-OcrWord {
    <#
    .SYNOPSIS
        A zero the engine read as a letter, put back, in a word that is otherwise a number.
    .DESCRIPTION
        Console fonts draw zero slashed or dotted, and the engine reads it as a slashed O
        ("127.0.0.1" came back as "127.0.<slashed o>.1"). Only in a word with a digit in it, and
        a plain O only between digits or dots, so a name keeps its letters.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Text)
    if ($Text -notmatch '\d') { return $Text }
    return (($Text -replace '[\xD8\xF8]', '0') -replace '(?<=[\d.])[Oo](?=[\d.])', '0')
}

function Test-WordOverlap {
    <# Two boxes on the same row covering the same stretch of it: half the narrower one or more. #>
    [CmdletBinding()]
    [OutputType([bool])]
    param([Parameter(Mandatory)]$One, [Parameter(Mandatory)]$Other)
    $across = [Math]::Min($One.X + $One.Width, $Other.X + $Other.Width) - [Math]::Max($One.X, $Other.X)
    $down = [Math]::Min($One.Y + $One.Height, $Other.Y + $Other.Height) - [Math]::Max($One.Y, $Other.Y)
    return ($down -gt 0 -and $across -ge 0.5 * [Math]::Min($One.Width, $Other.Width))
}

function Merge-OcrReading {
    <#
    .SYNOPSIS
        Two readings of one image as one: where they disagree, the one that read more.
    .DESCRIPTION
        Overlapping words are a cluster, and the reading with more characters in it wins the
        cluster ("C:\Users\..." over "C:", "==>" over "=>"); on a tie the first reading stays.
        A word only one reading found is kept, since the other simply missed it.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyCollection()][object[]]$First,
          [Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Second)
    $merged = New-Object System.Collections.Generic.List[object]
    $usedFirst = New-Object System.Collections.Generic.HashSet[int]
    $usedSecond = New-Object System.Collections.Generic.HashSet[int]
    for ($f = 0; $f -lt $First.Count; $f++) {
        if ($usedFirst.Contains($f)) { continue }
        $rivals = @(for ($s = 0; $s -lt $Second.Count; $s++) {
            if (-not $usedSecond.Contains($s) -and (Test-WordOverlap -One $First[$f] -Other $Second[$s])) { $s } })
        if ($rivals.Count -eq 0) { $merged.Add($First[$f]); [void]$usedFirst.Add($f); continue }
        # Every first-reading word over the same stretch, so a word read as two is weighed whole.
        $mine = @(for ($m = $f; $m -lt $First.Count; $m++) {
            $candidate = $First[$m]
            if (-not $usedFirst.Contains($m) -and @($rivals | Where-Object { Test-WordOverlap -One $candidate -Other $Second[$_] }).Count) { $m } })
        $mineLength = ($mine | ForEach-Object { $First[$_].Text.Length } | Measure-Object -Sum).Sum
        $theirLength = ($rivals | ForEach-Object { $Second[$_].Text.Length } | Measure-Object -Sum).Sum
        $winners = if ($theirLength -gt $mineLength) { @($rivals | ForEach-Object { $Second[$_] }) } else { @($mine | ForEach-Object { $First[$_] }) }
        foreach ($word in $winners) { $merged.Add($word) }
        foreach ($m in $mine) { [void]$usedFirst.Add($m) }
        foreach ($s in $rivals) { [void]$usedSecond.Add($s) }
    }
    for ($s = 0; $s -lt $Second.Count; $s++) { if (-not $usedSecond.Contains($s)) { $merged.Add($Second[$s]) } }
    return $merged
}

function ConvertTo-LayoutText {
    <#
    .SYNOPSIS
        Words with boxes become text with its rows and columns intact.
    .DESCRIPTION
        Words that overlap vertically are one row (a row of a table, or a line of prose). Within a
        row, a gap narrower than ColumnGap characters is a single space, so prose reads as prose.
        A wider gap is a column break, and the word is put at the character column its box is at:
        columns line up from row to row and a line keeps its indentation, as the screen showed
        it (the user, 2026-10-12: two spaces per break lost the alignment). Rows further apart
        than the usual line spacing keep the blank lines between them.

        This is what Snipping Tool's extractor does not do: it reads a three-column grid as one
        long list, and every segment after a run of spaces as a line of its own.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Words, [double]$ColumnGap = 2.5)
    if ($Words.Count -eq 0) { return '' }
    $averageChar = Get-CharacterWidth -Words $Words
    $left = ($Words | Measure-Object -Property X -Minimum).Minimum
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
    $ordered = @($rows | Sort-Object Top)
    $pitch = Get-RowPitch -Rows $ordered
    $lines = New-Object System.Collections.Generic.List[string]
    $above = $null
    foreach ($row in $ordered) {
        if ($null -ne $above -and $pitch -gt 0) {
            # Capped: a picture with a big empty band should not paste as a screenful of nothing.
            $blank = [Math]::Min(3, [int][Math]::Round(($row.Top - $above.Top) / $pitch) - 1)
            for ($i = 0; $i -lt $blank; $i++) { $lines.Add('') }
        }
        $lines.Add((ConvertTo-RowText -Words @($row.Words | Sort-Object X) -Left $left -CharWidth $averageChar -ColumnGap $ColumnGap))
        $above = $row
    }
    return ($lines -join "`r`n")
}

function Get-CharacterWidth {
    <#
    .SYNOPSIS
        One character's width in pixels, from the words' boxes.
    .DESCRIPTION
        The median over words of three letters or more: short words are mostly box padding, and
        one long misread word should not move every column on the page.
    #>
    [CmdletBinding()]
    [OutputType([double])]
    param([Parameter(Mandatory)][object[]]$Words)
    $widths = @($Words | Where-Object { $_.Text.Length -ge 3 } | ForEach-Object { $_.Width / $_.Text.Length } | Sort-Object)
    if ($widths.Count -eq 0) {
        $widths = @($Words | ForEach-Object { $_.Width / [Math]::Max(1, $_.Text.Length) } | Sort-Object)
    }
    $median = [double]$widths[[int][Math]::Floor($widths.Count / 2)]
    if ($median -le 0) { return 8.0 }
    return $median
}

function Get-RowPitch {
    <#
    The usual distance from one row to the next, or 0 with fewer than two rows. The lower median:
    with two steps, one of them the gap of a blank line, the gap is not the usual distance.
    #>
    [CmdletBinding()]
    [OutputType([double])]
    param([Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Rows)
    if ($Rows.Count -lt 2) { return 0.0 }
    $steps = @(@(for ($i = 1; $i -lt $Rows.Count; $i++) { $Rows[$i].Top - $Rows[$i - 1].Top }) | Sort-Object)
    return [double]$steps[[int][Math]::Floor(($steps.Count - 1) / 2)]
}

function ConvertTo-RowText {
    <#
    .SYNOPSIS
        One row's words, left to right, as a line: indented to its column, and column breaks
        padded so the next word starts where its box does.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][object[]]$Words, [double]$Left, [double]$CharWidth, [double]$ColumnGap)
    $text = New-Object System.Text.StringBuilder
    $previous = $null
    foreach ($word in $Words) {
        $column = [int][Math]::Round(($word.X - $Left) / $CharWidth)
        if ($null -eq $previous) {
            [void]$text.Append(' ' * [Math]::Max(0, $column))
        } elseif (($word.X - ($previous.X + $previous.Width)) -lt ($CharWidth * $ColumnGap)) {
            [void]$text.Append(' ')
        } else {
            # At least two spaces, so a break still reads as one when the text before ran long.
            [void]$text.Append(' ' * [Math]::Max(2, $column - $text.Length))
        }
        [void]$text.Append($word.Text)
        $previous = $word
    }
    return $text.ToString()
}

# Dot-sourced with no -Path: the functions are what was wanted, so stop here.
if ($Path) {
    Assert-WindowsPowerShell
    if (-not (Test-Path -LiteralPath $Path)) { throw "No such image: $Path" }
    ConvertTo-LayoutText -Words (Get-OcrWords -ImagePath $Path) -ColumnGap $ColumnGap
}
