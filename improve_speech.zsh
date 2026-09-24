#!/usr/bin/env zsh
# improve_speech.zsh — intonation self-improvement harness for an existing run
#
# Examples:
#   ./improve_speech.zsh
#   ./improve_speech.zsh --asr --max-rounds 4
#   ./improve_speech.zsh --asr --llm-judge --llm-model openai/gpt-4o-mini
#   ./improve_speech.zsh --segment-id 03_002 --segment-id 07_002
#   ./improve_speech.zsh --run-dir output/voxcpm2/run_YYYYMMDD_HHMMSS

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return 0
  fi
  echo "uv is required. Install it from https://docs.astral.sh/uv/ and retry." >&2
  exit 1
}

ensure_uv

RUN_DIR="${VOXCPM_RUN_DIR:-$SCRIPT_DIR/output/voxcpm2/latest}"
REFERENCE="${VOXCPM_REFERENCE:-}"
DEVICE="auto"
MAX_ROUNDS="3"
THRESHOLD="0.62"
MODEL_ID="openbmb/VoxCPM2"
CONTROL="日本語、明瞭な声、自然な抑揚、会話に近いテンポ"
CFG_VALUE="2.0"
TIMESTEPS="10"
SEED="42"
LIMIT=""
SEGMENT_IDS=()
USE_ASR=0
ASR_DEVICE="cpu"
USE_LLM=0
LLM_PROVIDER="${VOXCPM_LLM_PROVIDER:-openrouter}"
LLM_BASE_URL=""
LLM_MODEL=""
LLM_API_KEY=""
ONLY_AWKWARD=1
NO_CONTROL=0
NORMALIZE=1
OPTIMIZE=0

usage() {
  cat <<'EOF'
Usage: ./improve_speech.zsh [options]

Evaluate synthesized segments for awkward intonation / pacing, regenerate
candidates, keep improvements, and rebuild full.wav.

Options:
  --run-dir DIR           Existing run (default: output/voxcpm2/latest)
  --reference PATH        Reference voice (default: run_dir/reference.wav)
  --max-rounds N          Strategies per awkward segment (default: 3)
  --threshold F           Awkward score threshold (default: 0.62)
  --all                   Improve all segments, not only awkward ones
  --limit N               Only first N segments
  --segment-id ID         Target specific segment (repeatable)
  --asr                   Enable SenseVoice ASR + CER
  --asr-device NAME       ASR device (default: cpu)
  --llm-judge             Enable OpenRouter or Azure OpenAI LLM judge
  --llm-provider NAME     openrouter|azure (default: VOXCPM_LLM_PROVIDER or openrouter)
  --llm-base-url URL      Azure resource URL or OpenRouter API URL
  --llm-model NAME        OpenRouter model ID or Azure deployment name
  --llm-api-key KEY       default: selected provider's environment variable
  --device NAME           auto|cpu|mps|cuda
  --control TEXT          Base style control for retries
  --no-control
  --cfg VALUE
  --timesteps N
  --seed N
  --model-id ID
  -h, --help

Also:
  uv run voxcpm-narrate improve --run-dir output/voxcpm2/latest --asr
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir) RUN_DIR="$2"; shift 2 ;;
    --reference) REFERENCE="$2"; shift 2 ;;
    --max-rounds) MAX_ROUNDS="$2"; shift 2 ;;
    --threshold) THRESHOLD="$2"; shift 2 ;;
    --all) ONLY_AWKWARD=0; shift ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --segment-id) SEGMENT_IDS+=("$2"); shift 2 ;;
    --asr) USE_ASR=1; shift ;;
    --asr-device) ASR_DEVICE="$2"; shift 2 ;;
    --llm-judge) USE_LLM=1; shift ;;
    --llm-provider) LLM_PROVIDER="$2"; shift 2 ;;
    --llm-base-url) LLM_BASE_URL="$2"; shift 2 ;;
    --llm-model) LLM_MODEL="$2"; shift 2 ;;
    --llm-api-key) LLM_API_KEY="$2"; shift 2 ;;
    --device) DEVICE="$2"; shift 2 ;;
    --control) CONTROL="$2"; shift 2 ;;
    --no-control) NO_CONTROL=1; shift ;;
    --cfg) CFG_VALUE="$2"; shift 2 ;;
    --timesteps) TIMESTEPS="$2"; shift 2 ;;
    --seed) SEED="$2"; shift 2 ;;
    --model-id) MODEL_ID="$2"; shift 2 ;;
    --optimize) OPTIMIZE=1; shift ;;
    --no-normalize) NORMALIZE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ ! -e "$RUN_DIR" ]]; then
  echo "run dir not found: $RUN_DIR" >&2
  exit 1
fi

echo "==> uv sync"
uv sync

ARGS=(
  improve
  --run-dir "$RUN_DIR"
  --model-id "$MODEL_ID"
  --device "$DEVICE"
  --cfg-value "$CFG_VALUE"
  --inference-timesteps "$TIMESTEPS"
  --seed "$SEED"
  --max-rounds "$MAX_ROUNDS"
  --threshold "$THRESHOLD"
)

if (( OPTIMIZE )); then ARGS+=(--optimize); else ARGS+=(--no-optimize); fi
if (( NORMALIZE )); then ARGS+=(--normalize); else ARGS+=(--no-normalize); fi
if (( NO_CONTROL )); then ARGS+=(--no-control); else ARGS+=(--control "$CONTROL"); fi
if (( ! ONLY_AWKWARD )); then ARGS+=(--all); fi
if (( USE_ASR )); then ARGS+=(--asr --asr-device "$ASR_DEVICE"); fi
if (( USE_LLM )); then
  case "$LLM_PROVIDER" in
    azure) KEY_FROM_ENV="${AZURE_OPENAI_API_KEY:-}" ;;
    openrouter) KEY_FROM_ENV="${OPENROUTER_API_KEY:-${VOXCPM_LLM_API_KEY:-}}" ;;
    *) echo "--llm-provider must be openrouter or azure" >&2; exit 1 ;;
  esac
  if [[ -z "$LLM_API_KEY" && -z "$KEY_FROM_ENV" ]]; then
    echo "The selected provider's API key (or --llm-api-key) is required with --llm-judge" >&2
    exit 1
  fi
  ARGS+=(--llm-judge --llm-provider "$LLM_PROVIDER")
  if [[ -n "$LLM_BASE_URL" ]]; then ARGS+=(--llm-base-url "$LLM_BASE_URL"); fi
  if [[ -n "$LLM_MODEL" ]]; then ARGS+=(--llm-model "$LLM_MODEL"); fi
  if [[ -n "$LLM_API_KEY" ]]; then ARGS+=(--llm-api-key "$LLM_API_KEY"); fi
fi
if [[ -n "$REFERENCE" ]]; then ARGS+=(--reference "$REFERENCE"); fi
if [[ -n "$LIMIT" ]]; then ARGS+=(--limit "$LIMIT"); fi
for sid in "${SEGMENT_IDS[@]:-}"; do
  [[ -n "$sid" ]] && ARGS+=(--segment-id "$sid")
done

echo "==> run-dir : $RUN_DIR"
echo "==> asr     : $USE_ASR"
echo "==> llm     : $USE_LLM"
echo

uv run voxcpm-narrate "${ARGS[@]}"
