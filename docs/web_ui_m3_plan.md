# Web UI M3 実装計画 (レシピエディタ: 検証 → dry-run → git 保存)

作成: 2026-07-04 / 計画: Claude Fable 5 / 実装・テスト: Claude Opus / 最終確認: Claude Fable 5

前提: M1 (v2.20.0) / M2 (v2.21.0) コミット済み。テスト 1806 passed / 28 skipped (実機のみ)。
M1/M2 の絶対制約のうち **(1) MCP 面・serve 経路不変 / (3) 既存ロジック再実装禁止 /
(4) 必須依存追加禁止** は M3 でも有効。**(2) は変更**: M3 で UI は初めて書き込み能力を持つが、
**書き込先は `--edit-dir` で明示されたディレクトリ内の YAML ファイルと、その git 履歴のみ**。
state DB への接続は引き続き read-only。

## M3 のゴール

`lab-executor ui --edit-dir <PATH>` で起動すると、機器定義 YAML 内のレシピを
ブラウザで編集できる。保存は「検証 → (任意で dry-run) → git commit」の流れを強制する。

## 設計判断（ウィキ構想からのスコープ調整）

- ウィキ構想の「フォームビューと YAML の双方向同期」は M3 では見送り、
  **YAML テキストエディタ + レシピ単位の dry-run パネル**に絞る
  （双方向同期は複雑さに対して価値が薄い。中核価値は検証付き保存と dry-run）
- エディタは Monaco ではなく **CodeMirror 5**（ベンダリング ~400KB で済む。
  Monaco は数 MB でオフライン同梱に過大）
- **`--edit-dir` 未指定なら編集機能は完全無効**（M1/M2 と同一の read-only UI）。
  インストール済み extension pack や builtin 定義は編集対象にしない
  （ウィキの方針「編集はローカル開発ディレクトリのみ」）

## 中核 API の再利用（再実装禁止）

| 用途 | 既存 API |
|---|---|
| YAML 全体の検証 | `lab_executor.registry.validate_instrument_file(path, strict=bool) -> ValidationReport` |
| レシピの dry-run 展開 | `lab_executor.recipe_executor.recipe_to_plan(recipe, variables, primary_resource=None) -> Plan`（`$` 式を評価し IR Step 列に展開） |
| 定義のパース | `lab_executor.models.instrument_def.InstrumentDefinition`（Pydantic） |

dry-run は「編集中の YAML 文字列 → InstrumentDefinition パース → 指定レシピ +
パラメータ値で `recipe_to_plan` → 展開された Step 列（コマンド名・解決済み引数・
wait 秒数・description）を返す」まで。Mock 実行は M3 スコープ外。

## 新規/変更ファイル

```
src/lab_executor/ui/
├── edit_store.py        # 新規: EditDirStore (edit-dir 内のファイル列挙・読み書き・git)
├── app.py               # 変更: create_app(db_path, edit_dir=None) + 編集ルート
├── views.py             # 変更: dry-run 結果のビューモデル (dryrun_view)
├── templates/
│   ├── recipes.html     # 新規: ファイル/レシピ一覧
│   ├── recipe_edit.html # 新規: エディタ画面 (CodeMirror + 検証/dry-run/保存パネル)
│   └── base.html        # 変更: ナビに「レシピ」タブ (edit 有効時のみ)
└── static/vendor/
    ├── codemirror.min.js / codemirror.min.css / cm-yaml.min.js   # 新規ベンダリング
tests/test_ui_m3.py
docs/web_ui.md           # M3 追記
```

CodeMirror 取得元 (5.65.16): cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/
（codemirror.min.js, codemirror.min.css, mode/yaml/yaml.min.js）

## edit_store.py 仕様

```python
class EditStoreError(Exception): ...      # 案内用 (パス不正・検証失敗・git 失敗)

class EditDirStore:
    def __init__(self, edit_dir: Path): ...   # 存在しなければ EditStoreError
    def list_files(self) -> list[dict]        # *.yaml / *.yml を再帰列挙 (rel path, レシピ名一覧付き)
    def read_file(self, rel: str) -> str
    def save_file(self, rel: str, content: str, message: str = "") -> dict
        # 1) _resolve(rel) で edit_dir 外へのエスケープを拒否 (resolve() 後に
        #    edit_dir.resolve() 配下であることを検証。シンボリックリンク経由も拒否)
        # 2) validate_instrument_file を一時ファイル経由で実行し、
        #    errors があれば EditStoreError (警告のみなら保存可)
        # 3) LF 改行で書き込み (CRLF が来ても LF に正規化)
        # 4) git add <file> + git commit (edit_dir が git repo でなければ
        #    初回に git init。author/committer は "lab-executor-ui" を
        #    -c user.name/-c user.email で指定。commit message は
        #    "ui: edit <rel>" + ユーザー入力 message)
        # 返り値: {"saved": True, "commit": "<hash>", "validation": {...}}
```

