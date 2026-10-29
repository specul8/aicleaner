<#
.SYNOPSIS
    Reads the text out of an image with Windows' own OCR engine, rows and columns intact.

.DESCRIPTION
    The proxy runs this (aicleaner/convert.py) to read an image that arrived in a request, so its
    words can be filtered. It also read the screen for aicleaner's own capture tool until that was
    withdrawn (2026-10-13); the screenshot keys now go to Snipping Tool's text extractor.

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
    [double]$ColumnGap = 2.5,
    [switch]$Engine,
    # Feature 114: the words of the image's first page with their boxes, as JSON, instead of the laid-out text.
    [switch]$Boxes,
    # Feature 114: a JSON file of boxes ([{x,y,w,h}], in the image's pixels) to fill solid black, written to -OutPath.
    [string]$Fill = '',
    [string]$OutPath = ''
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
        The engines' habitual misreadings of screen text, put back where nothing else fits.
    .DESCRIPTION
        Each rule is narrow, so a hash, an ID, a host name or an address keeps its characters:
          - a zero read as a slashed O, or as @ in a version ("127.0.<slashed o>.1", "v@.9.211"):
            only in a word with a digit in it, and O or @ only beside digits and dots
          - a dash read as an em or en dash ("Format<em dash>Table", "d<em dash x4>"): only in a word
            with a letter or digit in it, never one standing alone
          - an l read as 1 mid-word ("AiC1eaner", "disp1ayName"): only after a letter and before a lower-case one,
            in a word with no other digit
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Text)
    if ($Text -match '[A-Za-z0-9]') { $Text = $Text -replace '[\u2013\u2014]', '-' }
    if ($Text -cmatch '^[^0-9]*1[^0-9]*$') { $Text = $Text -creplace '(?<=[A-Za-z])1(?=[a-z])', 'l' }
    if ($Text -notmatch '\d') { return $Text }
    $Text = $Text -replace '[\xD8\xF8]', '0'
    $Text = $Text -replace '(?<=[\d.])[Oo@](?=[\d.])', '0'
    # "@.9" or "v@.9": a version's leading zero, at the start of the word or after its v.
    return ($Text -replace '(?<=^[vV]?)@(?=\.\d)', '0')
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

function Find-Tesseract {
    <#
    .SYNOPSIS
        tesseract.exe on PATH or where its installer puts it, or '' (aicleaner/convert.py looks in
        the same places). The build winget installs leaves PATH alone.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param()
    $onPath = Get-Command tesseract -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($onPath) { return $onPath.Source }
    $bases = @($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ } | ForEach-Object { Join-Path $_ 'Tesseract-OCR' }
    if ($env:LOCALAPPDATA) { $bases += Join-Path $env:LOCALAPPDATA 'Programs\Tesseract-OCR' }
    foreach ($base in $bases) {
        $exe = Join-Path $base 'tesseract.exe'
        if (Test-Path -LiteralPath $exe) { return $exe }
    }
    return ''
}

function Read-TesseractWord {
    <#
    .SYNOPSIS
        Every word Tesseract finds, with its box and confidence (0-100), the image read at twice
        its size. Boxes come back in the image's own pixels.
    .DESCRIPTION
        Its TSV goes to a file and is read as UTF-8: through standard output Windows PowerShell
        decodes it with the console's code page, and anything past ASCII would be mangled.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$ImagePath, [Parameter(Mandatory)][string]$Tesseract, [double]$Scale = 2)
    Add-Type -AssemblyName System.Drawing
    $work = Join-Path ([IO.Path]::GetTempPath()) ('aicleaner-tess-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $work -Force | Out-Null
    try {
        $source = [System.Drawing.Image]::FromFile((Resolve-Path -LiteralPath $ImagePath).Path)
        try {
            $big = New-Object System.Drawing.Bitmap([int]($source.Width * $Scale), [int]($source.Height * $Scale))
            $graphics = [System.Drawing.Graphics]::FromImage($big)
            $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $graphics.DrawImage($source, 0, 0, $big.Width, $big.Height)
            $graphics.Dispose()
        } finally { $source.Dispose() }
        $scaled = Join-Path $work 'in.png'
        $big.Save($scaled, [System.Drawing.Imaging.ImageFormat]::Png)
        $big.Dispose()
        $base = Join-Path $work 'out'
        # Its warnings ("box outside rectangle") go to stderr, which Windows PowerShell turns into
        # errors that stop the script however they are redirected; the exit code says what matters.
        $savedEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try { & $Tesseract $scaled $base --psm 3 tsv 2>$null | Out-Null }
        finally { $ErrorActionPreference = $savedEap }
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath "$base.tsv")) { throw "tesseract failed (exit $LASTEXITCODE)" }
        $words = New-Object System.Collections.Generic.List[object]
        foreach ($row in ([IO.File]::ReadAllLines("$base.tsv", [Text.Encoding]::UTF8) | Select-Object -Skip 1)) {
            $cells = $row -split "`t"
            if ($cells.Count -lt 12 -or $cells[0] -ne '5' -or -not $cells[11].Trim()) { continue }
            $words.Add([pscustomobject]@{
                Text = Repair-OcrWord -Text $cells[11]
                X = [double]$cells[6] / $Scale; Y = [double]$cells[7] / $Scale
                Width = [double]$cells[8] / $Scale; Height = [double]$cells[9] / $Scale
                Confidence = [double]$cells[10]
            })
        }
        return $words
    } finally { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
}

