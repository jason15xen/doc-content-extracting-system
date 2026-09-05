# RAG ドキュメント取り込み・検索 API

[English](README.md) | 日本語

ドキュメントを取り込み、テキストを抽出（スキャン PDF ページは OCR 対応）し、Azure OpenAI でチャンク化・埋め込みを行い、Azure AI Search にインデックスして、LLM が生成する回答付きのハイブリッド RAG 検索を提供する FastAPI サービスです。複数の論点を含む質問はエージェント型検索レイヤーが処理します。LLM プランナーが質問を並列のサブクエリに分解し、リクエストごとの検索回数上限のもとで実行します。

## アーキテクチャ

```
  アップロード      バックグラウンドパイプライン        クエリ
  ────────────      ──────────────────────────        ──────

  POST /doc
       │
       ▼
  一時ファイルへ ──► テキスト抽出 ──► チャンク化(tiktoken) ──► 埋め込み(Azure OpenAI)
  ストリーム保存      (12形式,          800トークン/100重複     text-embedding-3-small
  + SHA-256ハッシュ    OCRフォールバック)                          │
       │                                                          ▼
       ▼                                                    Azure AI Search
  SQLite (./db/rag.db)                                      (ハイブリッド: ベクトル + BM25
  (documents, tasks, task_files,                             + セマンティックリランカー)
   datasets)                                                      ▲
                                                       POST /query│ 1〜5回の検索
                                                                  │
                                        LLMプランナー ── 単一意図 → ハイブリッド検索1回
                                             │
                                             └─ 複数意図 → 並列サブクエリ3本
                                                          → ジャッジ → 追加検索≤2回
                                                                  │
                                                                  ▼
                                                       上位5ドキュメント・上位12チャンク
                                                                  │
                                                                  ▼
                                                       チャットモデル (Azure OpenAI)
                                                                  │
                                                                  ▼
                                                       { answer, files, token_usage }
```

## 対応ドキュメント形式

| カテゴリ | 拡張子 |
|---|---|
| OpenXML | `.docx`, `.xlsx`, `.pptx`, `.docm`, `.xlsm`, `.pptm` |
| レガシーバイナリ | `.doc`, `.xls`, `.ppt`（LibreOffice 経由） |
| その他 | `.pdf`, `.txt`, `.md` |

スキャンされた PDF ページは自動検出され、上限付きプロセスプール内の Tesseract で OCR されます（`OCR_ENABLED`、「設定」参照）。`OCR_REJECT_SCANNED=true` の場合、OCR せずにスキャンページを含むドキュメント全体を拒否します。テキストのみのドキュメントを先に高速でインデックスしたい場合に有効です。

## クイックスタート

### 1. 設定

```bash
cp .env.example .env      # 開発時は .env.dev も利用可
```

必須項目を設定します:

| 変数 | 取得場所 |
|---|---|
| `AZURE_SEARCH_ENDPOINT` | Azure Portal > Search サービス > 概要 |
| `AZURE_SEARCH_API_KEY` | Azure Portal > Search サービス > キー（管理者キー） |
| `AZURE_OPENAI_ENDPOINT` | Azure Portal > OpenAI リソース > キーとエンドポイント |
| `AZURE_OPENAI_API_KEY` | 同上 |
| `AZURE_OPENAI_DEPLOYMENT` | Azure OpenAI Studio > デプロイ（チャットモデル） |
| `AZURE_OPENAI_EMBEDDING_ENDPOINT` | 埋め込みリソースのエンドポイント（別リソースの場合） |
| `AZURE_OPENAI_EMBEDDING_API_KEY` | 埋め込みリソースのキー（別リソースの場合） |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | 埋め込みデプロイ名 |

チャットと埋め込みが同一の Azure OpenAI リソースを共有する場合、`AZURE_OPENAI_EMBEDDING_ENDPOINT` と `AZURE_OPENAI_EMBEDDING_API_KEY` は空欄のままで構いません。アプリはチャット側のエンドポイント/キーにフォールバックします。

### 2. 起動

```bash
docker compose up --build
```

以下が起動します:
- **API**（ポート 8889）-- 起動時に Alembic マイグレーションを実行する FastAPI。メタデータはローカル SQLite ファイル `./db/rag.db` に保存されます（コンテナ内 `/srv/db/rag.db` にバインドマウント）。

### 3. 利用

Swagger UI: `http://localhost:8889/docs`

## API エンドポイント

