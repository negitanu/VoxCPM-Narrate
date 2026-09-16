# voxcpm-narrate

ローカルの [VoxCPM2](https://github.com/OpenBMB/VoxCPM) で、台本を読み上げ音声化するポータブルなバッチツールです。依存関係は **[uv](https://docs.astral.sh/uv/)** で管理します。

- 台本・参照声音・生成物は **git 管理外**（`workspace/` / `output/`）
- ツール本体だけをコピーすれば、案件ごとに再利用できる

---

## 目次

1. [ディレクトリ構成](#ディレクトリ構成)
2. [必要環境](#必要環境)
3. [クイックスタート](#クイックスタート)
4. [Web UI（Material Design）](#web-uimaterial-design)
5. [参照声音の用意](#参照声音の用意)
6. [入力形式](#入力形式)
7. [音声合成](#音声合成)
8. [自己改善ループ](#自己改善ループ)
9. [CLI / オプション一覧](#cli--オプション一覧)
10. [別プロジェクトへの持ち込み](#別プロジェクトへの持ち込み)
11. [トラブルシューティング](#トラブルシューティング)
12. [ライセンス](#ライセンス)

---

## ディレクトリ構成

```text
.
├── README.md / LICENSE
├── pyproject.toml / uv.lock / .python-version
├── generate_speech.zsh          # 合成エントリ
├── improve_speech.zsh           # 自己改善ループ
├── serve_web.zsh                # Material Design Web UI
├── src/voxcpm_narrate/          # Python パッケージ
│   ├── extract.py / ssml.py     # 台本パース
│   ├── synthesize.py            # VoxCPM2 合成
│   ├── harness/                 # 採点・再生成ループ
│   └── web/                     # FastAPI + Material UI
├── examples/
│   ├── script.md                # Markdown 台本テンプレ
│   ├── script.ssml              # SSML 台本テンプレ
│   ├── reference_recording.md   # 参照声音の録音台本（約20秒）
│   └── reference_recording_alt.md
├── workspace/                   # あなたの入力（gitignore）
│   ├── script.md / script.ssml
│   └── source.wav / source.ogg
└── output/voxcpm2/              # 生成物（gitignore）
    └── latest/ → run_YYYYMMDD_HHMMSS/
```

---

## 必要環境

### 共通（合成・改善の両方）

| 項目 | 内容 |
|------|------|
| OS | macOS / Linux（Windows は WSL 推奨） |
| Python | **3.10–3.12**（`.python-version` は 3.12） |
| パッケージ管理 | [uv](https://docs.astral.sh/uv/) |
| 音声変換 | [ffmpeg](https://ffmpeg.org/)（`.ogg` / `.mp3` 参照声音を使う場合） |
| ディスク | モデル初回ダウンロード用に **数 GB** |
| メモリ | 目安 16GB 以上（MPS/CPU は余裕があると安定） |

```zsh
# uv が無い場合
curl -LsSf https://astral.sh/uv/install.sh | sh

# 依存関係
uv sync
# または
./generate_speech.zsh --setup
```

### 自己改善ループで追加で必要になるもの

| レベル | 必要なもの | 用途 |
|--------|------------|------|
| **基本（必須）** | 合成済みの `run` ディレクトリ（`manifest.json` + `segments/*.wav`） | 採点対象 |
| **基本（必須）** | 上記の共通環境 + VoxCPM2 モデル | awkward セグメントの再生成 |
| **基本（推奨）** | 参照声音（`run_dir/reference.wav` または `--reference`） | 再生成時も声色を固定 |
| **ASR あり** | 追加インストール不要（`funasr` / SenseVoice は voxcpm 経由で利用） | 読み誤り検知（CER） |
| **ASR あり** | 初回の SenseVoice モデルダウンロード（ネット接続） | `--asr` 初回のみ |
| **LLM 判定あり** | [OpenRouter](https://openrouter.ai/) API キー | イントネーション違和感の補助判定 |
| **LLM 判定あり** | モデル ID（例: `openai/gpt-4o-mini`） | `--llm-judge` / Web UI |

> 最低限は **基本だけ** で回せます（話速・無音・エネルギーなどの音響指標）。  
> `--asr` と `--llm-judge`（OpenRouter）は精度を上げるオプションです。

---

## クイックスタート

```zsh
uv sync

# 1) 台本
cp examples/script.md workspace/script.md
# SSML なら: cp examples/script.ssml workspace/script.ssml

# 2) 参照声音（examples/reference_recording.md を約20秒録音）
cp /path/to/recording.wav workspace/source.wav

# 3) 分割確認（モデル不要）
./generate_speech.zsh --dry-run

# 4) 短い動作確認 → 全編合成
./generate_speech.zsh --limit 3
./generate_speech.zsh

# 5) 自己改善（任意）
./improve_speech.zsh --asr
```

### 既定の探索順

| 種類 | 探索順 |
|------|--------|
| 台本 | `workspace/script.ssml` → `workspace/script.md` → `./script.ssml` → `./script.md` |
| 参照声音 | `workspace/source.{ogg,wav,mp3,flac,m4a}` → `./source.*` |

環境変数で上書き可能: `VOXCPM_INPUT` / `VOXCPM_REFERENCE` / `VOXCPM_OUT_ROOT` / `VOXCPM_RUN_DIR`

---

## Web UI（Material Design）

参照声音と SSML をブラウザからアップロードして、進捗を見ながら音声を生成できます。

```zsh
./serve_web.zsh
# → http://127.0.0.1:7860
```

または:

```zsh
uv run voxcpm-narrate-web
# VOXCPM_WEB_HOST / VOXCPM_WEB_PORT / VOXCPM_WEB_OUT で変更可
```

### 画面の流れ

1. 制作名と台本（SSML / Markdown / プレーンテキスト）を入力し、必要なら参照声音を追加
2. **分割を確認**で、セグメントと各セグメント前の間を確認
3. 制作を保存し、未生成部分や選択した箇所を試聴生成
4. セグメントごとに本文・読み・話し方を編集し、候補を試聴してから採用（採用前の音声は保持）
5. 生成中にブラウザを閉じても制作は SQLite に保存。再起動後はライブラリから中断箇所を再開
6. 全体 WAV、ピークを -1 dB に揃えた WAV、制作データを含む ZIP をダウンロード

参照声音は任意です。指定した場合は制作ごとにコピー・変換して保存し、元ファイルを移動しても再生成できます。入力内容や候補を含む制作データは `VOXCPM_WEB_OUT` 以下に保存されます。

自己改善で LLM 判定を使う場合は、設定パネルで API キーとモデルを選びます（入力キーはタブのメモリ内のみ、またはサーバーの `OPENROUTER_API_KEY` を使用）。以前 localStorage に保存したキーは設定パネルから消去できます。

制作データとジョブ成果物は `output/voxcpm2/web_jobs/<job_id>/` に保存されます。制作一覧と状態は同じ場所の `productions.sqlite3` に保存されます。

### Web API の主な操作

Web UI は次の API を使用します。API キーは制作 JSON や SQLite には保存しません。

| API | 用途 |
|-----|------|
| `POST /api/preview` | 台本を解析して分割を確認 |
| `POST /api/jobs` | 制作を保存（参照声音は任意） |
| `POST /api/jobs/{id}/generate` | 未生成または選択セグメントをキューに入れる |
| `POST /api/jobs/{id}/cancel` | セグメント境界で中断 |
| `PATCH /api/jobs/{id}/segments/{segment}` | 版番号を検証して編集を保存 |
| `POST .../regenerate` / `.../adopt` | 候補を作り、試聴後に採用・元に戻す |
| `GET /api/jobs/{id}/archive` | WAV・章別音声・台本・条件を ZIP で保存 |

音声評価を OpenRouter に送る場合は、画面に示す送信内容を確認してください。「音声を聴いて改善提案」の結果は音声版に紐づけて保存し、提案の取り込み・再生成・採用を個別に操作できます。生成時の自己改善ループは、条件を満たした候補を自動採用します。

> 長い台本（20分級）はローカル推論のため数十分〜かかる場合があります。タブを閉じてもサーバ側のジョブは継続します（`./serve_web.zsh` を止めない限り）。

---

## 参照声音の用意

クローン品質は参照声音でほぼ決まります。

1. `examples/reference_recording.md` を読む（約 18〜25 秒）
2. 静かな部屋で 1 テイク録音（WAV 推奨）
3. `workspace/source.wav` に配置

```zsh
cp /path/to/recording.wav workspace/source.wav
./generate_speech.zsh
```

録音の要点:

- マイク距離 20〜30cm、普通の速さ・はっきり発音
- 大声・ささやき・極端な抑揚は避ける
- 実用尺は **5〜30秒**（短すぎ / 1分超は非推奨）

参照声音なし（Voice Design のみ）:

```zsh
./generate_speech.zsh --no-reference --control '落ち着いた女性声、ややゆっくり'
```

`.ogg` / `.mp3` は内部で 16 kHz mono WAV に変換します（`ffmpeg` 必須）。

---

## 入力形式

`--mode auto`（既定）は拡張子で判定します。`.ssml` / `.xml` → ssml、`.md` → markdown、`.txt` → plain。

### markdown

`##` セクション配下の `### 読み上げ本文` だけを読み上げます。

```markdown
## Section 1

### 読み上げ本文

こんにちは。本日は報告をします。
```

```zsh
./generate_speech.zsh --narration-heading Narration --input workspace/script.md
```

### plain / lines

```zsh
./generate_speech.zsh --mode plain --input workspace/notes.txt
./generate_speech.zsh --mode lines --input workspace/lines.txt
```

### ssml

VoxCPM2 は SSML を直接解釈しないため、実用サブセットを **セグメント + ポーズ + 話し方ヒント** へ変換します。

```zsh
cp examples/script.ssml workspace/script.ssml
./generate_speech.zsh --dry-run
```

| タグ | 扱い |
|------|------|
| `<speak>`, `<p>`, `<s>` | 構造。`<p>` / `<s>` の前後でポーズ |
| `<break time="400ms">` / `strength` | セグメント間の無音 |
| `<prosody rate="slow">` など | 話し方コントロールへ変換 |
| `<emphasis>` | 強調スタイルへ変換 |
| `<sub alias="エーアイ">AI</sub>` | alias を読み上げ |

```xml
<speak xml:lang="ja-JP">
  <p>
    導入です。<break time="500ms"/>
    <prosody rate="slow">ここはゆっくり話します。</prosody>
  </p>
</speak>
```

---

## 音声合成

### 成果物

```text
output/voxcpm2/run_YYYYMMDD_HHMMSS/
  full.wav           # 結合済み読み上げ
  segments/          # セグメント単位 WAV
  manifest.json      # 分割テキスト・ポーズ・スタイル
  segments.txt
  reference.wav      # 変換後の参照声音（ある場合）
output/voxcpm2/latest -> run_...   # 直近の成功した合成へのリンク
```

### よく使う例

```zsh
./generate_speech.zsh --dry-run
./generate_speech.zsh --limit 5 --device mps
./generate_speech.zsh --section 1
./generate_speech.zsh --max-chars 80 --cfg 1.6
./generate_speech.zsh --input workspace/script.ssml --reference workspace/source.wav
```

uv 直接:

```zsh
uv run voxcpm-narrate synthesize \
  --input workspace/script.md \
  --reference workspace/source.wav \
  --device auto
```

---

## 自己改善ループ

合成済み音声の「違和感がありそうなセグメント」を自動検出し、パラメータを変えて再生成 → スコアが上がった候補だけ置き換え → `full.wav` を再結合します。

### 前提チェックリスト

合成を一度完了したうえで、次を確認してください。

- [ ] `output/voxcpm2/latest/`（または `--run-dir`）が存在する
- [ ] その中に `manifest.json` がある
- [ ] `segments/*.wav` がある（**`--dry-run` だけの run では不可**）
- [ ] 再生成用に VoxCPM2 が動く（`uv sync` 済み）
- [ ] （推奨）`reference.wav` がある、または `--reference` を渡せる
- [ ] （任意）`--asr` を使うならネットで SenseVoice を取得できる
- [ ] （任意）`--llm-judge` を使うなら `OPENROUTER_API_KEY` が設定されている

```zsh
# 前提の確認例
ls output/voxcpm2/latest/manifest.json
ls output/voxcpm2/latest/segments | head
```

### ループの流れ

1. 各セグメントを採点  
   - **音響**: 話速（文字/秒）、前後無音、クリップ、エネルギー安定性  
   - **ASR（任意）**: SenseVoice で書き起こし → 台本との CER  
   - **LLM（任意）**: [OpenRouter](https://openrouter.ai/) 経由で台本・ASR・メトリクスから違和感を JSON 判定
2. 総合スコアが閾値未満（既定 `0.62`）を **awkward** とみなす  
3. 戦略を順に試す（最大 `--max-rounds` 回）  
   - seed 変更 / CFG 下げ / 話し方プロンプト / diffusion steps 増やす / normalize 切替 など  
4. 最良候補のスコアが上がればセグメント WAV を置換（元は `harness/originals/` に退避）  
5. 全セグメントから `full.wav` を再結合  
6. `harness/report.json` と `harness/summary.md` を出力  

### 実行例

```zsh
# 音響指標のみ（追加サーバ不要）
./improve_speech.zsh

# ASR で読み誤りも見る（推奨）
./improve_speech.zsh --asr

# ASR + OpenRouter LLM
export OPENROUTER_API_KEY=sk-or-v1-...
./improve_speech.zsh --asr --llm-judge --llm-model openai/gpt-4o-mini

# 評価だけ（再生成しない）
./improve_speech.zsh --max-rounds 0

# 特定セグメントだけ、戦略を多めに
./improve_speech.zsh --asr --segment-id 03_002 --max-rounds 4

# 別 run を指定
./improve_speech.zsh --run-dir output/voxcpm2/run_20260915_140643 --asr
```

OpenRouter のキーは `.env`（Web UI が読込）または環境変数で渡せます。Web UI では設定パネルからモデル選択もできます。

### 改善ループの成果物

```text
output/voxcpm2/latest/
  full.wav                 # 改善後に再結合
  segments/*.wav           # 置換されたものあり
  harness/
    report.json            # セグメントごとのスコア・試行履歴
    summary.md             # 一覧表
    originals/             # 置換前バックアップ
    candidates/            # 再生成した候補 WAV
```

### 改善ループの主なオプション

| オプション | 既定 | 説明 |
|------------|------|------|
| `--run-dir` | `output/voxcpm2/latest` | 対象 run |
| `--max-rounds` | `3` | awkward 1本あたりの再生成戦略数 |
| `--threshold` | `0.62` | これ未満を awkward |
| `--asr` | off | SenseVoice + CER |
| `--asr-device` | `cpu` | ASR デバイス |
| `--llm-judge` | off | OpenRouter LLM 判定 |
| `--llm-base-url` | `https://openrouter.ai/api/v1` | OpenAI 互換 endpoint |
| `--llm-model` | `openai/gpt-4o-mini` | 判定モデル（`OPENROUTER_MODEL` 可） |
| `--llm-api-key` | `OPENROUTER_API_KEY` | API キー |
| `--segment-id` | — | 対象を限定（繰り返し可） |
| `--all` | off | awkward 以外も改善対象にする |
| `--limit` | — | 先頭 N セグメントのみ |
| `--reference` | run 内の `reference.wav` | 再生成時の参照声音 |
| `--device` | `auto` | 再生成デバイス |

uv 直接:

```zsh
uv run voxcpm-narrate improve \
  --run-dir output/voxcpm2/latest \
  --asr \
  --max-rounds 3 \
  --threshold 0.62
```

---

## CLI / オプション一覧

### エントリポイント

| コマンド | 役割 |
|----------|------|
| `./generate_speech.zsh` | 合成（内部で `uv sync` + `voxcpm-narrate synthesize`） |
| `./improve_speech.zsh` | 自己改善（内部で `uv sync` + `voxcpm-narrate improve`） |
| `./serve_web.zsh` | Material Design Web UI（FastAPI） |
| `uv run voxcpm-narrate synthesize ...` | 合成を直接実行 |
| `uv run voxcpm-narrate improve ...` | 改善を直接実行 |
| `uv run voxcpm-narrate-web` | Web UI を直接起動 |

### 合成オプション（抜粋）

| オプション | 説明 |
|------------|------|
| `--input PATH` | 台本 |
| `--mode auto\|markdown\|plain\|lines\|ssml` | パーサ（既定 auto） |
| `--reference PATH` | 参照声音 |
| `--no-reference` | クローニングしない |
| `--device auto\|cpu\|mps\|cuda` | 推論デバイス |
| `--control TEXT` | 話速・雰囲気などの制御 |
| `--section N` | N 番目セクションのみ |
| `--limit N` | 先頭 N セグメントのみ |
| `--max-chars N` | 1 セグメント最大文字数（既定 120） |
| `--cfg VALUE` | CFG（既定 2.0） |
| `--timesteps N` | diffusion steps（既定 10） |
| `--seed N` | 乱数シード |
| `--dry-run` | 抽出・分割のみ（モデル不要） |
| `--setup` | `uv sync` のみ |

---

## 別プロジェクトへの持ち込み

```text
your-project/
  pyproject.toml / uv.lock / README.md / LICENSE
  generate_speech.zsh / improve_speech.zsh / serve_web.zsh
  src/voxcpm_narrate/
  examples/
  workspace/     # 案件入力（gitignore）
  output/        # 生成物（gitignore）
```

```zsh
cd your-project
uv sync
./serve_web.zsh
# または CLI:
cp examples/script.md workspace/script.md
cp /path/to/voice.wav workspace/source.wav
./generate_speech.zsh
./improve_speech.zsh --asr
```

---

## トラブルシューティング

| 症状 | 対処 |
|------|------|
| 入力が見つからない | `cp examples/script.md workspace/script.md` または `--input` |
| 参照声音がない | `workspace/source.wav` を置く / `--reference` / `--no-reference` |
| improve が「no segment wav」 | `--dry-run` の run を見ていないか確認。合成済み `latest` を指定 |
| Python バージョンエラー | 3.10–3.12 が必要。`uv` が `.python-version` の 3.12 を使用 |
| MPS で不安定 | `--device cpu` |
| 声がセグメントごとに揺れる | 同じ `--reference` と `--seed` を固定 |
| 長文でノイズ | `--max-chars 80`、`--cfg 1.6` |
| ASR 初回が遅い | SenseVoice のダウンロード中。完了後はキャッシュ利用 |
| LLM 判定が効かない | `OPENROUTER_API_KEY` / Web UI の API キーとモデル ID を確認。課金・モデル公開状態も確認 |
| 改善で置換されない | 候補スコアが元より十分に上がっていない。`--max-rounds` を増やすか `--threshold` を調整 |

---

## ライセンス

本ツールのコードは Apache-2.0 です。VoxCPM2 本体の利用条件は upstream に従ってください。

- VoxCPM: https://github.com/OpenBMB/VoxCPM
- ドキュメント: https://voxcpm.readthedocs.io/
