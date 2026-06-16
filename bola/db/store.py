import json
from datetime import datetime
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from bola.db.models import (
    AggregatedReportRecord,
    AnalysisItemResult,
    AnalysisRecord,
    Base,
    ExecutionRecord,
    RelationCache,
    RelationGroupResult,
    RunRecord,
    SnapshotRecord,
    StrategyMemoryRecord,
)


class Store:
    """Repository over the SQLite database — the single read/write seam for all persisted state.

    Wraps every table (relation cache + checkpoints, snapshots, analysis passes, the execution log,
    strategy memory, run records) behind plain methods that take/return plain dicts and JSON, so the
    rest of the tool never touches SQLAlchemy. Append-only tables back the resumable runners.
    """

    def __init__(self, db_path: str = "bola.db"):
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        self._session = sessionmaker(engine)

    # ------------------------------------------------------------------
    # Relation cache
    # ------------------------------------------------------------------

    def get_relations(self, spec_hash: str, model_name: str) -> list | None:
        """The cached consolidated relations for a (spec, model), or None if not yet detected."""
        with self._session() as s:
            row = s.scalar(
                select(RelationCache).where(
                    RelationCache.spec_hash == spec_hash,
                    RelationCache.model_name == model_name,
                )
            )
            return json.loads(row.relations_json) if row else None

    def save_relations(
        self, spec_hash: str, model_name: str, relations: list,
        tokens: dict | None = None, duration_s: float | None = None,
    ) -> None:
        """Upsert the consolidated relation cache for a (spec, model), with the step's token + time cost."""
        tokens_json = json.dumps(tokens) if tokens is not None else None
        with self._session() as s:
            row = s.scalar(
                select(RelationCache).where(
                    RelationCache.spec_hash == spec_hash,
                    RelationCache.model_name == model_name,
                )
            )
            if row:
                row.relations_json = json.dumps(relations)
                row.tokens_json = tokens_json
                row.duration_s = duration_s
            else:
                s.add(RelationCache(
                    spec_hash=spec_hash,
                    model_name=model_name,
                    relations_json=json.dumps(relations),
                    tokens_json=tokens_json,
                    duration_s=duration_s,
                ))
            s.commit()

    def get_relation_tokens(self, spec_hash: str, model_name: str) -> dict | None:
        """The token cost of detecting a (spec, model)'s relations, or None if absent."""
        return (self.get_relation_usage(spec_hash, model_name) or {}).get("tokens")

    def get_relation_usage(self, spec_hash: str, model_name: str) -> dict | None:
        """The detection step's `{tokens, duration_s}` for a (spec, model), or None if absent."""
        with self._session() as s:
            row = s.scalar(
                select(RelationCache).where(
                    RelationCache.spec_hash == spec_hash,
                    RelationCache.model_name == model_name,
                )
            )
            if row is None:
                return None
            return {
                "tokens": json.loads(row.tokens_json) if row.tokens_json else None,
                "duration_s": row.duration_s,
            }

    # ------------------------------------------------------------------
    # Relation group checkpoints (resumable detection)
    # ------------------------------------------------------------------

    def get_group_results(self, spec_hash: str, model_name: str) -> dict[tuple[str, int], dict]:
        """All checkpointed units for a (spec, model), keyed by (source_key, group_index)."""
        with self._session() as s:
            rows = s.scalars(
                select(RelationGroupResult).where(
                    RelationGroupResult.spec_hash == spec_hash,
                    RelationGroupResult.model_name == model_name,
                )
            )
            return {
                (r.source_key, r.group_index): {
                    "status": r.status,
                    "result": json.loads(r.result_json) if r.result_json else None,
                    "error": r.error,
                }
                for r in rows
            }

    def save_group_result(
        self,
        spec_hash: str,
        model_name: str,
        source_key: str,
        group_index: int,
        status: str,
        result: list | None = None,
        error: str | None = None,
    ) -> None:
        """Upsert one relation-detection unit checkpoint (`done`/`failed`) for resume."""
        with self._session() as s:
            row = s.scalar(
                select(RelationGroupResult).where(
                    RelationGroupResult.spec_hash == spec_hash,
                    RelationGroupResult.model_name == model_name,
                    RelationGroupResult.source_key == source_key,
                    RelationGroupResult.group_index == group_index,
                )
            )
            result_json = json.dumps(result) if result is not None else None
            if row:
                row.status = status
                row.result_json = result_json
                row.error = error
            else:
                s.add(RelationGroupResult(
                    spec_hash=spec_hash,
                    model_name=model_name,
                    source_key=source_key,
                    group_index=group_index,
                    status=status,
                    result_json=result_json,
                    error=error,
                ))
            s.commit()

    # ------------------------------------------------------------------
    # Snapshots (resumable capture — frozen evidence for analysis)
    # ------------------------------------------------------------------

    def save_snapshot(
        self,
        run_id: str,
        phase: str,
        op_key: str,
        object_key: str,
        status: str,
        *,
        request: dict | None = None,
        status_code: int | None = None,
        response: Any = None,
        error: str | None = None,
    ) -> None:
        """Upsert one captured object snapshot for a `(run_id, phase, object_key)` — frozen evidence."""
        with self._session() as s:
            row = s.scalar(
                select(SnapshotRecord).where(
                    SnapshotRecord.run_id == run_id,
                    SnapshotRecord.phase == phase,
                    SnapshotRecord.object_key == object_key,
                )
            )
            fields = dict(
                op_key=op_key,
                status=status,
                request_json=json.dumps(request or {}),
                status_code=status_code,
                response_json=json.dumps(response) if response is not None else None,
                error=error,
            )
            if row:
                for k, v in fields.items():
                    setattr(row, k, v)
            else:
                s.add(SnapshotRecord(
                    run_id=run_id, phase=phase, object_key=object_key, **fields
                ))
            s.commit()

    def get_snapshots(self, run_id: str) -> list[dict]:
        """All captured snapshots for a run, newest-irrelevant order, as plain dicts."""
        with self._session() as s:
            rows = s.scalars(
                select(SnapshotRecord).where(SnapshotRecord.run_id == run_id)
            )
            return [
                {
                    "phase": r.phase,
                    "op_key": r.op_key,
                    "object_key": r.object_key,
                    "status": r.status,
                    "request": json.loads(r.request_json) if r.request_json else {},
                    "status_code": r.status_code,
                    "response": json.loads(r.response_json) if r.response_json else None,
                    "error": r.error,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]

    # ------------------------------------------------------------------
    # Analysis passes (resumable, re-runnable over the same snapshots)
    # ------------------------------------------------------------------

    def start_analysis(
        self, analysis_id: str, run_id: str, model_name: str, access_desc_hash: str, config: dict
    ) -> None:
        """Create the `running` record for a new analysis pass (no-op if it already exists)."""
        with self._session() as s:
            row = s.get(AnalysisRecord, analysis_id)
            if row is None:
                s.add(AnalysisRecord(
                    analysis_id=analysis_id,
                    run_id=run_id,
                    model_name=model_name,
                    access_desc_hash=access_desc_hash,
                    config_json=json.dumps(config),
                    status="running",
                ))
                s.commit()

    def get_analysis_items(self, analysis_id: str) -> dict[str, dict]:
        """Checkpointed per-object verdicts for a pass, keyed by object_key."""
        with self._session() as s:
            rows = s.scalars(
                select(AnalysisItemResult).where(
                    AnalysisItemResult.analysis_id == analysis_id
                )
            )
            return {
                r.object_key: {
                    "status": r.status,
                    "verdict": json.loads(r.verdict_json) if r.verdict_json else None,
                    "error": r.error,
                }
                for r in rows
            }

    def save_analysis_item(
        self,
        analysis_id: str,
        object_key: str,
        status: str,
        verdict: dict | None = None,
        error: str | None = None,
    ) -> None:
        """Upsert one per-object verdict checkpoint within an analysis pass (for resume)."""
        with self._session() as s:
            row = s.scalar(
                select(AnalysisItemResult).where(
                    AnalysisItemResult.analysis_id == analysis_id,
                    AnalysisItemResult.object_key == object_key,
                )
            )
            verdict_json = json.dumps(verdict) if verdict is not None else None
            if row:
                row.status = status
                row.verdict_json = verdict_json
                row.error = error
            else:
                s.add(AnalysisItemResult(
                    analysis_id=analysis_id,
                    object_key=object_key,
                    status=status,
                    verdict_json=verdict_json,
                    error=error,
                ))
            s.commit()

    def finish_analysis(
        self, analysis_id: str, findings: list,
        tokens: dict | None = None, duration_s: float | None = None,
    ) -> None:
        """Mark an analysis pass `completed` and store its findings + the step's token + time cost."""
        with self._session() as s:
            row = s.get(AnalysisRecord, analysis_id)
            if row is None:
                return
            row.findings_json = json.dumps(findings)
            if tokens is not None:
                row.tokens_json = json.dumps(tokens)
            if duration_s is not None:
                row.duration_s = duration_s
            row.status = "completed"
            s.commit()

    def save_analysis_metrics(self, analysis_id: str, metrics: dict) -> None:
        """Attach the computed metrics document to an analysis pass."""
        with self._session() as s:
            row = s.get(AnalysisRecord, analysis_id)
            if row is None:
                return
            row.metrics_json = json.dumps(metrics)
            s.commit()

    def get_analysis(self, analysis_id: str) -> dict | None:
        """One analysis pass as a plain dict (config/findings/metrics/status), or None if unknown."""
        with self._session() as s:
            row = s.get(AnalysisRecord, analysis_id)
            if row is None:
                return None
            return {
                "analysis_id": row.analysis_id,
                "run_id": row.run_id,
                "model_name": row.model_name,
                "access_desc_hash": row.access_desc_hash,
                "config": json.loads(row.config_json) if row.config_json else {},
                "findings": json.loads(row.findings_json) if row.findings_json else None,
                "metrics": json.loads(row.metrics_json) if row.metrics_json else None,
                "tokens": json.loads(row.tokens_json) if row.tokens_json else None,
                "duration_s": row.duration_s,
                "status": row.status,
            }

    def list_analyses(self, run_id: str) -> list[dict]:
        """All analysis passes for a run (newest first), as summary dicts."""
        with self._session() as s:
            rows = s.scalars(
                select(AnalysisRecord)
                .where(AnalysisRecord.run_id == run_id)
                .order_by(AnalysisRecord.created_at.desc())
            )
            return [
                {
                    "analysis_id": r.analysis_id,
                    "run_id": r.run_id,
                    "model_name": r.model_name,
                    "access_desc_hash": r.access_desc_hash,
                    "status": r.status,
                }
                for r in rows
            ]

    # ------------------------------------------------------------------
    # Execution log (append-only source of truth; FieldRepo is replayed from it)
    # ------------------------------------------------------------------

    def append_execution(
        self,
        run_id: str,
        seq: int,
        phase: str,
        op_key: str,
        *,
        identity: str = "regular",
        turn_index: int = 0,
        rationale: str = "",
        request: dict | None = None,
        media_type: str | None = None,
        status_code: int | None = None,
        ok: bool = False,
        response: Any = None,
        error: str | None = None,
    ) -> None:
        """Append one executed action to the log — the run's append-only source of truth."""
        with self._session() as s:
            s.add(ExecutionRecord(
                run_id=run_id,
                seq=seq,
                phase=phase,
                identity=identity,
                turn_index=turn_index,
                op_key=op_key,
                rationale=rationale,
                request_json=json.dumps(request or {}),
                media_type=media_type,
                status_code=status_code,
                ok=ok,
                response_json=json.dumps(response) if response is not None else None,
                error=error,
            ))
            s.commit()

    def get_executions(self, run_id: str) -> list[dict]:
        """All logged actions for a run, ordered by seq, as plain dicts."""
        with self._session() as s:
            rows = s.scalars(
                select(ExecutionRecord)
                .where(ExecutionRecord.run_id == run_id)
                .order_by(ExecutionRecord.seq)
            )
            return [
                {
                    "seq": r.seq,
                    "phase": r.phase,
                    "identity": r.identity,
                    "turn_index": r.turn_index,
                    "op_key": r.op_key,
                    "rationale": r.rationale,
                    "request": json.loads(r.request_json) if r.request_json else {},
                    "media_type": r.media_type,
                    "status_code": r.status_code,
                    "ok": r.ok,
                    "response": json.loads(r.response_json) if r.response_json else None,
                    "error": r.error,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]

    # ------------------------------------------------------------------
    # Strategy memory (AI reasoning state, snapshotted per turn)
    # ------------------------------------------------------------------

    def save_strategy_memory(
        self, run_id: str, phase: str, turn_index: int, memory: dict,
        tokens: dict | None = None,
    ) -> None:
        """Upsert the AI strategy's memory snapshot for one turn (its reasoning state + token cost)."""
        tokens_json = json.dumps(tokens) if tokens is not None else None
        with self._session() as s:
            row = s.scalar(
                select(StrategyMemoryRecord).where(
                    StrategyMemoryRecord.run_id == run_id,
                    StrategyMemoryRecord.phase == phase,
                    StrategyMemoryRecord.turn_index == turn_index,
                )
            )
            if row:
                row.memory_json = json.dumps(memory)
                row.tokens_json = tokens_json
            else:
                s.add(StrategyMemoryRecord(
                    run_id=run_id, phase=phase, turn_index=turn_index,
                    memory_json=json.dumps(memory), tokens_json=tokens_json,
                ))
            s.commit()

    def get_strategy_memories(self, run_id: str, phase: str | None = None) -> list[dict]:
        """Per-turn memory snapshots, ordered by (phase, turn_index)."""
        with self._session() as s:
            stmt = select(StrategyMemoryRecord).where(
                StrategyMemoryRecord.run_id == run_id
            )
            if phase is not None:
                stmt = stmt.where(StrategyMemoryRecord.phase == phase)
            rows = s.scalars(
                stmt.order_by(
                    StrategyMemoryRecord.phase, StrategyMemoryRecord.turn_index
                )
            )
            return [
                {
                    "phase": r.phase,
                    "turn_index": r.turn_index,
                    "memory": json.loads(r.memory_json) if r.memory_json else {},
                    "tokens": json.loads(r.tokens_json) if r.tokens_json else None,
                }
                for r in rows
            ]

    # ------------------------------------------------------------------
    # Run records
    # ------------------------------------------------------------------

    def create_run(self, record: RunRecord) -> None:
        """Insert a new run record."""
        with self._session() as s:
            s.add(record)
            s.commit()

    def update_run(self, run_id: str, **fields) -> None:
        """Update fields on an existing run record (no-op if the run is unknown)."""
        with self._session() as s:
            record = s.get(RunRecord, run_id)
            if record is None:
                return
            for key, value in fields.items():
                setattr(record, key, value)
            s.commit()

    def get_run(self, run_id: str) -> RunRecord | None:
        """A run record detached from the session, or None if unknown."""
        with self._session() as s:
            record = s.get(RunRecord, run_id)
            if record is None:
                return None
            s.expunge(record)
            return record

    def list_runs(self, limit: int = 20) -> list[RunRecord]:
        """The most recent runs (newest first), detached from the session."""
        with self._session() as s:
            records = list(s.scalars(
                select(RunRecord)
                .order_by(RunRecord.created_at.desc())
                .limit(limit)
            ))
            for r in records:
                s.expunge(r)
            return records

    # ------------------------------------------------------------------
    # Aggregated report (one derived projection per run; regenerate overwrites)
    # ------------------------------------------------------------------

    def save_aggregated_report(
        self, run_id: str, analysis_id: str | None, model_name: str,
        payload: dict, tokens: dict | None = None, duration_s: float | None = None,
    ) -> None:
        """Upsert the aggregated report for a run (one per run; regenerating overwrites).

        Stores the step's token + time cost separately (`tokens_json`/`duration_s`) as well as
        inside the payload's `steps` block.
        """
        data = json.dumps(payload, default=str)
        tokens_json = json.dumps(tokens) if tokens is not None else None
        with self._session() as s:
            row = s.get(AggregatedReportRecord, run_id)
            if row:
                row.analysis_id = analysis_id
                row.model_name = model_name
                row.report_json = data
                row.tokens_json = tokens_json
                row.duration_s = duration_s
            else:
                s.add(AggregatedReportRecord(
                    run_id=run_id, analysis_id=analysis_id,
                    model_name=model_name, report_json=data,
                    tokens_json=tokens_json, duration_s=duration_s,
                ))
            s.commit()

    def get_aggregated_report(self, run_id: str) -> dict | None:
        """The stored aggregated-report payload for a run, or None if not generated yet."""
        with self._session() as s:
            row = s.get(AggregatedReportRecord, run_id)
            if row is None:
                return None
            return json.loads(row.report_json) if row.report_json else None