- 新規ファイル作成は M3 では非対応（既存ファイルの編集のみ。`read_file` が
  存在しない rel を受けたら EditStoreError）
- git 操作は `subprocess.run(["git", ...], cwd=edit_dir)`。失敗時は
  **ファイルは保存済み・commit のみ失敗**である旨を返り値で区別する

## ルート追加 (app.py)

`create_app(db_path=None, edit_dir: Path | str | None = None)`。
`edit_dir=None` なら以下のルートはすべて登録しない（既存の read-only UI と完全同一）。

| ルート | 内容 |
|---|---|
| `GET /recipes` | ファイル/レシピ一覧 HTML |
| `GET /recipes/edit/{rel:path}` | エディタ画面 HTML |
| `GET /api/edit/files` | JSON 一覧 |
| `GET /api/edit/file/{rel:path}` | JSON {content} |
| `POST /api/edit/validate` | body {rel, content} → ValidationReport JSON (保存はしない) |
| `POST /api/edit/dryrun` | body {rel, content, recipe, parameters} → 展開 Step 列 JSON。パース/式評価エラーは 422 で理由を返す |
| `POST /api/edit/save` | body {rel, content, message} → EditDirStore.save_file。検証エラーは 422 |

- POST 系は `Content-Type: application/json` のみ受け付ける
- `EditStoreError` は exception handler で JSON 422/400 に変換
- `--host` が外部の場合、編集ルートは登録拒否して警告
  （認証なしの書き込み経路を外部公開しないため。M1 の警告より一段強い措置）

## エディタ画面 (recipe_edit.html)

- 左: CodeMirror (yaml mode)。右: 操作パネル
  1. 「検証」ボタン → /api/edit/validate → errors/warnings 一覧表示
  2. レシピ選択ドロップダウン（content 内 recipes からクライアント側で抽出 or
     validate 応答に含める）+ パラメータ入力欄（型・default は定義から）→
     「dry-run」→ 展開ステップ表(コマンド / 解決済み引数 / wait)表示
  3. コミットメッセージ入力 + 「保存」（直近の検証が成功するまで disabled。
     サーバ側でも保存時に再検証するため、クライアントの状態は UX のみ）
- 実行中ジョブがこのファイル由来のレシピを使用中かは M3 では判定しない
  （「変更は次回実行から有効」の注意書きを画面に常設表示）

## CLI (cli.py)

- `ui` サブコマンドに `--edit-dir PATH` を追加。ハンドラで
  `create_app(db_path, edit_dir=...)`。edit_dir 指定 + 外部 host は起動拒否 (exit 1)

## テスト (tests/test_ui_m3.py)

fixture: tmp_path に git 無しの edit_dir を作り、mock 定義 YAML
（registry/instruments/mock の既存 mock 定義をコピーして流用可）を配置。

| テスト | 検証 |
|---|---|
| test_edit_disabled_without_dir | edit_dir なしで /recipes と /api/edit/* が 404 |
| test_list_and_read | 一覧にファイルとレシピ名、read が内容一致 |
| test_path_traversal_blocked | `../` や絶対パスの rel が 400/422 (読み書き両方) |
| test_validate_ok_and_error | 正常 YAML は ok、壊した YAML は errors 付き |
| test_dryrun_expands_expressions | `$target_v * 1.1` が数値に解決された Step 列 |
| test_dryrun_bad_params | 必須パラメータ欠落で 422 |
| test_save_creates_git_commit | 保存後 git log に commit、内容一致、LF 保存 |
| test_save_rejects_invalid | 検証エラー時 422 + ファイル未変更 |
| test_save_crlf_normalized | CRLF content を送っても LF で保存される |
| test_monitor_routes_unaffected | edit_dir ありでも / と /api/jobs が従来どおり |

## バージョン・ドキュメント

- v2.22.0 (pyproject + __init__)。CHANGELOG 既存書式でエントリ追加
- docs/web_ui.md に M3 追記（--edit-dir、保存フロー、外部 host 制限）

## スコープ外

- フォーム⇔YAML 双方向同期、新規ファイル作成、Mock 実行での dry-run、
  実行中ジョブとの競合検知、認証、M4（ジョブ操作）

## 作業手順

1. edit_store.py + テスト（path traversal / git / 検証ゲートを最初に固める）
2. API ルート + エディタ画面 + CodeMirror ベンダリング
3. CLI + docs + CHANGELOG + version bump
4. tests/test_ui_m3.py → test_ui_m1/m2 → 全スイート（0 failed / 28 skipped 維持）
5. git commit はしない（Fable 検証後にコミット）
