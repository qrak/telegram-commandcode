"""
Gateway package — async event router, sender, processor, and media handling.

Re-exports `BotGateway`, the central class that owns all per-instance state
(rate limits, chat locks, bot identity) and wires together the sender,
processor, and router components.
"""

from .gateway import BotGateway

__all__ = ["BotGateway"]