function Merge-EngineReading {
    <#
    .SYNOPSIS
        Windows' reading and Tesseract's as one, word by word (2026-10-13).
    .DESCRIPTION
        Weighed on 25 of the user's own screenshots: Windows' engine misreads l as 1 or I, $ as S
        and 0 as D, where Tesseract, when confident, is right; Tesseract's own mistakes come with a
        low confidence, and it reads frames of # as rows of H at confidence 0. So, for each stretch
        the two read differently:
          - one reading the other one plus more ("Ro" and "Ronin"): the longer
          - Tesseract at ConfidentAt or above for every word of it: Tesseract's
          - otherwise: Windows'
        A word only Tesseract saw is kept when its confidence reaches KeepAlone (its 0s are frames
        and noise); a word only Windows saw is kept.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Windows,
          [Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Tesseract,
          [double]$ConfidentAt = 80, [double]$KeepAlone = 40)
    $merged = New-Object System.Collections.Generic.List[object]
    $usedWindows = New-Object System.Collections.Generic.HashSet[int]
    $usedTesseract = New-Object System.Collections.Generic.HashSet[int]
    for ($t = 0; $t -lt $Tesseract.Count; $t++) {
        if ($usedTesseract.Contains($t)) { continue }
        $rivals = @(for ($w = 0; $w -lt $Windows.Count; $w++) {
            if (-not $usedWindows.Contains($w) -and (Test-WordOverlap -One $Tesseract[$t] -Other $Windows[$w])) { $w } })
        if ($rivals.Count -eq 0) {
            if ($Tesseract[$t].Confidence -ge $KeepAlone) { $merged.Add($Tesseract[$t]) }
            [void]$usedTesseract.Add($t)
            continue
        }
        # Every Tesseract word over the same stretch, so a word one engine split is weighed whole.
        $mine = @(for ($m = $t; $m -lt $Tesseract.Count; $m++) {
            $candidate = $Tesseract[$m]
            if (-not $usedTesseract.Contains($m) -and @($rivals | Where-Object { Test-WordOverlap -One $candidate -Other $Windows[$_] }).Count) { $m } })
        $tesseractText = (@($mine | ForEach-Object { $Tesseract[$_].Text }) -join ' ')
        $windowsText = (@($rivals | ForEach-Object { $Windows[$_].Text }) -join ' ')
        $lowest = (@($mine | ForEach-Object { $Tesseract[$_].Confidence }) | Measure-Object -Minimum).Minimum
        $takeTesseract = if ($tesseractText.Length -gt $windowsText.Length -and
                             $tesseractText.StartsWith($windowsText, [StringComparison]::OrdinalIgnoreCase)) { $true }
                         elseif ($windowsText.Length -gt $tesseractText.Length -and
                                 $windowsText.StartsWith($tesseractText, [StringComparison]::OrdinalIgnoreCase)) { $false }
                         else { $lowest -ge $ConfidentAt }
        if ($takeTesseract) { foreach ($m in $mine) { $merged.Add($Tesseract[$m]) } }
        else { foreach ($w in $rivals) { $merged.Add($Windows[$w]) } }
        foreach ($m in $mine) { [void]$usedTesseract.Add($m) }
        foreach ($w in $rivals) { [void]$usedWindows.Add($w) }
    }
    for ($w = 0; $w -lt $Windows.Count; $w++) { if (-not $usedWindows.Contains($w)) { $merged.Add($Windows[$w]) } }
    return $merged
}

