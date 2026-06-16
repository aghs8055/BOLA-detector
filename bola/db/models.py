from datetime import datetime, timezone

from sqlalchemy import Float, String, Text, DateTime, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for every ORM model in this module."""


class RelationCache(Base):
    """Cached LLM relation detection result, keyed by spec hash + model name."""

    __tablename__ = "relation_cache"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    spec_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    relations_json: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # {input,output,total} the detection step spent
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)  # wall-clock the detection step took
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("spec_hash", "model_name", name="uq_relation_cache_spec_model"),
    )


class RelationGroupResult(Base):
    """Per-unit checkpoint for relation detection: one `source × target-group` call.

    Finer-grained than RelationCache so a run can resume mid-spec — a completed unit is
    skipped on rerun, and the consolidated result is assembled into RelationCache only once
    every unit is done.
    """

    __tablename__ = "relation_group_result"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    spec_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    source_key: Mapped[str] = mapped_column(String(300), nullable=False)
    group_index: Mapped[int] = mapped_column(nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False)  # done | failed
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint(
            "spec_hash", "model_name", "source_key", "group_index",
            name="uq_relation_group_unit",
        ),
    )


class SnapshotRecord(Base):
    """One captured GET of one object, taken as one user at one phase — the frozen evidence.

    Append-only per `(run_id, phase, object_key)`: a captured object is skipped on resume, and a
    failed/missing one is retried. `object_key` is the stable identity of the object across the
    three phases (`snap_before`/`snap_hacker`/`snap_after`), so analysis groups the trio by it.
    Read-only GETs make re-capture harmless. Mirrors `RelationGroupResult`'s per-unit shape.
    """

    __tablename__ = "snapshot_record"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    phase: Mapped[str] = mapped_column(String(20), nullable=False)  # snap_before|snap_hacker|snap_after
    op_key: Mapped[str] = mapped_column(String(300), nullable=False)
    object_key: Mapped[str] = mapped_column(String(500), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False)  # done | failed
    request_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status_code: Mapped[int | None] = mapped_column(nullable=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("run_id", "phase", "object_key", name="uq_snapshot_unit"),
    )


class AnalysisRecord(Base):
    """One analysis *pass* over a run's snapshots — the consolidated projection (findings).

    A single execution `run_id` accumulates many of these: each pass has its own `analysis_id` and
    is tagged with the inputs that produced it (`access_desc_hash`, `model`), so passes under
    different authorization assumptions or models are comparable side by side. Written once every
    per-object verdict (`AnalysisItemResult`) for the pass is done.
    """

    __tablename__ = "analysis_record"

    analysis_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    access_desc_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    findings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # {input,output,total} this analysis pass spent
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)  # wall-clock this analysis pass took
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")  # running|completed

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class AnalysisItemResult(Base):
    """Per-object verdict checkpoint within one analysis pass (resume boundary).

    Finer-grained than `AnalysisRecord` so a pass can resume mid-run: a done object is skipped on
    rerun, a failed/missing one is retried, and survivors in a failing batch stay checkpointed. The
    `AnalysisRecord` projection is assembled only once every unit is done. Mirrors
    `RelationGroupResult` → `RelationCache`.
    """

    __tablename__ = "analysis_item_result"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    analysis_id: Mapped[str] = mapped_column(String(36), nullable=False)
    object_key: Mapped[str] = mapped_column(String(500), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False)  # done | failed
    verdict_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("analysis_id", "object_key", name="uq_analysis_item_unit"),
    )


class ExecutionRecord(Base):
    """One executed action, appended the moment it is sent — the run's source of truth.

    Append-only and keyed by `(run_id, seq)`. The `FieldRepo` is *not* persisted; it is a pure
    in-memory projection rebuilt on resume by replaying `harvest` over these logged responses
    (a pure fold). Per-action granularity (not per-phase) is required because both the explore and
    attack phases mutate the target, so neither can be safely re-run from scratch — on resume the
    runner reduces the remaining budget by the actions already logged. Mirrors `RelationGroupResult`.
    """

    __tablename__ = "execution_record"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    seq: Mapped[int] = mapped_column(nullable=False)
    phase: Mapped[str] = mapped_column(String(20), nullable=False)  # combined loop: "run"
    identity: Mapped[str] = mapped_column(String(20), nullable=False, default="regular")  # regular | attacker
    turn_index: Mapped[int] = mapped_column(nullable=False, default=0)
    op_key: Mapped[str] = mapped_column(String(300), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")

    request_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    media_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status_code: Mapped[int | None] = mapped_column(nullable=True)
    ok: Mapped[bool] = mapped_column(default=False)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_execution_seq"),
    )


class StrategyMemoryRecord(Base):
    """The AI strategy's `AgentMemory` snapshotted after each turn (per phase).

    Unlike the `FieldRepo`, `AgentMemory` is genuine reasoning state — *not* reconstructable by
    replaying harvests — so it is persisted in its own right: snapshotted per turn (for the run
    result and resume) and the latest one fed back into a restored strategy on resume. The random
    baseline has no memory, so it writes no rows here.
    """

    __tablename__ = "strategy_memory_record"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    phase: Mapped[str] = mapped_column(String(20), nullable=False)  # explore | attack
    turn_index: Mapped[int] = mapped_column(nullable=False)
    memory_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    tokens_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # {input,output,total} for this turn's LLM call

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("run_id", "phase", "turn_index", name="uq_strategy_memory_turn"),
    )


class AggregatedReportRecord(Base):
    """The aggregated, human-facing report for one run — the fusion of the analysis findings and the
    AI explorer's memory into one structured document (the artifact a later HTML view renders).

    One row per run (`run_id` primary key): it is a derived projection of the run's persisted
    evidence, not an append-only log, so regenerating it overwrites in place. `report_json` holds the
    full payload (report + deterministic metrics + metadata) exactly as written to
    `runs/<run_id>/report.json`.
    """

    __tablename__ = "aggregated_report"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    analysis_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    report_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    tokens_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # {input,output,total} the aggregation step spent
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)  # wall-clock the aggregation step took
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class RunRecord(Base):
    """Persisted record of a single BOLA test run."""

    __tablename__ = "run_records"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_name: Mapped[str] = mapped_column(String(200), nullable=False)
    strategy: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    # running | completed | failed

    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    findings_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
