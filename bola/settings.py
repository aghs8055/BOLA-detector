from dataclasses import dataclass, field
from pathlib import Path
import os

import yaml
from dotenv import load_dotenv


@dataclass
class LLMSettings:
    """Chat-model configuration shared by every LLM step (model, sampling, retries, timeout)."""

    model: str = "anthropic/claude-sonnet-4-6"
    analysis_model: str = ""  # falls back to model if empty
    temperature: float = 0.0
    max_retries: int = 2
    timeout: int = 120


@dataclass
class TestSettings:
    """Strategy selection and the per-phase action/turn budgets for a run."""

    regular_ops: int = 50
    hacker_ops: int = 50
    strategy: str = "random"  # random | ai
    ai_max_turns: int = 10
    ai_build_max_turns: int = 5  # hard cap on build-stage turns before the agent must attack
    ai_require_full_coverage: bool = False  # if set, the agent may only finish once every relation-
    # consuming operation has been attacked (else keep going to the turn cap) — drives coverage at scale
    attack_mode: str = "sweep"  # sweep (deterministic enumerate→build→sweep→creative) | freeform (old single-pass)
    attack_ops_per_call: int = 4   # ops packed into one attack-fill LLM call (sweep mode)
    attack_ids_per_op: int = 2     # victim ids replayed per op / followed per coordinate in BFS
    attack_max_attempts_per_op: int = 3  # drop a consumer op from the sweep after this many attacker
    # attempts (credited or not) so a stuck/failing op can't starve the worklist of the rest
    revise_max: int = 2            # on a 4xx input error (not 401/403/404), let the LLM revise + retry this many times
    creative_turns: int = 5        # bounded free-form turns after the sweep (multi-step + mass-assignment)
    request_timeout: int = 30
    api_max_retries: int = 2


@dataclass
class RelationSettings:
    """Relation-detection knobs: grouping, batching, the revise budget, and error policy."""

    group_size: int = 10  # targets per LLM call
    batch_size: int = 1  # source×group units sent concurrently per batch (1 = sequential)
    max_attempts: int = 2  # detect + (max_attempts-1) revise passes
    on_error: str = "stop"  # stop | continue


@dataclass
class AnalysisSettings:
    """BOLA-analysis batching knobs and error policy for one analysis pass."""

    objects_per_call: int = 5  # objects packed into one analysis LLM prompt
    batch_size: int = 1  # analysis prompts sent concurrently per batch (1 = sequential)
    on_error: str = "stop"  # stop | continue


@dataclass
class StorageSettings:
    """Where persisted state lives: the SQLite database and the per-run result directory."""

    db_path: str = "bola.db"
    runs_dir: str = "runs"


@dataclass
class ObservabilitySettings:
    """Telemetry and logging toggles (Langfuse tracing, log level)."""

    langfuse_enabled: bool = True
    langfuse_timeout: int = 20  # seconds for the OTLP span export; generous so a slow network degrades to dropped traces, never an aborted run
    log_level: str = "INFO"


@dataclass
class Settings:
    """The whole resolved configuration: the setting blocks plus secrets from the environment."""

    llm: LLMSettings = field(default_factory=LLMSettings)
    test: TestSettings = field(default_factory=TestSettings)
    relations: RelationSettings = field(default_factory=RelationSettings)
    analysis: AnalysisSettings = field(default_factory=AnalysisSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    observability: ObservabilitySettings = field(default_factory=ObservabilitySettings)

    # Secrets — always from env vars or .env file, never in settings.yaml
    openai_api_key: str = ""
    openai_base_url: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""


def load_settings(
    settings_path: str = "settings.yaml",
    env_path: str = ".env",
) -> Settings:
    """Build `Settings` from the YAML file (non-secret config) and the `.env`/environment (secrets).

    Both sources are optional: a missing file falls back to the dataclass defaults.
    """
    if Path(env_path).exists():
        load_dotenv(env_path, override=False)

    data: dict = {}
    if Path(settings_path).exists():
        with open(settings_path) as f:
            data = yaml.safe_load(f) or {}

    llm = data.get("llm", {})
    test = data.get("test", {})
    relations = data.get("relations", {})
    analysis = data.get("analysis", {})
    storage = data.get("storage", {})
    obs = data.get("observability", {})

    return Settings(
        llm=LLMSettings(
            model=llm.get("model", "anthropic/claude-sonnet-4-6"),
            analysis_model=llm.get("analysis_model", ""),
            temperature=float(llm.get("temperature", 0.0)),
            max_retries=int(llm.get("max_retries", 2)),
            timeout=int(llm.get("timeout", 120)),
        ),
        test=TestSettings(
            regular_ops=int(test.get("regular_ops", 50)),
            hacker_ops=int(test.get("hacker_ops", 50)),
            strategy=test.get("strategy", "random"),
            ai_max_turns=int(test.get("ai_max_turns", 10)),
            ai_build_max_turns=int(test.get("ai_build_max_turns", 5)),
            ai_require_full_coverage=bool(test.get("ai_require_full_coverage", False)),
            attack_mode=test.get("attack_mode", "sweep"),
            attack_ops_per_call=int(test.get("attack_ops_per_call", 4)),
            attack_ids_per_op=int(test.get("attack_ids_per_op", 2)),
            attack_max_attempts_per_op=int(test.get("attack_max_attempts_per_op", 3)),
            revise_max=int(test.get("revise_max", 2)),
            creative_turns=int(test.get("creative_turns", 5)),
            request_timeout=int(test.get("request_timeout", 30)),
            api_max_retries=int(test.get("api_max_retries", 2)),
        ),
        relations=RelationSettings(
            group_size=int(relations.get("group_size", 10)),
            batch_size=int(relations.get("batch_size", 1)),
            max_attempts=int(relations.get("max_attempts", 2)),
            on_error=relations.get("on_error", "stop"),
        ),
        analysis=AnalysisSettings(
            objects_per_call=int(analysis.get("objects_per_call", 5)),
            batch_size=int(analysis.get("batch_size", 1)),
            on_error=analysis.get("on_error", "stop"),
        ),
        storage=StorageSettings(
            db_path=storage.get("db_path", "bola.db"),
            runs_dir=storage.get("runs_dir", "runs"),
        ),
        observability=ObservabilitySettings(
            langfuse_enabled=bool(obs.get("langfuse_enabled", True)),
            langfuse_timeout=int(obs.get("langfuse_timeout", 20)),
            log_level=obs.get("log_level", "INFO"),
        ),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        openai_base_url=os.environ.get("OPENAI_BASE_URL", ""),
        langfuse_public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
        langfuse_secret_key=os.environ.get("LANGFUSE_SECRET_KEY", ""),
        langfuse_host=os.environ.get("LANGFUSE_HOST", ""),
    )