# ---------------------------------------------------------------------------
# Snipping Tool's engine, OneOCR (feature 113)
# ---------------------------------------------------------------------------
# Microsoft's own engine, inside the Snipping Tool package: undocumented and never shipped with
# aicleaner. The user's installed copy is used - its two DLLs copied to a folder of this user's
# (WindowsApps will not load them in place) and signature-checked before every load, its model read
# from the package folder, which nobody but Windows can write. Signatures as the public wrappers
# publish them (b1tg/win11-oneocr and those built on it).

$script:OneOcrKey = 'kj)TGtrK>f]b[Piow.gU+nC@s""""""4'

$script:OneOcrSource = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public class AicOcrWord {
    public string Text; public double X; public double Y; public double Width; public double Height; public double Confidence;
}

public static class AicOneOcr {
    [StructLayout(LayoutKind.Sequential)]
    struct Img { public int Type; public int Width; public int Height; public int Reserved; public long Step; public IntPtr Data; }
    [StructLayout(LayoutKind.Sequential)]
    struct Box { public float X1, Y1, X2, Y2, X3, Y3, X4, Y4; }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool SetDllDirectory(string path);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr LoadLibrary(string path);
    [DllImport("oneocr.dll")] static extern long CreateOcrInitOptions(out long options);
    [DllImport("oneocr.dll")] static extern long OcrInitOptionsSetUseModelDelayLoad(long options, byte value);
    [DllImport("oneocr.dll")] static extern long CreateOcrPipeline(byte[] model, byte[] key, long options, out long pipeline);
    [DllImport("oneocr.dll")] static extern long CreateOcrProcessOptions(out long options);
    [DllImport("oneocr.dll")] static extern long OcrProcessOptionsSetMaxRecognitionLineCount(long options, long count);
    [DllImport("oneocr.dll")] static extern long RunOcrPipeline(long pipeline, ref Img image, long options, out long result);
    [DllImport("oneocr.dll")] static extern long GetOcrLineCount(long result, out long count);
    [DllImport("oneocr.dll")] static extern long GetOcrLine(long result, long index, out long line);
    [DllImport("oneocr.dll")] static extern long GetOcrLineContent(long line, out IntPtr content);
    [DllImport("oneocr.dll")] static extern long GetOcrLineBoundingBox(long line, out IntPtr box);
    [DllImport("oneocr.dll")] static extern long GetOcrLineWordCount(long line, out long count);
    [DllImport("oneocr.dll")] static extern long GetOcrWord(long line, long index, out long word);
    [DllImport("oneocr.dll")] static extern long GetOcrWordContent(long word, out IntPtr content);
    [DllImport("oneocr.dll")] static extern long GetOcrWordBoundingBox(long word, out IntPtr box);
    [DllImport("oneocr.dll")] static extern long GetOcrWordConfidence(long word, out float confidence);
    [DllImport("oneocr.dll")] static extern void ReleaseOcrResult(long result);

    static long pipeline, process;

    static byte[] Bytes(string text) {
        byte[] encoded = System.Text.Encoding.UTF8.GetBytes(text);
        byte[] terminated = new byte[encoded.Length + 1];
        Array.Copy(encoded, terminated, encoded.Length);
        return terminated;
    }

    static string Utf8(IntPtr pointer) {
        if (pointer == IntPtr.Zero) return "";
        int length = 0;
        while (Marshal.ReadByte(pointer, length) != 0) length++;
        byte[] buffer = new byte[length];
        Marshal.Copy(pointer, buffer, 0, length);
        return System.Text.Encoding.UTF8.GetString(buffer);
    }

