"""Optional AI advisor layer.

Three interchangeable backends, one contract.  See ``service.py`` for the rules
that hold regardless of which one is active.
"""
from .base import Advice, AdviceProvider, AdvisorUnavailable, extract_json, sanitize
from .service import (
    Advisor,
    build_provider,
    get_advisor,
    provider_catalogue,
    sdk_available,
)

__all__ = [
    "Advice",
    "AdviceProvider",
    "Advisor",
    "AdvisorUnavailable",
    "build_provider",
    "extract_json",
    "get_advisor",
    "provider_catalogue",
    "sanitize",
    "sdk_available",
]
