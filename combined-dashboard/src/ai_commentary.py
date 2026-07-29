"""
Narrates numbers src/forecaster.py already computed, by shelling out to a
locally-installed `claude` CLI in non-interactive mode (`claude -p`). The
LLM never computes a number itself — it only explains/prioritizes figures
that are already correct, verifiable Python arithmetic.

Warn-don't-crash: any failure (claude not installed, timeout, malformed
output) returns None from generate_commentary() rather than raising. The
dashboard must always build successfully with or without this feature.

Deliberately does NOT use `claude --bare` — bare mode skips OAuth/keychain
and requires ANTHROPIC_API_KEY, which would reintroduce a paid-API
dependency. Plain `-p` reuses the caller's existing Claude Code login.
"""

from __future__ import annotations

import glob
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from .forecaster import summarize_utilization_trends


def _claude_exe() -> str:
    """Return the claude binary — system PATH first, then VSCode extension fallback."""
    if shutil.which("claude"):
        return "claude"
    pattern = str(Path.home() / ".vscode" / "extensions" /
                  "anthropic.claude-code-*" / "resources" / "native-binary" / "claude.exe")
    matches = sorted(glob.glob(pattern), reverse=True)
    return matches[0] if matches else "claude"

COMMENTARY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
        "narrative": {"type": "string"},
        "recommended_action": {"type": "string"},
    },
    "required": ["headline", "risk_level", "narrative", "recommended_action"],
}

INSTRUCTION_GM = (
    "You are a financial analyst assistant reviewing one business division's revenue "
    "and gross margin. You will receive JSON on stdin: historical monthly actuals, a "
    "linear projection to year-end, and variance vs. the annual AOP (plan) target — all "
    "already computed and correct; do not recompute or second-guess the numbers. Return "
    "a brief CFO-facing risk assessment: a short headline, a risk_level (low/medium/high) "
    "based on the sign and magnitude of variance_vs_aop_annual, a 2-3 sentence narrative "
    "explaining what's driving the trend, and one concrete recommended_action."
)

INSTRUCTION_UTIL = (
    "You are reviewing billable-utilization data for a professional services company. "
    "You will receive JSON on stdin with already-computed persistent trend flags per "
    "employee and division/portfolio rollups — do not invent new statistics. Return a "
    "brief CFO-facing summary: a headline, a risk_level (low/medium/high) based on the "
    "volume and severity of critical flags, a 2-3 sentence narrative prioritizing what to "
    "look at first, and one concrete recommended_action."
)

CACHE_DIR = Path("output") / ".ai_cache"


def _enabled(cfg: dict) -> bool:
    return bool(cfg.get("ai_commentary", {}).get("enabled", False))


def _cache_path(payload: dict) -> Path:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def _call_claude(payload: dict, instruction: str, timeout_sec: int) -> dict | None:
    try:
        proc = subprocess.run(
            [_claude_exe(), "-p", instruction, "--output-format", "json",
             "--json-schema", json.dumps(COMMENTARY_SCHEMA), "--allowedTools", ""],
            input=json.dumps(payload, default=str),
            capture_output=True, text=True, timeout=timeout_sec,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if proc.returncode != 0:
        return None

    try:
        envelope = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return None

    structured = envelope.get("structured_output")
    if not isinstance(structured, dict):
        return None
    if not all(k in structured for k in COMMENTARY_SCHEMA["required"]):
        return None
    return structured


def generate_commentary(payload: dict, instruction: str, timeout_sec: int) -> dict | None:
    """Cache-checked wrapper around _call_claude — a payload that's already
    been narrated (identical JSON, byte-for-byte) never re-invokes claude."""
    cache_file = _cache_path(payload)
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass  # corrupt cache entry — fall through and regenerate

    result = _call_claude(payload, instruction, timeout_sec)
    if result is None:
        return None

    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(result), encoding="utf-8")
    except OSError:
        pass  # cache write failure must not block returning the commentary
    return result


def generate_gm_commentary(forecasts: dict, cfg: dict) -> dict:
    """One commentary dict per division that has any nonzero revenue,
    keyed by division code. Empty dict if the feature is disabled or every
    call failed/was skipped."""
    if not _enabled(cfg):
        return {}
    timeout = cfg.get("ai_commentary", {}).get("timeout_seconds", 45)

    results = {}
    for div, forecast in forecasts.items():
        if not any(rev for _, rev, _ in forecast.historical_monthly):
            continue
        payload = {
            "division": forecast.division,
            "historical_monthly": forecast.historical_monthly,
            "projected_yearend_revenue": forecast.projected_yearend_revenue,
            "projected_yearend_gm": forecast.projected_yearend_gm,
            "trend_direction": forecast.trend_direction,
            "variance_vs_aop_annual": forecast.variance_vs_aop_annual,
        }
        commentary = generate_commentary(payload, INSTRUCTION_GM, timeout)
        if commentary is not None:
            results[div] = commentary
    return results


def generate_utilization_commentary(views: list, cfg: dict) -> dict:
    """One commentary dict per summarized view ("year_to_date", optionally
    "last_quarter"), keyed the same way summarize_utilization_trends() keys
    its output. Empty dict if disabled, no flags to discuss, or the call
    failed."""
    if not _enabled(cfg):
        return {}
    timeout = cfg.get("ai_commentary", {}).get("timeout_seconds", 45)

    summary = summarize_utilization_trends(views, cfg)
    results = {}
    for key, view_summary in summary.items():
        if view_summary["critical_count"] == 0 and view_summary["warning_count"] == 0:
            continue
        commentary = generate_commentary(view_summary, INSTRUCTION_UTIL, timeout)
        if commentary is not None:
            results[key] = commentary
    return results
