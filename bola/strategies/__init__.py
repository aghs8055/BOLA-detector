"""Strategies: decide *what* to call. The actuator (Step 4) sends and remembers; a strategy chooses.

`RandomStrategy` is the no-LLM baseline (sample operations, fill inputs from a harvested id or a
generator). `AIStrategy` reasons over the spec, relations, id pool, and prior results to plan calls
and carry memory across turns. Both are pure planners: `plan()` → (runner executes + harvests) →
`observe(results)`; the run manager (Step 7) owns the loop and all persistence. See `README.md`.
"""

from bola.strategies.ai_strategy import AIStrategy
from bola.strategies.base import (
    ActionResult,
    PlannedAction,
    Strategy,
    StrategyContext,
    build_body,
)
from bola.strategies.models import (
    AgentMemory,
    AgentTurn,
    BodyInput,
    ConcreteChoice,
    GeneratorChoice,
    ParamInput,
    PlannedCall,
    RepoChoice,
)
from bola.strategies.random_strategy import RandomStrategy

__all__ = [
    "Strategy",
    "StrategyContext",
    "PlannedAction",
    "ActionResult",
    "build_body",
    "RandomStrategy",
    "AIStrategy",
    "AgentMemory",
    "AgentTurn",
    "PlannedCall",
    "ParamInput",
    "BodyInput",
    "GeneratorChoice",
    "RepoChoice",
    "ConcreteChoice",
]