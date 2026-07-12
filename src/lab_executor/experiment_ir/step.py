"""
Experiment IR の Step 型定義 (v0.5.0 / v0.5.1)

discriminator フィールド `type` による Pydantic discriminated union。

v0.5.0:
- CommandStep
- WaitStep (単純秒待機)

v0.5.1:
- WaitUntilStep              ── 絶対 / 相対の deadline まで待つ
- WaitForConditionStep       ── 条件式が True になるまで polling
- WaitForStableStep          ── window 内の (max - min) が tolerance 以下になるまで polling

今後のバージョンで以下を追加予定:
- GroupStep / BarrierStep / StaggerStep (v0.6.x)
- SweepStep / ParallelStep / LoopStep / BranchStep (v0.8.0 DSL)
- SafeShutdownStep (v0.8.0)
"""
from __future__ import annotations
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator


class CommandStep(BaseModel):
    """
    YAML 機器定義の名前付きコマンドを 1 回実行するステップ。

    `command` は機器定義の commands.<name> を参照するキー。
    `args` の値は文字列で "$" 始まりなら式評価 (recipe parameter を変数として参照)。
    `result_as` を指定すると後続ステップから ${steps.<result_as>} で参照可能 (v0.6.0+)。

    v0.6.0 再導入:
    `instrument` は logical role 参照 ("$psu" 形式) または alias / resource 名。
    map_recipe の target 内で bindings 経由で実 resource に解決される。
    省略時は Job の主 resource (start_recipe_job の resource_name) を使う。
    """
    type: Literal["command"] = "command"
    command: str
    args: dict[str, Any] = Field(default_factory=dict)
    result_as: str | None = None
    description: str = ""
    # v0.6.0: logical instrument ref. None なら Job 主 resource を使用
    instrument: str | None = None
    # v0.6.1: step 開始の意図的な遅延 (ms)
    # Map Job の各 target が同じ command step を実行する際、target_index に応じた
    # 遅延 (target_index * stagger_ms / 1000) を入れて突入電流等を避ける。
    # 単一 Job / 通常 recipe では効果なし (target_index=0 のみ)。
    # None なら遅延なし。
    stagger_ms: int | None = None
    # v2.17.0 / v2.7: sweep 展開由来の command step にだけ付く観察用文脈。
    # DSL schema は変更せず、IR と永続化 result の追加 field として扱う。
    sweep_index: int | None = None
    sweep_param: str | None = None
    sweep_value: Any = None
    # v2.28.0 (SP-1): capture 拡張。result_as で登録する値の抽出パスと単位注記。
    # value_path 未指定時は observation と同じ寛容抽出を使う。
    value_path: str = ""
    unit: str = ""
    # v2.28.0 (SP-2): ${...} 実行時引数解決。arg 名 -> {"expr", "min", "max"}。
    # コンパイル時は解決せず deferred として保持し、ステップ実行直前に評価 + 範囲執行する。
    # min/max は ParameterDefinition.range と requires.ranges の積集合 (片側可)。
    deferred_args: dict[str, Any] = Field(default_factory=dict)

    @field_validator("stagger_ms")
    @classmethod
    def _stagger_nonneg(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError(f"stagger_ms は 0 以上である必要があります: {v}")
        if v is not None and v > 600_000:  # 10 分上限 (誤入力防止)
            raise ValueError(
                f"stagger_ms は最大 600000 (10 分) です: {v}"
            )
        return v


class WaitStep(BaseModel):
    """
    指定秒数だけ待機するステップ (v0.5.0-rc1)。
    """
    type: Literal["wait"] = "wait"
    seconds: float
    description: str = ""

    @field_validator("seconds")
    @classmethod
    def _validate_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"WaitStep.seconds は 0 以上である必要があります: {v}")
        return v


# ============================================================
# v0.5.1: Polling wait Step
# ============================================================


