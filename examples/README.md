# Examples

Tracked templates for portable use. Your real scripts and reference voices
belong in `workspace/` (gitignored).

All example text is fictional and intentionally generic. Do not place real
production scripts, personal information, customer data, or recorded voices in
this directory.

| File | Description |
|------|-------------|
| `script.md` | Markdown template with `### 読み上げ本文` blocks |
| `script.ssml` | SSML talk-script template (`<break>` / `<prosody>` / …) |
| `reference_recording.md` | ~20s script to record a reference voice |
| `reference_recording_alt.md` | Alternate short reference-recording script |

## Setup

```zsh
cp examples/script.md workspace/script.md
# or: cp examples/script.ssml workspace/script.ssml

# Record examples/reference_recording.md, then:
cp /path/to/voice.wav workspace/source.wav

./generate_speech.zsh --dry-run
```

## Explicit paths

```zsh
./generate_speech.zsh \
  --input examples/script.ssml \
  --reference /path/to/voice.wav \
  --dry-run
```
