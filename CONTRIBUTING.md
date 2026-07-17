# Contributing to lab-executor

このプロジェクトは AI エージェント実験自動化評価基盤として、外部
contributor からの **definition pack** / **benchmark task** /
**bug fix** / **docs** の貢献を歓迎します。

> v1.0 で MCP API は凍結済 (詳細は `docs/v1_stability_policy.md`)。
> 既存 Stable tool の名称・引数変更は v1.x 内で **行いません**。

## 1. Definition pack を作る (推奨手順)

v1.7 で authoring CLI が揃っています。以下の流れで誰でも作れます。

```bash
# 1. scaffold (空 directory に雛形生成)
lab-executor extension init my_pack --template instrument_pack \
    --id com.example.my_pack --author "Acme"

# 2. instrument 等を編集

# 3. doctor で「次に何を直せば良いか」をまとめて確認
lab-executor extension doctor my_pack/extension.yaml --strict

# 4. package preview (zip を作らない)
lab-executor extension package my_pack/extension.yaml --dry-run

# 5. strict で package 化
lab-executor extension package my_pack/extension.yaml --strict

# 6. install して動作確認
lab-executor extension install dist/com.example.my_pack-0.1.0.lab-executor-ext.zip
lab-executor extension check com.example.my_pack
```

詳細は [`docs/extension_authoring.md`](docs/extension_authoring.md)。

### 1.5 Instrument definition の追加 (v1.8)

pack に instrument YAML を 1 件足す場合の最短手順:

```bash
# 単体 YAML の試作
lab-executor instrument scaffold power_supply \
    --output instruments/kikusui_pmx.yaml \
    --manufacturer "Kikusui" --model "PMX-A"
lab-executor validate instrument instruments/kikusui_pmx.yaml

# pack に登録 (scaffold + extension.yaml 更新 + registry index)
lab-executor extension add-instrument my_pack \
    --id kikusui_pmx --category power_supply \
    --manufacturer "Kikusui" --model "PMX-A" --dry-run
lab-executor extension add-instrument my_pack \
    --id kikusui_pmx --category power_supply \
    --manufacturer "Kikusui" --model "PMX-A"
```

instrument PR checklist:

- [ ] `instrument scaffold` または等価の手動雛形から始める
- [ ] `metadata.manual_ref` に manual の **題名 / リビジョン / page range**
      を明記
- [ ] `safety` / `safe_shutdown` / `state_query` / `verify` を確認
- [ ] `lab-executor validate instrument <file>` が通る
- [ ] pack に組み込んだ後 `lab-executor extension doctor --strict` が通る
- [ ] `support_level` を実態に合わせて設定
      (LLM 生成のみは draft 必須)
- [ ] `verified` の場合は `metadata.validation_evidence` を埋める
      (tested_by / tested_at / interface / firmware / tested_items)

詳細は [`docs/instrument_authoring.md`](docs/instrument_authoring.md)。

## 2. Definition pack PR の checklist

registry / 公開向け pack は **以下すべて** が望ましいです。CI で
strict gate を通すための最低ラインです。

- [ ] `lab-executor extension doctor <ext.yaml> --strict` が通る
      (`ready_for_registry_review: true`)
- [ ] `lab-executor extension package <ext.yaml> --strict` が成功
- [ ] `lab-executor extension verify-package dist/<...>.zip` が成功
- [ ] `executable_code: false` (v1.x ポリシー、変更不可)
- [ ] `README.md` が pack 内に含まれる
- [ ] `catalog.summary` / `catalog.license` を埋めている
- [ ] `catalog.safety_notes` を 1 件以上書いている (実機系の場合は必須)
- [ ] `support_level` が draft より上の場合、根拠 (`validation_evidence`
      or PR description) を明示
- [ ] `support_level: verified` の instrument には
      `metadata.validation_evidence` を必須
- [ ] `tests/` ローカル test を回しても回帰なし
      (`pytest -k "not hardware_integration"`)

詳細 checklist は
[`docs/extension_publishing_checklist.md`](docs/extension_publishing_checklist.md)。

## 3. Bug fix / feature PR の流れ

1. issue を立てる (再現手順 / 期待動作 / 環境)。新規 MCP tool は
   慎重に判断するため、まず discussion を推奨。
2. branch を切る (`fix/<id>` / `feat/<id>`)。**issue 番号が無い依頼なら
   `fix/<短い説明>` で構わない** (英小文字ケバブケース。例:
   `fix/worker-terminate-race`)。maintainer からの ID 指定を待つ必要はない。
3. 必要なら関連 docs を更新。`docs/` 配下を touch する PR は歓迎。
4. tests を回す (`python -m pytest -k "not hardware_integration"`)。
   ハードウェア依存テストは local hardware が無いと skip されるが、
   CI で network/visa を必要としないテストはすべて pass する必要あり。