class WaitUntilStep(BaseModel):
    """
    指定された絶対時刻 (ISO8601) または相対秒数の deadline まで待つ (v0.5.1)。

    timestamp: ISO8601 文字列 (例: "2026-05-22T15:00:00+09:00")
    seconds_from_now: 開始時刻からの相対秒数 (timestamp と排他、どちらか一方を指定)

    cancel / job_timeout への即応は manager 側で slice ループにより実現。
    """
    type: Literal["wait_until"] = "wait_until"
    timestamp: str | None = None
    seconds_from_now: float | None = None
    description: str = ""

    @model_validator(mode="after")
    def _exactly_one(self) -> "WaitUntilStep":
        has_ts = self.timestamp is not None and self.timestamp != ""
        has_sec = self.seconds_from_now is not None
        if has_ts and has_sec:
            raise ValueError("wait_until: timestamp と seconds_from_now は排他です")
        if not has_ts and not has_sec:
            raise ValueError("wait_until: timestamp または seconds_from_now のいずれかが必須です")
        if has_sec and self.seconds_from_now < 0:  # type: ignore[operator]
            raise ValueError("wait_until.seconds_from_now は 0 以上である必要があります")
        return self


class _PollingCommon(BaseModel):
    """polling 系 Step の共通フィールドと validation"""
    instrument: str
    command: str
    args: dict[str, Any] = Field(default_factory=dict)
    interval_s: float = 1.0
    timeout_s: float = 60.0
    command_timeout_s: float | None = None  # 1 回の query に対する VISA timeout (None = command 定義値)
    value_path: str | None = None           # parsed response 内の数値フィールド名
    retry_on_error: int = 1                 # 1 polling 失敗時の即時 retry 回数
    max_consecutive_errors: int = 3         # 連続失敗許容数。超えたら step failed
    description: str = ""

    @field_validator("interval_s")
    @classmethod
    def _interval_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"interval_s は正の値である必要があります: {v}")
        return v

    @field_validator("timeout_s")
    @classmethod
    def _timeout_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"timeout_s は正の値である必要があります: {v}")
        return v

    @field_validator("retry_on_error")
    @classmethod
    def _retry_nonneg(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"retry_on_error は 0 以上である必要があります: {v}")
        return v

    @field_validator("max_consecutive_errors")
    @classmethod
    def _mce_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"max_consecutive_errors は 1 以上である必要があります: {v}")
        return v


class WaitForConditionStep(_PollingCommon):
    """
    条件式が True を返すまで定期測定するステップ (v0.5.1)。

    condition_expr: 許可される構文 (safe_eval_condition):
      - 変数 `value` (最新の measurement)
      - 数値リテラル
      - 比較演算子: < <= > >= == !=
      - 論理演算: and / or
      - abs(value - target) のような単項関数 abs()
    禁止: 属性 / 関数呼び出し全般 / import / indexing / 文字列操作 / 代入 / 内包表記

    例:
      condition_expr: "value > 80"
      condition_expr: "abs(value - 25) < 0.2"
    """
    type: Literal["wait_for_condition"] = "wait_for_condition"
    condition_expr: str

    @field_validator("condition_expr")
    @classmethod
    def _cond_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("condition_expr は空にできません")
        return v


class WaitForStableStep(_PollingCommon):
    """
    window_s 期間内の測定値が安定 (max - min <= tolerance) するまで polling (v0.5.1)。

    定義:
      max(samples_in_window) - min(samples_in_window) <= tolerance
    で stable と判定。
    最低 min_samples 点 (デフォルト 3) のサンプルが必要。

    method は v0.5.1 では "range" のみ対応。
    将来 "stddev" / "slope" / "median_range" を追加可能。
    """
    type: Literal["wait_for_stable"] = "wait_for_stable"
    tolerance: float
    window_s: float
    min_samples: int = 3
    method: Literal["range"] = "range"

    @field_validator("tolerance")
    @classmethod
    def _tol_nonneg(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"tolerance は 0 以上である必要があります: {v}")
        return v

    @field_validator("window_s")
    @classmethod
    def _window_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"window_s は正の値である必要があります: {v}")
        return v

    @field_validator("min_samples")
    @classmethod
    def _ms_positive(cls, v: int) -> int:
        if v < 2:
            raise ValueError(f"min_samples は 2 以上である必要があります: {v}")
        return v

    @model_validator(mode="after")
    def _cross_check(self) -> "WaitForStableStep":
        # window <= timeout
        if self.window_s > self.timeout_s:
            raise ValueError(
                f"window_s ({self.window_s}) は timeout_s ({self.timeout_s}) 以下である必要があります"
            )
        # interval <= window
        if self.interval_s > self.window_s:
            raise ValueError(
                f"interval_s ({self.interval_s}) は window_s ({self.window_s}) 以下である必要があります"
            )
        # 測定点数下限
        # ceil(window / interval) + 1 >= min_samples
        import math
        possible = math.ceil(self.window_s / self.interval_s) + 1
        if possible < self.min_samples:
            raise ValueError(
                f"window_s/interval_s から得られる最大サンプル数 ({possible}) が "
                f"min_samples ({self.min_samples}) に満たないため安定判定不可能です"
            )
        return self


