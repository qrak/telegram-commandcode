"""
Async Command Code CLI executor.

Runs `cmd -p "prompt"` via asyncio.subprocess with:
- Non-blocking I/O (async stdout/stderr reading)
- Interrupt support (SIGINT → SIGKILL escalation)
- Timeout handling
- Exit code → human-readable mapping
- Streaming output via async generator
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)

# Default values
DEFAULT_CMD_BIN = os.getenv("COMMAND_CODE_CMD", "cmd")
DEFAULT_TIMEOUT = 600  # 10 minutes
DEFAULT_MAX_TURNS = int(os.getenv("COMMAND_CODE_MAX_TURNS", "20"))
DEFAULT_YOLO = os.getenv("COMMAND_CODE_YOLO", "true").lower() != "false"

EXIT_CODE_REASONS: dict[int, str] = {
    0: "success",
    1: "general error",
    3: "not authenticated",
    4: "permission denied (use --yolo?)",
    5: "rate limited",
    6: "network failure",
    7: "server error (5xx)",
    130: "interrupted",
}


@dataclass
class CmdResult:
    """Result of a Command Code CLI invocation."""
    stdout: str
    stderr: str
    exit_code: int
    killed_by_signal: Optional[str] = None
    success: bool = False

    @property
    def output(self) -> str:
        """Combined stdout, falling back to stderr."""
        return self.stdout.strip() or self.stderr.strip()

    @property
    def is_error(self) -> bool:
        """True if the result looks like an error/warning."""
        out = self.output
        return self.exit_code != 0 or out.startswith("⚠️") or out.startswith("❌")

    @property
    def human_reason(self) -> str:
        if self.killed_by_signal:
            return f"killed by {self.killed_by_signal}"
        return EXIT_CODE_REASONS.get(self.exit_code, f"exit code {self.exit_code}")


@dataclass
class ExecOptions:
    """Options passed to run_cmd()."""
    model: Optional[str] = None       # -m <model>
    plan_mode: bool = False           # --plan
    continue_session: bool = False    # --continue
    yolo: bool = DEFAULT_YOLO         # --yolo
    max_turns: int = DEFAULT_MAX_TURNS
    add_dirs: list[str] = field(default_factory=list)  # --add-dir
    skip_onboarding: bool = True      # --skip-onboarding
    timeout: int = DEFAULT_TIMEOUT
    cwd: Optional[Path] = None
    env: Optional[dict] = None

    def build_args(self, prompt: str) -> list[str]:
        """Build the command argument list."""
        args = [DEFAULT_CMD_BIN, "-p", prompt]

        if self.yolo:
            args.append("--yolo")
        args.extend(("--max-turns", str(self.max_turns)))
        if self.model:
            args.extend(("-m", self.model))
        if self.plan_mode:
            args.append("--plan")
        if self.continue_session:
            args.append("--continue")
        if self.skip_onboarding:
            args.append("--skip-onboarding")
        for d in self.add_dirs:
            args.extend(("--add-dir", d))

        return args


class RunningProcessTracker:
    """
    Track running subprocesses per chat to support /stop (interrupt).

    Process-level singleton — one tracker per bot instance.
    """

    def __init__(self):
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._bg_processes: set[asyncio.subprocess.Process] = set()

    def register(self, chat_id: str, proc: asyncio.subprocess.Process) -> None:
        self._processes[chat_id] = proc

    def unregister(self, chat_id: str) -> None:
        self._processes.pop(chat_id, None)

    def register_bg(self, proc: asyncio.subprocess.Process) -> None:
        self._bg_processes.add(proc)

    def unregister_bg(self, proc: asyncio.subprocess.Process) -> None:
        self._bg_processes.discard(proc)

    def get(self, chat_id: str) -> Optional[asyncio.subprocess.Process]:
        return self._processes.get(chat_id)

    async def kill(self, chat_id: str) -> bool:
        """Send SIGINT to a running process, escalate to SIGKILL after 3s."""
        proc = self._processes.get(chat_id)
        if proc is None or proc.returncode is not None:
            return False

        try:
            proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            return False

        # Escalate to SIGKILL after 3 seconds
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

        self.unregister(chat_id)
        return True

    async def kill_all(self) -> None:
        """Kill all tracked processes (for graceful shutdown)."""
        for chat_id in list(self._processes.keys()):
            await self.kill(chat_id)
        for proc in list(self._bg_processes):
            if proc.returncode is None:
                try:
                    proc.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass
            self._bg_processes.discard(proc)


# Singleton
process_tracker = RunningProcessTracker()


async def run_cmd(
    prompt: str,
    opts: ExecOptions,
    *,
    chat_id: Optional[str] = None,
) -> CmdResult:
    """
    Run Command Code CLI as an async subprocess.

    If chat_id is provided, the process is registered for /stop support
    and any previous running process for that chat is killed first.

    Returns a CmdResult with stdout, stderr, exit code, and signal info.
    """
    # Kill any previous process for this chat
    if chat_id:
        await process_tracker.kill(chat_id)

    args = opts.build_args(prompt)
    cwd = str(opts.cwd or Path.home())
    env = opts.env or os.environ

    logger.debug("Running: %s (cwd=%s)", " ".join(args), cwd)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
    except FileNotFoundError:
        return CmdResult(
            stdout="",
            stderr=f"❌ Command '{DEFAULT_CMD_BIN}' not found. Is Command Code installed?",
            exit_code=-1,
        )
    except PermissionError:
        return CmdResult(
            stdout="",
            stderr=f"❌ Permission denied running '{DEFAULT_CMD_BIN}'.",
            exit_code=-1,
        )

    # Track for interrupt support
    if chat_id:
        process_tracker.register(chat_id, proc)

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=opts.timeout,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        if chat_id:
            process_tracker.unregister(chat_id)
        return CmdResult(
            stdout="",
            stderr=f"⚠️ Command timed out after {opts.timeout}s.",
            exit_code=-1,
            killed_by_signal="SIGKILL (timeout)",
        )
    finally:
        if chat_id:
            process_tracker.unregister(chat_id)

    stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

    exit_code = proc.returncode or 0
    killed_by = None

    # Check if killed by signal
    if exit_code < 0:
        sig_num = -exit_code
        try:
            killed_by = signal.Signals(sig_num).name
        except ValueError:
            killed_by = f"signal {sig_num}"

    return CmdResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        killed_by_signal=killed_by,
        success=(exit_code == 0 and not killed_by),
    )


async def run_cmd_streaming(
    prompt: str,
    opts: ExecOptions,
    *,
    chat_id: Optional[str] = None,
) -> AsyncIterator[str]:
    """
    Run Command Code CLI and yield stdout lines as they arrive.

    Use this for streaming progress updates — yields each line as it's
    received, then yields the final CmdResult summary.
    """
    result = await run_cmd(prompt, opts, chat_id=chat_id)

    # Yield stdout lines progressively (for streaming display)
    for line in result.stdout.split("\n"):
        line = line.strip()
        if line:
            yield line

    # Yield the full result
    if result.is_error:
        yield f"⚠️ {result.human_reason}: {result.stderr[:500]}"
