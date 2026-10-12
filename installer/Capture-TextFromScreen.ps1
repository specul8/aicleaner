<#
.SYNOPSIS
    Reads the text off part of the screen and puts it on the clipboard, columns intact.

.DESCRIPTION
    Screenshots are the one thing the proxy cannot filter: an image is base64, not text, so
    aicleaner withholds it and the model never sees it. This turns the picture into text on this
    machine, so what reaches the clipboard (and then the model) is text the filter can read.

    Drag a rectangle over the part of the screen you want. The text goes to the clipboard and to
    standard output. Nothing is sent anywhere: no network, no mapping, no proxy.

    Better than Snipping Tool's extractor for tables: the engine returns a box for every word, and
    those boxes are used to rebuild rows and columns, so a three-column grid comes back as three
    columns rather than one long list.

    Windows' own OCR engine is used (the one Snipping Tool uses), through WindowsRuntime, which only
    Windows PowerShell 5.1 can reach: run it with powershell.exe, not pwsh.

.PARAMETER Path
    Read an image file instead of capturing the screen (a saved screenshot, or for testing).

.PARAMETER NoClipboard
    Print the text without touching the clipboard.

.PARAMETER ColumnGap
    How wide a gap between words counts as a column break, as a multiple of the average character
    width. Default 2.5: lower splits more eagerly, higher keeps columns together.

.EXAMPLE
    powershell.exe -File .\Capture-TextFromScreen.ps1
    Drag over a table; the text is on the clipboard.

.EXAMPLE
    powershell.exe -File .\Capture-TextFromScreen.ps1 -Path .\screenshot.png -NoClipboard

.NOTES
    Contact:       see contact-*.json stored alongside this script
    Last-modified: 2026-09-20
    Requires:      Windows PowerShell 5.1 (not PowerShell 7) and an OCR language pack
#>

