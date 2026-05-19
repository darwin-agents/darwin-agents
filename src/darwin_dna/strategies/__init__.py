"""Default strategy priors. Layer 0 ships exactly one."""

from ._base import (
    Strategy,
    StrategyAction,
    StrategyObservation,
    StrategySpec,
)
from .content_affiliate import ContentAffiliateStrategy, founding_strategy

__all__ = [
    "Strategy",
    "StrategyAction",
    "StrategyObservation",
    "StrategySpec",
    "ContentAffiliateStrategy",
    "founding_strategy",
]