# ============================================================
# v0.6.1: Barrier (Group/Map 同期点)
# ============================================================


class BarrierStep(BaseModel):
    """v0.6.1: Group/Map Job 内の target 間同期点。

    複数 target が同じ name の BarrierStep に到達するまで待機し、
    全 target 到達 (または failure_policy で除外された target を除いて) で
    次 step へ進む。

    重要 (実装方針):
      - **barrier 待ち中は target-level resource lock を解放する**
        (deadlock 回避: 親 Job lock があるので外部からは触られない)
      - **失敗 target は barrier 対象から自動除外** (failure_policy=continue 時)
      - barrier_key = (name, step_index) ── 同一 name でも step_index が違えば別物
      - timeout_s 必須 (無限待ち禁止)

    対応範囲 (v0.6.1 MVP):
      - same Map/Group Job 内 target 間 barrier のみ
      - quorum / nested / target-local Plan 内 barrier は未対応

    Field notes (v0.6.1.1 補足):
      - `timeout_s` は **必ず有限値を持つ**。省略時 default=60s で無限待ちは禁止。
        (旧 docstring の「必須」表現を訂正: 省略可能だが必ず有限値)
    """
    type: Literal["barrier"] = "barrier"
    name: str
    timeout_s: float = 60.0   # 省略可能だが必ず有限値 (無限待ち禁止)
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("BarrierStep.name は空にできません")
        return v

    @field_validator("timeout_s")
    @classmethod
    def _timeout_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"BarrierStep.timeout_s は正の値: {v}")
        return v


# ============================================================
# v2.28.0 (SP-1): compute (演算処理)
# ============================================================


class ComputeStep(BaseModel):
    """1 ステップ 1 代入の演算処理 (sequence_processing_spec §5.2)。

    ``set`` に指定した名前で ``vars.*`` へ代入する。``expr`` は統合式言語
    (utils/seq_expression) で評価され、steps.* / vars.* / params.* / env.* を
    参照できる。評価エラー (ゼロ除算・NaN/inf・型不整合・未定義参照) 時は
    ``on_error`` に従う (abort=step failed / safe_shutdown=安全停止後 failed)。
    """
    type: Literal["compute"] = "compute"
    set: str
    expr: str
    unit: str = ""
    on_error: Literal["abort", "safe_shutdown"] = "abort"
    description: str = ""

    @field_validator("set")
    @classmethod
    def _set_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("ComputeStep.set は空にできません")
        return v

    @field_validator("expr")
    @classmethod
    def _expr_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("ComputeStep.expr は空にできません")
        return v


# ============================================================
# v2.29.0 (SP-3): guard / branch / repeat
# ============================================================


class GuardStep(BaseModel):
    """範囲検証と安全動作 (sequence_processing_spec §5.5)。assert 相当。

    ``expr`` が偽のとき ``on_fail`` に従う:
    - ``abort``: ステップ failed で終端
    - ``safe_shutdown``: 装置の安全停止後 failed
    - ``warn``: 続行 + timeline warning (``guard_failed`` イベント)

    ``pause`` は SP-4 で追加予定 (paused 状態機械が前提のため)。
    式評価エラー (未定義参照等) は on_fail に関わらず failed (判定不能を通さない)。
    """
    type: Literal["guard"] = "guard"
    expr: str
    on_fail: Literal["abort", "safe_shutdown", "warn"] = "abort"
    message: str = ""
    description: str = ""

    @field_validator("expr")
    @classmethod
    def _expr_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("GuardStep.expr は空にできません")
        return v


class BranchCase(BaseModel):
    """branch の 1 分岐。``when=None`` は else 分岐 (最後のみ許可)。"""
    when: str | None = None
    steps: list["Step"] = Field(default_factory=list)


