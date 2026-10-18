"""
Async Command Code CLI executor.

Runs ``cmd -p "prompt"`` via asyncio.subprocess with:

- Non-blocking line-by-line stdout reading (live progress)
- ``--output-format json`` parsing: session id, token usage, final text
- Interrupt support (SIGINT → SIGKILL escalation), foreground and background
- Timeout handling that never leaks a child process
- Exit code → human-readable mapping (including 8/9/10/130)

The flags map 1:1 to the documented CLI reference:

    -m/--model, --effort, --plan, -c/--continue, -r/--resume <id|name>,
    --fork-session, -n/--name, --config key=value, --add-dir,
    --max-turns, --yolo, --skip-onboarding, --trust, --output-format
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from telegram_commandcode.streamjson import (
    parse_frame,
    render_progress,
    render_result,
)

logger = logging.getLogger(__name__)

# ── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_CMD_BIN = os.getenv("COMMAND_CODE_CMD", "cmd")
DEFAULT_TIMEOUT = 600  # 10 minutes


def _int_env(name: str, default: int) -> int:
    """Parse an int env var without crashing the whole bot on a typo."""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("Invalid %s, falling back to %s", name, default)
        return default


def _bool_env(name: str, default: bool) -> bool:
    """Parse a boolean env var ("false"/"0"/"no" turn it off)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


DEFAULT_MAX_TURNS = _int_env("COMMAND_CODE_MAX_TURNS", 20)
DEFAULT_YOLO = _bool_env("COMMAND_CODE_YOLO", True)
DEFAULT_TRUST = _bool_env("COMMAND_CODE_TRUST", True)
# JSON output gives us session ids and token usage. Older CLI builds do not
# understand --output-format; set COMMAND_CODE_JSON=0 for those.
DEFAULT_JSON_OUTPUT = _bool_env("COMMAND_CODE_JSON", True)
DEFAULT_WORKDIR = os.getenv("COMMAND_CODE_WORKDIR", "").strip()

