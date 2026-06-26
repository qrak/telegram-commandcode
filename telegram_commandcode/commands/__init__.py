"""
Command handler package — slash command routing and execution.

Each command category lives in its own module, all inheriting from
BaseCommandHandler for shared formatting, CLI execution, and state access.
"""

from .router import CommandRouter

__all__ = ["CommandRouter"]
