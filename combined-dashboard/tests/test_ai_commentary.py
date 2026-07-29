import json
import subprocess
from unittest.mock import patch

import pytest

from src import ai_commentary
from src.ai_commentary import (
    _call_claude,
    generate_commentary,
    generate_gm_commentary,
    generate_utilization_commentary,
)

VALID_STRUCTURED = {
    "headline": "MS1 tracking below plan",
    "risk_level": "medium",
    "narrative": "Revenue growth is decelerating relative to AOP.",
    "recommended_action": "Review MS1 pricing on the TO5 contract.",
}


def _envelope(structured=VALID_STRUCTURED, returncode=0):
    proc = subprocess.CompletedProcess(
        args=["claude"], returncode=returncode,
        stdout=json.dumps({"result": "ok", "structured_output": structured}),
        stderr="",
    )
    return proc


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_commentary, "CACHE_DIR", tmp_path / ".ai_cache")


# ─────────────────────────────────────────────────────────────────────────────
# _call_claude — subprocess success/failure paths
# ─────────────────────────────────────────────────────────────────────────────

def test_call_claude_parses_valid_structured_output():
    with patch("src.ai_commentary.subprocess.run", return_value=_envelope()) as mock_run:
        result = _call_claude({"division": "MS1"}, "instruction", 30)
    assert result == VALID_STRUCTURED
    assert mock_run.called


def test_call_claude_returns_none_when_claude_not_installed():
    with patch("src.ai_commentary.subprocess.run", side_effect=FileNotFoundError()):
        assert _call_claude({"division": "MS1"}, "instruction", 30) is None


def test_call_claude_returns_none_on_nonzero_exit():
    with patch("src.ai_commentary.subprocess.run", return_value=_envelope(returncode=1)):
        assert _call_claude({"division": "MS1"}, "instruction", 30) is None


def test_call_claude_returns_none_on_timeout():
    with patch("src.ai_commentary.subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=30)):
        assert _call_claude({"division": "MS1"}, "instruction", 30) is None


def test_call_claude_returns_none_on_malformed_stdout():
    bad_proc = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout="not json", stderr="")
    with patch("src.ai_commentary.subprocess.run", return_value=bad_proc):
        assert _call_claude({"division": "MS1"}, "instruction", 30) is None


def test_call_claude_returns_none_when_structured_output_missing_required_fields():
    incomplete = _envelope(structured={"headline": "only this"})
    with patch("src.ai_commentary.subprocess.run", return_value=incomplete):
        assert _call_claude({"division": "MS1"}, "instruction", 30) is None


# ─────────────────────────────────────────────────────────────────────────────
# generate_commentary — caching
# ─────────────────────────────────────────────────────────────────────────────

def test_generate_commentary_caches_and_skips_second_subprocess_call():
    payload = {"division": "MS1", "trend_direction": "up"}
    with patch("src.ai_commentary.subprocess.run", return_value=_envelope()) as mock_run:
        first = generate_commentary(payload, "instruction", 30)
        second = generate_commentary(payload, "instruction", 30)

    assert first == VALID_STRUCTURED
    assert second == VALID_STRUCTURED
    assert mock_run.call_count == 1


def test_generate_commentary_different_payloads_both_call_subprocess():
    with patch("src.ai_commentary.subprocess.run", return_value=_envelope()) as mock_run:
        generate_commentary({"division": "MS1"}, "instruction", 30)
        generate_commentary({"division": "MS2"}, "instruction", 30)
    assert mock_run.call_count == 2


def test_generate_commentary_returns_none_without_caching_on_failure():
    with patch("src.ai_commentary.subprocess.run", side_effect=FileNotFoundError()) as mock_run:
        first = generate_commentary({"division": "MS1"}, "instruction", 30)
        second = generate_commentary({"division": "MS1"}, "instruction", 30)
    assert first is None and second is None
    assert mock_run.call_count == 2  # no cache entry written on failure, so it retries


# ─────────────────────────────────────────────────────────────────────────────
# Public entry points — enabled/disabled + skip logic
# ─────────────────────────────────────────────────────────────────────────────

def test_generate_gm_commentary_returns_empty_when_disabled():
    cfg = {"ai_commentary": {"enabled": False}}
    forecasts = {"MS1": _fake_forecast()}
    assert generate_gm_commentary(forecasts, cfg) == {}


def test_generate_gm_commentary_skips_divisions_with_no_revenue():
    cfg = {"ai_commentary": {"enabled": True, "timeout_seconds": 30}}
    empty_forecast = _fake_forecast(historical_monthly=[("Jan 2026", 0, None)])
    with patch("src.ai_commentary.subprocess.run", return_value=_envelope()) as mock_run:
        result = generate_gm_commentary({"MS1": empty_forecast}, cfg)
    assert result == {}
    assert not mock_run.called


def test_generate_gm_commentary_returns_commentary_for_active_division():
    cfg = {"ai_commentary": {"enabled": True, "timeout_seconds": 30}}
    forecast = _fake_forecast()
    with patch("src.ai_commentary.subprocess.run", return_value=_envelope()):
        result = generate_gm_commentary({"MS1": forecast}, cfg)
    assert result == {"MS1": VALID_STRUCTURED}


def test_generate_utilization_commentary_returns_empty_when_disabled():
    cfg = {"ai_commentary": {"enabled": False}}
    assert generate_utilization_commentary([_fake_view()], cfg) == {}


def test_generate_utilization_commentary_skips_views_with_no_flags():
    cfg = {"ai_commentary": {"enabled": True, "timeout_seconds": 30}}
    clean_view = _fake_view(critical=0, warning=0)
    with patch("src.ai_commentary.subprocess.run", return_value=_envelope()) as mock_run:
        result = generate_utilization_commentary([clean_view], cfg)
    assert result == {}
    assert not mock_run.called


def test_generate_utilization_commentary_returns_commentary_for_flagged_view():
    cfg = {"ai_commentary": {"enabled": True, "timeout_seconds": 30}}
    flagged_view = _fake_view(critical=2, warning=1)
    with patch("src.ai_commentary.subprocess.run", return_value=_envelope()):
        result = generate_utilization_commentary([flagged_view], cfg)
    assert result == {"year_to_date": VALID_STRUCTURED}


def _fake_forecast(historical_monthly=None):
    from src.forecaster import DivisionForecast
    return DivisionForecast(
        division="MS1",
        historical_monthly=historical_monthly or [("Jan 2026", 100_000, 30.0)],
        projected_yearend_revenue=1_200_000,
        projected_yearend_gm=360_000,
        trend_direction="up",
        variance_vs_aop_annual=50_000,
    )


def _fake_view(view_id="ytd", critical=1, warning=0):
    return {
        "view_id": view_id,
        "view_label": "Year to Date",
        "view_period_range": "2026-01-01 to 2026-05-31",
        "critical_flags": [{"person": f"P{i}", "division": "MS1", "trend_type": "low_billable",
                             "explanation": "x"} for i in range(critical)],
        "warning_flags": [{"person": f"W{i}", "division": "MS1", "trend_type": "high_bp",
                            "explanation": "y"} for i in range(warning)],
        "division_rows": [],
        "portfolio_rows": [],
    }
