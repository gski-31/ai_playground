# ==================== CONFIGURATION ====================
$source        = "C:\MY_GAMES\Eden"
$dest          = "D:\Eden"
$logFile       = Join-Path $PSScriptRoot "Eden_copy_log.txt"
$errorLog      = Join-Path $PSScriptRoot "Eden_errors.txt"

# --- SLC cache tuning (SanDisk Ultra) ---
$slcCacheMB       = 500             # approx SLC cache size in MB (conservative estimate)
$burstSpeedMBps   = 15              # speed above this = writing to SLC cache
$cacheFloorMBps   = 6               # speed below this = cache exhausted, trigger mid-copy pause
$midPauseSeconds  = 20              # pause during copy to let SLC cache partially recover
$betweenPauseMax  = 30              # max pause between files (short, cache recovers fast)
$measureWindowMB  = 50              # measure speed over this rolling window
# ========================================================

# Normalise source path to ensure trailing backslash
$source = $source.TrimEnd('\') + '\'

function Write-Log {
    param([string]$Message, [string]$Color = "White")
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logLine = "[$timestamp] $Message"
    Write-Host $logLine -ForegroundColor $Color
    Add-Content -Path $logFile -Value $logLine
}

function Copy-FileWithProgress {
    param(
        [string]$SourcePath,
        [string]$DestPath,
        [string]$FileName,
        [long]$FileSize
    )
    $bufferSize = 4MB
    $buffer = New-Object byte[] $bufferSize
    $totalRead = 0
    $windowBytes = 0                   # bytes in current measurement window
    $midPauses = 0
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $windowSw = [System.Diagnostics.Stopwatch]::StartNew()

    $srcStream = $null
    $dstStream = $null
    try {
        $srcStream = [System.IO.File]::OpenRead($SourcePath)
        $dstStream = [System.IO.File]::Create($DestPath)
        while (($bytesRead = $srcStream.Read($buffer, 0, $bufferSize)) -gt 0) {
            $dstStream.Write($buffer, 0, $bytesRead)
            $totalRead += $bytesRead
            $windowBytes += $bytesRead

            # --- Rolling speed measurement over $measureWindowMB chunks ---
            $windowMB = $windowBytes / 1MB
            $windowSec = $windowSw.Elapsed.TotalSeconds
            $windowSpeed = if ($windowSec -gt 0) { $windowMB / $windowSec } else { 999 }

            # --- Mid-copy SLC cache pause ---
            if ($windowMB -ge $measureWindowMB -and $windowSpeed -lt $cacheFloorMBps -and ($FileSize - $totalRead) -gt 10MB) {
                $dstStream.Flush()
                $copiedMB = [math]::Round($totalRead / 1MB, 1)
                Write-Log "   Cache pause at ${copiedMB} MB (${([math]::Round($windowSpeed,1))} MB/s) - waiting ${midPauseSeconds}s" "DarkYellow"
                Start-Sleep -Seconds $midPauseSeconds
                $midPauses++
                $windowBytes = 0
                $windowSw.Restart()
            }
            elseif ($windowMB -ge $measureWindowMB) {
                # Reset window without pausing
                $windowBytes = 0
                $windowSw.Restart()
            }

            # --- Progress display ---
            $elapsedSec = $sw.Elapsed.TotalSeconds
            $speedMB = if ($elapsedSec -gt 0) { [math]::Round(($totalRead / 1MB) / $elapsedSec, 1) } else { 0 }
            $copiedMB = [math]::Round($totalRead / 1MB, 1)
            $totalMB  = [math]::Round($FileSize / 1MB, 1)
            $pct = if ($FileSize -gt 0) { [int][math]::Min(100, [math]::Round(($totalRead / $FileSize) * 100)) } else { 100 }
            Write-Progress -Id 1 -Activity "Copying: $FileName" `
                -Status "$copiedMB / $totalMB MB - $speedMB MB/s (window: $([math]::Round($windowSpeed,1)) MB/s)" `
                -PercentComplete $pct
        }
    }
    finally {
        if ($srcStream) { $srcStream.Close() }
        if ($dstStream) { $dstStream.Close() }
        Write-Progress -Id 1 -Activity "Copying: $FileName" -Completed
    }
    return @{ Seconds = $sw.Elapsed.TotalSeconds; MidPauses = $midPauses }
}

function Get-StreamHash {
    param([string]$FilePath, [string]$Label)
    $bufferSize = 4MB
    $buffer = New-Object byte[] $bufferSize
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    $fileInfo = Get-Item -LiteralPath $FilePath
    $totalSize = $fileInfo.Length
    $totalRead = 0
    $stream = $null
    try {
        $stream = [System.IO.File]::OpenRead($FilePath)
        while (($bytesRead = $stream.Read($buffer, 0, $bufferSize)) -gt 0) {
            $null = $hasher.TransformBlock($buffer, 0, $bytesRead, $buffer, 0)
            $totalRead += $bytesRead
            $pct = if ($totalSize -gt 0) { [int][math]::Min(100, [math]::Round(($totalRead / $totalSize) * 100)) } else { 100 }
            $readMB = [math]::Round($totalRead / 1MB, 1)
            $totalMB = [math]::Round($totalSize / 1MB, 1)
            Write-Progress -Id 2 -Activity "Verifying ($Label)" `
                -Status "$readMB / $totalMB MB" -PercentComplete $pct
        }
        $null = $hasher.TransformFinalBlock($buffer, 0, 0)
        return [BitConverter]::ToString($hasher.Hash) -replace '-', ''
    }
    finally {
        if ($stream) { $stream.Close() }
        if ($hasher) { $hasher.Dispose() }
        Write-Progress -Id 2 -Activity "Verifying ($Label)" -Completed
    }
}

function Test-FileCopy {
    param([string]$SourcePath, [string]$DestPath, [string]$FileName, [long]$ExpectedSize)
    # Size check
    $destInfo = Get-Item -LiteralPath $DestPath
    if ($destInfo.Length -ne $ExpectedSize) {
        return @{ OK = $false; Reason = "Size mismatch: source=$ExpectedSize dest=$($destInfo.Length)" }
    }
    # Hash check
    $srcHash  = Get-StreamHash -FilePath $SourcePath -Label "source"
    $destHash = Get-StreamHash -FilePath $DestPath   -Label "dest"
    if ($srcHash -ne $destHash) {
        return @{ OK = $false; Reason = "Hash mismatch: src=$srcHash dest=$destHash" }
    }
    return @{ OK = $true; Reason = "" }
}

Write-Log "=== Eden copy started ===" "Cyan"
Write-Log "Source: $source" "Cyan"
Write-Log "Dest:   $dest" "Cyan"

# --- Count files via streaming (low memory) ---
Write-Host "Counting files..." -ForegroundColor Cyan
$totalFiles = 0
Get-ChildItem -LiteralPath $source -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object { $totalFiles++ }
Write-Log "Found $totalFiles files to process." "Cyan"

$copied = 0
$verified = 0
$verifyFailed = 0
$skipped = 0
$failed = 0
$largeFiles = 0
$index = 0

# --- Stream files one at a time instead of loading all into memory ---
Get-ChildItem -LiteralPath $source -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
    $file = $_
    $index++
    $fileMB = [math]::Round($file.Length / 1MB, 2)
    $percent = if ($totalFiles -gt 0) { [math]::Round(($index / $totalFiles) * 100) } else { 0 }

    try {
        $relativePath = $file.FullName.Substring($source.Length)
        $destFile = Join-Path $dest $relativePath
        $destFolder = Split-Path $destFile -Parent

        # --- Skip if destination file already exists and matches size ---
        if (Test-Path -LiteralPath $destFile) {
            $destInfo = Get-Item -LiteralPath $destFile
            if ($destInfo.Length -eq $file.Length) {
                $skipped++
                Write-Host "[$percent%] Skipped (already exists, same size): $($file.Name)" -ForegroundColor DarkGray
                return   # next file in ForEach-Object
            }
        }

        # --- Create destination folder if needed ---
        if (-not (Test-Path -LiteralPath $destFolder)) {
            New-Item -ItemType Directory -Path $destFolder -Force | Out-Null
        }

        # --- Overall progress bar ---
        Write-Progress -Id 0 -Activity "Eden Copy - Overall Progress" `
            -Status "File $index of $totalFiles ($percent%)" `
            -PercentComplete $percent

        Write-Host "[$percent%] Copying ($fileMB MB): $($file.Name)" -ForegroundColor Green

        # --- Copy with live per-file progress bar + mid-copy cache pauses ---
        $result = Copy-FileWithProgress -SourcePath $file.FullName -DestPath $destFile `
                -FileName $file.Name -FileSize $file.Length
        $seconds = [math]::Round($result.Seconds, 2)
        $speedMBps = if ($seconds -gt 0) { [math]::Round($fileMB / $seconds, 1) } else { "instant" }
        $pauseNote = if ($result.MidPauses -gt 0) { " ($($result.MidPauses) cache pauses)" } else { "" }
        Write-Log "   Copied in ${seconds}s at $speedMBps MB/s${pauseNote}: $relativePath" "White"
        $copied++

        # --- Verify copy: size + SHA-256 hash ---
        Write-Host "[$percent%] Verifying: $($file.Name)" -ForegroundColor Cyan
        $check = Test-FileCopy -SourcePath $file.FullName -DestPath $destFile `
                -FileName $file.Name -ExpectedSize $file.Length
        if ($check.OK) {
            $verified++
            Write-Log "   Verified OK: $relativePath" "Green"
        } else {
            $verifyFailed++
            Write-Log "   VERIFY FAILED: $($check.Reason) - $relativePath" "Red"
            Add-Content -Path $errorLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') VERIFY FAILED: $($check.Reason) - $relativePath"
            # Remove bad copy so it gets re-copied next run
            Remove-Item -LiteralPath $destFile -Force -ErrorAction SilentlyContinue
            Write-Log "   Removed bad copy, will retry next run" "Red"
        }

        # --- Light between-file pause: just enough to flush OS write buffer ---
        if ($fileMB -gt 100 -and $index -lt $totalFiles) {
            $largeFiles++
            # Scale: 10s per GB, capped at $betweenPauseMax
            $actualPause = [math]::Min([math]::Max(5, [int]($fileMB / 100)), $betweenPauseMax)
            Write-Log "   Between-file pause ${actualPause}s (buffer flush)" "Yellow"
            Start-Sleep -Seconds $actualPause
        }
    }
    catch {
        $failed++
        $errMsg = "FAILED [$percent%] $($file.FullName): $($_.Exception.Message)"
        Write-Host $errMsg -ForegroundColor Red
        Add-Content -Path $errorLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $errMsg"
    }
}

Write-Progress -Id 0 -Activity "Eden Copy - Overall Progress" -Completed

Write-Log "" "Magenta"
Write-Log "=== Eden copy finished ===" "Magenta"
Write-Log "  Copied:   $copied" "Green"
Write-Log "  Verified: $verified / $copied" "$(if ($verifyFailed -gt 0) { 'Red' } else { 'Green' })"
if ($verifyFailed -gt 0) {
    Write-Log "  Verify failures: $verifyFailed (bad copies removed, re-run to retry)" "Red"
}
Write-Log "  Skipped:  $skipped (already existed)" "DarkGray"
Write-Log "  Failed:   $failed" "$(if ($failed -gt 0) { 'Red' } else { 'Green' })"
Write-Log "  Pauses:   $largeFiles" "Yellow"
if ($failed -gt 0) {
    Write-Log "  Error log: $errorLog" "Red"
}
Write-Log "  Full log:  $logFile" "Cyan"
