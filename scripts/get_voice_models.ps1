# Download the offline speech models the hostapp uses (Windows dev box).
#   Piper voices (TTS) -> .\voices\{ca,en}.onnx (+ .onnx.json)
#   Vosk models (STT)  -> .\models\vosk-{ca,en}\
# ca = Catalan, en = English. Same models the CM4 uses (get_voice_models.sh), and like it
# one argument per further voice: <lang>=<its path under rhasspy/piper-voices>.
# Install the engines too:  uv pip install -e ".[speech,stt]"
#
# Best effort, model by model, like get_voice_models.sh: every file is downloaded with a
# timeout and retries into <file>.part, checked (a non-trivial .onnx, a .onnx.json that
# parses) and renamed into place only when whole -- the .onnx last. A voice is present
# only as a whole pair with no .part beside it, so a cut download is fetched again. A
# model that still fails is a warning; the rest are installed. Exit 1 when one failed.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Windows PowerShell's progress bar crawls a large download
$root = Split-Path $PSScriptRoot -Parent
$voices = Join-Path $root 'voices'
$models = Join-Path $root 'models'
New-Item -ItemType Directory -Force $voices | Out-Null
New-Item -ItemType Directory -Force $models | Out-Null

# The smallest Piper voice is ~20 MB: under 1 MB is an error page, not a model.
$minOnnxBytes = 1000000
$failed = @()

# Download $uri to "$dest.part": $true when it arrived whole (3 tries, bounded).
function Get-Part([string]$uri, [string]$dest) {
    $part = "$dest.part"
    for ($try = 1; $try -le 4; $try++) {
        Remove-Item -Force -ErrorAction SilentlyContinue $part
        try {
            Invoke-WebRequest -Uri $uri -OutFile $part -TimeoutSec 900 -UseBasicParsing
            if ((Test-Path $part) -and (Get-Item $part).Length -gt 0) { return $true }
        } catch {
            Write-Host "  (attempt $try failed: $($_.Exception.Message))"
        }
        if ($try -lt 4) { Start-Sleep -Seconds 5 }
    }
    Remove-Item -Force -ErrorAction SilentlyContinue $part
    return $false
}

function Test-Json([string]$path) {
    try { Get-Content -Raw -Encoding UTF8 $path | ConvertFrom-Json | Out-Null; return $true }
    catch { return $false }
}

function Test-Onnx([string]$path) {
    return (Test-Path $path) -and (Get-Item $path).Length -ge $minOnnxBytes
}

function Test-Voice([string]$onnx) {
    return -not (Test-Path "$onnx.part") -and -not (Test-Path "$onnx.json.part") `
        -and (Test-Onnx $onnx) -and (Test-Path "$onnx.json") -and (Test-Json "$onnx.json")
}

$piperBase = 'https://huggingface.co/rhasspy/piper-voices/resolve/main'
$piper = @(
    @{ lang = 'ca'; path = "ca/ca_ES/upc_ona/medium/ca_ES-upc_ona-medium" },
    @{ lang = 'en'; path = "en/en_US/lessac/medium/en_US-lessac-medium" }
)
foreach ($extra in $args) {
    $lang, $path = "$extra" -split '=', 2
    if ($lang -cnotmatch '^[a-z]{2,3}$' -or -not $path) {
        Write-Warning "'$extra' is not <lang>=<piper-voices path>: ignored"
        $failed += "voice argument $extra"
        continue
    }
    $piper += @{ lang = $lang; path = $path }
}
foreach ($v in $piper) {
    $onnx = Join-Path $voices "$($v.lang).onnx"
    if (Test-Voice $onnx) { Write-Host "Piper voice $($v.lang) already present"; continue }
    Write-Host "Downloading Piper voice $($v.lang)..."
    $url = "$piperBase/$($v.path)"
    if ((Get-Part "$url.onnx.json?download=true" "$onnx.json") -and (Test-Json "$onnx.json.part") `
            -and (Get-Part "$url.onnx?download=true" $onnx) -and (Test-Onnx "$onnx.part")) {
        Move-Item -Force "$onnx.json.part" "$onnx.json"
        Move-Item -Force "$onnx.part" $onnx
        Write-Host "  Piper voice $($v.lang) installed"
    } else {
        Remove-Item -Force -ErrorAction SilentlyContinue "$onnx.part", "$onnx.json.part"
        if (Test-Voice $onnx) {
            # Only a leftover .part had made a whole voice look unfinished: it stays.
            Write-Host "  (could not download it again; the whole voice installed before stays)"
            continue
        }
        # A pair that is not whole is not a voice: never leave half of one for the hostapp.
        Remove-Item -Force -ErrorAction SilentlyContinue $onnx
        Write-Warning ("the Piper voice '$($v.lang)' could not be downloaded; until it is, " +
            "'$($v.lang)' is spoken by the first installed voice. Run this script again.")
        $failed += "voice $($v.lang)"
    }
}

$vosk = @(
    @{ lang = 'ca'; name = 'vosk-model-small-ca-0.4' },
    @{ lang = 'en'; name = 'vosk-model-small-en-us-0.15' }
)
foreach ($m in $vosk) {
    $dest = Join-Path $models "vosk-$($m.lang)"
    if (Test-Path $dest) { Write-Host "Vosk model $($m.lang) already present"; continue }
    Write-Host "Downloading Vosk model $($m.lang)..."
    $zip = Join-Path $env:TEMP "$($m.name).zip"
    $unpacked = Join-Path $models $m.name
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $unpacked
    try {
        if (-not (Get-Part "https://alphacephei.com/vosk/models/$($m.name).zip" $zip)) {
            throw "download failed"
        }
        Move-Item -Force "$zip.part" $zip
        Expand-Archive -Path $zip -DestinationPath $models -Force
        Rename-Item -Path $unpacked -NewName "vosk-$($m.lang)"
        Write-Host "  Vosk model $($m.lang) installed"
    } catch {
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $unpacked
        Write-Warning "the Vosk model '$($m.lang)' could not be installed ($($_.Exception.Message))."
        $failed += "vosk $($m.lang)"
    } finally {
        Remove-Item -Force -ErrorAction SilentlyContinue $zip, "$zip.part"
    }
}

if ($failed.Count -gt 0) {
    Write-Warning "NOT installed: $($failed -join ', ') (see above). Everything else is."
    exit 1
}
Write-Host "`nDone. The hostapp picks these up automatically (voices/*.onnx, models/vosk-*)."