class BranchStep(BaseModel):
    """条件判断 (sequence_processing_spec §5.3)。

    ``cases`` を上から評価し、最初に真になった when の steps のみ実行する
    (if / elif / else 相当)。else (when=None) は省略可・最後のみ。
    採択された分岐は timeline イベント ``branch_taken`` に記録される。
    ネスト最大深さ 3 (コンパイル時に recipe_to_plan が検証)。
    """
    type: Literal["branch"] = "branch"
    cases: list[BranchCase] = Field(default_factory=list)
    description: str = ""

    @model_validator(mode="after")
    def _validate_cases(self) -> "BranchStep":
        if not self.cases:
            raise ValueError("BranchStep には 1 つ以上の case が必要です")
        for i, c in enumerate(self.cases):
            if c.when is None and i != len(self.cases) - 1:
                raise ValueError("branch の else は最後の case のみ許可されます")
        return self


class RepeatStep(BaseModel):
    """反復 (sequence_processing_spec §5.4)。

    - count 型: ``count`` 回 body を実行 (コンパイル時解決済みの int)
    - while 型: ``while_expr`` が真の間 body を実行。``max_iterations`` 必須
      (無限ループ禁止)。上限到達は「条件不成立のまま終了」として
      ``repeat_ended`` (reason=max_iterations) を記録し **failed にはしない**
      (後続 guard で扱えるようにする)

    body 内では ``env.loop_index`` (0 始まり) を参照できる。

    v2.31.0 (SP-5): ``collect`` — ``{<反復内変数>: "<array 変数名>"}``。
    各反復の capture / compute 値を蓄積し、repeat 終了時に vars.* へ
    ndarray として代入する (要素は数値のみ。while 型で 0 回なら空配列)。
    """
    type: Literal["repeat"] = "repeat"
    count: int | None = None
    while_expr: str | None = None
    max_iterations: int | None = None
    body: list["Step"] = Field(default_factory=list)
    # SP-5: {反復内変数名: 蓄積先 array 変数名}
    collect: dict[str, str] = Field(default_factory=dict)
    description: str = ""

    @model_validator(mode="after")
    def _validate_mode(self) -> "RepeatStep":
        has_count = self.count is not None
        has_while = self.while_expr is not None and self.while_expr.strip() != ""
        if has_count and has_while:
            raise ValueError("repeat: count と while は排他です")
        if not has_count and not has_while:
            raise ValueError("repeat: count または while のいずれかが必須です")
        if has_count and self.count < 1:  # type: ignore[operator]
            raise ValueError(f"repeat.count は 1 以上である必要があります: {self.count}")
        if has_while:
            if self.max_iterations is None:
                raise ValueError(
                    "repeat: while 使用時は max_iterations が必須です (無限ループ禁止)"
                )
            if self.max_iterations < 1:
                raise ValueError(
                    f"repeat.max_iterations は 1 以上である必要があります: {self.max_iterations}"
                )
        if not self.body:
            raise ValueError("repeat.body (steps) は空にできません")
        return self


# ============================================================
# v2.30.0 (SP-4): pause (人間 / AI の呼び出し)
# ============================================================


class PauseStep(BaseModel):
    """実行を一時停止し、人間 (UI) または AI (control plane / CLI) の応答を待つ
    (sequence_processing_spec §5.6)。

    - ``message``: 確認画面に表示する文字列。``${...}`` 補間可
      (SP-4 で解禁したのは表示文字列のみ。args への部分埋め込みは禁止のまま)
    - ``timeout_s``: 応答待ちの上限 (必須、既定 3600)。超過時は ``on_timeout``
    - ``on_timeout``: abort | safe_shutdown (既定 safe_shutdown — 応答が無ければ安全側)
    - ``expose``: 確認画面に表示する参照式のリスト (例 "vars.resistivity")

    状態機械上は WAITING のまま (JobStatus 8 状態は変更しない)。
    「pause 要求中」は job_pauses テーブル + timeline イベント
    ``pause_requested`` で表現し、observation 層が phase="paused" を返す。
    Job 経路のみ対応 (同期 execute_plan は AsyncStepRequiresJob で Job 化を促す)。
    """
    type: Literal["pause"] = "pause"
    message: str = ""
    timeout_s: float = 3600.0
    on_timeout: Literal["abort", "safe_shutdown"] = "safe_shutdown"
    expose: list[str] = Field(default_factory=list)
    description: str = ""

    @field_validator("timeout_s")
    @classmethod
    def _timeout_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"PauseStep.timeout_s は正の値である必要があります: {v}")
        return v