#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$Path = '',
    [switch]$NoClipboard,
    [double]$ColumnGap = 2.5
)
# The app folder: this script's own, or its parent when it sits in installer\ (feature 102).
$script:AppFolder = if (-not $PSScriptRoot) { $PWD.Path } elseif ((Split-Path -Leaf $PSScriptRoot) -eq 'installer') { Split-Path -Parent $PSScriptRoot } else { $PSScriptRoot }

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- Contact attribution (inlined per script so each script stays self-contained) ---------
function Get-ContactInfo {
    <#
    .SYNOPSIS
        The first contact-*.json beside this script (ordinal order of the lower-cased name) as an
        object, or $null when there is none or it does not parse.
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

# ---------------------------------------------------------------------------
# Windows OCR (WindowsRuntime, Windows PowerShell 5.1 only)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Layout: rows from the vertical positions, columns from the gaps
# ---------------------------------------------------------------------------

# --- The reader, shared with the proxy ------------------------------------------------------
# Rebuilding rows and columns from the word boxes is the whole value of this script, and the proxy
# needs the same answer for an image that arrives in a request, so the logic lives in one file
# rather than two copies that drift. That makes this the one script here that is not standalone:
# it is checked for by name, before anything is captured, and says plainly what is missing.
$script:Reader = Join-Path $PSScriptRoot 'Read-ImageText.ps1'

function Assert-Reader {
    <# The companion file, or a message that names it and where it should be. #>
    [CmdletBinding()]
    param()
    if (-not (Test-Path -LiteralPath $script:Reader)) {
        throw (("Read-ImageText.ps1 is missing from {0}. It holds the OCR and the column rebuild, " +
                "and this script cannot read anything without it. Copy it from the aicleaner " +
                "install folder (or the repository) to sit beside this file.") -f $PSScriptRoot)
    }
}

# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

function Get-ScreenRegion {
    <#
    .SYNOPSIS
        Drag a rectangle over the screen; returns the path of a PNG of that region, or '' if
        cancelled (Escape, or a click with no drag).
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param()
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
    $form = New-Object System.Windows.Forms.Form
    $form.FormBorderStyle = 'None'
    $form.StartPosition = 'Manual'
    $form.Bounds = $bounds
    $form.BackColor = [System.Drawing.Color]::Black
    $form.Opacity = 0.35
    $form.TopMost = $true
    $form.Cursor = [System.Windows.Forms.Cursors]::Cross
    $script:startPoint = $null
    $script:selection = [System.Drawing.Rectangle]::Empty
    $form.Add_KeyDown({ if ($_.KeyCode -eq 'Escape') { $form.Close() } })
    $form.Add_MouseDown({ $script:startPoint = $_.Location })
    $form.Add_MouseUp({
        if ($script:startPoint) {
            $script:selection = [System.Drawing.Rectangle]::FromLTRB(
                [Math]::Min($script:startPoint.X, $_.X), [Math]::Min($script:startPoint.Y, $_.Y),
                [Math]::Max($script:startPoint.X, $_.X), [Math]::Max($script:startPoint.Y, $_.Y))
        }
        $form.Close()
    })
    $form.KeyPreview = $true
    [void]$form.ShowDialog()
    $region = $script:selection
    if ($region.Width -lt 5 -or $region.Height -lt 5) { return '' }
    $bitmap = New-Object System.Drawing.Bitmap($region.Width, $region.Height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.CopyFromScreen($bounds.X + $region.X, $bounds.Y + $region.Y, 0, 0, $region.Size)
    $graphics.Dispose()
    $file = Join-Path ([IO.Path]::GetTempPath()) ("aicleaner-capture-{0}.png" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    $bitmap.Save($file, [System.Drawing.Imaging.ImageFormat]::Png)
    $bitmap.Dispose()
    return $file
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if ($MyInvocation.InvocationName -eq '.') { return }  # dot-sourced by the tests: functions only

# Set before the try, because finally runs however the try ended - including on a throw from the
# first line of it, when StrictMode would otherwise turn the cleanup into a second error.
$temporary = ''

try {
    # Dot-sourcing a script runs its param block in this scope, so the companion's own -Path and
    # -ColumnGap defaults would overwrite the ones this script was called with. Held first, used
    # after.
    $wantedPath = $Path
    $wantedGap = $ColumnGap
    # Checked before the screen is touched: a missing companion should not be discovered after
    # somebody has dragged a rectangle.
    Assert-Reader
    . $script:Reader
    Assert-WindowsPowerShell
    $image = $wantedPath
    if (-not $image) {
        $image = Get-ScreenRegion
        $temporary = $image
        if (-not $image) { Write-Host 'Nothing selected.'; return }
    }
    if (-not (Test-Path -LiteralPath $image)) { throw "No such image: $image" }
    $words = Get-OcrWords -ImagePath $image
    $text = ConvertTo-LayoutText -Words $words -ColumnGap $wantedGap
    if (-not $text) { Write-Host 'No text found in that region.'; return }
    if (-not $NoClipboard) {
        Set-Clipboard -Value $text
        Write-Host ("Text on the clipboard ({0} line(s), {1} word(s)). Paste it; do not paste the screenshot." -f
            (($text -split "`r`n").Count), $words.Count) -ForegroundColor Green
    }
    Write-Output $text
} catch {
    $problem = "Could not read the screen: $($_.Exception.Message)"
    Write-Host $problem -ForegroundColor Red
    # Started from the screenshot keys this runs hidden, so a console message is seen by nobody: the
    # keys looked as if they did nothing (the user, 2026-10-12). Said in a box, and kept in a log.
    try {
        Add-Content -LiteralPath (Join-Path ([IO.Path]::GetTempPath()) 'aicleaner-capture.log') -Value ("{0:u} {1}" -f (Get-Date), $problem)
        Add-Type -AssemblyName System.Windows.Forms
        [void][System.Windows.Forms.MessageBox]::Show($problem, 'aicleaner screen capture', 'OK', 'Warning')
    } catch { Write-Verbose "could not show the problem: $($_.Exception.Message)" }
    exit 1
} finally {
    if ($temporary -and (Test-Path -LiteralPath $temporary)) { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
}