### ヘルスチェック・情報

| メソッド | パス | 説明 |
|---|---|---|
| `GET` | `/health` | `{"status": "ok"}` |
| `GET` | `/supported` | 受け付ける拡張子の一覧 |

### ドキュメント

| メソッド | パス | 説明 |
|---|---|---|
| `POST` | `/doc` | ファイルアップロード（multipart）。クライアント指定のドキュメント ID を含む `items` JSON フォームフィールド必須。任意で `dataset`・`description` フォームフィールド。202 でバッチの `task_id` + `status_url` を返却。 |
| `GET` | `/doc` | ドキュメント一覧。クエリパラメータ: `limit`, `offset`, `dataset`。 |
| `DELETE` | `/doc` | 指定ドキュメントの削除。ボディ: `{"doc_ids": [...]}`。DB と Azure AI Search から削除。202 + task_id を返却。 |
| `DELETE` | `/doc/all` | 全ドキュメントを DB と Azure AI Search から削除。202 + task_id を返却。 |

ファイルごとのアップロード結果（sample-api 準拠のセマンティクス）: 変更のない再アップロード（同一 ID・ファイル名・内容）は**スキップ**、既存 ID への内容変更ありの再アップロードはメタデータを保持したままその場で**更新**、非対応形式・サイズ超過・保存/コミットエラーは理由付きで `failed_files` に報告されます。同じドキュメントに対するアクティブなタスクと競合するバッチは **409** で拒否されます。

#### アップロードレスポンス (202)

```json
{
  "task_id": "uuid",
  "message": "2 files accepted, 1 skipped",
  "status_url": "/doc/status/uuid",
  "total_files": 3,
  "skipped_files": [{"filename": "same.pdf", "docId": "doc-1", "reason": "unchanged (same ID, filename, and content)"}],
  "failed_files": [],
  "updated_files": ["changed.pdf"],
  "dataset": null
}
```

### タスク（処理モニター）

| メソッド | パス | 説明 |
|---|---|---|
| `GET` | `/doc/status/{task_id}` | バッチタスクの状態とファイルごとの進捗（`files[]`: status, current_step, actionType, reason/error）。 |
| `GET` | `/doc/tasks` | タスク一覧。クエリパラメータ: `status_filter`, `limit`。 |
| `POST` | `/doc/tasks/{task_id}/cancel` | 待機中/実行中タスクのキャンセル。 |
| `DELETE` | `/doc/tasks` | 完了済みタスクレコードの削除。 |

タスクは 1 つの**バッチ**（アップロードまたは削除）を表し、カウンター（`total_files`, `processed_files`, `failed_file_count`, `skipped_file_count`）と、各ファイルのパイプライン進捗を追跡する `files[]` 配列を持ちます。タスクはバッチの完了時に完了となり、個別ファイルの失敗はタスク全体を失敗させずに `failed_files` に列挙されます。

### データセット

| メソッド | パス | 説明 |
|---|---|---|
| `POST` | `/datasets` | データセット作成。ボディ: `{"name": "...", "description": "..."}`。 |
| `GET` | `/datasets` | 全データセットの一覧。 |
| `GET` | `/datasets/{id}` | データセット単体の取得。 |
| `GET` | `/datasets/{id}/documents` | データセット内のドキュメント一覧。 |
| `PUT` | `/datasets/{id}` | 名前・説明の更新（フォームフィールド）。 |
| `DELETE` | `/datasets/{id}` | カスケード削除: データセット + 所属ドキュメント + AI Search のチャンクを削除。202 + task_id を返却。409 アクティブタスクロックの対象。デフォルトデータセットは保護されます。 |

### クエリ（RAG 検索）

| メソッド | パス | 説明 |
|---|---|---|
| `POST` | `/query` | エージェント型ハイブリッド RAG 検索 + LLM 回答。 |

#### リクエスト

```json
{
  "query": "構造物と機械コンポーネントの耐震設計要件を比較してください。",
  "dataset": "uuid（任意 -- 省略時は全データセットを検索）",
  "use_cache": true
}
```

#### レスポンス

```json
{
  "query": "…",
  "answer": "… 出典は [doc_name#chunk_index] 形式でインライン引用 …",
  "from_cache": false,
  "files": [
    {
      "id": "doc-uuid",
      "name": "report.pdf",
      "relevance_score": 100.0,
      "dataset_id": "uuid",
      "dataset_name": "contracts"
    }
  ],
  "dataset": null,
  "token_usage": {"prompt_tokens": 9676, "completion_tokens": 663, "total_tokens": 10339}
}
```

