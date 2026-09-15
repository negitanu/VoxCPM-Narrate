# Local workspace
#
# Put your own narration inputs here. This directory is gitignored except
# for this README and .gitkeep, so projects stay reusable/portable.
#
#   workspace/script.md      — narration script (markdown)
#   workspace/script.ssml    — or SSML talk script
#   workspace/source.ogg     — reference voice (wav/ogg/mp3 also OK)
#
# Quick start:
#   cp examples/script.md workspace/script.md
#   # or: cp examples/script.ssml workspace/script.ssml
#   cp /path/to/your-voice.wav workspace/source.wav
#   ./generate_speech.zsh --dry-run
#
# Or pass paths explicitly:
#   ./generate_speech.zsh --input path/to/script.md --reference path/to/voice.wav