# Exit codes documented in https://commandcode.ai/docs/headless
EXIT_CODE_REASONS: dict[int, str] = {
    0: "success",
    1: "general error",
    3: "not authenticated (run `cmd login` on the host)",
    4: "permission denied (try /yolo)",
    5: "rate limited",
    6: "network failure",
    7: "server error (5xx)",
    8: "max turns reached",
    9: "model returned no response",
    10: "insufficient credits",
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

    # Populated from `--output-format json`
    final_text: str = ""
    session_id: Optional[str] = None
    usage: dict = field(default_factory=dict)
    duration_ms: Optional[int] = None
    stop_reason: Optional[str] = None
    subtype: Optional[str] = None

    @property
    def output(self) -> str:
        """Model answer, falling back to raw stdout, then stderr."""
        return (
            (self.final_text or "").strip()
            or self.stdout.strip()
            or self.stderr.strip()
        )

    @property
    def is_error(self) -> bool:
        """True if the result looks like an error/warning."""
        out = self.output
        return (
            self.exit_code != 0
            or self.subtype == "error"
            or out.startswith("⚠️")
            or out.startswith("❌")
        )

    @property
    def human_reason(self) -> str:
        if self.killed_by_signal:
            return f"killed by {self.killed_by_signal}"
        return EXIT_CODE_REASONS.get(self.exit_code, f"exit code {self.exit_code}")


@dataclass
class ExecOptions:
    """Options passed to run_cmd() / run_cmd_with_progress()."""

    model: Optional[str] = None       # -m <model>
    effort: Optional[str] = None      # --effort <level>
    plan_mode: bool = False           # --plan
    continue_session: bool = False    # --continue
    resume_session: Optional[str] = None  # --resume <id|name>
    fork_session: bool = False        # --fork-session (with continue/resume)
    session_name: Optional[str] = None    # -n <name>
    compact_mode: Optional[str] = None    # --config compact-mode=<default|fast>
    yolo: bool = DEFAULT_YOLO         # --yolo
    max_turns: int = DEFAULT_MAX_TURNS
    add_dirs: list[str] = field(default_factory=list)  # --add-dir
    skip_onboarding: bool = True      # --skip-onboarding
    trust: bool = DEFAULT_TRUST       # --trust
    json_output: bool = DEFAULT_JSON_OUTPUT  # --output-format json
    timeout: int = DEFAULT_TIMEOUT
    cwd: Optional[Path] = None
    env: Optional[dict] = None

    def build_args(self, prompt: str) -> list[str]:
        """Build the command argument list.

        The prompt always goes last: a prompt starting with "-" is then never
        parsed as a flag, and no flag can be swallowed by the prompt argument.
        """
        args = [DEFAULT_CMD_BIN]

        if self.yolo:
            args.append("--yolo")
        args.extend(("--max-turns", str(self.max_turns)))
        if self.model:
            args.extend(("-m", self.model))
        if self.effort:
            args.extend(("--effort", self.effort))
        if self.plan_mode:
            args.append("--plan")

        # --fork-session only means something together with a resumed session.
        continuing = self.continue_session or self.fork_session
        if self.resume_session:
            args.extend(("--resume", self.resume_session))
        elif continuing:
            args.append("--continue")
        if self.fork_session:
            args.append("--fork-session")

        if self.session_name:
            args.extend(("-n", self.session_name))
        if self.compact_mode:
            args.extend(("--config", f"compact-mode={self.compact_mode}"))
        for d in self.add_dirs:
            args.extend(("--add-dir", d))
        if self.skip_onboarding:
            args.append("--skip-onboarding")
        if self.trust:
            args.append("--trust")
        if self.json_output:
            args.extend(("--output-format", "json"))

        args.extend(("-p", prompt))
        return args


def _workdir(opts: "ExecOptions") -> str:
    """Resolve the directory the agent runs in.

    Defaults to the directory the bot was started from (usually the project),
    never to the user's home directory: running an agent in $HOME is how it
    ends up editing unrelated files.
    """
    if opts.cwd:
        return str(opts.cwd)
    if DEFAULT_WORKDIR:
        return DEFAULT_WORKDIR
    return str(Path.cwd())


def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Ask a child to stop, tolerating races with its own exit."""
    try:
        if os.name == "nt":
            # Windows has no SIGINT delivery to another process group.
            proc.terminate()
        else:
            proc.send_signal(signal.SIGINT)
    except (ProcessLookupError, PermissionError):
        pass


class RunningProcessTracker:
    """Track running subprocesses per chat to support /stop (interrupt).

    Foreground runs (the prompt the user is waiting on) and background runs
    (/background) are tracked separately: starting a new prompt must kill the
    previous *foreground* run, but it must not cancel background work.
    """

    def __init__(self) -> None:
        self._foreground: dict[str, asyncio.subprocess.Process] = {}
        self._background: dict[str, list[asyncio.subprocess.Process]] = {}

    # ── Registration ───────────────────────────────────────────────────

    def register(self, chat_id: str, proc: asyncio.subprocess.Process) -> None:
        self._foreground[chat_id] = proc

    def unregister(self, chat_id: str) -> None:
        self._foreground.pop(chat_id, None)

    def register_bg(self, chat_id: str, proc: asyncio.subprocess.Process) -> None:
        procs = [p for p in self._background.get(chat_id, []) if p.returncode is None]
        procs.append(proc)
        self._background[chat_id] = procs

    def unregister_bg(self, chat_id: str, proc: asyncio.subprocess.Process) -> None:
        remaining = [
            p for p in self._background.get(chat_id, []) if p is not proc
        ]
        if remaining:
            self._background[chat_id] = remaining
        else:
            self._background.pop(chat_id, None)

    # ── Introspection ──────────────────────────────────────────────────

    def get(self, chat_id: str) -> Optional[asyncio.subprocess.Process]:
        """Return the running foreground process, if any (never a stale one)."""
        proc = self._foreground.get(chat_id)
        if proc is None or proc.returncode is not None:
            return None
        return proc

    def is_running(self, chat_id: str) -> bool:
        return self.get(chat_id) is not None

    def background_count(self, chat_id: str) -> int:
        return sum(
            1
            for p in self._background.get(chat_id, [])
            if p.returncode is None
        )

    def background_total(self) -> int:
        return sum(
            sum(1 for p in procs if p.returncode is None)
            for procs in self._background.values()
        )

    # ── Termination ────────────────────────────────────────────────────

    async def kill(self, chat_id: str) -> bool:
        """Send SIGINT to the foreground process, escalate to SIGKILL after 3s."""
        proc = self._foreground.get(chat_id)
        if proc is None or proc.returncode is not None:
            self._foreground.pop(chat_id, None)
            return False

        _terminate(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

        self.unregister(chat_id)
        return True

    async def kill_background(self, chat_id: str) -> int:
        """Stop every background run for a chat; return how many were stopped."""
        procs = [
            p for p in self._background.get(chat_id, []) if p.returncode is None
        ]
        self._background.pop(chat_id, None)
        if not procs:
            return 0

        for proc in procs:
            _terminate(proc)
        try:
            await asyncio.wait_for(
                asyncio.gather(*(p.wait() for p in procs)),
                timeout=3.0,
            )
        except asyncio.TimeoutError:
            for proc in procs:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        return len(procs)

    async def kill_all(self) -> None:
        """Kill every tracked process (for graceful shutdown)."""
        background = self.background_total()
        if background:
            logger.info("Killing %d background process(es)", background)
        for chat_id in list(self._foreground):
            await self.kill(chat_id)
        for chat_id in list(self._background):
            await self.kill_background(chat_id)


# Singleton
process_tracker = RunningProcessTracker()


# ── Runner ──────────────────────────────────────────────────────────────────


async def _run(
    prompt: str,
    opts: ExecOptions,
    *,
    chat_id: Optional[str] = None,
    background: bool = False,
    on_progress=None,          # async callable(batch_text: str) -> None
    batch_interval: float = 1.5,
    max_preview_chars: int = 1500,
) -> CmdResult:
    """Spawn one CLI run, read stdout line by line, and build a CmdResult."""
    if chat_id and not background:
        await process_tracker.kill(chat_id)

    args = opts.build_args(prompt)
    cwd = _workdir(opts)
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

    if chat_id:
        if background:
            process_tracker.register_bg(chat_id, proc)
        else:
            process_tracker.register(chat_id, proc)

    stderr_chunks: list[str] = []

    async def _read_stderr() -> None:
        if proc.stderr:
            async for line in proc.stderr:
                stderr_chunks.append(line.decode("utf-8", errors="replace"))

    stderr_task = asyncio.create_task(_read_stderr())

    raw_lines: list[str] = []
    progress_lines: list[str] = []
    result_frame: dict = {}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + opts.timeout
    last_callback = loop.time()
    timed_out = False

    def _progress_text() -> str:
        if not progress_lines:
            return "".join(raw_lines)[-max_preview_chars:]
        text = "\n".join(progress_lines[-40:])
        if len(text) > max_preview_chars:
            return "…" + text[-(max_preview_chars - 1):]
        return text

    async def _report() -> None:
        if not on_progress:
            return
        try:
            await on_progress(_progress_text())
        except Exception as exc:  # a progress edit must never kill the run
            logger.debug("Progress callback failed: %s", exc)

    try:
        if proc.stdout:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    break
                if not line:
                    break

                decoded = line.decode("utf-8", errors="replace")
                raw_lines.append(decoded)

                frame = parse_frame(decoded) if opts.json_output else None
                if frame is None:
                    text = decoded.rstrip()
                    if text:
                        progress_lines.append(text)
                elif frame.kind == "result":
                    result_frame = frame.result
                elif frame.kind == "event":
                    rendered = render_progress(frame.event)
                    if rendered:
                        progress_lines.append(rendered)

                now = loop.time()
                if on_progress and now - last_callback >= batch_interval:
                    await _report()
                    last_callback = now

        if on_progress and not timed_out:
            await _report()

    finally:
        if timed_out:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

        await stderr_task

        reaped = True
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            reaped = False
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        finally:
            if chat_id:
                if background:
                    process_tracker.unregister_bg(chat_id, proc)
                else:
                    process_tracker.unregister(chat_id)

    stdout = "".join(raw_lines)
    stderr = "".join(stderr_chunks)
    exit_code = proc.returncode
    if exit_code is None:
        # Killed but not reaped within the grace period: report a failure
        # rather than pretending the run succeeded.
        exit_code = -1
        reaped = False

    killed_by = None
    if exit_code < 0:
        sig_num = -exit_code
        try:
            killed_by = signal.Signals(sig_num).name
        except ValueError:
            killed_by = f"signal {sig_num}"

    parsed = render_result(result_frame) if result_frame else {}
    subtype = parsed.get("subtype")
    success = (
        exit_code == 0
        and reaped
        and not killed_by
        and not timed_out
        and subtype != "error"
    )

    if timed_out:
        timeout_line = f"⚠️ Command timed out after {opts.timeout}s."
        stderr = f"{stderr}\n{timeout_line}".strip()
        killed_by = "SIGKILL (timeout)"

    return CmdResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        killed_by_signal=killed_by,
        success=success,
        final_text=parsed.get("final_text", ""),
        session_id=parsed.get("session_id"),
        usage=parsed.get("usage", {}),
        duration_ms=parsed.get("duration_ms"),
        stop_reason=parsed.get("stop_reason"),
        subtype=subtype,
    )


async def run_cmd(
    prompt: str,
    opts: ExecOptions,
    *,
    chat_id: Optional[str] = None,
    background: bool = False,
) -> CmdResult:
    """Run a prompt to completion (no live progress callbacks)."""
    return await _run(prompt, opts, chat_id=chat_id, background=background)


async def run_cmd_with_progress(
    prompt: str,
    opts: ExecOptions,
    *,
    chat_id: Optional[str] = None,
    on_progress=None,          # async callable(batch_text: str) -> None
    batch_interval: float = 1.5,
    max_preview_chars: int = 1500,
) -> CmdResult:
    """Run a prompt, calling ``on_progress`` with a short activity log."""
    return await _run(
        prompt, opts,
        chat_id=chat_id,
        on_progress=on_progress,
        batch_interval=batch_interval,
        max_preview_chars=max_preview_chars,
    )
