"""Optional, dependency-free public benchmark adapters."""

from .swe_task import adapt_swe_task


ADAPTERS = {"swe-style": adapt_swe_task}


def get_adapter(name):
    try:
        return ADAPTERS[str(name)]
    except KeyError as exc:
        raise ValueError(f"unknown evaluation adapter: {name}") from exc


__all__ = ["ADAPTERS", "adapt_swe_task", "get_adapter"]
