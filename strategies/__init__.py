"""Strategy families. Each `strategies/<type>.py` registers pure `signals()`/`period()`/
`warmup()` functions under its spec `strategy.type`; see strategies/base.py and docs/roadmap.md."""
from .base import (REGISTRY, LookaheadError, MarketData, Signal, StrategyImpl, ThesisState,  # noqa: F401
                   for_spec, get, register)
from . import dca, momentum  # noqa: F401,E402  (registers the built-in types)


def signals(spec, data: MarketData, state: ThesisState) -> list[Signal]:
    """Dispatch on the spec's strategy type."""
    return for_spec(spec).signals(spec, data, state)