5. CHANGELOG.md の **`## Unreleased`** に entry を追加する
   (無ければ `# 変更履歴` の直下に新設してよい)。
   **version は bump しない** — maintainer がリリース時に `## Unreleased` を
   `## vX.Y.Z — <タイトル>` へ書き換え、合言葉を添え、version を bump する。
   合言葉はリリース単位の要約なので maintainer が付ける。

### 3.1 作業ディレクトリ: 別クローンではなく worktree

正規リポジトリは 1 つだけとし、**コードをコピーした別クローンで作業しない**。
別クローンは履歴が分岐し、古いチェックアウトを最新と誤認する事故につながる。

並行作業が必要なら `git worktree` を使う。別ディレクトリだが**コピーではなく**、
同一リポジトリの履歴・ブランチ・オブジェクトを共有する。

```bash
# 作業用 worktree を用意する (一度だけ)
git worktree add --detach ../lab-executor-codex main

# 作業のたびに「場所は変えず」ブランチを切る
cd ../lab-executor-codex
git switch -c fix/<id> main

# 不要になったら
git worktree remove ../lab-executor-codex
```

- 同一ブランチを複数の worktree で同時に checkout できない仕様が衝突防止になる
- worktree でのコミットは主リポジトリから fetch なしで即座に見える

**import の注意**: `pip install -e .` は主リポジトリを指すため、worktree 内で
`python -c "import lab_executor"` すると **主リポジトリのコード**を読んでしまう。
`python -m pytest` は root の `conftest.py` がその worktree の `src/` を
`sys.path` 先頭へ挿すので正しく解決される。pytest 以外で worktree のコードを
動かすときは `PYTHONPATH=<worktree>/src` を明示する。

### 3.2 AI エージェント (Claude / Codex 等) へ依頼するとき

- cwd は**正規リポジトリか、その worktree** を明示的に指定する。新しい clone を
  作らせない (指定を曖昧にすると古い別チェックアウトを掴むことがある)
- 着手前に `git switch -c fix/<id>` でブランチを切らせ、`main` へ直接コミット
  させない。統合は maintainer が `--no-ff` で行う
- **version は bump させない** (上記 5 のとおり maintainer の役割)。CHANGELOG は
  `## Unreleased` への entry 追加を依頼してよい
- レビューや修正を別エージェントへ委ねた場合も、**別環境で追検証する**。
  タイミング依存の欠陥は速いマシンでは緑のまま通り抜けることがある

## 4. ポリシー

- **新規 MCP tool 追加は最小限**。CLI 化で済む機能は CLI に閉じる
  (例: extension install / catalog / doctor)。
- `executable_code: true` および remote install / git pull は **v1.x で
  対応しない**。改めて議論が必要 (`docs/v1_stability_policy.md`)。
- error_class / response_envelope は v1.0 で凍結済。新規 error_class の
  追加は OK、既存の意味変更は NG。
- raw VISA 操作は v1.0 から `VISA_MCP_ALLOW_RAW=1` 環境変数 gate。

## 5. CoC / データ取り扱いポリシー

### 機材 / 安全

- 「実機未検証」は明示する。`support_level: draft` / `safety_notes` を
  きちんと書く。実機で高電圧 / 高電流 / 化学薬品が関わる場合は
  `catalog.safety_notes` に **必須** で記述する。
- LLM 生成 instrument YAML は **`support_level: draft`** で出すこと
  (validation_evidence が無いので)。`tested` / `verified` への昇格は
  実機検証の証拠が必要。

### 計測器マニュアル / SCPI 表 / proprietary 情報

- 機器メーカー manual / datasheet の **転記** は出典 (型番 / リビジョン /
  page) を `instrument.yaml` の `metadata.manual_ref` または PR description
  に明示する。
- メーカーが NDA / 非公開とする SCPI 拡張、internal command、
  service mode 等は **公開 PR に含めない**。private fork で管理。
- 製品 firmware 内部のバグ workaround を含む場合は、`safety_notes` に
  「特定 firmware version でのみ動作」と明示。

### 認証情報 / raw data

- instrument YAML / extension.yaml に **API key / password / IP /
  MAC / Serial 等の個人 / 拠点固有情報を含めない**。テンプレート化して
  `_system.yaml` 側で解決する設計を維持する。
- benchmark task / experiment plan / bundle に実測 raw data を含める
  場合は、被験者識別子 / 内部 project code 等を除去する。

### LLM-generated content

- `description` / `safety_notes` を LLM で生成する場合は、人間が必ず
  目視確認する。誤った安全表記 / 過信を招く記述 / 機種違いの混入を
  チェックする。
- `support_level=verified` の判定は **LLM のみで行わない**。実機 evidence
  (firmware / interface / tested_items) を `validation_evidence` に明示。

## 6. 参考

- 設計方針: [`docs/v1_stability_policy.md`](docs/v1_stability_policy.md)
- error taxonomy: [`docs/error_taxonomy.md`](docs/error_taxonomy.md)
- compatibility: [`docs/compatibility.md`](docs/compatibility.md)

質問・提案は GitHub Discussions / Issues へ。