    public static void Open(string dllFolder, string modelPath, string key) {
        if (pipeline != 0) return;
        SetDllDirectory(dllFolder);
        if (LoadLibrary(System.IO.Path.Combine(dllFolder, "oneocr.dll")) == IntPtr.Zero)
            throw new InvalidOperationException("oneocr.dll would not load (error " + Marshal.GetLastWin32Error() + ")");
        long init;
        if (CreateOcrInitOptions(out init) != 0 || OcrInitOptionsSetUseModelDelayLoad(init, 0) != 0)
            throw new InvalidOperationException("the engine's options could not be created");
        long made;
        if (CreateOcrPipeline(Bytes(modelPath), Bytes(key), init, out made) != 0)
            throw new InvalidOperationException("the engine would not open its model (a Snipping Tool update may have changed it)");
        long options;
        if (CreateOcrProcessOptions(out options) != 0 || OcrProcessOptionsSetMaxRecognitionLineCount(options, 1000) != 0)
            throw new InvalidOperationException("the engine's reading options could not be set");
        pipeline = made; process = options;
    }

    static AicOcrWord Item(string text, IntPtr boxPointer, double confidence) {
        Box box = (Box)Marshal.PtrToStructure(boxPointer, typeof(Box));
        double left = Math.Min(Math.Min(box.X1, box.X2), Math.Min(box.X3, box.X4));
        double right = Math.Max(Math.Max(box.X1, box.X2), Math.Max(box.X3, box.X4));
        double top = Math.Min(Math.Min(box.Y1, box.Y2), Math.Min(box.Y3, box.Y4));
        double bottom = Math.Max(Math.Max(box.Y1, box.Y2), Math.Max(box.Y3, box.Y4));
        return new AicOcrWord { Text = text, X = left, Y = top, Width = right - left, Height = bottom - top, Confidence = confidence };
    }

    // The engine's segments: each line's own text (its spacing, as Snipping Tool pastes it) and box,
    // with the lowest confidence of its words.
    public static AicOcrWord[] ReadLines(byte[] bgra, int width, int height, int stride) {
        List<AicOcrWord> lines = new List<AicOcrWord>();
        GCHandle pinned = GCHandle.Alloc(bgra, GCHandleType.Pinned);
        try {
            Img image = new Img { Type = 3, Width = width, Height = height, Reserved = 0, Step = stride, Data = pinned.AddrOfPinnedObject() };
            long result;
            if (RunOcrPipeline(pipeline, ref image, process, out result) != 0) throw new InvalidOperationException("the engine could not read the image");
            try {
                long count;
                GetOcrLineCount(result, out count);
                for (long l = 0; l < count; l++) {
                    long line, words; IntPtr text; IntPtr boxPointer;
                    if (GetOcrLine(result, l, out line) != 0) continue;
                    GetOcrLineContent(line, out text);
                    GetOcrLineBoundingBox(line, out boxPointer);
                    if (boxPointer == IntPtr.Zero) continue;
                    double lowest = 100;
                    GetOcrLineWordCount(line, out words);
                    for (long w = 0; w < words; w++) {
                        long word; float confidence;
                        if (GetOcrWord(line, w, out word) == 0 && GetOcrWordConfidence(word, out confidence) == 0)
                            lowest = Math.Min(lowest, confidence * 100);
                    }
                    lines.Add(Item(Utf8(text), boxPointer, lowest));
                }
            } finally { ReleaseOcrResult(result); }
        } finally { pinned.Free(); }
        return lines.ToArray();
    }

    // bgra: 4 bytes a pixel, stride bytes a row. Boxes come back in that image's pixels.
    public static AicOcrWord[] Read(byte[] bgra, int width, int height, int stride) {
        List<AicOcrWord> words = new List<AicOcrWord>();
        GCHandle pinned = GCHandle.Alloc(bgra, GCHandleType.Pinned);
        try {
            Img image = new Img { Type = 3, Width = width, Height = height, Reserved = 0, Step = stride, Data = pinned.AddrOfPinnedObject() };
            long result;
            if (RunOcrPipeline(pipeline, ref image, process, out result) != 0) throw new InvalidOperationException("the engine could not read the image");
            try {
                long lines;
                GetOcrLineCount(result, out lines);
                for (long l = 0; l < lines; l++) {
                    long line, count;
                    if (GetOcrLine(result, l, out line) != 0) continue;
                    GetOcrLineWordCount(line, out count);
                    for (long w = 0; w < count; w++) {
                        long word; IntPtr text; IntPtr boxPointer; float confidence;
                        if (GetOcrWord(line, w, out word) != 0) continue;
                        GetOcrWordContent(word, out text);
                        GetOcrWordBoundingBox(word, out boxPointer);
                        GetOcrWordConfidence(word, out confidence);
                        if (boxPointer == IntPtr.Zero) continue;
                        Box box = (Box)Marshal.PtrToStructure(boxPointer, typeof(Box));
                        double left = Math.Min(Math.Min(box.X1, box.X2), Math.Min(box.X3, box.X4));
                        double right = Math.Max(Math.Max(box.X1, box.X2), Math.Max(box.X3, box.X4));
                        double top = Math.Min(Math.Min(box.Y1, box.Y2), Math.Min(box.Y3, box.Y4));
                        double bottom = Math.Max(Math.Max(box.Y1, box.Y2), Math.Max(box.Y3, box.Y4));
                        words.Add(new AicOcrWord { Text = Utf8(text), X = left, Y = top, Width = right - left, Height = bottom - top, Confidence = confidence * 100 });
                    }
                }
            } finally { ReleaseOcrResult(result); }
        } finally { pinned.Free(); }
        return words.ToArray();
    }
}
'@

