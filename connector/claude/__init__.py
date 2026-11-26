"""
Claude Code connector for the cortex memory service.

Public API:
    from connector.claude import Cortex, CortexError
    cx = Cortex()
    cx.health()
    cx.remember("...")
    cx.recall("...")
"""
from .claude_cortex_client import (
    Cortex,
    CortexError,
    DEFAULT_BASE,
    TIMEOUT,
    LONG_TIMEOUT,
)

__all__ = [
    "Cortex",
    "CortexError",
    "DEFAULT_BASE",
    "TIMEOUT",
    "LONG_TIMEOUT",
]
__version__ = "1.0.0"