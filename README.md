# voxcpm-narrate

ローカルの [VoxCPM2](https://github.com/OpenBMB/VoxCPM) で、台本を読み上げ音声化するポータブルなバッチツールです。依存関係は **[uv](https://docs.astral.sh/uv/)** で管理します。

- Markdown / プレーンテキスト / 実用 SSML から、長い台本をセグメント単位で生成
- 参照声音による音声クローニングと、参照声音なしの Voice Design に対応
- 日本語の本文を中国語・英語用の正規化に渡さず、数字・日付・単位の読みを明示的に処理
- Web UI で編集、候補比較、採用・取り消し、中断・再開、読み辞書、仕上げまで管理
- 原音を保持したまま、文間・音量・ラウドネスを調整した別 WAV を出力
- 音響指標、任意の ASR / LLM 判定を使って、違和感のあるセグメントを再生成
- 台本・参照声音・生成物は **git 管理外**（`workspace/` / `output/`）
- ツール本体だけをコピーすれば、案件ごとに再利用できる

---

## 目次

1. [ディレクトリ構成](#ディレクトリ構成)
2. [必要環境](#必要環境)
3. [クイックスタート](#クイックスタート)
4. [Web UI](#web-ui)
5. [参照声音の用意](#参照声音の用意)
6. [入力形式](#入力形式)
7. [日本語の読みと音声の仕上げ](#日本語の読みと音声の仕上げ)
8. [音声合成](#音声合成)
9. [自己改善ループ](#自己改善ループ)
10. [CLI / オプション一覧](#cli--オプション一覧)
11. [別プロジェクトへの持ち込み](#別プロジェクトへの持ち込み)
12. [トラブルシューティング](#トラブルシューティング)
13. [ライセンス](#ライセンス)

コンテナで利用する場合は [Docker / クラウドでの起動](#docker--クラウドでの起動) を参照してください。

---

## ディレクトリ構成

```text
.
├── README.md / LICENSE
├── pyproject.toml / uv.lock / .python-version
├── generate_speech.zsh          # 合成エントリ
├── improve_speech.zsh           # 自己改善ループ
├── serve_web.zsh                # Material風 Web UI
├── src/voxcpm_narrate/          # Python パッケージ
│   ├── extract.py / ssml.py     # 台本パース
│   ├── synthesize.py            # VoxCPM2 合成
│   ├── artifacts.py             # 原子的な成果物保存
│   ├── japanese.py              # 日本語の数字・単位の読み
│   ├── text_input.py            # 日本語の読み上げ入力
│   ├── pronunciation.py         # 未知語候補の抽出
│   ├── audio_quality.py         # 音量測定・仕上げ
│   ├── quality_benchmark.py     # 再現可能な A/B 比較
│   ├── harness/                 # 採点・再生成ループ
│   └── web/                     # FastAPI + 自前CSS/JavaScript
├── benchmarks/
│   └── japanese-quality.json    # 公開可能な日本語30文
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
| 音声変換・仕上げ | [ffmpeg](https://ffmpeg.org/)（`.ogg` / `.mp3` の変換、音量測定・仕上げに使用） |
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
| **LLM 判定あり** | OpenRouter または Azure OpenAI Service の API キー | イントネーション違和感の補助判定 |
| **LLM 判定あり** | OpenRouter のモデル ID、または Azure のデプロイ名 | `--llm-judge` / Web UI |

> 最低限は **基本だけ** で回せます（話速・無音・エネルギーなどの音響指標）。  
> `--asr` と `--llm-judge`（OpenRouter / Azure OpenAI）は精度を上げるオプションです。

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
| 参照声音 | `workspace/source.*` → `workspace/reference.*` → `./source.*` → `./reference.*` |

環境変数で上書き可能: `VOXCPM_INPUT` / `VOXCPM_REFERENCE` / `VOXCPM_OUT_ROOT` / `VOXCPM_RUN_DIR`

---

## Docker / クラウドでの起動

Docker Engine / Docker Desktop と Docker Compose v2 以降を使用します。
Python 3.12、ffmpeg、libsndfile と `uv.lock` の依存関係をイメージに含めるため、
ホスト側で Python や uv を用意する必要はありません。

```sh
docker compose up --build -d
docker compose logs -f narrate
# http://127.0.0.1:7860

# 停止（制作データとモデルキャッシュは保持）
docker compose down
```

既定では GPU を要求せず、UI のデバイス設定 `auto` で CPU を利用します。
Apple Silicon の Docker 内では MPS を利用できません。CPU 合成は時間がかかるため、
まず短い台本で確認してください。モデルは最初の合成・ASR 実行時にダウンロードされます。
初回はインターネット接続と数 GB 以上のモデル保存領域が必要です。
Linux 用 PyTorch の CUDA 依存も含む共通イメージのため、ビルド用にも十分な空き容量を確保してください。

### 保存先と設定

- `output` 名前付きボリューム → `/app/output`：SQLite、参照音声、生成音声、読み辞書など。
- `model-cache` 名前付きボリューム → `/home/narrate/.cache`：Hugging Face / ModelScope などのキャッシュ。
- ホストの `./workspace` → `/app/workspace`：CLI の入力や既存 run の移行元。読み取り専用です。

ホストの既存 `output/` は自動では取り込みません。既存 run を `workspace/` に置き、
次のようにボリューム内へコピーしてから Web UI の既存 run 一覧で取り込みます
（`run_YYYYMMDD_HHMMSS` は実際の run ディレクトリ名に置き換えてください）。

```sh
docker compose exec narrate cp -R /app/workspace/run_YYYYMMDD_HHMMSS /app/output/voxcpm2/
```

生成物は Web UI からダウンロードするか、`docker compose cp narrate:/app/output ./container-output` で取り出せます。
`docker compose down -v` は制作データとモデルキャッシュも削除するので、通常の停止では `-v` を付けないでください。

`.env` またはシェルの環境変数で `VOXCPM_WEB_PORT`（ホスト側ポート、既定 `7860`）、
`OPENROUTER_API_KEY`、`OPENROUTER_MODEL`、`OPENROUTER_AUDIO_MODEL`、Azure の接続設定、`HF_TOKEN` などを設定できます。
`.env` 自体はコンテナにコピーせず、Compose が明示した変数だけを実行時に渡します。
コンテナ内のポートは `7860`、制作データの保存先は `/app/output/voxcpm2/web_jobs` に固定しています。
`.dockerignore` により、ローカルの秘密情報・録音・制作データ・仮想環境をビルドコンテキストから除外します。

CLI も同じイメージで使用できます（zsh ラッパーの代わりに Python の CLI を直接実行）。

```sh
docker compose run --rm narrate voxcpm-narrate --help
```

### NVIDIA GPU

Linux の GPU ホストに、ロックされた PyTorch の CUDA ランタイムに対応する NVIDIA ドライバーと
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
を導入してから起動します。同じイメージに GPU 1 枚を割り当てます。

```sh
docker compose -f compose.yml -f compose.gpu.yml up --build -d
docker compose -f compose.yml -f compose.gpu.yml exec narrate \
  python -c "import torch; print('CUDA:', torch.version.cuda); print('available:', torch.cuda.is_available())"
```

`available: True` を確認し、Web UI で `auto` または `cuda` を選択してください。
GPU 割り当ては [Docker Compose の device reservations](https://docs.docker.com/compose/how-tos/gpu-support/) を使用します。

### クラウドでの運用

イメージ単体でも起動できます。コンテナ内では全インターフェイスで待ち受けるため、
アプリの外部接続許可 `VOXCPM_ALLOW_REMOTE=1` を明示してください。

```sh
docker build -t voxcpm-narrate:local .
docker run --rm --init -p 127.0.0.1:7860:7860 \
  -e VOXCPM_ALLOW_REMOTE=1 \
  -v narrate-output:/app/output \
  -v narrate-model-cache:/home/narrate/.cache \
  voxcpm-narrate:local
```

イメージは既定でビルド元の CPU アーキテクチャ用になります。Apple Silicon から
x86_64 のクラウド VM 向けに作る場合は、ビルドに `--platform linux/amd64` を付けてください。

Compose はホストの `127.0.0.1` にのみポートを公開します。クラウド VM でもこのまま
SSH トンネルや同一ホスト上のリバースプロキシから接続できます。
ロードバランサーなどから接続する場合は、認証・TLS・アクセス制御を用意したうえで
`VOXCPM_BIND_ADDRESS=0.0.0.0` を設定し、ファイアウォールで接続元を制限してください。
アプリ自体に認証機能はありません。Compose の `VOXCPM_ALLOW_REMOTE=1` はコンテナのネットワーク越しに
接続するための設定であり、認証を追加するものではありません。

マネージドコンテナ基盤では `/app/output` と `/home/narrate/.cache` に永続ディスクを割り当て、
実行ユーザー UID/GID `10001:10001` に書き込み権限を与えてください。
既定の名前付きボリュームはイメージ内ディレクトリの権限で初期化されますが、bind mount や
クラウドのディスクでは事前に権限の設定が必要です。
SQLite とインメモリの生成キューを使用するため、同じ保存先を共有するプロセス・レプリカは **1 個** にします。
常時稼働の CPU/GPU と十分なメモリを割り当て、合成中にスケールゼロにならない構成にしてください。
ヘルスチェックは `/api/health`（HTTP サーバーの生存確認）で、モデルのロード完了や GPU の利用可否は判定しません。

---

## Web UI

参照声音と SSML をブラウザからアップロードして、進捗を見ながら音声を生成できます。
画面は Material Design に着想を得た自前の CSS / JavaScript で構成しています。

```zsh
./serve_web.zsh
# → http://127.0.0.1:7860
```

または:

```zsh
uv run voxcpm-narrate-web
# VOXCPM_WEB_HOST / VOXCPM_WEB_PORT / VOXCPM_WEB_OUT で変更可
```

> Web UI はローカル単一ユーザー向けで、認証機能はありません。既定の `127.0.0.1` のまま使用し、インターネットへ直接公開しないでください。非 loopback アドレスへの起動は既定で拒否します。認証・TLS・アクセス制御を備えたリバースプロキシで保護した場合に限り、`VOXCPM_ALLOW_REMOTE=1` を明示して起動してください。

### 画面の流れ

1. 制作名と台本（SSML / Markdown / プレーンテキスト）を入力し、必要なら参照声音を追加
2. **分割を確認**で、セグメントと各セグメント前の間を確認
3. 制作を保存し、未生成部分や選択した箇所を試聴生成
4. セグメントごとに本文・読み・話し方を編集し、候補を試聴してから採用（採用前の音声は保持）
5. 生成中にブラウザを閉じても制作は SQLite に保存。再起動後はライブラリから中断箇所を再開
6. 原音 WAV、音量・文間を整えた WAV、制作データや測定結果を含む ZIP をダウンロード

既存の CLI run はライブラリ画面から制作として取り込み、Web 上で編集・再生成できます。

生成前に未知語候補を抽出し、読みを確定してから生成できます。Web の「この制作の読み辞書」または `uv run python -m voxcpm_narrate.pronunciation path/to/script.txt` を使います。

参照声音は任意です。指定した場合は制作ごとにコピー・変換して保存し、元ファイルを移動しても再生成できます。入力内容や候補を含む制作データは `VOXCPM_WEB_OUT` 以下に保存されます。

自己改善で LLM 判定を使う場合は、設定パネルで OpenRouter または Azure OpenAI Service と評価モデルを選びます。入力キーはタブのメモリ内のみで、サーバーの環境変数からも利用できます。以前 localStorage に保存したキーは設定パネルから消去できます。プロバイダーは制作の保存時に固定され、既存の制作は OpenRouter のままです。

Azure を使う場合はサーバーに `AZURE_OPENAI_ENDPOINT`（リソース URL）と `AZURE_OPENAI_API_KEY` を設定します。テキスト評価には `AZURE_OPENAI_TEXT_DEPLOYMENT`、音声評価には `AZURE_OPENAI_AUDIO_DEPLOYMENT` を指定します（Web UI からデプロイ名を入力することもできます）。音声評価には音声入力対応のデプロイが必要です。Azure の [v1 Chat Completions API](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle) を使用し、`api-version` は指定しません。API キーをリポジトリにコミットしないでください。

制作データとジョブ成果物は `output/voxcpm2/web_jobs/<job_id>/` に保存されます。制作一覧と状態は同じ場所の `productions.sqlite3` に保存されます。

保存後の制作名は、制作画面の「制作名を変更」から編集できます。
生成中も名前を変更でき、音声・台本・採用状態には影響しません。

### 全て再生成・ライブラリの削除

「全て再生成」は保存済みの本文・読み辞書・参照音声・話速設定で全セグメントを
新しい乱数で生成し、全て検査に合格した時点で一括採用します。
音声検査に未合格があっても残りの候補を最後まで生成し、一括採用を保留して以前の採用と書き出しを保持します。その他の失敗・中断時も以前の採用と書き出しを保持します。生成済みの候補は比較用に残り、
以前の採用音声も履歴から戻せます。再度「全て再生成」を押すと先頭からやり直します。

ライブラリの「削除」は制作をごみ箱へ移動します。「ごみ箱を表示」から復元できます。
処理中の制作は削除できません。ごみ箱の制作は通常の一覧・音声取得から除外されますが、
音声ファイルと台本は復元のため保持するので、ディスクの空き容量は増えません。
共通読み辞書や取り込み元のファイルには影響しません。
「ごみ箱を空にする」は確認した件数の制作と、その台本・参照音声・候補・書き出しを
完全削除します。復元はできません。確認後にごみ箱の内容が変わった場合は処理を止めます。
ファイル削除に失敗した制作はごみ箱に残ります。完全削除を開始した制作は復元できませんが、
アクセス権などを解決してから「ごみ箱を空にする」を再実行できます。

### 話し方を選び、参照音声を録音する

新規制作の「話し方と参照音声」で、標準・落ち着いたナレーション・明るい案内・
プレゼンテーション・やさしい語りかけを選べます。
初期設定は「日本語、明瞭な声、自然な抑揚、会話に近いテンポ」です。
「自由に指定」や話し方欄の直接編集にも対応しています。

選択した話し方に応じて録音用の例文とヒントが切り替わります。
例文を必要に応じて編集し、「録音を開始」でマイクを許可して読み上げ、
「録音を停止」で試聴してください。30秒で自動停止し、録り直しもできます。
録音した音声は「制作を保存して試聴へ」でローカルサーバーに保存されます。
保存前にページを閉じると録音は失われます。既存の音声ファイルも選択できます。

ブラウザー録音には localhost または HTTPS とマイクの使用許可が必要です。
ブラウザーの録音形式（WebM / MP4 / OGG）は ffmpeg で参照用WAVに変換します。
録音後は「録音を文字起こしする」でローカルASRを利用するか、実際に話した内容を
手動で入力します。録音を聴いて修正し、一致の確認にチェックを付けてください。
確認済みの内容は `prompt_text`、録音は `prompt_wav_path` と `reference_wav_path` の
両方へ渡します。例文そのものを未確認で生成に使うことはありません。
「録音と文字起こしの両方から話し方を再現する」を外すと、従来の声質参照になります。
過去の制作の例文は確認済み文字起こしへ自動昇格しません。

「平均話速を揃える」では、確認済みの録音または数値の目標を指定できます。
初期値は「補正しない」です。日本語の読みを軽量辞書で変換し、モーラ数と
発話時間から話速を推定します。発話時間は冒頭・末尾の無音と 0.25 秒以上の間を除いた
長さです（複数文の録音と一文ずつの生成音声を同じ基準で比べるため）。
読みが未登録の英字・略語（`AI`、`SharePoint` など）はエラーにせず、略語は
アルファベット読み、単語はカタカナ読みの目安でモーラ数を推定して測定を続けます。
推定した語は各版の「読み推定」に表示されるので、あとから読み辞書で正確にできます。
目標から3%以内なら補正を省略し、それ以外は速度倍率0.9〜1.1の範囲で
ピッチを保つ補正を行います。倍率が範囲外、推定読みを含む場合、補正ツールの
失敗時は原音を使います。補正後の残差も許容し、話速だけでは再生成・不合格にしません。
ごく短い文は測定が不安定なため補正を省略します。段落間の指定した間は維持します。
補正前の音声は各版と並ぶ `.raw.wav` に保持し、補正理由・速度・所要時間を記録します。
検査は補正後の音声に対して実行します。文章内の瞬間的な話速まで一定にはしません。

### 採用前の音声検査

新規制作の「数字・日付を日本語の読みに変換する」（初期値オン）で、数字の
読み変換を切り替えられます。以前は内部で常に有効でした。「分割を確認」には
変換後の読みも表示されます。「数字の渡し方」は新規制作で「漢数字＋助数詞を保持」を
初期選択します。`2026年8月26日`、`2026/08/26`、`2026-08-26` は
`二千二十六年八月二十六日` に変換します。「2026年に」も「二千二十六年に」となり、
数詞・助数詞・助詞の表記を保持します。VoxCPM2には品詞を直接指定する引数はなく、
表記によって数詞の解釈を助ける方式です。発音やアクセントの改善は試聴で確認してください。
「ひらがな（従来）」では `にせんにじゅうろくねんはちがつにじゅうろくにち` に変換します。
設定のない過去の制作は従来方式を保持します。手入力・辞書で指定したかなは変換しません。
既存制作は各セグメントの「数字・日付の日本語読み」で上書きし、保存後に再生成してください。
変換は読み方の上書き・辞書の適用後に行います。各音声版には実際に生成に使った読みを表示します。
すべての助数詞や型番の読みを推定する機能ではないため、特殊な読みは読み方の上書きで指定してください。

Web UI の生成・再生成では、ローカルの SenseVoice で全文と6秒区間を認識し、
原稿との大きな不一致、冒頭の余計な発話を検査します。8秒以下は全文だけを検査し、
長い音声でも2秒未満の末尾は前の区間に結合して、断片の言語誤判定を避けます。
表記とかな読みの両方で照合し、日付・数字・カタカナの表記差を許容します。
全文の文字誤り率45%超、区間の最良一致部分に対する誤り率50%超を不合格とし、
無音、不正な音声、全文の認識不能、冒頭の余計な発話も引き続き不合格です。
外国語タグだけでは不合格にせず、原稿との一致を優先します。中程度の差異や
日付・数値の文字起こしの相違は「確認事項」として試聴を促します。
これは読みの正確さを保証する判定ではなく、重大な混入を防ぎながら生成を進める基準です。
「自動改善」「ASR」の任意設定とは独立した必須検査です。
不合格の場合だけ最大2回再生成（初回を含め計3回）し、合格できない場合や検査不能の場合はその候補の採用を止め、次のセグメントへ進みます。未合格の理由は候補に残るため、生成終了後に「要確認」でまとめて確認できます。
Web生成ではモデル内部の再試行を無効にし、二重の再生成ループを避けます。
既存の採用音声は保持され、不合格候補は理由とともに試聴できます。
既存・取り込み音声も採用／全文WAV・ZIP書き出し時に検査します。
各版の「音声を再検査」では既存WAVを新基準で検査でき、音声生成は行いません。
旧基準で不合格だった候補は再検査してから採用できます。

ASRモデルは初回に読み込み（未取得ならダウンロード）、以降は再利用します。
検査結果は音声のハッシュと検査バージョン付きで保存し、同じ音声の再検査を省きます。
検査はCPU上で動き、有料APIは使用しません。長さ120秒超の単一候補は検査対象外として
不合格にするため、長いセグメントは分割してください。
認識誤りによる誤検出・見落としはあり得ます。固有名詞は読み辞書を調整してください。
読みの事前試聴と個別候補の試聴は確認用のため、この採用ゲートの対象外です。
CLIの生成・改善コマンドの動作は変更しません。

### Web API の主な操作

Web UI は次の API を使用します。API キーは制作 JSON や SQLite には保存しません。

| API | 用途 |
|-----|------|
| `POST /api/preview` | 台本を解析して分割を確認 |
| `POST /api/jobs` | 制作を保存（参照声音は任意） |
| `POST /api/jobs/{id}/generate` | 未生成または選択セグメントをキューに入れる |
| `POST /api/jobs/{id}/cancel` | セグメント境界で中断 |
| `PATCH /api/jobs/{id}/segments/{segment}` | 版番号を検証して編集を保存 |
| `PUT /api/jobs/{id}/dictionary` | 制作ごとの読み辞書を保存 |
| `POST /api/jobs/{id}/pronunciation-candidates` | 読み確認が必要な未知語候補を抽出 |
| `POST .../regenerate` / `.../adopt` | 候補を作り、試聴後に採用・元に戻す |
| `POST .../audio-judge` | 音声版に対する改善提案を取得 |
| `GET /api/jobs/{id}/download?normalize=true` | 文間・音量を整えた WAV を保存 |
| `GET /api/jobs/{id}/archive?normalize=true` | 仕上げ WAV・測定値・原音などを ZIP で保存 |
| `GET /api/legacy-runs` / `POST /api/import` | CLI run を検索して制作へ取り込む |

音声評価を OpenRouter または Azure OpenAI に送る場合は、画面に示す送信内容を確認してください。Azure の音声入力は 20 MB 以下に制限しています。「音声を聴いて改善提案」の結果は音声版に紐づけて保存し、提案の取り込み・再生成・採用を個別に操作できます。生成時の自己改善ループは、条件を満たした候補を自動採用します。

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

## 日本語の読みと音声の仕上げ

### 読み上げ入力

既定の `--normalize` は**音量の正規化ではなく、日本語の数字・日付・時刻・金額・割合を読みへ展開する指定**です。日本語の本文は VoxCPM の中国語・英語用 `TextNormalizer` に渡さず、長音、漢字、かな、句読点と、読み辞書や SSML `<sub alias>` で確定した本文を保持します。

```zsh
# 読み確認が必要な語を JSON で抽出（読みは自動で推測しません）
uv run python -m voxcpm_narrate.pronunciation workspace/script.txt

# 保存済み辞書の語を候補から除外
uv run python -m voxcpm_narrate.pronunciation workspace/script.txt \
  --dictionary workspace/dictionary.json
```

CLI 生成では、モデルへ実際に渡した文字列、入力処理バージョン、モデル revision、実行環境を `segments/*.input.json` に記録します。Web UI では候補ごとの制作データに同じ情報を保存します。

### 原音を保持した仕上げ

生成した原音は FLOAT WAV のまま変更しません。仕上げ処理は、セグメント前後の余分な無音、指定した文間、文ごとの音量差を控えめに整えた後、全体を暫定目標 **−16 LUFS / −1 dBTP 以下**に調整し、別の PCM_24 WAV と測定 JSON を作ります。入力がすでに十分大きい場合は、音量を下げない適応目標を使います。

```zsh
# 測定のみ
uv run python -m voxcpm_narrate.audio_quality \
  output/voxcpm2/latest/full.wav

# 文間・音量を仕上げ、finished.wav と finished.quality.json を作成
uv run python -m voxcpm_narrate.audio_quality \
  output/voxcpm2/latest/full.wav \
  --manifest output/voxcpm2/latest/manifest.json \
  --segments-dir output/voxcpm2/latest/segments \
  --output output/voxcpm2/latest/finished.wav
```

無音、極端に小さい音声、3秒未満の音声は過剰増幅せずエラーにします。目標値は本アプリの試聴用設定であり、汎用の納品規格ではありません。

### 品質の A/B 比較

`benchmarks/japanese-quality.json` の30文と固定 seed を使い、従来の入力正規化と現在の日本語入力を比較できます。既定ではモデルをダウンロードせず、ローカルキャッシュだけを使用します。

```zsh
# モデルをロードせず、変換後の入力文字列だけを比較
uv run python -m voxcpm_narrate.quality_benchmark \
  benchmarks/japanese-quality.json output/quality-input --prepare-only

# 3文・seed 42 の小規模な実音声比較
uv run python -m voxcpm_narrate.quality_benchmark \
  benchmarks/japanese-quality.json output/quality-pilot --limit 3 --seeds 42
```

生成条件、入力差分、原音、RMS を合わせたブラインド試聴用 A/B、音響測定値を保存します。機械測定から主観評価点を作ることはありません。入力処理や音量調整だけでイントネーション改善が実証されたとは扱わず、実音声のブラインド試聴で評価してください。

---

## 音声合成

### 成果物

```text
output/voxcpm2/run_YYYYMMDD_HHMMSS/
  full.wav           # 結合済み読み上げ
  segments/          # セグメント単位の原音 FLOAT WAV
    *.input.json     # モデルへ渡した入力と実行情報
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
- [ ] （任意）`--llm-judge` を使うなら、選択したプロバイダーの API キーとモデル / デプロイ名が設定されている

```zsh
# 前提の確認例
ls output/voxcpm2/latest/manifest.json
ls output/voxcpm2/latest/segments | head
```

### ループの流れ

1. 各セグメントを採点  
   - **音響**: 話速（文字/秒）、前後無音、クリップ、エネルギー安定性  
   - **ASR（任意）**: SenseVoice で書き起こし → 台本との CER  
   - **LLM（任意）**: OpenRouter または Azure OpenAI 経由で台本・ASR・メトリクスから違和感を JSON 判定
2. 総合スコアが閾値未満（既定 `0.62`）を **awkward** とみなす  
3. 戦略を順に試す（最大 `--max-rounds` 回）  
   - seed 変更 / CFG 下げ / 話し方プロンプト / diffusion steps 増やす など。日本語の入力は読みを保持し、中国語・英語用のテキスト正規化には渡しません。
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
export OPENROUTER_API_KEY='your-key-here'
./improve_speech.zsh --asr --llm-judge --llm-model openai/gpt-4o-mini

# ASR + Azure OpenAI LLM（デプロイ名は Azure 上で作成した名前）
export AZURE_OPENAI_ENDPOINT='https://your-resource.openai.azure.com/'
export AZURE_OPENAI_API_KEY='your-key-here'
export AZURE_OPENAI_TEXT_DEPLOYMENT='narration-text'
./improve_speech.zsh --asr --llm-judge --llm-provider azure

# 評価だけ（再生成しない）
./improve_speech.zsh --max-rounds 0

# 特定セグメントだけ、戦略を多めに
./improve_speech.zsh --asr --segment-id 03_002 --max-rounds 4

# 別 run を指定
./improve_speech.zsh --run-dir output/voxcpm2/run_YYYYMMDD_HHMMSS --asr
```

API キーは `.env`（Web UI が読込）または環境変数で渡せます。CLI の既定プロバイダーは `VOXCPM_LLM_PROVIDER` で切り替えられます。Web UI では設定パネルからプロバイダーとモデル / デプロイ名を選択できます。

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
| `--llm-judge` | off | OpenRouter / Azure OpenAI LLM 判定 |
| `--llm-provider` | `openrouter` | `azure` も指定可（`VOXCPM_LLM_PROVIDER` で変更可） |
| `--llm-base-url` | プロバイダーの環境変数 | OpenRouter API URL または Azure リソース URL |
| `--llm-model` | プロバイダーの環境変数 | OpenRouter モデル ID または Azure テキスト用デプロイ名 |
| `--llm-api-key` | プロバイダーの環境変数 | `OPENROUTER_API_KEY` または `AZURE_OPENAI_API_KEY` |
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
| `./serve_web.zsh` | Material風 Web UI（FastAPI） |
| `uv run voxcpm-narrate synthesize ...` | 合成を直接実行 |
| `uv run voxcpm-narrate improve ...` | 改善を直接実行 |
| `uv run voxcpm-narrate-web` | Web UI を直接起動 |
| `uv run python -m voxcpm_narrate.pronunciation ...` | 未知語候補を抽出 |
| `uv run python -m voxcpm_narrate.audio_quality ...` | WAV を測定・仕上げ |
| `uv run python -m voxcpm_narrate.quality_benchmark ...` | 日本語入力を A/B 比較 |

### 合成オプション（抜粋）

| オプション | 説明 |
|------------|------|
| `--input PATH` | 台本 |
| `--mode auto\|markdown\|plain\|lines\|ssml` | パーサ（既定 auto） |
| `--reference PATH` | 参照声音 |
| `--no-reference` | クローニングしない（`generate_speech.zsh` のみ） |
| `--out-root DIR` | run の出力ルート |
| `--model-id ID` | Hugging Face のモデル ID |
| `--device auto\|cpu\|mps\|cuda` | 推論デバイス |
| `--control TEXT` | 話速・雰囲気などの制御 |
| `--no-control` | 話し方の制御文を付けない |
| `--section N` | N 番目セクションのみ |
| `--limit N` | 先頭 N セグメントのみ |
| `--max-chars N` | 1 セグメント最大文字数（既定 120） |
| `--cfg VALUE` | CFG（既定 2.0） |
| `--timesteps N` | diffusion steps（既定 10） |
| `--seed N` | 乱数シード |
| `--no-normalize` | 日本語の数字・日付・単位を読みへ展開しない |
| `--optimize` | `torch.compile` を有効化（主に CUDA 向け） |
| `--dry-run` | 抽出・分割のみ（モデル不要） |
| `--setup` | `uv sync` のみ |

上表の `--cfg` と `--timesteps` はシェルスクリプト用です。`uv run voxcpm-narrate synthesize` / `improve` を直接使う場合は、それぞれ `--cfg-value` と `--inference-timesteps` を指定します。直接実行では参照声音を省略するだけで Voice Design になり、出力先を固定する `--output-dir` も利用できます。全オプションは各コマンドの `--help` で確認できます。

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
| LLM 判定が効かない | 選択したプロバイダーの API キーとモデル ID / デプロイ名を確認。Azure は `AZURE_OPENAI_ENDPOINT` とデプロイの対応 API も確認 |
| 改善で置換されない | 候補スコアが元より十分に上がっていない。`--max-rounds` を増やすか `--threshold` を調整 |
| 仕上げ WAV を作れない | `ffmpeg` を確認。無音・極小音量・3秒未満は安全のため仕上げ対象外 |
| 数字の読みを変えたくない | `--no-normalize`。固有名詞は Web の読み辞書または SSML `<sub alias>` で指定 |
| Web UI を外部公開したい | 認証なしのため直接公開しない。保護済みリバースプロキシ配下でのみ `VOXCPM_ALLOW_REMOTE=1` を使用 |

---

## ライセンス

本ツールのコードは Apache-2.0 です。VoxCPM2 本体の利用条件は upstream に従ってください。

- VoxCPM: https://github.com/OpenBMB/VoxCPM
- ドキュメント: https://voxcpm.readthedocs.io/