`relevance_score` は、その結果セット内で最もマッチしたドキュメントを基準に正規化した 0〜100 のパーセンテージです（トップのドキュメントは常に 100）。`token_usage` はリクエスト全体をカバーします: クエリプランニング + ジャッジ + 回答生成。

#### エージェント型検索

各クエリはまず LLM プランナー（[app/services/query_planner.py](app/services/query_planner.py)）で分類されます:

- **単一意図**（大半の事実確認型の質問）: ちょうど **1 回**のハイブリッド検索 — 従来どおりのパスで、インデックスへの追加負荷はありません。
- **複数意図**（比較、複数の論点を含む質問）: プランナーが **3 本**の焦点を絞ったサブクエリを作成して並列実行し、続いてジャッジ呼び出しが結果を十分と判断するか、最大 **2 回**の追加検索を要求します。上限: **1 リクエストあたり 5 回のインデックス検索**。モデルの出力にかかわらずコード側で強制されます（[app/services/retrieval.py](app/services/retrieval.py)）。

全検索の結果行はチャンク ID で重複排除（高スコア優先）されてから、後述のドキュメントランキングに渡されます。エージェント層は失敗せず縮退します: プランナーのエラーやタイムアウト → 従来の単一検索、ジャッジのエラー → 1 巡目の結果で回答、サブクエリ 1 本の失敗 → 残りをそのまま利用、追加検索が全滅 → 1 巡目の結果で回答。各リクエストはプランをログに記録します: `N search(es), single_intent=…, subqueries=…, followups=…`

検索後: チャンクをドキュメント単位にグループ化し、各ドキュメントを最良チャンクのスコアで評価、上位 `SEARCH_TOP_K_DOCS` 件を残し、そのチャンクのうちスコア上位 `CHAT_MAX_CONTEXT_CHUNKS` 件を「ソースのみから回答する」指示とともにチャットモデルへ送ります。

クエリの埋め込みは優先レーンで実行されます: バルク取り込みの埋め込みバッチの後ろに並ぶことはなく、Azure の激しいスロットリング時はハングせず即座に失敗します（503）。

### テキスト抽出（レガシー）

| メソッド | パス | 説明 |
|---|---|---|
| `POST` | `/extract` | 同期テキスト抽出。DB・埋め込み・インデックスなしで即座にプレーンテキストを返却。 |

### 管理・クリーンアップ

| メソッド | パス | 説明 |
|---|---|---|
| `POST` | `/admin/cleanup/orphan-files` | DB レコードのないディスク上のファイルを削除。 |
| `POST` | `/admin/cleanup/orphan-index` | ドキュメントが DB に存在しない AI Search チャンクを削除。 |
| `GET` | `/admin/logs` | アプリケーションログファイルの一覧。 |
| `GET` | `/admin/logs/{filename}` | ログファイルのダウンロード。 |

## 取り込みパイプライン

バッチがアップロードされると、バックグラウンドタスクがファイルごとに以下のパイプラインを実行します:

1. **アップロード** -- ファイルを OS の一時ファイル（`tempfile.mkstemp`）へストリーム保存。元のバイト列が `storage/uploads/` 配下に書かれることはありません。ストリーム中に SHA-256 を計算。
2. **抽出** -- 形式に応じたエクストラクタ（docx、pdf など）でテキストを抽出。スキャン PDF ページは検出のうえ OCR（Tesseract プロセスプール）されます（OCR 無効時を除く）。
3. **チャンク化** -- テキストを 800 トークン・100 トークン重複で分割（tiktoken `cl100k_base`）。
4. **埋め込み** -- チャンクを Azure OpenAI で埋め込み（1536 次元、バッチ処理、`EMBED_MAX_INFLIGHT_BATCHES` で上限制御）。
5. **インデックス** -- チャンクをベクトル + メタデータ付きで Azure AI Search へ登録（`SEARCH_MAX_INFLIGHT_UPLOADS` で上限制御）。
6. **クリーンアップ** -- パイプラインの成否にかかわらず一時ファイルは必ず削除。

**バッチの処理順** -- 1 回のアップロード内では大きいファイルから処理します（LPT スケジューリング）。最大のドキュメントがキューの末尾で直列に残るのではなく `t=0` から処理を開始します。