function Get-SnippingToolPackage {
    <# The installed Snipping Tool: @{ Version; Folder } (its engine's folder), or $null. #>
    [CmdletBinding()]
    param()
    $package = Get-AppxPackage -Name 'Microsoft.ScreenSketch' -ErrorAction SilentlyContinue | Sort-Object { [version]$_.Version } -Descending | Select-Object -First 1
    if (-not $package) { return $null }
    $folder = Join-Path $package.InstallLocation 'SnippingTool'
    if (-not (Test-Path -LiteralPath (Join-Path $folder 'oneocr.dll'))) { return $null }
    return [pscustomobject]@{ Version = [string]$package.Version; Folder = $folder }
}

function Test-MicrosoftSignature {
    <# True when the file carries a valid Authenticode signature from Microsoft Corporation. #>
    [CmdletBinding()]
    [OutputType([bool])]
    param([Parameter(Mandatory)][string]$Path)
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    return ($signature.Status -eq 'Valid' -and $signature.SignerCertificate -and
            $signature.SignerCertificate.Subject -like 'CN=Microsoft Corporation,*')
}

function Copy-OneOcrLibrary {
    <#
    .SYNOPSIS
        The folder holding this Snipping Tool version's two engine DLLs, copied for this user and
        signature-checked; other versions' folders removed. Throws when a DLL is not Microsoft's.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)]$Package, [string]$CacheRoot = (Join-Path $env:LOCALAPPDATA 'aicleaner\oneocr'))
    $folder = Join-Path $CacheRoot $Package.Version
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    foreach ($name in 'oneocr.dll', 'onnxruntime.dll') {
        $copy = Join-Path $folder $name
        if (-not (Test-Path -LiteralPath $copy)) { Copy-Item -LiteralPath (Join-Path $Package.Folder $name) -Destination $copy }
        # Every time, not only after copying: the folder is the user's, so a DLL in it may have
        # been replaced since. One not signed by Microsoft is never loaded.
        if (-not (Test-MicrosoftSignature -Path $copy)) {
            Remove-Item -LiteralPath $copy -Force -ErrorAction SilentlyContinue
            throw "$name in $folder is not signed by Microsoft; it was removed and will be copied again from Snipping Tool"
        }
    }
    Get-ChildItem -LiteralPath $CacheRoot -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne $Package.Version } |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    return $folder
}

function Open-OneOcr {
    <# Snipping Tool's engine, ready to read; throws with the reason when it cannot be. #>
    [CmdletBinding()]
    param()
    $package = Get-SnippingToolPackage
    if (-not $package) { throw 'Snipping Tool (with its text engine) is not installed' }
    $folder = Copy-OneOcrLibrary -Package $package
    if (-not ('AicOneOcr' -as [type])) { Add-Type -TypeDefinition $script:OneOcrSource }
    [AicOneOcr]::Open($folder, (Join-Path $package.Folder 'oneocr.onemodel'), $script:OneOcrKey)
    return $package
}

