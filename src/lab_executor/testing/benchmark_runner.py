"""v0.9.0: Benchmark runner (3 layer).

Layer 1: validate_experiment_plan 相当
Layer 2: dry_run_plan 相当
Layer 3: start_experiment_job (mock instruments で実行)

LLM を呼ばずに「与えられた Plan / template が期待通り通るか」を回帰評価する。
本物の LLM ベンチは v1.0 で導入予定 (v0.9.0 では fixture / 固定 plan による
構造的検証のみ)。
"""
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

import yaml

from lab_executor.dsl.compiler import validate_and_compile
from lab_executor.dsl.template import apply_template_override
from lab_executor.job import JobManager, JobStore
from lab_executor.job.state_machine import is_terminal
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor.observation import build_run_summary, compute_job_outcome

if TYPE_CHECKING:
    # v1.11: 型ヒント目的。実体 (`InstrumentSession(...)`) は内部関数で
    # lazy import する (`_build_sessions` 内で `from
    # lab_visa_mcp.session_manager import InstrumentSession`)。
    from lab_visa_mcp.session_manager import InstrumentSession

from lab_executor.system_config import (
    SystemConfig, InstrumentBinding, ExperimentUnit,
)
from lab_executor.testing.benchmark_task import BenchmarkTask, load_benchmark_task
from lab_executor.testing.mock_instruments import (
    MockVisaManager, scenarios_from_dict,
)


# ============================================================
# Result
# ============================================================


@dataclass
class CheckResult:
    name: str
    status: str          # "passed" / "failed" / "skipped"
    message: str = ""


@dataclass
class BenchmarkResult:
    task_id: str
    status: str          # "passed" / "failed"
    scores: dict[str, float] = field(default_factory=dict)
    checks: list[CheckResult] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    tool_call_log: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "scores": dict(self.scores),
            "checks": [
                {"name": c.name, "status": c.status, "message": c.message}
                for c in self.checks
            ],
            "artifacts": dict(self.artifacts),
            "tool_call_log": list(self.tool_call_log),
        }


# ============================================================
# Fixture loading
# ============================================================


def _load_system_config_from_dict(data: dict) -> SystemConfig:
    """SystemConfig.from_yaml の path 経由を使わず dict から構築"""
    instruments = {
        k: InstrumentBinding(**v)
        for k, v in (data.get("instruments") or {}).items()
    }
    units = {}
    for name, body in (data.get("experiment_units") or {}).items():
        if isinstance(body, dict):
            units[name] = ExperimentUnit.from_raw(dict(body))
    return SystemConfig(
        instruments=instruments, experiment_units=units,
    )


def _load_instrument_definitions(
    paths: list[str], base: Path,
) -> dict[str, InstrumentDefinition]:
    out: dict[str, InstrumentDefinition] = {}
    for p in paths:
        full = (base / p) if not Path(p).is_absolute() else Path(p)
        if not full.exists():
            raise FileNotFoundError(f"instrument fixture not found: {full}")
        data = yaml.safe_load(full.read_text(encoding="utf-8")) or {}
        name = full.stem
        out[name] = InstrumentDefinition(**data)
    return out


def _build_session_manager(
    sys_cfg: SystemConfig,
    definitions: dict[str, InstrumentDefinition],
):
    """各 resource に definition を bind した SessionManager 互換オブジェクト"""
    # v1.11: lazy import (lab-executor 候補 module の top-level に
    # session_manager を載せない、KNOWN_V111_TO_RESOLVE 解消)
    from lab_visa_mcp.session_manager import InstrumentSession
    sessions: dict[str, InstrumentSession] = {}
    # bindings の resource → 同名 definition (基本的に 1:1 で対応する fixture 想定)
    for alias, binding in sys_cfg.instruments.items():
        res = binding.resource
        # definitions は file stem を key にしているので alias と一致するものを探す
        defn = definitions.get(alias) or definitions.get(res)
        if defn is None and definitions:
            # 単一 definition なら全 resource に同じものを使う (簡略 fixture 用)
            if len(definitions) == 1:
                defn = next(iter(definitions.values()))
        if defn is not None:
            sessions[res] = InstrumentSession(
                resource_name=res, idn_response="<mock>",
                idn_parsed={}, definition=defn,
            )

    class _SM:
        def get_session(self, name):
            return sessions.get(name)

    return _SM(), sessions


