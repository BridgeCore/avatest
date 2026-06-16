"""
Headless Claude Code subprocess launcher.

Writes a per-run task file then invokes:
    claude --dangerously-skip-permissions -p "<task>"

Claude Code reads CLAUDE.md automatically on startup, then reads context.json
and writes results.json via its own file tools — no Anthropic API key required.
"""
import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def _find_claude() -> str:
    for name in ("claude", "claude.exe"):
        path = shutil.which(name)
        if path:
            return path
    raise FileNotFoundError(
        "Could not find 'claude' in PATH.\n"
        "Install Claude Code from https://claude.ai/code and make sure it is on your PATH."
    )


def start_validation(run_id: str, project_root: Path) -> subprocess.Popen | None:
    """
    Try to spawn a headless Claude Code CLI process.
    Returns the Popen handle if the CLI is available, or None for manual mode
    (user triggers /validate in the Claude Code window themselves).
    """
    try:
        claude = _find_claude()
    except FileNotFoundError:
        logger.info("claude CLI not on PATH — falling back to manual mode.")
        return None

    prompt = (
        f"Perform the proposal validation task described in CLAUDE.md.\n"
        f"Run ID: {run_id}\n"
        f"1. Read runs/{run_id}/context.json\n"
        f"2. Classify and validate every paragraph against the source chunks\n"
        f"3. Write the complete results to runs/{run_id}/results.json\n"
        f"Follow the schema and matching rules in CLAUDE.md exactly."
    )

    cmd = [claude, "--dangerously-skip-permissions", "-p", prompt]
    logger.info("Launching Claude Code CLI: %s", " ".join(cmd[:2]) + " -p [prompt]")

    return subprocess.Popen(
        cmd,
        cwd=str(project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