function ConvertTo-Bgra {
    <#
    .SYNOPSIS
        A bitmap as the BGRA bytes the engine reads: @{ Bytes; Width; Height; Stride; Scale }. Padded
        with white to 50 px a side, scaled down past 10,000 (Scale, to put boxes back).
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][System.Drawing.Bitmap]$Bitmap)
    $scale = [Math]::Min(1.0, 10000.0 / [Math]::Max($Bitmap.Width, $Bitmap.Height))
    $width = [Math]::Max(50, [int][Math]::Floor($Bitmap.Width * $scale))
    $height = [Math]::Max(50, [int][Math]::Floor($Bitmap.Height * $scale))
    $canvas = New-Object System.Drawing.Bitmap($width, $height, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [System.Drawing.Graphics]::FromImage($canvas)
    try {
        $graphics.Clear([System.Drawing.Color]::White)
        $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $graphics.DrawImage($Bitmap, 0, 0, [int][Math]::Floor($Bitmap.Width * $scale), [int][Math]::Floor($Bitmap.Height * $scale))
    } finally { $graphics.Dispose() }
    $locked = $canvas.LockBits((New-Object System.Drawing.Rectangle(0, 0, $width, $height)),
        [System.Drawing.Imaging.ImageLockMode]::ReadOnly, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    try {
        $bytes = New-Object byte[] ($locked.Stride * $height)
        [System.Runtime.InteropServices.Marshal]::Copy($locked.Scan0, $bytes, 0, $bytes.Length)
        return [pscustomobject]@{ Bytes = $bytes; Width = $width; Height = $height; Stride = $locked.Stride; Scale = $scale }
    } finally { $canvas.UnlockBits($locked); $canvas.Dispose() }
}

function Read-OneOcrWord {
    <#
    .SYNOPSIS
        What Snipping Tool's engine finds in an image file, boxes in the image's own pixels: its
        segments (each line of text as the engine joined it) by default, single words with -SingleWords.
    .DESCRIPTION
        Segments, because the engine's own line text has the spacing Snipping Tool pastes ("==>"
        whole, where its words were "==" and ">"); placing segments by their boxes is Snipping
        Tool's text with the layout kept. Words remain for the comparison tests.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$ImagePath, [switch]$SingleWords)
    Add-Type -AssemblyName System.Drawing
    $null = Open-OneOcr
    $bitmap = New-Object System.Drawing.Bitmap((Resolve-Path -LiteralPath $ImagePath).Path)
    try { $image = ConvertTo-Bgra -Bitmap $bitmap } finally { $bitmap.Dispose() }
    $found = if ($SingleWords) { [AicOneOcr]::Read($image.Bytes, $image.Width, $image.Height, $image.Stride) }
             else { [AicOneOcr]::ReadLines($image.Bytes, $image.Width, $image.Height, $image.Stride) }
    $words = New-Object System.Collections.Generic.List[object]
    foreach ($word in $found) {
        $words.Add([pscustomobject]@{
            Text = $word.Text; Confidence = $word.Confidence
            X = $word.X / $image.Scale; Y = $word.Y / $image.Scale
            Width = $word.Width / $image.Scale; Height = $word.Height / $image.Scale
        })
    }
    return $words
}

function Get-ImageEngine {
    <# Which engine would read an image here, and why not Snipping Tool's when it is not. #>
    [CmdletBinding()]
    [OutputType([string])]
    param()
    try { $package = Open-OneOcr; return "snipping-tool $($package.Version)" }
    catch { return "windows ($($_.Exception.Message))" }
}

