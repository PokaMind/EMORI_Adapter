#!/usr/bin/env bash
# Download the offline speech models the hostapp uses (CM4 / Linux).
#   Piper voices (TTS) -> ./voices/{ca,en}.onnx (+ .onnx.json)
#   Vosk models (STT)  -> ./models/vosk-{ca,en}/
# ca = Catalan, en = English. Same models as get_voice_models.ps1.
# An add-on's install hook asks for another voice with one argument each:
#   <lang>=<its path under rhasspy/piper-voices, without the extension>
# Install the engines too:  pip install -e ".[speech,stt]"
#
# Best effort, model by model. install.sh runs this on every full deploy, and a robot's
# internet is whatever the site gives it: one connection that drops mid-download must
# not end the WHOLE install at step 3 (set -e), nor leave a truncated voice that a later
# run takes for "already present". Every file is fetched with a timeout and retries into <file>.part, checked, and
# renamed into place only when it is whole -- the .onnx last, because its presence is
# what the hostapp reads as "this language has a voice". A model that still fails is a
# WARNING; the others are installed anyway, the language falls back (PiperTts reads it
# with the first voice), and the exit code is 1 so the caller can say so.
# Exit: 0 every model present | 1 at least one could not be downloaded (named above).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p voices models

# Bounded and retried. Plain --retry covers a stall (timeout) and 408/429/5xx but not a
# connection cut mid-download; --retry-all-errors (curl >= 7.71) does, and with -o curl
# discards the partial data before it retries. Both it and --retry-connrefused (curl >=
# 7.52; Trixie ships 8.x) are asked for only when this curl knows them, so an older one
# still downloads.
CURL=(curl -fsSL --connect-timeout 20 --max-time 900 --retry 3 --retry-delay 5)
for opt in --retry-connrefused --retry-all-errors; do
  if curl "$opt" --version >/dev/null 2>&1; then CURL+=("$opt"); fi
done
# The smallest Piper voice is ~20 MB: anything under 1 MB is an error page a captive
# portal or proxy answered 200 with, not a model.
MIN_ONNX_BYTES=1000000
FAILED=()

# fetch URL DEST: DEST.part, then DEST only when the download completed. 1 on failure,
# with no .part left behind.
fetch() {
  rm -f "$2.part"
  if "${CURL[@]}" "$1" -o "$2.part" && [ -s "$2.part" ]; then
    return 0
  fi
  rm -f "$2.part"
  return 1
}

json_ok() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json, sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$1" \
      >/dev/null 2>&1
  else
    [ "$(head -c1 "$1" 2>/dev/null)" = "{" ]
  fi
}

onnx_ok() {
  [ -s "$1" ] && [ "$(wc -c < "$1")" -ge "$MIN_ONNX_BYTES" ]
}

# A voice is present only as a whole PAIR, with no download of it left half-done.
voice_present() {
  [ ! -e "voices/$1.onnx.part" ] && [ ! -e "voices/$1.onnx.json.part" ] \
    && onnx_ok "voices/$1.onnx" && [ -s "voices/$1.onnx.json" ] \
    && json_ok "voices/$1.onnx.json"
}

PIPER_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main"
declare -A PIPER=(
  [ca]="ca/ca_ES/upc_ona/medium/ca_ES-upc_ona-medium"
  [en]="en/en_US/lessac/medium/en_US-lessac-medium"
)
VOICES=(ca en)
for extra in "$@"; do
  lang="${extra%%=*}"
  case "$lang" in
    [a-z][a-z]|[a-z][a-z][a-z]) ;;
    *) echo "  !! WARNING: '$extra' is not <lang>=<piper-voices path>: ignored" >&2
       FAILED+=("voice argument $extra")
       continue ;;
  esac
  PIPER[$lang]="${extra#*=}"
  VOICES+=("$lang")
done
for lang in "${VOICES[@]}"; do
  if voice_present "$lang"; then
    echo "Piper voice ${lang} already present"
    continue
  fi
  echo "Downloading Piper voice ${lang}..."
  url="${PIPER_BASE}/${PIPER[$lang]}"
  if fetch "${url}.onnx.json?download=true" "voices/${lang}.onnx.json" \
     && json_ok "voices/${lang}.onnx.json.part" \
     && fetch "${url}.onnx?download=true" "voices/${lang}.onnx" \
     && onnx_ok "voices/${lang}.onnx.part"; then
    mv -f "voices/${lang}.onnx.json.part" "voices/${lang}.onnx.json"
    mv -f "voices/${lang}.onnx.part" "voices/${lang}.onnx"
    echo "  Piper voice ${lang} installed"
  else
    rm -f "voices/${lang}.onnx.part" "voices/${lang}.onnx.json.part"
    if voice_present "$lang"; then
      # Only a leftover .part had made a whole voice look unfinished: it stays.
      echo "  (could not download it again; the whole voice installed before stays)"
      continue
    fi
    # A pair that is not whole is not a voice: never leave half of one for the hostapp.
    rm -f "voices/${lang}.onnx"
    echo "  !! WARNING: the Piper voice '${lang}' could not be downloaded (no internet, or" >&2
    echo "     the download failed or was cut). Until it is, '${lang}' is spoken by the" >&2
    echo "     first installed voice. Run this script again once the robot is online." >&2
    FAILED+=("voice ${lang}")
  fi
done

declare -A VOSK=(
  [ca]="vosk-model-small-ca-0.4"
  [en]="vosk-model-small-en-us-0.15"
)
for lang in ca en; do
  if [ -d "models/vosk-${lang}" ]; then
    echo "Vosk model ${lang} already present"
    continue
  fi
  echo "Downloading Vosk model ${lang}..."
  zip="/tmp/vosk-${lang}.zip"
  # A half-unpacked earlier attempt: unzip would stop to ask before overwriting it.
  rm -rf "models/${VOSK[$lang]}"
  ok=0
  if fetch "https://alphacephei.com/vosk/models/${VOSK[$lang]}.zip" "$zip"; then
    mv -f "$zip.part" "$zip"
    unzip -q "$zip" -d models && mv "models/${VOSK[$lang]}" "models/vosk-${lang}" && ok=1
  fi
  if [ "$ok" = 1 ]; then
    echo "  Vosk model ${lang} installed"
  else
    rm -rf "models/${VOSK[$lang]}"
    echo "  !! WARNING: the Vosk model '${lang}' could not be downloaded or unpacked:" >&2
    echo "     the robot cannot hear '${lang}'. Run this script again once it is online." >&2
    FAILED+=("vosk ${lang}")
  fi
  rm -f "$zip" "$zip.part"
done

echo
if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "!! NOT installed: ${FAILED[*]} (see the warnings above). Everything else is." >&2
  exit 1
fi
echo "Done. The hostapp picks these up automatically (voices/*.onnx, models/vosk-*)."