**タスクとファイルの状態** -- バッチタスクはバッチの完走時に完了となります。ファイル単位の失敗はタスクの `failed_files` と `GET /doc` から確認できます。

## データベーススキーマ

SQLAlchemy + Alembic による SQLite（[app/db/models.py](app/db/models.py)）。

| テーブル | 用途 | 主なカラム |
|---|---|---|
| `datasets` | グルーピング | `id` (UUID), `name` (unique), `description`, タイムスタンプ |
| `documents` | ドキュメント 1 件につき 1 行 | `id` (TEXT, クライアント指定), `name`, `hash` (SHA-256), `description`, `dataset_id` FK, `status`, `storage_path`, `file_size`, `chunk_count`, タイムスタンプ |
| `tasks` | バッチ（アップロード/削除）1 件につき 1 行 | `id` (UUID), `action`, `status`, ファイルカウンター, `current_file`, `current_step`, `error_message`, 時刻カラム |
| `task_files` | バッチ内のファイルごとの進捗 | `task_id` FK, `filename`, `doc_id`, `status`, `current_step`, `action_type`, `reason`, `error`, `error_details` |

## Azure AI Search インデックス

単一インデックス（デフォルト `rag-documents`）、プッシュモデル。スキーマは [app/services/search_index.py](app/services/search_index.py) の `build_index()` でコードとして定義され、`ENSURE_INDEX_ON_STARTUP=true` の場合は起動時に作成/更新されます。手動 PUT 用の JSON ダンプは `python -m scripts.export_index_schema` で取得できます。

主なフィールド: `id`（チャンクキー: `{doc_id}_{chunk_idx}`）, `doc_id`, `doc_name`, `dataset_id`（フィルタ可能）, `content`（検索可能、BM25）, `content_vector`（1536 次元 HNSW コサイン、非公開）, `uploaded_at`。

各検索はハイブリッドです: ベクトル類似度 + BM25 キーワードマッチを RRF で融合し、さらに任意でセマンティックリランキング（Standard S1 以上が必要）。エージェント層は `/query` 1 リクエストにつきこの検索を 1〜5 回発行します。

## 設定

全設定は `.env` / `.env.dev` に置かれ、[app/settings.py](app/settings.py) が読み込みます。主なチューニング項目:

| 変数 | デフォルト | 説明 |
|---|---|---|
| `CHUNK_TOKENS` | 800 | チャンクあたりのトークン数 |
| `CHUNK_OVERLAP` | 100 | チャンク間の重複 |
| `EMBED_BATCH_SIZE` | 16 | 埋め込み API 呼び出し 1 回あたりのチャンク数 |
| `SEARCH_TOP_K_CHUNKS` | 30 | 従来型（単一意図）検索 1 回で取得するチャンク数 |
| `SEARCH_TOP_K_DOCS` | 5 | レスポンスで返す上位ドキュメント数 |
| `CHAT_MAX_CONTEXT_CHUNKS` | 12 | チャットモデルに渡すチャンク数の上限 |
| `AGENTIC_SEARCH_ENABLED` | true | `/query` の LLM クエリプランナー（false = 従来の単一検索） |
| `AGENTIC_MAX_SUBQUERIES` | 3 | 複数意図の質問に対する並列サブクエリ数 |
| `AGENTIC_MAX_SEARCHES` | 5 | 1 リクエストあたりのインデックス検索回数の上限（サブクエリ + 追加検索） |
| `AGENTIC_SUBQUERY_TOP_K` | 15 | サブクエリ 1 本あたりの取得チャンク数（0 = `SEARCH_TOP_K_CHUNKS`） |
| `AGENTIC_LLM_TIMEOUT_S` | 20 | プランナー/ジャッジ呼び出しのタイムアウト。超過時は従来検索に縮退 |
| `INGEST_CONCURRENCY` | 2 | バックグラウンド取り込みタスクの最大並列数 |
| `EMBED_MAX_INFLIGHT_BATCHES` | 4 | 同時埋め込みバッチの上限（Azure TPM 保護） |
| `SEARCH_MAX_INFLIGHT_UPLOADS` | 4 | 同時インデックス登録バッチの上限 |
| `OCR_ENABLED` | true | スキャン PDF ページの OCR（Tesseract） |
| `OCR_WORKERS` | 6 | OCR プロセスプールのサイズ |
| `OCR_REJECT_SCANNED` | false | OCR せずスキャンページを含むドキュメントを拒否 |
| `ENABLE_SEMANTIC_RANKING` | true | セマンティックリランカーを使用（Standard S1 以上が必要） |
| `ENSURE_INDEX_ON_STARTUP` | true | 起動時に AI Search インデックスを作成/更新 |

