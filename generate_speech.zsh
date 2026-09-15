#!/usr/bin/env zsh
# generate_speech.zsh — portable entrypoint for voxcpm-narrate (uv-managed)
#
# Prerequisites: uv (https://docs.astral.sh/uv/), ffmpeg (for non-WAV references)
#
# Typical flow:
#   cp examples/script.md workspace/script.md
#   cp /path/to/voice.wav workspace/source.wav
#   ./generate_speech.zsh --dry-run
#   ./generate_speech.zsh

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return 0
  fi
  echo "==> uv not found. Installing via astral.sh ..." >&2
  curl -LsSf https://astral.sh/uv/install.sh | sh
  if [[ -x "$HOME/.local/bin/uv" ]]; then
    export PATH="$HOME/.local/bin:$PATH"
  fi
  if ! command -v uv >/dev/null 2>&1; then
    echo "Failed to install uv. Install manually: https://docs.astral.sh/uv/" >&2
    exit 1
  fi
}

resolve_default_input() {
  local candidates=(
    "${VOXCPM_INPUT:-}"
    "$SCRIPT_DIR/workspace/script.ssml"
    "$SCRIPT_DIR/workspace/script.md"
    "$SCRIPT_DIR/script.ssml"
    "$SCRIPT_DIR/script.md"
  )
  local c
  for c in "${candidates[@]}"; do
    [[ -n "$c" && -f "$c" ]] && { echo "$c"; return 0; }
  done
  return 1
}

resolve_default_reference() {
  if [[ -n "${VOXCPM_REFERENCE:-}" && -f "${VOXCPM_REFERENCE}" ]]; then
    echo "$VOXCPM_REFERENCE"
    return 0
  fi
  local bases=(
    "$SCRIPT_DIR/workspace/source"
    "$SCRIPT_DIR/workspace/reference"
    "$SCRIPT_DIR/source"
    "$SCRIPT_DIR/reference"
  )
  local exts=(ogg wav mp3 flac m4a)
  local b e
  for b in "${bases[@]}"; do
    for e in "${exts[@]}"; do
      if [[ -f "${b}.${e}" ]]; then
        echo "${b}.${e}"
        return 0
      fi
    done
  done
  return 1
}

ensure_uv

# Force dependency sync then exit
if [[ "${1:-}" == "--" && "${2:-}" == "setup" ]] || [[ "${1:-}" == "--setup" ]]; then
  echo "==> uv sync"
  uv sync
  echo "==> Setup complete."
  exit 0
fi

INPUT=""
REFERENCE=""
OUT_ROOT="${VOXCPM_OUT_ROOT:-$SCRIPT_DIR/output/voxcpm2}"
MODE="auto"
DEVICE="auto"
CONTROL="ややゆっくり、落ち着いたプレゼン説明調"
MODEL_ID="openbmb/VoxCPM2"
NARRATION_HEADING="読み上げ本文"
CFG_VALUE="2.0"
TIMESTEPS="10"
MAX_CHARS="120"
SEED="42"
SECTION=""
LIMIT=""
DRY_RUN=0
NO_CONTROL=0
NO_REFERENCE=0
OPTIMIZE=0
NORMALIZE=1
EXTRA_ARGS=()
INPUT_SET=0
REFERENCE_SET=0