# ============================================================
# v2.32.0 (SP-6): py / dll (コード実行ステップ + ポリシーゲート)
# ============================================================


class PyStep(BaseModel):
    """Python コード実行ステップ (sequence_processing_spec §5.7)。

    - ``file`` / ``code`` は排他 (file は scripts_dir 基準で解決済み絶対 path を
      ``resolved_path`` に持つ)
    - ``inputs``: {ローカル名: 参照式}。評価値がワーカーの ``ctx`` に載る
      (加えて ctx["params"] / ctx["env"] の読み取りコピー)
    - ``outputs``: ``out`` dict のこのキー **だけ** が vars.* に取り込まれる
      (暗黙の全取り込みはしない)
    - ``sha256``: file はファイル内容 / code は全文の hash (来歴、timeline
      ``py_executed`` に記録)

    **信頼モデル (spec §6.1)**: subprocess 分離は安定性のためであり
    **サンドボックスではない**。実行可否は code_policy が制御する。
    """
    type: Literal["py"] = "py"
    file: str | None = None            # 元の相対指定 (表示・来歴用)
    code: str | None = None
    resolved_path: str = ""            # file 型のみ: 解決済み絶対 path
    sha256: str = ""
    inputs: dict[str, str] = Field(default_factory=dict)
    outputs: list[str] = Field(default_factory=list)
    timeout_s: float = 60.0
    on_error: Literal["abort", "safe_shutdown", "pause"] = "abort"
    description: str = ""

    @model_validator(mode="after")
    def _validate(self) -> "PyStep":
        has_file = bool(self.file)
        has_code = self.code is not None and self.code.strip() != ""
        if has_file == has_code:
            raise ValueError("py: file と code はどちらか一方を指定してください")
        if self.timeout_s <= 0:
            raise ValueError(
                f"py.timeout_s は正の値である必要があります: {self.timeout_s}"
            )
        return self


class DllStep(BaseModel):
    """ネイティブ DLL 呼び出しステップ (sequence_processing_spec §5.8)。

    **計算専用の位置付け**: 機器の「制御」を dll ステップで行うことは非推奨。
    制御はバックエンドとして実装し、Job・安全層・資産の管理下に置くのが正道。

    - ``argtypes`` / ``restype`` の型宣言は必須 (検証エラー)
    - ``args``: 数値リテラル、または参照式文字列 (実行時に評価。
      ``${...}`` 形式・裸の式のどちらも可)
    - ``out_args``: {引数位置: vars 名} — 書き換えバッファ (array) の回収先
    - ``result_as``: 戻り値 (数値) を steps.* へ capture
    - 専用ワーカー subprocess で呼び出し、アクセス違反はワーカー死として
      回収 (ステップ failed、ランタイムは無事)
    """
    type: Literal["dll"] = "dll"
    path: str                          # 解決済み絶対 path
    function: str
    argtypes: list[str]
    restype: str = "void"
    args: list[Any] = Field(default_factory=list)
    out_args: dict[str, str] = Field(default_factory=dict)
    result_as: str | None = None
    sha256: str = ""
    timeout_s: float = 30.0
    on_error: Literal["abort", "safe_shutdown", "pause"] = "abort"
    description: str = ""

    @model_validator(mode="after")
    def _validate(self) -> "DllStep":
        if not self.path or not self.function:
            raise ValueError("dll: path と function は必須です")
        if self.timeout_s <= 0:
            raise ValueError(
                f"dll.timeout_s は正の値である必要があります: {self.timeout_s}"
            )
        if len(self.args) != len(self.argtypes):
            raise ValueError(
                f"dll: args ({len(self.args)}) と argtypes ({len(self.argtypes)}) "
                "の数が一致しません"
            )
        return self


# discriminated union: type フィールドで自動的に正しいモデルが選ばれる
Step = Annotated[
    Union[
        CommandStep, WaitStep, WaitUntilStep,
        WaitForConditionStep, WaitForStableStep,
        BarrierStep, ComputeStep,
        GuardStep, BranchStep, RepeatStep,
        PauseStep,
        PyStep, DllStep,
    ],
    Field(discriminator="type"),
]

# 再帰参照 (BranchCase.steps / RepeatStep.body -> Step) の解決
BranchCase.model_rebuild()
BranchStep.model_rebuild()
RepeatStep.model_rebuild()