## プロジェクト構成

```
app/
  main.py                      アプリファクトリ、lifespan、ルーター登録
  settings.py                  pydantic-settings（.env / .env.dev を読み込み）
  errors.py                    例外クラス
  deps.py                      FastAPI 依存性注入プロバイダー
  extraction/                  ドキュメントテキスト抽出
    config.py                  対応拡張子、アップロード上限
    dispatcher.py              拡張子 -> エクストラクタのルーティング
    scan_detect.py             PDF のスキャンページ検出
    ocr.py                     Tesseract OCR（プロセスプール）
    schemas.py                 ExtractionResponse モデル
    extractors/                ファイル形式ごとのモジュール
    services/libreoffice.py    soffice サブプロセスラッパー
  db/
    base.py                    SQLAlchemy DeclarativeBase
    session.py                 非同期エンジン + セッションファクトリ
    models.py                  Document, Task, TaskFile, Dataset ORM モデル
  repositories/                データアクセス層（documents, tasks, datasets）
  schemas/                     Pydantic リクエスト/レスポンスモデル
  services/
    hashing.py                 ストリーミング SHA-256 + サイズ上限付き保存
    chunker.py                 tiktoken ベースのテキスト分割
    embeddings.py              Azure OpenAI 埋め込みクライアント（クエリ優先レーン）
    chat.py                    Azure OpenAI チャットクライアント（RAG 回答）
    query_planner.py           エージェント型検索の LLM プランナー + ジャッジ（JSON モード）
    retrieval.py               エージェント型検索のオーケストレーション（並列実行、重複排除、検索バジェット）
    search_index.py            Azure AI Search ゲートウェイ（インデックススキーマ、upsert、削除、検索）
    logging_setup.py           storage/logs/ 配下へのローテーション付きファイルロギング
    storage.py                 ローカルファイルパスヘルパー
  pipeline/
    context.py                 バックグラウンドタスク用ランタイムコンテキスト
    ingest.py                  アップロード -> 抽出 -> チャンク化 -> 埋め込み -> インデックス
    delete.py                  DB + AI Search からのドキュメント/データセット削除
  routers/
    health.py                  GET /health, GET /supported
    extract.py                 POST /extract（レガシー同期抽出）
    datasets.py                データセット CRUD + カスケード削除
    documents.py               POST/GET/DELETE /doc（アップロード、一覧、削除）
    tasks.py                   GET /doc/status/{id}, /doc/tasks, キャンセル, クリア
    query.py                   POST /query（エージェント型ハイブリッド RAG）
    admin.py                   孤児クリーンアップ + ログアクセス
migrations/                    Alembic（entrypoint 経由で起動時に自動実行）
scripts/
  entrypoint.sh                alembic upgrade head + uvicorn
  export_index_schema.py       build_index() の出力を JSON でダンプ（手動 PUT 用）
  diagnose_pdf.py              PDF 抽出/スキャン検出のデバッグヘルパー
docker-compose.yml             API（SQLite: ./db/rag.db、外部 DB サービスなし）
Dockerfile
```

## テスト

```bash
pip install -r requirements-dev.txt   # ランタイム依存 + pytest + フィクスチャ生成
python -m pytest tests/ -v
```

- `test_extract.py` -- 抽出エンドポイント（全 12 形式 + 段組 PDF）
- `test_chunker.py`, `test_chunk_key.py` -- トークンベース分割 + チャンクキーのエッジケース
- `test_hashing.py` -- ストリーミング SHA-256 + サイズ超過時の中断
- `test_upload_api.py`, `test_ingest_skip.py` -- アップロードセマンティクス（skip/update/failed、409 ロック）
- `test_embed_query.py` -- 優先埋め込みレーン（セマフォバイパス、即時失敗リトライ）
- `test_search_index.py` -- インデックススキーマとゲートウェイの挙動
- `test_search_aggregation.py` -- `/query` の上位 K ドキュメント集約 + 関連度の正規化
- `test_agentic_search.py` -- エージェント型検索: 検索バジェット（1/3/5）、重複排除、プロンプト解析、縮退パス
- `test_log_filter.py` -- アクセスログのポーリングフィルタ