function Get-ImageWord {
    <#
    .SYNOPSIS
        The words of an image: Snipping Tool's engine when it can (feature 113); otherwise Windows'
        engine, merged with Tesseract when Tesseract is installed.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$ImagePath)
    try { return @(Read-OneOcrWord -ImagePath $ImagePath) }
    catch { Write-Verbose "Snipping Tool's engine could not read it, so the Windows engine: $($_.Exception.Message)" }
    $windows = @(Get-OcrWords -ImagePath $ImagePath)
    $tesseract = Find-Tesseract
    if (-not $tesseract) { return $windows }
    try { $theirs = @(Read-TesseractWord -ImagePath $ImagePath -Tesseract $tesseract) }
    catch {
        Write-Verbose "Tesseract could not read it, so Windows' reading alone: $($_.Exception.Message)"
        return $windows
    }
    return (Merge-EngineReading -Windows $windows -Tesseract $theirs)
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
        $gap = if ($null -eq $previous) { 0 } else { $word.X - ($previous.X + $previous.Width) }
        if ($null -eq $previous) {
            [void]$text.Append(' ' * [Math]::Max(0, $column))
        } elseif ($gap -lt ($CharWidth * $ColumnGap)) {
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

function Split-ImagePage {
    <#
    .SYNOPSIS
        The pages of an image file as PNG files in a folder of their own (a multi-page TIFF has
        several; anything else one), and the folder, to remove afterwards.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$ImagePath)
    Add-Type -AssemblyName System.Drawing
    $folder = Join-Path ([IO.Path]::GetTempPath()) ('aicleaner-pages-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $folder -Force | Out-Null
    $image = [System.Drawing.Image]::FromFile((Resolve-Path -LiteralPath $ImagePath).Path)
    try {
        $count = 1
        try { $count = $image.GetFrameCount([System.Drawing.Imaging.FrameDimension]::Page) } catch { $count = 1 }
        $pages = for ($index = 0; $index -lt [Math]::Min($count, 20); $index++) {
            if ($count -gt 1) { [void]$image.SelectActiveFrame([System.Drawing.Imaging.FrameDimension]::Page, $index) }
            $page = Join-Path $folder ('page-{0:d3}.png' -f ($index + 1))
            $image.Save($page, [System.Drawing.Imaging.ImageFormat]::Png)
            $page
        }
    } finally { $image.Dispose() }
    return [pscustomobject]@{ Folder = $folder; Pages = @($pages) }
}

# Dot-sourced with no -Path: the functions are what was wanted, so stop here.
if ($Engine) {
    Assert-WindowsPowerShell
    Get-ImageEngine
} elseif ($Path -and $Fill) {
    # Feature 114: the boxes filled solid in a copy of the image, saved as PNG. Nothing else of the picture changes.
    Add-Type -AssemblyName System.Drawing
    # Through the pipeline once more: Windows PowerShell's ConvertFrom-Json hands a JSON array on as ONE object, so
    # @() of it was a list holding the whole list - the loop ran once, and filling failed for no box or for two or
    # more (D218, 2026-10-29; only a picture with exactly one box was ever redacted).
    $parsed = Get-Content -LiteralPath $Fill -Raw -Encoding UTF8 | ConvertFrom-Json
    $rects = @($parsed | Where-Object { $null -ne $_ })
    $source = [System.Drawing.Image]::FromFile((Resolve-Path -LiteralPath $Path).Path)
    try {
        $canvas = New-Object System.Drawing.Bitmap($source.Width, $source.Height)
        $graphics = [System.Drawing.Graphics]::FromImage($canvas)
        try {
            $graphics.DrawImage($source, 0, 0, $source.Width, $source.Height)
            $brush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::Black)
            foreach ($rect in $rects) {
                $graphics.FillRectangle($brush, [single]$rect.x, [single]$rect.y, [single]$rect.w, [single]$rect.h)
            }
            $brush.Dispose()
        } finally { $graphics.Dispose() }
        $canvas.Save($OutPath, [System.Drawing.Imaging.ImageFormat]::Png)
        $canvas.Dispose()
    } finally { $source.Dispose() }
} elseif ($Path -and $Boxes) {
    # Feature 114: each word or line the reader found, with its box in the first page's pixels.
    Assert-WindowsPowerShell
    $split = Split-ImagePage -ImagePath $Path
    try {
        $found = @(Get-ImageWord -ImagePath $split.Pages[0])
        $out = foreach ($word in $found) {
            [pscustomobject]@{ text = [string]$word.Text; x = [double]$word.X; y = [double]$word.Y
                               w = [double]$word.Width; h = [double]$word.Height }
        }
        ConvertTo-Json -InputObject @($out) -Compress -Depth 3
    } finally { Remove-Item -LiteralPath $split.Folder -Recurse -Force -ErrorAction SilentlyContinue }
} elseif ($Path) {
    Assert-WindowsPowerShell
    if (-not (Test-Path -LiteralPath $Path)) { throw "No such image: $Path" }
    $split = Split-ImagePage -ImagePath $Path
    try {
        $texts = foreach ($page in $split.Pages) { ConvertTo-LayoutText -Words (Get-ImageWord -ImagePath $page) -ColumnGap $ColumnGap }
        if (@($texts).Count -gt 1) {
            $numbered = for ($index = 0; $index -lt @($texts).Count; $index++) { "--- page $($index + 1) ---`r`n" + @($texts)[$index] }
            $numbered -join "`r`n`r`n"
        } else { @($texts)[0] }
    } finally { Remove-Item -LiteralPath $split.Folder -Recurse -Force -ErrorAction SilentlyContinue }
}
