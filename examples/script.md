# Example narration script

Use this as a template. Copy it into your workspace:

```bash
cp examples/script.md workspace/script.md
```

In `markdown` mode, only text under `### 読み上げ本文` (configurable via
`--narration-heading`) is spoken. Change the heading name if you prefer English:

```bash
./generate_speech.zsh --narration-heading Narration --input workspace/script.md
```

## Section 1 — Introduction

### 読み上げ本文

これはサンプルの読み上げ本文です。
短い文を並べると、音声合成が安定しやすくなります。

固有名詞や英語が混ざる場合も、そのまま書いて構いません。

## Section 2 — Closing

### 読み上げ本文

以上で、サンプル読み上げを終わります。
ご清聴ありがとうございました。