usage() {
  cat <<'EOF'
Usage: ./generate_speech.zsh [options]

uv-managed portable batch for local VoxCPM2 narration TTS.

Default inputs (first match wins):
  script   : workspace/script.ssml|script.md  or  ./script.ssml|./script.md
  reference: workspace/source.{ogg,wav,mp3,...}  or  ./source.*

Setup:
  cp examples/script.md workspace/script.md
  # or SSML: cp examples/script.ssml workspace/script.ssml
  cp /path/to/voice.wav workspace/source.wav

Options:
  --setup                 Run `uv sync` and exit
  --dry-run               Extract/split only (no model load)
  --input PATH            Input file
  --mode NAME             auto|markdown|plain|lines|ssml (default: auto)
  --narration-heading H   Markdown ### heading (default: 読み上げ本文)
  --out-root DIR          Output root (default: ./output/voxcpm2)
  --reference PATH        Reference voice
  --no-reference          Voice design only (no cloning)
  --device NAME           auto|cpu|mps|cuda (default: auto)
  --optimize              Enable torch.compile
  --cfg VALUE             CFG scale (default: 2.0)
  --timesteps N           Diffusion steps (default: 10)
  --max-chars N           Max chars per segment (default: 120)
  --control TEXT          Style / voice-design control
  --no-control            Disable control prefix
  --model-id ID           HF model id (default: openbmb/VoxCPM2)
  --section N             Only one section/slide (1-based)
  --slide N               Alias of --section
  --limit N               Only first N segments
  --seed N                Random seed (default: 42)
  --no-normalize          Disable text normalization
  -h, --help              Show help

Environment overrides:
  VOXCPM_INPUT, VOXCPM_REFERENCE, VOXCPM_OUT_ROOT

Also:
  uv sync
  uv run voxcpm-narrate synthesize --input workspace/script.md --reference workspace/source.wav
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --setup) uv sync; echo "==> Setup complete."; exit 0 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --input|--script) INPUT="$2"; INPUT_SET=1; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    --narration-heading) NARRATION_HEADING="$2"; shift 2 ;;
    --out|--out-root) OUT_ROOT="$2"; shift 2 ;;
    --device) DEVICE="$2"; shift 2 ;;
    --optimize) OPTIMIZE=1; shift ;;
    --cfg) CFG_VALUE="$2"; shift 2 ;;
    --timesteps) TIMESTEPS="$2"; shift 2 ;;
    --max-chars) MAX_CHARS="$2"; shift 2 ;;
    --control) CONTROL="$2"; shift 2 ;;
    --no-control) NO_CONTROL=1; shift ;;
    --reference) REFERENCE="$2"; REFERENCE_SET=1; shift 2 ;;
    --no-reference) NO_REFERENCE=1; shift ;;
    --model-id) MODEL_ID="$2"; shift 2 ;;
    --section|--slide) SECTION="$2"; shift 2 ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --seed) SEED="$2"; shift 2 ;;
    --no-normalize) NORMALIZE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    --)
      shift
      EXTRA_ARGS+=("$@")
      break
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if (( ! INPUT_SET )); then
  if ! INPUT="$(resolve_default_input)"; then
    echo "input script not found." >&2
    echo "Create one with:" >&2
    echo "  cp examples/script.md workspace/script.md" >&2
    echo "  # or: cp examples/script.ssml workspace/script.ssml" >&2
    echo "Or pass --input PATH" >&2
    exit 1
  fi
fi

if [[ ! -f "$INPUT" ]]; then
  echo "input not found: $INPUT" >&2
  exit 1
fi

if (( ! DRY_RUN )) && (( ! NO_REFERENCE )); then
  if (( ! REFERENCE_SET )); then
    if ! REFERENCE="$(resolve_default_reference)"; then
      echo "reference voice not found." >&2
      echo "Place a file at workspace/source.wav (or .ogg/.mp3), or pass --reference PATH," >&2
      echo "or use --no-reference for voice design only." >&2
      exit 1
    fi
  elif [[ ! -f "$REFERENCE" ]]; then
    echo "reference voice not found: $REFERENCE" >&2
    exit 1
  fi
fi

echo "==> Ensuring dependencies with uv sync"
uv sync

ARGS=(
  synthesize
  --input "$INPUT"
  --mode "$MODE"
  --narration-heading "$NARRATION_HEADING"
  --out-root "$OUT_ROOT"
  --model-id "$MODEL_ID"
  --device "$DEVICE"
  --cfg-value "$CFG_VALUE"
  --inference-timesteps "$TIMESTEPS"
  --max-chars "$MAX_CHARS"
  --seed "$SEED"
)

if (( OPTIMIZE )); then
  ARGS+=(--optimize)
else
  ARGS+=(--no-optimize)
fi

if (( NORMALIZE )); then
  ARGS+=(--normalize)
else
  ARGS+=(--no-normalize)
fi

if (( NO_CONTROL )); then
  ARGS+=(--no-control)
else
  ARGS+=(--control "$CONTROL")
fi

if (( ! DRY_RUN )) && (( ! NO_REFERENCE )); then
  ARGS+=(--reference "$REFERENCE")
fi

if [[ -n "$SECTION" ]]; then
  ARGS+=(--section "$SECTION")
fi

if [[ -n "$LIMIT" ]]; then
  ARGS+=(--limit "$LIMIT")
fi

if (( DRY_RUN )); then
  ARGS+=(--dry-run)
fi

ARGS+=("${EXTRA_ARGS[@]}")

echo "==> input     : $INPUT"
echo "==> mode      : $MODE"
if (( NO_REFERENCE )) || (( DRY_RUN )); then
  echo "==> reference : (none)"
else
  echo "==> reference : $REFERENCE"
fi
echo "==> out-root  : $OUT_ROOT"
echo "==> device    : $DEVICE"
echo "==> model     : $MODEL_ID"
echo

uv run voxcpm-narrate "${ARGS[@]}"