# ============================================================
# Runner
# ============================================================


@dataclass
class RunnerConfig:
    db_dir: Path
    benchmarks_root: Path
    artifacts_dir: Path | None = None


class BenchmarkRunner:
    """1 タスクを実行して `BenchmarkResult` を返す。

    fixtures (system_config / instruments / mock_scenarios / templates) を読み込み、
    MockVisaManager + JobManager を組み立てて Plan を validate / dry_run / execute。
    """

    def __init__(self, cfg: RunnerConfig) -> None:
        self.cfg = cfg

    async def run(self, task: BenchmarkTask) -> BenchmarkResult:
        result = BenchmarkResult(task_id=task.id, status="passed")
        try:
            if task.layer == "repair":
                await self._run_repair(task, result)
            else:
                await self._run(task, result)
        except Exception as e:
            result.checks.append(CheckResult(
                "runner_exception", "failed",
                f"{type(e).__name__}: {e}",
            ))
            result.status = "failed"
        # 集計
        if any(c.status == "failed" for c in result.checks):
            result.status = "failed"
        return result

    # ============================================================
    # v0.9.1: repair runner (2 stage)
    # ============================================================

    async def _run_repair(
        self, task: BenchmarkTask, result: BenchmarkResult,
    ) -> None:
        """Stage A: broken_plan が期待通り失敗するか
        Stage B: repaired_plan が validate / dry_run / execute を通るか
        """
        if task.broken_plan is None or task.repaired_plan is None:
            _add(result, "repair_inputs_present", False,
                 "broken_plan / repaired_plan の両方が必要")
            return
        if task.expected_failure is None:
            _add(result, "expected_failure_defined", False,
                 "expected_failure セクションが必要")
            return

        # fixture 解決 (通常 run と共通の最小セット)
        sys_cfg = SystemConfig()
        if task.fixtures.system_config:
            sc_path = self.cfg.benchmarks_root / task.fixtures.system_config
            if sc_path.exists():
                raw = yaml.safe_load(sc_path.read_text(encoding="utf-8")) or {}
                sys_cfg = _load_system_config_from_dict(raw)
        defns = _load_instrument_definitions(
            task.fixtures.instruments, self.cfg.benchmarks_root,
        )
        sm, _sessions = _build_session_manager(sys_cfg, defns)

        # safety_mode override
        if task.fixtures.safety_mode:
            import os
            os.environ["VISA_MCP_SAFETY_MODE"] = task.fixtures.safety_mode

        # ---- Stage A: broken_plan ----
        result.tool_call_log.append("validate_experiment_plan")
        broken = validate_and_compile(task.broken_plan, sm, sys_cfg)
        result.artifacts["broken_plan"] = task.broken_plan
        result.artifacts["broken_validation"] = {
            "valid": broken.valid,
            "errors": broken.errors,
            "warnings": broken.warnings,
        }

        ef = task.expected_failure
        if ef.phase == "validate":
            # v0.9.1.1: error_class が明示されていない場合は「broken_plan は
            # validate 段階では valid のまま、後段 (execute/runtime) で失敗する」
            # ことを許容する (repair_006 等の partial_failure シナリオ用)
            if ef.error_class is None:
                _add(result, "broken_plan_validates_for_runtime_failure",
                     broken.valid,
                     f"valid={broken.valid} (runtime 失敗を想定)")
            else:
                # broken_plan は invalid であるべき
                _add(result, "broken_plan_fails_at_validate",
                     not broken.valid,
                     f"valid={broken.valid}")
            if ef.error_class:
                classes = [e.get("error_class") for e in broken.errors]
                _add(result, "broken_plan_has_expected_error_class",
                     ef.error_class in classes,
                     f"expected={ef.error_class} actual={classes}")
                # required_recommended_actions
                if ef.required_recommended_actions:
                    matching_errs = [
                        e for e in broken.errors
                        if e.get("error_class") == ef.error_class
                    ]
                    found_actions: set[str] = set()
                    for e in matching_errs:
                        for a in (e.get("recommended_next_actions") or []):
                            found_actions.add(a.get("action") or "")
                    missing = [
                        a for a in ef.required_recommended_actions
                        if a not in found_actions
                    ]
                    _add(result, "broken_plan_has_recommended_actions",
                         not missing,
                         f"missing={missing}" if missing else "ok")
            if ef.field_path:
                paths = [e.get("field_path") for e in broken.errors]
                _add(result, "broken_plan_has_expected_field_path",
                     ef.field_path in paths,
                     f"expected={ef.field_path} actual={paths}")
        elif ef.phase == "dry_run":
            # validate は通るが warnings がある想定
            if broken.valid:
                wclasses = [w.get("warning_class") for w in broken.warnings]
                if ef.error_class:
                    _add(result, "broken_plan_has_expected_warning_class",
                         ef.error_class in wclasses,
                         f"expected={ef.error_class} actual={wclasses}")
                else:
                    _add(result, "broken_plan_has_any_warning",
                         len(broken.warnings) > 0,
                         f"warnings={len(broken.warnings)}")
            else:
                _add(result, "broken_plan_validates",
                     False, "validate で既に失敗 (dry_run まで到達しない)")

        # ---- Stage B: repaired_plan ----
        repaired = validate_and_compile(task.repaired_plan, sm, sys_cfg)
        result.artifacts["repaired_plan"] = task.repaired_plan
        result.artifacts["repaired_validation"] = {
            "valid": repaired.valid,
            "errors": repaired.errors,
            "warnings": repaired.warnings,
        }

        _add(result, "repaired_plan_validates",
             repaired.valid,
             f"valid={repaired.valid} errors={len(repaired.errors)}")

        er = task.expected_repair or ExpectedRepair()
        if er.layer in ("dry_run", "execute"):
            _add(result, "repaired_plan_dry_run_ok",
                 repaired.valid and not repaired.errors,
                 f"valid={repaired.valid} errors={len(repaired.errors)}")

        # must_not チェック (例: use_raw_command / override_safety)
        if er.must_not:
            forbidden_found: list[str] = []
            for tool in er.must_not:
                # plan dict 全体に該当文字列が含まれないことを軽く check
                import json as _j
                blob = _j.dumps(task.repaired_plan, ensure_ascii=False)
                if tool in blob:
                    forbidden_found.append(tool)
            _add(result, "repaired_plan_must_not",
                 not forbidden_found,
                 f"forbidden_found={forbidden_found}"
                 if forbidden_found else "ok")

        # tool call log の体裁を整える
        result.tool_call_log.append("dry_run_plan")

    async def _run(
        self, task: BenchmarkTask, result: BenchmarkResult,
    ) -> None:
        # v0.9.0.1: random seed (mock の noise 等を再現可能に)
        if task.fixtures.random_seed is not None:
            import random
            random.seed(task.fixtures.random_seed)
        # v0.9.0.1: 安全モードを task ごとに override 可能に
        if task.fixtures.safety_mode:
            import os
            os.environ["VISA_MCP_SAFETY_MODE"] = task.fixtures.safety_mode
        # ---- fixture 解決 ----
        sys_cfg = SystemConfig()
        if task.fixtures.system_config:
            sc_path = self.cfg.benchmarks_root / task.fixtures.system_config
            if sc_path.exists():
                raw = yaml.safe_load(sc_path.read_text(encoding="utf-8")) or {}
                sys_cfg = _load_system_config_from_dict(raw)
        defns = _load_instrument_definitions(
            task.fixtures.instruments, self.cfg.benchmarks_root,
        )
        sm, sessions = _build_session_manager(sys_cfg, defns)

        # ---- mock VisaManager ----
        mock_visa = MockVisaManager()
        if task.fixtures.mock_scenarios:
            for res, scs in scenarios_from_dict(
                task.fixtures.mock_scenarios,
            ).items():
                mock_visa.register(res, *scs)

        # ---- JobManager ----
        store = JobStore(db_path=self.cfg.db_dir / f"{task.id}.sqlite")
        try:
            mgr = JobManager(
                mock_visa, sm, store=store, system_config=sys_cfg,
            )

            # ---- templates 事前登録 ----
            for tpl in task.fixtures.templates:
                store.save_experiment_template(
                    name=tpl["name"],
                    dsl_version=tpl.get("dsl_version", "0.8"),
                    plan=tpl["plan"],
                    description=tpl.get("description", ""),
                )

            # ---- plan 解決 (直接 plan / template + override) ----
            plan: dict[str, Any]
            if task.input.plan is not None:
                plan = task.input.plan
            elif task.input.template_name:
                tpl_rec = store.get_experiment_template(task.input.template_name)
                if tpl_rec is None:
                    result.checks.append(CheckResult(
                        "template_loaded", "failed",
                        f"template {task.input.template_name!r} not found",
                    ))
                    return
                plan, _summary = apply_template_override(
                    tpl_rec["plan"], task.input.template_override or {},
                )
            else:
                result.checks.append(CheckResult(
                    "input_present", "failed",
                    "input.plan も input.template_name も指定なし",
                ))
                return

            result.artifacts["plan"] = plan

            # ---- Layer 1: validate ----
            compiled = validate_and_compile(plan, sm, sys_cfg)
            result.tool_call_log.append("validate_experiment_plan")
            result.artifacts["summary"] = compiled.summary
            result.artifacts["warnings"] = compiled.warnings
            result.artifacts["errors"] = compiled.errors
            sc = task.expected.success_criteria
            if sc.validation_status == "ok":
                _add(result, "validation_status_ok",
                     compiled.valid, f"valid={compiled.valid}")
            elif sc.validation_status == "error":
                _add(result, "validation_status_error",
                     not compiled.valid,
                     f"valid={compiled.valid}")

            # plan features check
            _check_plan_features(result, plan, compiled, task.expected.plan_features)

            if not compiled.valid:
                # validate-only layer の場合はここで終了 OK
                if task.layer == "validate":
                    return
                # 他 layer はそもそも実行できないので終了
                return

            if task.layer == "validate":
                return

            # ---- Layer 2: dry-run ----
            result.tool_call_log.append("dry_run_plan")
            result.artifacts["rendered_steps"] = compiled.rendered_steps
            if sc.dry_run_has_no_errors is not None:
                _add(result, "dry_run_has_no_errors",
                     bool(sc.dry_run_has_no_errors) == (not compiled.errors),
                     f"errors={len(compiled.errors)}")
            if sc.dry_run_max_warnings is not None:
                _add(result, "dry_run_max_warnings",
                     len(compiled.warnings) <= sc.dry_run_max_warnings,
                     f"warnings={len(compiled.warnings)}")

            if task.layer == "dry_run":
                return

            # ---- Layer 3: execute ----
            result.tool_call_log.append("start_experiment_job")
            rec = await mgr.start_experiment_job(
                plan_dict=plan, owner="benchmark",
                job_timeout_s=30.0,
            )
            # 完了待ち (polling)
            t0 = time.monotonic()
            while not is_terminal(rec.status):
                if time.monotonic() - t0 > 60.0:
                    _add(result, "job_terminal_within_60s", False,
                         "job did not terminate in 60s")
                    break
                await asyncio.sleep(0.05)
                rec = mgr.get(rec.job_id)
            result.tool_call_log.append("get_job_status")

            target_runs = store.list_target_runs(rec.job_id)
            steps = store.list_steps(rec.job_id)
            summary = build_run_summary(
                rec.to_dict(), steps, target_runs,
            )
            result.artifacts["job_summary"] = summary
            result.artifacts["job_status"] = rec.status.value
            result.artifacts["job_outcome"] = summary.get("job_outcome")
            result.tool_call_log.append("get_job_summary")

            if sc.job_status != "any":
                _add(result, "job_status_match",
                     rec.status.value == sc.job_status,
                     f"actual={rec.status.value} expected={sc.job_status}")
            if sc.expected_job_outcome != "any":
                outcome = summary.get("job_outcome")
                _add(result, "job_outcome_match",
                     outcome == sc.expected_job_outcome,
                     f"actual={outcome} expected={sc.expected_job_outcome}")
            if sc.expected_verify_failed_count is not None:
                vf = (summary.get("verify_summary") or {}).get("failed", 0)
                _add(result, "verify_failed_count",
                     vf == sc.expected_verify_failed_count,
                     f"verify_failed={vf}")

            # tool sequence check (required / forbidden)
            _check_tool_sequence(
                result, result.tool_call_log,
                task.expected.required_tool_sequence,
            )

            # artifacts 出力 (オプション)
            if self.cfg.artifacts_dir is not None:
                self._write_artifacts(task.id, result)
        finally:
            store.close()

    def _write_artifacts(
        self, task_id: str, result: BenchmarkResult,
    ) -> None:
        import json
        d = self.cfg.artifacts_dir / task_id
        d.mkdir(parents=True, exist_ok=True)
        for name in ("plan", "summary", "rendered_steps", "job_summary"):
            v = result.artifacts.get(name)
            if v is None:
                continue
            (d / f"{name}.json").write_text(
                json.dumps(v, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )


# ============================================================
# checks
# ============================================================


def _add(result: BenchmarkResult, name: str, ok: bool, msg: str = "") -> None:
    result.checks.append(CheckResult(
        name=name, status="passed" if ok else "failed", message=msg,
    ))


def _check_plan_features(
    result: BenchmarkResult, plan: dict, compiled, expected,
) -> None:
    feat = expected
    if feat.uses_unit is not None:
        used = bool(plan.get("unit"))
        _add(result, "uses_unit", used == feat.uses_unit,
             f"actual={used}")
    if feat.uses_sweep is not None:
        used = _plan_has_step_type(plan, "sweep")
        _add(result, "uses_sweep", used == feat.uses_sweep,
             f"actual={used}")
    if feat.uses_parallel is not None:
        used = _plan_has_step_type(plan, "parallel")
        _add(result, "uses_parallel", used == feat.uses_parallel,
             f"actual={used}")
    if feat.uses_wait_for_stable is not None:
        used = _plan_has_step_type(plan, "wait_for_stable")
        _add(result, "uses_wait_for_stable", used == feat.uses_wait_for_stable,
             f"actual={used}")
    if feat.uses_wait_for_condition is not None:
        used = _plan_has_step_type(plan, "wait_for_condition")
        _add(result, "uses_wait_for_condition",
             used == feat.uses_wait_for_condition, f"actual={used}")
    if feat.uses_safe_shutdown is not None:
        used = _plan_has_step_type(plan, "safe_shutdown")
        _add(result, "uses_safe_shutdown", used == feat.uses_safe_shutdown,
             f"actual={used}")
    if feat.uses_verify is not None:
        used = bool(compiled.summary.get("uses_verify"))
        _add(result, "uses_verify", used == feat.uses_verify,
             f"actual={used}")
    if feat.min_required_resources is not None:
        n = len(compiled.summary.get("required_resources") or [])
        _add(result, "min_required_resources",
             n >= feat.min_required_resources, f"actual={n}")
    if feat.max_required_resources is not None:
        n = len(compiled.summary.get("required_resources") or [])
        _add(result, "max_required_resources",
             n <= feat.max_required_resources, f"actual={n}")


def _plan_has_step_type(plan: dict, step_type: str) -> bool:
    def walk(steps):
        for s in steps or []:
            if isinstance(s, dict):
                t = s.get("type")
                if t == step_type:
                    return True
                if t == "sweep" and walk(s.get("body") or []):
                    return True
                if t == "parallel":
                    for branch in s.get("branches") or []:
                        if walk(branch):
                            return True
        return False

    return walk(plan.get("steps") or [])


def _check_tool_sequence(
    result: BenchmarkResult, log: list[str], expected,
) -> None:
    # required_order: 出現順を保ったまま全部含まれていればOK
    seq = list(log)
    pos = 0
    missing = []
    for tool in expected.required_order:
        try:
            i = seq.index(tool, pos)
            pos = i + 1
        except ValueError:
            missing.append(tool)
    _add(
        result, "tool_sequence_required",
        not missing,
        f"missing={missing}" if missing else "ok",
    )
    # forbidden
    forbidden_used = [t for t in expected.forbidden if t in seq]
    _add(
        result, "tool_sequence_forbidden",
        not forbidden_used,
        f"forbidden_used={forbidden_used}" if forbidden_used else "ok",
    )


# ============================================================
# CLI / module entry
# ============================================================


async def run_task_file(
    task_path: str | Path,
    benchmarks_root: str | Path,
    db_dir: str | Path,
    artifacts_dir: str | Path | None = None,
) -> BenchmarkResult:
    task = load_benchmark_task(task_path)
    cfg = RunnerConfig(
        db_dir=Path(db_dir),
        benchmarks_root=Path(benchmarks_root),
        artifacts_dir=Path(artifacts_dir) if artifacts_dir else None,
    )
    Path(db_dir).mkdir(parents=True, exist_ok=True)
    runner = BenchmarkRunner(cfg)
    return await runner.run(task)
