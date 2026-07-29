#!/usr/bin/env python3
"""
Local web server for the BCore Performance Dashboard.
Run:  python serve.py
Then open http://localhost:5001 and drop the Revenue/GM workbook and/or the
utilization workbook (at least one is required). Files are processed in
memory and never written to disk.
"""

import io
import json
import threading
import time
import traceback
import uuid
import warnings
import webbrowser
from datetime import date, datetime
from pathlib import Path

import yaml
from flask import Flask, abort, jsonify, render_template_string, request

ROOT = Path(__file__).parent
app = Flask(__name__)

MAX_UPLOAD_BYTES = 200 * 1024 * 1024   # 200 MB per file
ALLOWED_EXTENSIONS = (".xlsx", ".xls")
JOB_EXPIRY_SECONDS = 30 * 60           # sweep finished jobs after 30 minutes

# In-memory job store {job_id: {...}}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

# ── Last-uploaded file cache (enables settings re-run without re-upload) ──────
LAST_UPLOADS_DIR = ROOT / "data" / "last_uploads"
_LAST: dict = {"util_bytes": None, "util_name": "", "gm_bytes": None, "gm_name": ""}
_LAST_LOCK = threading.Lock()


def _persist_last_uploads(util_bytes, util_name, gm_bytes, gm_name):
    """Save file bytes to disk so re-runs survive server restarts."""
    LAST_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    if util_bytes:
        (LAST_UPLOADS_DIR / "util.xlsx").write_bytes(util_bytes)
        (LAST_UPLOADS_DIR / "util_name.txt").write_text(util_name or "util.xlsx")
    if gm_bytes:
        (LAST_UPLOADS_DIR / "gm.xlsx").write_bytes(gm_bytes)
        (LAST_UPLOADS_DIR / "gm_name.txt").write_text(gm_name or "gm.xlsx")


def _load_last_uploads():
    """Restore last-upload cache from disk on startup."""
    with _LAST_LOCK:
        for key, fname, nfname in [
            ("util_bytes", "util.xlsx", "util_name.txt"),
            ("gm_bytes",  "gm.xlsx",   "gm_name.txt"),
        ]:
            p = LAST_UPLOADS_DIR / fname
            np = LAST_UPLOADS_DIR / nfname
            if p.exists():
                _LAST[key] = p.read_bytes()
                _LAST[key.replace("_bytes", "_name")] = (
                    np.read_text().strip() if np.exists() else fname
                )


# ── Run history (persisted dashboards) ──────────────────────────────────────
HISTORY_DIR = ROOT / "output" / "history"
HISTORY_MANIFEST = HISTORY_DIR / "manifest.json"
HISTORY_LOCK = threading.Lock()


def _load_history_unlocked() -> list[dict]:
    if not HISTORY_MANIFEST.exists():
        return []
    try:
        return json.loads(HISTORY_MANIFEST.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _load_history() -> list[dict]:
    with HISTORY_LOCK:
        return _load_history_unlocked()


def _save_run_to_history(run_id: str, html: str, label: str, stats: dict) -> None:
    """Writes the rendered HTML to output/history/<run_id>.html and records
    a manifest entry so it can be reopened later from the upload page."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    (HISTORY_DIR / f"{run_id}.html").write_text(html, encoding="utf-8")

    entry = {
        "id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "label": label,
        **stats,
    }
    with HISTORY_LOCK:
        entries = _load_history_unlocked()
        entries.insert(0, entry)
        HISTORY_MANIFEST.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def _sweep_expired_jobs() -> None:
    """Drop finished jobs older than JOB_EXPIRY_SECONDS so a long-running
    local server session doesn't leak memory across repeated uploads."""
    now = time.time()
    with JOBS_LOCK:
        expired = [
            jid for jid, job in JOBS.items()
            if job["status"] in ("done", "error", "awaiting_confirmation")
            and now - job["created_at"] > JOB_EXPIRY_SECONDS
        ]
        for jid in expired:
            del JOBS[jid]


# ── Upload page ───────────────────────────────────────────────────────────────

UPLOAD_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BCore Performance Dashboard</title>
<style>
:root {
  --ground:   #F3F6FA;
  --surface:  #FFFFFF;
  --elevated: #EEF2F8;
  --border:   #D1D9E8;
  --text-1:   #111827;
  --text-2:   #4B5675;
  --text-3:   #8A94A8;
  --accent:   #2563EB;
  --crit:     #B91C1C;
  --crit-bg:  #FEF2F2;
  --crit-bd:  #FCA5A5;
  --warn:     #92400E;
  --warn-bg:  #FFFBEB;
  --warn-bd:  #FCD34D;
  --good:     #065F46;
  --good-bg:  #ECFDF5;
  --good-bd:  #A7F3D0;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--ground); color: var(--text-1);
  display: flex; min-height: 100vh;
}

/* ── Left branding panel ──────────────────────────────────────────────────── */
.left-panel {
  width: 380px; flex-shrink: 0;
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column;
  padding: 48px 40px;
  position: sticky; top: 0; height: 100vh; overflow-y: auto;
}
@media (max-width: 860px) { .left-panel { display: none; } }

.brand-lockup {
  display: flex; align-items: center; gap: 12px; margin-bottom: 40px;
}
.brand-mark {
  width: 36px; height: 36px; border-radius: 5px;
  background: var(--accent); display: flex; align-items: center; justify-content: center;
  font-size: 14px; font-weight: 900; color: #FFFFFF; letter-spacing: -0.5px;
  flex-shrink: 0;
}
.brand-name { font-size: 14px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
.brand-tagline { font-size: 12px; color: var(--text-3); margin-top: 1px; }

.left-headline {
  font-size: 22px; font-weight: 700; line-height: 1.3;
  margin-bottom: 14px; color: var(--text-1);
}
.left-desc {
  font-size: 14px; color: var(--text-2); line-height: 1.65; margin-bottom: 36px;
}

.steps-label {
  font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em;
  color: var(--text-3); margin-bottom: 16px;
}
.steps { list-style: none; display: flex; flex-direction: column; gap: 20px; }
.step { display: flex; gap: 14px; align-items: flex-start; }
.step-num {
  width: 26px; height: 26px; border-radius: 50%;
  background: var(--elevated); border: 1px solid var(--border);
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 700; color: var(--accent);
  flex-shrink: 0; margin-top: 1px;
}
.step-body { flex: 1; }
.step-title { font-size: 14px; font-weight: 600; color: var(--text-1); margin-bottom: 3px; }
.step-desc  { font-size: 13px; color: var(--text-2); line-height: 1.5; }

.left-footer {
  margin-top: auto; padding-top: 32px;
  font-size: 12px; color: var(--text-3); line-height: 1.6;
  border-top: 1px solid var(--border);
}
.privacy-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--good); margin-right: 6px; vertical-align: middle; }

/* ── Right content panel ──────────────────────────────────────────────────── */
.right-panel {
  flex: 1; display: flex; align-items: flex-start; justify-content: center;
  padding: 56px 24px; min-height: 100vh;
}
.card {
  background: var(--surface); border-radius: 4px; padding: 40px 44px;
  box-shadow: 0 4px 20px rgba(0,0,0,.08); max-width: 600px; width: 100%;
  border: 1px solid var(--border);
}

h1 { font-size: 1.2rem; font-weight: 700; color: var(--text-1); margin-bottom: 6px; }
.subtitle { font-size: 14px; color: var(--text-2); margin-bottom: 28px; line-height: 1.6; }

.screen { display: none; }
.screen.active { display: block; }

/* ── Landing screen ───────────────────────────────────────────────────────── */
.upload-cta {
  display: inline-flex; align-items: center; gap: 8px;
  background: var(--accent); color: #fff; border: none; border-radius: 3px;
  padding: 13px 28px; font-size: 14px; font-weight: 700;
  cursor: pointer; transition: filter .15s, transform .1s;
  margin-bottom: 10px;
}
.upload-cta:hover { filter: brightness(1.15); }
.upload-cta:active { transform: scale(.98); }
.btn-guide {
  display: block; width: 100%; padding: 9px; margin-bottom: 20px;
  background: none; border: 1px solid var(--border); border-radius: 4px;
  color: var(--text-2); font-size: 13px; font-weight: 500; cursor: pointer;
  transition: color .15s, border-color .15s; text-align: center;
}
.btn-guide:hover { color: var(--accent); border-color: var(--accent); }

.section-label {
  font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
  color: var(--text-3); text-align: left; margin-bottom: 12px;
}
.history-list { text-align: left; max-height: 360px; overflow-y: auto; padding-right: 2px; }
.history-empty { font-size: 14px; color: var(--text-3); font-style: italic; padding: 28px 0; text-align: center; }
.history-row {
  display: flex; align-items: center; gap: 14px;
  padding: 13px 15px; border-radius: 3px; border: 1px solid var(--border); margin-bottom: 8px;
  text-decoration: none; color: inherit; transition: border-color .15s, background .15s;
}
.history-row:hover { background: var(--elevated); border-color: var(--accent); }
.history-icon {
  font-size: 1.2rem; flex-shrink: 0; width: 38px; height: 38px; border-radius: 3px;
  background: var(--elevated); display: flex; align-items: center; justify-content: center;
}
.history-main { min-width: 0; flex: 1; }
.history-label { font-size: 14px; font-weight: 600; color: var(--text-1); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.history-meta  { font-size: 12px; color: var(--text-2); margin-top: 3px; }
.history-flags { display: flex; gap: 6px; flex-shrink: 0; }
.history-flag  { font-size: 10px; font-weight: 700; border-radius: 10px; padding: 3px 9px; white-space: nowrap; }
.history-flag.crit { background: var(--crit-bg); color: var(--crit); border: 1px solid var(--crit-bd); }
.history-flag.warn { background: var(--warn-bg); color: var(--warn); border: 1px solid var(--warn-bd); }
.history-flag.ok   { background: var(--good-bg); color: var(--good); border: 1px solid var(--good-bd); }

.danger-link {
  display: inline-block; margin-top: 16px; font-size: 12px;
  color: var(--crit); text-decoration: none; cursor: pointer; opacity: .7;
}
.danger-link:hover { opacity: 1; text-decoration: underline; }

/* ── Modal ────────────────────────────────────────────────────────────────── */
.modal-overlay {
  position: fixed; inset: 0; background: rgba(13, 17, 26, .6);
  display: flex; align-items: center; justify-content: center; z-index: 100;
}
.modal-box {
  background: var(--surface); border-radius: 4px; padding: 26px 28px; max-width: 420px;
  text-align: left; box-shadow: 0 20px 60px rgba(0, 0, 0, .6); border: 1px solid var(--border);
}
.modal-box h3 { margin: 0 0 10px; font-size: 16px; color: var(--crit); }
.modal-box p { font-size: 13px; color: var(--text-2); line-height: 1.5; margin: 0 0 10px; }
.modal-box code { background: var(--elevated); color: var(--text-1); border-radius: 4px; padding: 1px 5px; font-size: 12px; }
.modal-box input {
  width: 100%; box-sizing: border-box; padding: 9px 12px; border-radius: 3px;
  border: 1px solid var(--border); background: var(--ground); color: var(--text-1); font-size: 13px; margin-bottom: 6px;
}
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 14px; }
.modal-actions button { border-radius: 3px; padding: 8px 18px; font-size: 13px; font-weight: 600; cursor: pointer; border: none; }
.modal-actions .btn-cancel { background: var(--elevated); color: var(--text-1); }
.modal-actions .btn-danger { background: var(--crit); color: #fff; }
.modal-actions .btn-danger:disabled { background: var(--crit-bg); color: var(--text-3); cursor: not-allowed; }
.modal-error { font-size: 12px; color: var(--crit); margin-top: 8px; min-height: 1em; }

/* ── Upload screen ────────────────────────────────────────────────────────── */
.back-link {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 13px; color: var(--text-2); text-decoration: none; cursor: pointer;
  margin-bottom: 20px;
}
.back-link:hover { color: var(--accent); }

.drop-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 8px; }
.drop-zone {
  border: 2px dashed var(--border); border-radius: 3px;
  padding: 26px 14px; cursor: pointer; transition: all .2s;
  position: relative; text-align: center;
}
.drop-zone.over   { border-color: var(--accent); background: rgba(77,143,214,.08); }
.drop-zone.filled { border-color: var(--good);   background: rgba(60,168,112,.08); }
.drop-zone input[type=file] { position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%; }
.drop-icon  { font-size: 1.8rem; margin-bottom: 8px; }
.drop-label { font-size: 14px; color: var(--text-1); font-weight: 600; }
.drop-sub   { font-size: 12px; color: var(--text-3); margin-top: 4px; }
.file-chosen { font-size: 12px; color: var(--text-2); margin-top: 8px; font-weight: 500; min-height: 16px; word-break: break-all; }
.note { font-size: 12px; color: var(--text-3); margin: 12px 0 4px; }

.btn {
  display: inline-block; margin-top: 18px;
  background: var(--accent); color: #fff; border: none; border-radius: 3px;
  padding: 12px 36px; font-size: 14px; font-weight: 700;
  cursor: pointer; transition: filter .15s;
}
.btn:hover:not(:disabled) { filter: brightness(1.15); }
.btn:disabled { background: var(--elevated); color: var(--text-3); cursor: not-allowed; }

.progress-area { margin-top: 24px; display: none; text-align: left; }
.progress-bar-wrap { height: 5px; background: var(--elevated); border-radius: 3px; overflow: hidden; margin-bottom: 14px; }
.progress-bar { height: 100%; background: var(--accent); width: 0%; transition: width .4s; border-radius: 3px; }
.log-box {
  background: var(--ground); border: 1px solid var(--border); border-radius: 3px;
  padding: 10px 14px; font-size: 12px; font-family: monospace;
  max-height: 220px; overflow-y: auto; text-align: left; color: var(--text-1);
}
.confirm-box {
  margin-top: 16px; padding: 14px 16px; border-radius: 3px;
  background: var(--warn-bg); border: 1px solid var(--warn-bd); border-left: 4px solid var(--warn); text-align: left;
}
.confirm-box .confirm-title  { font-weight: 600; font-size: 14px; color: var(--warn); margin-bottom: 6px; }
.confirm-box .confirm-detail { font-size: 13px; color: var(--text-2); margin-bottom: 10px; }
.confirm-actions { display: flex; gap: 10px; }
.confirm-actions button { border-radius: 3px; padding: 8px 18px; font-size: 13px; font-weight: 600; cursor: pointer; border: none; }
.confirm-actions .btn-confirm { background: var(--accent); color: #fff; }
.confirm-actions .btn-cancel  { background: var(--elevated); color: var(--text-1); }

.log-line { padding: 2px 0; border-bottom: 1px solid var(--border); }
.log-line.ok   { color: #4ade80; }
.log-line.warn { color: #f5a623; }
.log-line.err  { color: #f87171; }

.status-msg { font-size: 14px; color: var(--text-2); margin-bottom: 8px; }
.status-msg.error { color: var(--crit); font-weight: 500; }

/* Widen card when FAQ screen is active */
.card.faq-open { max-width: 820px; }

/* ── FAQ screen ──────────────────────────────────────────────────────────── */
.faq-tabs { display: flex; gap: 4px; margin-bottom: 22px; border-bottom: 1px solid var(--border); }
.faq-tab {
  padding: 8px 16px; font-size: 13px; font-weight: 600;
  color: var(--text-3); background: none; border: none; cursor: pointer;
  border-bottom: 3px solid transparent; margin-bottom: -1px;
  transition: color .15s;
}
.faq-tab:hover { color: var(--text-1); }
.faq-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.faq-panel { display: none; }
.faq-panel.active { display: block; }

.faq-h2 { font-size: 13px; font-weight: 700; color: var(--text-1); margin: 18px 0 8px; letter-spacing: .02em; }
.faq-h2:first-child { margin-top: 0; }
.faq-p { font-size: 13px; color: var(--text-2); line-height: 1.6; margin-bottom: 10px; }
.faq-tbl-wrap { overflow-x: auto; margin-bottom: 14px; border-radius: 3px; border: 1px solid var(--border); }
.faq-tbl { width: 100%; border-collapse: collapse; font-size: 12px; margin-bottom: 0; }
.faq-tbl th {
  background: var(--elevated); text-align: left; padding: 6px 10px;
  color: var(--text-2); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
  border-bottom: 1px solid var(--border);
}
.faq-tbl td { padding: 6px 10px; border-bottom: 1px solid var(--border); color: var(--text-2); vertical-align: top; }
.faq-tbl td:first-child { color: var(--text-1); font-weight: 600; white-space: nowrap; font-family: 'Courier New', monospace; font-size: 11px; }
.faq-tbl tr:last-child td { border-bottom: none; }
.faq-chips { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 12px; }
.faq-chip {
  font-family: 'Courier New', monospace; font-size: 11px;
  background: var(--elevated); border: 1px solid var(--border);
  color: var(--text-1); padding: 3px 8px; border-radius: 3px;
}
.faq-note {
  font-size: 12px; color: var(--text-3); background: var(--elevated);
  border-left: 3px solid var(--accent); border-radius: 2px;
  padding: 8px 12px; margin-bottom: 12px; line-height: 1.5;
}
.faq-warn {
  font-size: 12px; color: var(--warn); background: var(--warn-bg);
  border: 1px solid var(--warn-bd); border-left: 3px solid var(--warn);
  border-radius: 2px; padding: 8px 12px; margin-bottom: 12px; line-height: 1.5;
}

/* Claude prompt box */
.prompt-box-wrap { position: relative; margin-top: 10px; }
.prompt-box {
  width: 100%; height: 380px; font-family: 'Courier New', monospace; font-size: 11px;
  line-height: 1.55; background: var(--ground); color: var(--text-1);
  border: 1px solid var(--border); border-radius: 3px; padding: 14px;
  resize: vertical; box-sizing: border-box; white-space: pre;
}
.prompt-copy-btn {
  position: absolute; top: 8px; right: 8px;
  background: var(--elevated); border: 1px solid var(--border);
  color: var(--text-2); border-radius: 3px; padding: 4px 12px;
  font-size: 11px; font-weight: 600; cursor: pointer; transition: color .15s, background .15s;
}
.prompt-copy-btn:hover { color: var(--text-1); background: var(--surface); }
.prompt-copy-btn.copied { color: var(--good); border-color: var(--good-bd); }
</style>
</head>
<body>

<!-- ═══════════ Left branding panel ═══════════ -->
<aside class="left-panel">
  <div class="brand-lockup">
    <div class="brand-mark">BC</div>
    <div>
      <div class="brand-name">BCore</div>
      <div class="brand-tagline">Performance Intelligence</div>
    </div>
  </div>

  <h2 class="left-headline">Know where your revenue risk is before the quarter ends.</h2>
  <p class="left-desc">
    Tracks employee utilization and billable hours alongside team revenue vs. plan.
    Any division underperforming on <strong style="color:var(--text-1)">both</strong>
    revenue and utilization simultaneously is flagged — that combination usually signals
    something structural, not noise.
  </p>

  <div class="steps-label">How it works</div>
  <ol class="steps">
    <li class="step">
      <div class="step-num">1</div>
      <div class="step-body">
        <div class="step-title">Upload your workbooks</div>
        <div class="step-desc">Drop in your Revenue/GM workbook and/or Utilization workbook. One or both.</div>
      </div>
    </li>
    <li class="step">
      <div class="step-num">2</div>
      <div class="step-body">
        <div class="step-title">Dashboard generates automatically</div>
        <div class="step-desc">Analysis runs locally in 30–90 seconds. Flags anomalies, computes trends, drills into root causes.</div>
      </div>
    </li>
    <li class="step">
      <div class="step-num">3</div>
      <div class="step-body">
        <div class="step-title">Review the interactive report</div>
        <div class="step-desc">Utilization by team, GM vs. AOP by project, cross-dataset flags, workforce health — all in one page.</div>
      </div>
    </li>
  </ol>

  <div class="left-footer">
    <span class="privacy-dot"></span>
    <strong style="color:var(--text-2)">Processed locally.</strong>
    Your workbooks are analyzed in memory and never written to disk.
    No data leaves your machine.
  </div>
</aside>

<!-- ═══════════ Right content panel ═══════════ -->
<main class="right-panel">
<div class="card">

  <!-- ═══════════ Landing screen: recent dashboards + upload CTA ═══════════ -->
  <div class="screen{{ ' active' if history else '' }}" id="screen-landing">
    <h1>BCore Performance Dashboard</h1>
    <p class="subtitle">Select a recent dashboard or upload new workbooks.</p>

    <button class="upload-cta" onclick="showScreen('upload')">+ Upload New Data</button>
    <button class="btn-guide" onclick="showScreen('faq')">&#128196; Data Format Guide &amp; FAQ</button>

    <div class="section-label">Recent Dashboards</div>
    <div class="history-list">
      {% if history %}
        {% for h in history %}
        <a class="history-row" href="/history/{{ h.id }}" target="_blank">
          <div class="history-icon">{{ '📊' if (h.gm_name and h.util_name) else ('💵' if h.gm_name else '👥') }}</div>
          <div class="history-main">
            <div class="history-label">{{ h.label }}</div>
            <div class="history-meta" data-ts="{{ h.created_at }}">
              {{ h.created_at.replace('T', ' ') }}
              {% if h.employees_analyzed is defined %} &middot; {{ h.employees_analyzed }} employees{% endif %}
            </div>
          </div>
          <div class="history-flags">
            {% if h.critical_count is defined and h.critical_count > 0 %}
              <span class="history-flag crit">{{ h.critical_count }}C</span>
            {% endif %}
            {% if h.warning_count is defined and h.warning_count > 0 %}
              <span class="history-flag warn">{{ h.warning_count }}W</span>
            {% endif %}
            {% if h.critical_count is defined and h.critical_count == 0 and h.warning_count == 0 %}
              <span class="history-flag ok">All OK</span>
            {% endif %}
          </div>
        </a>
        {% endfor %}
      {% else %}
        <div class="history-empty">No saved dashboards yet — generate one and it'll show up here.</div>
      {% endif %}
    </div>

    <a class="danger-link" onclick="openClearDataModal()">Clear All Data</a>
  </div>

  <!-- ═══════════ Clear-data confirmation modal ═══════════ -->
  <div class="modal-overlay" id="clear-data-modal" style="display:none;">
    <div class="modal-box">
      <h3>Clear All Data</h3>
      <p>This permanently deletes the structured data store
        (<code>data/bcore.db</code>) and the insights export
        (<code>exports/insights_latest.json</code>) — the period/division
        rollups used for duplicate-upload detection and cross-dataset
        flagging. Your saved dashboard history above is <strong>not</strong>
        affected and the raw workbooks you uploaded are never stored on disk
        in the first place.</p>
      <p>Type <strong>CONFIRM</strong> below to proceed.</p>
      <input type="text" id="clear-data-input" placeholder="Type CONFIRM" autocomplete="off">
      <div class="modal-actions">
        <button class="btn-cancel" onclick="closeClearDataModal()">Cancel</button>
        <button class="btn-danger" id="clear-data-btn" disabled onclick="clearAllData()">Delete</button>
      </div>
      <div class="modal-error" id="clear-data-error"></div>
    </div>
  </div>

  <!-- ═══════════ Upload screen: drop zones + progress log ═══════════ -->
  <div class="screen{{ '' if history else ' active' }}" id="screen-upload">
    <h1>Upload Workbooks</h1>
    <p class="subtitle">Drop one or both workbooks below. Processed locally — data never leaves your machine.</p>

    {% if history %}
    <a class="back-link" onclick="showScreen('landing')">&larr; Back to recent dashboards</a>
    {% endif %}

    <div class="drop-grid">
      <div class="drop-zone" id="drop-zone-gm">
        <input type="file" id="file-input-gm" accept=".xlsx,.xls">
        <div class="drop-icon">💵</div>
        <div class="drop-label">Revenue &amp; GM workbook</div>
        <div class="drop-sub">GM Report / AOP / Compare sheets</div>
      </div>
      <div class="drop-zone" id="drop-zone-util">
        <input type="file" id="file-input-util" accept=".xlsx,.xls">
        <div class="drop-icon">👥</div>
        <div class="drop-label">Utilization workbook</div>
        <div class="drop-sub">Export / Lookups / etc.</div>
      </div>
    </div>
    <div class="file-chosen" id="file-chosen-gm"></div>
    <div class="file-chosen" id="file-chosen-util"></div>
    <div class="note">At least one workbook is required.</div>

    <button class="btn" id="run-btn" onclick="startJob()" disabled>Generate Dashboard</button>

    <div class="progress-area" id="progress-area">
      <div class="status-msg" id="status-msg">Starting...</div>
      <div class="progress-bar-wrap"><div class="progress-bar" id="progress-bar"></div></div>
      <div class="log-box" id="log-box"></div>
      <div class="confirm-box" id="confirm-box" style="display:none;">
        <div class="confirm-title" id="confirm-title"></div>
        <div class="confirm-detail" id="confirm-detail"></div>
        <div class="confirm-actions">
          <button class="btn-confirm" onclick="confirmJob()">Confirm &amp; Overwrite</button>
          <button class="btn-cancel" onclick="cancelJob()">Cancel</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══════════ FAQ / Data Format Guide screen ═══════════ -->
  <div class="screen" id="screen-faq">
    <h1>Data Format Guide</h1>
    <p class="subtitle">Reference for formatting workbooks and fixing broken ones with Claude.</p>
    <a class="back-link" onclick="showScreen('landing')" style="cursor:pointer;">&larr; Back to home</a>

    <div class="faq-tabs">
      <button class="faq-tab active" id="ftab-util" onclick="showFaqPanel('util')">Utilization Workbook</button>
      <button class="faq-tab" id="ftab-gm" onclick="showFaqPanel('gm')">GM / Revenue Workbook</button>
      <button class="faq-tab" id="ftab-claude" onclick="showFaqPanel('claude')">Claude Reformatter</button>
    </div>

    <!-- ── UTILIZATION PANEL ─────────────────────────────────── -->
    <div class="faq-panel active" id="faq-util">
      <div class="faq-h2">Required Sheets</div>
      <p class="faq-p">Sheet names must match <strong>exactly</strong> (case-sensitive). Configurable in <code>config.yaml</code> under <code>sheets:</code>.</p>
      <div class="faq-tbl-wrap">
        <table class="faq-tbl">
          <thead><tr><th>Sheet Name</th><th>Purpose</th><th>Required?</th></tr></thead>
          <tbody>
            <tr><td>Export</td><td>Daily time entries — one row per employee per date per hour-type. Core data source.</td><td>Yes</td></tr>
            <tr><td>Portfolio Leads Proj Types</td><td>Maps employees to their portfolio lead and project type.</td><td>Yes</td></tr>
            <tr><td>Unique Employees</td><td>Full employee roster for period coverage checks.</td><td>Yes</td></tr>
            <tr><td>First Days Last Days</td><td>Employment start and end dates per employee.</td><td>Yes</td></tr>
            <tr><td>Current PTO Balances</td><td>Accrued PTO hours per employee.</td><td>Yes</td></tr>
            <tr><td>Billable Ute</td><td>Billable utilization targets by period (used when <code>source: lookups</code>).</td><td>Conditional</td></tr>
            <tr><td>Lookups</td><td>Period calendar. Only needed when <code>period_detection.source: lookups</code>.</td><td>Conditional</td></tr>
            <tr><td>Discrepancies</td><td>Pre-flagged issues from the source system. Shown in the Workbook Scan panel.</td><td>No</td></tr>
          </tbody>
        </table>
      </div>

      <div class="faq-h2">Export Sheet — Required Columns</div>
      <p class="faq-p">Row 1 must contain these exact header names. Extra columns are ignored.</p>
      <div class="faq-tbl-wrap">
        <table class="faq-tbl">
          <thead><tr><th>Column Name</th><th>Description</th></tr></thead>
          <tbody>
            <tr><td>Person</td><td>Employee full name. Must be spelled <em>identically</em> across all sheets — extra spaces or different capitalization will cause mismatches.</td></tr>
            <tr><td>PersonOrganization</td><td>Organizational unit code.</td></tr>
            <tr><td>PersonDivision</td><td>Division code. Must be one of: <code>MS1</code>, <code>MS2</code>, <code>MS3</code>, <code>IS1</code>, <code>BL1</code>, or <code>Corp</code>.</td></tr>
            <tr><td>Date</td><td>Date of the entry. Accepts Excel serial dates or <code>YYYY-MM-DD</code> text.</td></tr>
            <tr><td>Hours</td><td>Hours as a decimal number (e.g. <code>4.5</code>). Must be numeric — formula cells that resolve to text will fail.</td></tr>
            <tr><td>ProjectSubGroup</td><td>Hour type. Must be <strong>exactly</strong> one of the values listed below.</td></tr>
            <tr><td>ProjectCode</td><td>Project identifier code.</td></tr>
            <tr><td>ProjectTitle</td><td>Project name.</td></tr>
            <tr><td>PayCode</td><td>Pay code. Rows where PayCode equals <code>UL</code> are excluded from analysis.</td></tr>
          </tbody>
        </table>
      </div>

      <div class="faq-h2">ProjectSubGroup — Valid Values</div>
      <p class="faq-p">These values are <strong>case-sensitive and must match exactly</strong>. Rows with any other value are ignored.</p>
      <div class="faq-chips">
        <span class="faq-chip">ProjectBillable</span>
        <span class="faq-chip">ProjectNonBillable</span>
        <span class="faq-chip">B&amp;P</span>
        <span class="faq-chip">G&amp;A</span>
        <span class="faq-chip">IR&amp;D</span>
        <span class="faq-chip">Overhead</span>
        <span class="faq-chip">PTO</span>
        <span class="faq-chip">Holiday</span>
        <span class="faq-chip">LWOP</span>
        <span class="faq-chip">Other</span>
      </div>
      <div class="faq-warn">Common pitfalls: &ldquo;Billable&rdquo; instead of &ldquo;ProjectBillable&rdquo; &middot; &ldquo;Bid &amp; Proposal&rdquo; instead of &ldquo;B&amp;P&rdquo; &middot; &ldquo;G &amp; A&rdquo; with spaces &middot; trailing whitespace in any value.</div>

      <div class="faq-h2">Portfolio Leads Sheet — Required Columns</div>
      <div class="faq-tbl-wrap">
        <table class="faq-tbl">
          <thead><tr><th>Column Name</th><th>Description</th></tr></thead>
          <tbody>
            <tr><td>Person</td><td>Must match the <code>Export</code> sheet Person column exactly — same spelling, capitalization, and spacing.</td></tr>
            <tr><td>Portfolio Lead</td><td>Name of the portfolio lead or manager.</td></tr>
            <tr><td>Project Type</td><td>Project type classification (Cost-Plus, FFP, T&amp;M, etc.).</td></tr>
          </tbody>
        </table>
      </div>

      <div class="faq-h2">Period Detection</div>
      <p class="faq-p">Controlled by <code>period_detection</code> in <code>config.yaml</code>:</p>
      <div class="faq-tbl-wrap">
        <table class="faq-tbl">
          <thead><tr><th>Setting</th><th>Options</th><th>Behavior</th></tr></thead>
          <tbody>
            <tr><td>source</td><td><code>export</code> / <code>lookups</code></td><td>When <code>export</code> (default), periods are derived from the Date range in the Export sheet. When <code>lookups</code>, the Lookups sheet calendar is used.</td></tr>
            <tr><td>grouping</td><td><code>monthly</code> / <code>biweekly</code></td><td>How dates are grouped. Monthly = one period per calendar month. Biweekly = 14-day windows from the earliest date.</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ── GM / REVENUE PANEL ────────────────────────────────── -->
    <div class="faq-panel" id="faq-gm">
      <div class="faq-h2">Sheet Auto-Detection</div>
      <p class="faq-p">Sheet names are detected by keyword — exact names are not required. Include the keyword anywhere in the tab name.</p>
      <div class="faq-tbl-wrap">
        <table class="faq-tbl">
          <thead><tr><th>Keyword in Tab Name</th><th>Parsed As</th></tr></thead>
          <tbody>
            <tr><td>GM Report</td><td>Monthly GM actuals — revenue, gross margin, direct costs by program</td></tr>
            <tr><td>AOP</td><td>Annual Operating Plan — monthly revenue and gross profit targets by division</td></tr>
            <tr><td>Compare</td><td>Month-over-month comparison — actuals vs AOP by program</td></tr>
          </tbody>
        </table>
      </div>
      <div class="faq-note">All other sheets are ignored. Multiple GM Report sheets are supported (e.g. &ldquo;Sep GM Report&rdquo;, &ldquo;Oct GM Report&rdquo;) — all will be parsed.</div>

      <div class="faq-h2">GM Report Sheet — Required Columns</div>
      <p class="faq-p">Column header matching is flexible. One spelling from each group must be present.</p>
      <div class="faq-tbl-wrap">
        <table class="faq-tbl">
          <thead><tr><th>Canonical Name</th><th>Accepted Spellings</th></tr></thead>
          <tbody>
            <tr><td>AOP Legend</td><td>AOP Legend, AOPLegend</td></tr>
            <tr><td>Org</td><td>Org, ORG, Organization, Division</td></tr>
            <tr><td>Revenue</td><td>Revenue, revenue</td></tr>
            <tr><td>GrossMargin</td><td>GrossMargin, Gross Margin, GM</td></tr>
            <tr><td>GrossMarginPercentage</td><td>GrossMarginPercentage, Gross Margin %, GM%, GM %</td></tr>
            <tr><td>TotalDirectCost</td><td>TotalDirectCost, TotalDirectCosts, Total Direct Cost, Total Direct Costs</td></tr>
          </tbody>
        </table>
      </div>

      <div class="faq-h2">AOP Sheet — Fixed Layout</div>
      <p class="faq-p">The AOP sheet uses a fixed grid — do not add or remove rows or columns.</p>
      <div class="faq-tbl-wrap">
        <table class="faq-tbl">
          <thead><tr><th>Location</th><th>Content</th></tr></thead>
          <tbody>
            <tr><td>Column C (index 2)</td><td>Division names. Use: <code>MS1</code>, <code>MS2</code>, <code>MS3</code>, <code>IS1</code>, <code>BL1</code>. Aliases: <code>Fuel</code>&rarr;MS3, <code>Insight</code>&rarr;IS1, <code>BCore Labs</code>&rarr;BL1.</td></tr>
            <tr><td>Columns D–O (index 3–14)</td><td>Monthly values January through December.</td></tr>
            <tr><td>Rows 3–7 (Excel)</td><td>Revenue per division (one row per division).</td></tr>
            <tr><td>Rows 12–16 (Excel)</td><td>Gross Profit per division (one row per division).</td></tr>
          </tbody>
        </table>
      </div>

      <div class="faq-h2">Compare Sheet — Required Columns</div>
      <div class="faq-tbl-wrap">
        <table class="faq-tbl">
          <thead><tr><th>Canonical Name</th><th>Accepted Spellings</th></tr></thead>
          <tbody>
            <tr><td>Program</td><td>Program, ProgramName, Program Name</td></tr>
            <tr><td>Portfolio</td><td>Portfolio, Portfolio Lead, PortfolioLead</td></tr>
            <tr><td>Actuals</td><td>Actuals, Actual, Rev Actual</td></tr>
            <tr><td>AOP</td><td>AOP</td></tr>
            <tr><td>Var</td><td>Var, Variance, Var $</td></tr>
            <tr><td>GP Actual</td><td>GP Actual, GPActual, GP Act</td></tr>
            <tr><td>AOP GM%</td><td>AOP GM%, AOP GM %, AOPGM%</td></tr>
            <tr><td>GP AOP</td><td>GP AOP, GPAOP</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ── CLAUDE REFORMATTER PANEL ──────────────────────────── -->
    <div class="faq-panel" id="faq-claude">
      <div class="faq-h2">How to Use</div>
      <p class="faq-p">Copy the instructions below and paste them into a new Claude conversation along with your broken Excel file. Claude will read the file and produce a corrected version the dashboard can load. You can also run the <code>/fix-excel</code> skill in Claude Code to automate this.</p>
      <div class="faq-note">These instructions cover both workbooks. Paste them in full even if you only have one — Claude will skip inapplicable sections.</div>

      <div class="faq-h2">Reformatting Instructions (copy all)</div>
      <div class="prompt-box-wrap">
        <button class="prompt-copy-btn" id="copy-btn" onclick="copyPrompt()">Copy</button>
        <textarea class="prompt-box" id="claude-prompt" readonly>BCORE DASHBOARD — WORKBOOK REFORMATTING INSTRUCTIONS
You are reformatting an Excel workbook so it can be loaded by the BCore Performance Dashboard.
Follow every rule exactly. Output a corrected Excel file. Do not add commentary — produce the file.

══════════════════════════════════════════════════════════
UTILIZATION WORKBOOK
══════════════════════════════════════════════════════════

REQUIRED SHEETS — rename so the names match exactly (case-sensitive):
  "Export"                      daily time entries (one row per employee per date per hour type)
  "Portfolio Leads Proj Types"  employee-to-portfolio-lead mapping
  "Unique Employees"            full employee roster
  "First Days Last Days"        employment date ranges
  "Current PTO Balances"        accrued PTO hours
  "Billable Ute"                billable utilization targets by period
  "Lookups"                     period calendar (only needed if source=lookups in config)
  "Discrepancies"               pre-flagged issues (optional)

EXPORT SHEET — row 1 must have these exact column headers:
  Person            employee full name (must be spelled identically across all sheets)
  PersonOrganization  organizational unit code
  PersonDivision    division code — must be exactly one of: MS1 MS2 MS3 IS1 BL1 Corp
  Date              date (Excel serial date or YYYY-MM-DD string)
  Hours             numeric decimal hours per entry (e.g. 4.5)
  ProjectSubGroup   hour type — must be EXACTLY one of these values (case-sensitive):
                      ProjectBillable
                      ProjectNonBillable
                      B&P
                      G&A
                      IR&D
                      Overhead
                      PTO
                      Holiday
                      LWOP
                      Other
  ProjectCode       project identifier
  ProjectTitle      project name
  PayCode           pay code — rows where this is "UL" are excluded from analysis

PORTFOLIO LEADS SHEET — row 1 must have these exact column headers:
  Person            must match Export.Person exactly (same spelling, spacing, capitalization)
  Portfolio Lead    manager or portfolio lead name
  Project Type      project type classification

COMMON PROBLEMS — check and fix each:
  1. ProjectSubGroup contains different labels than the exact list above
     Examples to fix: "Billable" -> "ProjectBillable"  |  "Bid & Proposal" -> "B&P"
                      "G & A" -> "G&A"  |  "Non-Billable" -> "ProjectNonBillable"
  2. Person names are inconsistent between sheets (extra spaces, title case vs all-caps, etc.)
     Fix: standardize to the same format in every sheet
  3. Date column contains text strings instead of proper dates — convert to Excel dates
  4. Hours column contains non-numeric values, text, or formula errors — replace with plain numbers
  5. PersonDivision values do not match the allowed list (MS1/MS2/MS3/IS1/BL1/Corp)
  6. Sheet names have leading/trailing spaces or wrong capitalization — rename exactly as listed above
  7. Merged cells in header rows — unmerge all header cells, put the header in the leftmost cell

══════════════════════════════════════════════════════════
GM / REVENUE WORKBOOK
══════════════════════════════════════════════════════════

SHEET NAMING — sheets are detected by keyword in the tab name:
  Include "GM Report" in any monthly actuals sheet name
  Include "AOP" in the annual operating plan sheet name
  Include "Compare" in any month-over-month comparison sheet name
  All other sheets are ignored

GM REPORT SHEET — required columns (flexible spelling, pick one from each group):
  AOP Legend     -> "AOP Legend" or "AOPLegend"
  Division       -> "Org", "ORG", "Organization", "Division"
  Revenue        -> "Revenue"
  Gross Margin   -> "GrossMargin", "Gross Margin", "GM"
  GM Percent     -> "GrossMarginPercentage", "Gross Margin %", "GM%", "GM %"
  Direct Cost    -> "TotalDirectCost", "Total Direct Cost", "Total Direct Costs"

AOP SHEET — fixed grid layout (do NOT add or remove rows/columns):
  Column C (index 2): division names — use exactly: MS1 MS2 MS3 IS1 BL1
    Accepted aliases: "Fuel"->MS3  |  "Insight"->IS1  |  "BCore Labs"->BL1
  Columns D through O (index 3-14): monthly values January through December
  Rows 3-7   (Excel rows 3-7,  0-indexed 2-6):  Revenue per division
  Rows 12-16 (Excel rows 12-16, 0-indexed 11-15): Gross Profit per division
  Note: revenue cells may have formulas — replace with plain numeric values if needed

COMPARE SHEET — required columns (flexible spelling):
  Program    -> "Program", "ProgramName", "Program Name"
  Portfolio  -> "Portfolio", "Portfolio Lead", "PortfolioLead"
  Actuals    -> "Actuals", "Actual", "Rev Actual"
  AOP        -> "AOP"
  Var        -> "Var", "Variance", "Var $"
  GP Actual  -> "GP Actual", "GPActual", "GP Act"
  AOP GM%    -> "AOP GM%", "AOP GM %", "AOPGM%"
  GP AOP     -> "GP AOP", "GPAOP"

COMMON PROBLEMS — check and fix each:
  1. Division codes in AOP sheet don't match allowed values or aliases
  2. Revenue/GP cells contain formula errors (#REF!, #VALUE!) — replace with 0
  3. Column headers have extra spaces or unexpected capitalization
  4. Merged header cells — unmerge and put text in the leftmost cell
  5. Sheet tab name does not contain the required keyword
     ("GM Report", "AOP", or "Compare")

══════════════════════════════════════════════════════════
OUTPUT
══════════════════════════════════════════════════════════
Produce a corrected Excel file (.xlsx) with all issues fixed.
If you are unsure about a value, leave it as-is and note the uncertainty.
Do not invent data — only restructure and rename.</textarea>
      </div>
    </div>

  </div><!-- end screen-faq -->

</div>
</main>

<script>
const zones = { gm: document.getElementById('drop-zone-gm'), util: document.getElementById('drop-zone-util') };
const inputs = { gm: document.getElementById('file-input-gm'), util: document.getElementById('file-input-util') };
const chosen = { gm: document.getElementById('file-chosen-gm'), util: document.getElementById('file-chosen-util') };
const runBtn = document.getElementById('run-btn');
const progressArea = document.getElementById('progress-area');
const progressBar  = document.getElementById('progress-bar');
const statusMsg    = document.getElementById('status-msg');
const logBox       = document.getElementById('log-box');

let selected = { gm: null, util: null };
let pollInterval = null;
let currentJobId = null;
const confirmBox = document.getElementById('confirm-box');
const confirmTitle = document.getElementById('confirm-title');
const confirmDetail = document.getElementById('confirm-detail');

function showScreen(name) {
  ['landing', 'upload', 'faq'].forEach(s => {
    document.getElementById('screen-' + s).classList.toggle('active', s === name);
  });
  document.querySelector('.card').classList.toggle('faq-open', name === 'faq');
}
function showFaqPanel(name) {
  ['util', 'gm', 'claude'].forEach(p => {
    document.getElementById('faq-' + p).classList.toggle('active', p === name);
    document.getElementById('ftab-' + p).classList.toggle('active', p === name);
  });
}
function copyPrompt() {
  const txt = document.getElementById('claude-prompt').value;
  navigator.clipboard.writeText(txt).then(() => {
    const btn = document.getElementById('copy-btn');
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2200);
  });
}

// ── Clear All Data ──────────────────────────────────────────────────────────
const clearModal = document.getElementById('clear-data-modal');
const clearInput  = document.getElementById('clear-data-input');
const clearBtn    = document.getElementById('clear-data-btn');
const clearError  = document.getElementById('clear-data-error');

function openClearDataModal() {
  clearInput.value = '';
  clearBtn.disabled = true;
  clearError.textContent = '';
  clearModal.style.display = 'flex';
  clearInput.focus();
}

function closeClearDataModal() {
  clearModal.style.display = 'none';
}

clearInput.addEventListener('input', () => {
  clearBtn.disabled = clearInput.value !== 'CONFIRM';
});

async function clearAllData() {
  clearBtn.disabled = true;
  try {
    const resp = await fetch('/clear-data', { method: 'DELETE' });
    const data = await resp.json();
    if (!resp.ok) { clearError.textContent = data.error || 'Could not clear data'; clearBtn.disabled = false; return; }
    location.reload();
  } catch (e) {
    clearError.textContent = e.message;
    clearBtn.disabled = false;
  }
}

// Render each history row's timestamp in the viewer's local time, human-readable.
document.querySelectorAll('.history-meta[data-ts]').forEach(el => {
  const iso = el.dataset.ts;
  const d = new Date(iso);
  if (isNaN(d.getTime())) return;
  const pretty = d.toLocaleString(undefined, {
    month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit'
  });
  el.textContent = el.textContent.replace(iso.replace('T', ' '), pretty);
});

for (const key of ['gm', 'util']) {
  zones[key].addEventListener('dragover',  e => { e.preventDefault(); zones[key].classList.add('over'); });
  zones[key].addEventListener('dragleave', () => zones[key].classList.remove('over'));
  zones[key].addEventListener('drop', e => {
    e.preventDefault(); zones[key].classList.remove('over');
    const f = e.dataTransfer.files[0];
    if (f) setFile(key, f);
  });
  inputs[key].addEventListener('change', e => { if (e.target.files[0]) setFile(key, e.target.files[0]); });
}

function setFile(key, f) {
  if (!f.name.match(/\\.xlsx?$/i)) { alert('Please select an Excel (.xlsx) file.'); return; }
  selected[key] = f;
  chosen[key].textContent = f.name + ' (' + (f.size / 1024 / 1024).toFixed(1) + ' MB)';
  zones[key].classList.add('filled');
  runBtn.disabled = !(selected.gm || selected.util);
}

function addLog(msg, cls) {
  const div = document.createElement('div');
  div.className = 'log-line' + (cls ? ' ' + cls : '');
  div.textContent = msg;
  logBox.appendChild(div);
  logBox.scrollTop = logBox.scrollHeight;
}

async function startJob() {
  if (!selected.gm && !selected.util) return;
  runBtn.disabled = true;
  Object.values(zones).forEach(z => z.style.pointerEvents = 'none');
  progressArea.style.display = 'block';
  logBox.innerHTML = '';
  setProgress(5, 'Uploading file(s)...');

  const form = new FormData();
  if (selected.gm) form.append('gm_file', selected.gm);
  if (selected.util) form.append('util_file', selected.util);

  let jobId;
  try {
    const resp = await fetch('/upload', { method: 'POST', body: form });
    const data = await resp.json();
    if (!resp.ok) { showError(data.error || resp.statusText); return; }
    jobId = data.job_id;
  } catch(e) { showError(e.message); return; }

  currentJobId = jobId;
  setProgress(10, 'Processing...');
  pollInterval = setInterval(() => poll(jobId), 1000);
}

async function poll(jobId) {
  try {
    const resp = await fetch('/status/' + jobId);
    const data = await resp.json();

    (data.new_lines || []).forEach(line => {
      const cls = line.startsWith('[OK]') ? 'ok' : line.startsWith('[!!]') ? 'warn' : line.startsWith('[XX]') ? 'err' : '';
      addLog(line, cls);
    });

    if (data.progress !== undefined) setProgress(data.progress, data.status_text || 'Processing...');

    if (data.status === 'awaiting_confirmation') {
      clearInterval(pollInterval);
      showConfirm(data.preview);
    } else if (data.status === 'done') {
      clearInterval(pollInterval);
      setProgress(100, 'Opening dashboard...');
      const htmlResp = await fetch('/result/' + jobId);
      const html = await htmlResp.text();
      document.open(); document.write(html); document.close();
    } else if (data.status === 'error') {
      clearInterval(pollInterval);
      showError(data.error || 'Unknown error — check terminal for details.');
    }
  } catch(e) { /* network blip — keep polling */ }
}

function showConfirm(preview) {
  preview = preview || { new_count: 0, collision_count: 0, collisions: [] };
  confirmTitle.textContent = `${preview.collision_count} period/division combo(s) already ingested`;
  const sample = preview.collisions.slice(0, 5)
    .map(c => `${c.kind === 'gm' ? 'GM' : 'Utilization'} — ${c.division}`).join(', ');
  confirmDetail.textContent = preview.new_count > 0
    ? `This will add ${preview.new_count} new period/division row(s) and overwrite the ${preview.collision_count} listed above (${sample}${preview.collisions.length > 5 ? ', ...' : ''}).`
    : `This will overwrite ${preview.collision_count} existing period/division row(s) (${sample}${preview.collisions.length > 5 ? ', ...' : ''}) with the newly uploaded data.`;
  confirmBox.style.display = 'block';
  setProgress(70, 'Waiting for confirmation...');
}

async function confirmJob() {
  if (!currentJobId) return;
  confirmBox.style.display = 'none';
  setProgress(75, 'Confirmed — finishing up...');
  try {
    const resp = await fetch('/confirm/' + currentJobId, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) { showError(data.error || 'Could not confirm'); return; }
  } catch(e) { showError(e.message); return; }
  pollInterval = setInterval(() => poll(currentJobId), 1000);
}

function cancelJob() {
  confirmBox.style.display = 'none';
  addLog('[!!] Cancelled — existing data/bcore.db rows were not overwritten', 'warn');
  showError('Upload cancelled. No data was changed. Choose a different file or try again.');
}

function setProgress(pct, msg) {
  progressBar.style.width = pct + '%';
  statusMsg.textContent = msg;
  statusMsg.className = 'status-msg';
}

function showError(msg) {
  statusMsg.textContent = 'Error: ' + msg;
  statusMsg.className = 'status-msg error';
  progressBar.style.background = '#ef4444';
  runBtn.disabled = false;
  Object.values(zones).forEach(z => z.style.pointerEvents = '');
}
</script>
</body>
</html>
"""


# ── Background worker ──────────────────────────────────────────────────────────

def _log(job_id: str, msg: str):
    print(msg)
    with JOBS_LOCK:
        JOBS[job_id]["log"].append(msg)


def _set_progress(job_id: str, pct: int, text: str):
    with JOBS_LOCK:
        JOBS[job_id]["progress"] = pct
        JOBS[job_id]["status_text"] = text


def _run_gm_job(job_id: str, cfg: dict, file_bytes: bytes, filename: str):
    from src.gm_loader import load_gm_workbook

    _log(job_id, f'[OK] Loading GM workbook: {filename}')
    data = load_gm_workbook(io.BytesIO(file_bytes), cfg)

    for s in data.sheet_log:
        if s.type == "ignored":
            continue
        tag = "[OK]" if not s.issues else "[!!]"
        _log(job_id, f'{tag} [{s.type.upper()}] "{s.name}": {s.rows} row(s)'
             + (f' -- {"; ".join(s.issues)}' if s.issues else ""))

    if not data.actuals:
        _log(job_id, "[!!] No GM Report data parsed — dashboard will show the Workbook Scan only")
    else:
        _log(job_id, f"[OK] {len(data.actuals)} GM actual rows across {len(data.months)} month(s), "
                      f"{len(data.projects)} project(s)")
    _log(job_id, f'{"[OK]" if data.has_aop else "[!!]"} AOP data: {"found" if data.has_aop else "not found"}')
    return data


def _run_util_job(job_id: str, cfg: dict, file_bytes: bytes, filename: str):
    _log(job_id, f'[OK] Loading utilization workbook: {filename}')
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        from src.loader import load, get_ul_row_count
        data = load(io.BytesIO(file_bytes), cfg)

    ul_count = get_ul_row_count()
    _log(job_id, f'[OK] Sheet "Export": {len(data.export_df) + ul_count:,} rows loaded')
    _log(job_id, f'[OK] {len(data.periods)} periods parsed ({data.report_start} to {data.report_end})')
    _log(job_id, f'[OK] Roster: {len(data.all_employees)} employees')
    _log(job_id, f'[OK] PT employees: {len(data.pt_employees)}')
    _log(job_id, f'[OK] Partial-period employees: {len(data.partial_period_employees)}')
    _log(job_id, f'[OK] PayCode "UL" rows excluded: {ul_count}')
    _log(job_id, f'[!!] {len(data.excluded_no_pto)} excluded — in Time Details, no PTO match')
    _log(job_id, f'[!!] {len(data.flagged_no_timesheet)} flagged — in PTO Balances, no Time Details')

    from src.classifier import identify_corporate_roles
    corp_roles = identify_corporate_roles(data, cfg)
    _log(job_id, f'[OK] Corporate roles excluded: {len(corp_roles)}')

    from src.calculator import compute_all
    emp_stats = compute_all(data, cfg, corp_roles)
    _log(job_id, f'[OK] Per-employee utilization computed: {len(emp_stats)} employees')

    from src.view_builder import build_all_views
    views = build_all_views(data, emp_stats, corp_roles, cfg, date.today())
    for v in views:
        _log(job_id, f'[OK] [{v["view_label"]}] periods={v["view_period_count"]}, '
                      f'persistence={v["view_persistence"]}, '
                      f'{v["critical_count"]} critical / {v["warning_count"]} warning flags')

    return data, corp_roles, views, emp_stats


def _build_store_inputs(job_id, gm_data, gm_name, util_bundle, emp_stats, util_name, cfg):
    """Builds the rows that would be written to data/bcore.db and checks for
    period/division collisions, without writing anything yet — that happens
    in _finish_analysis(), after an explicit confirm if there were any."""
    from src import store
    from src.aggregator import build_rollups

    generated_at = datetime.now().isoformat(timespec="seconds")
    util_rows, gm_rows = [], []
    collisions = []

    conn = store.connect()
    try:
        if util_bundle is not None:
            data, corp_roles, _views = util_bundle
            _, division_rollups = build_rollups(data, emp_stats, corp_roles, cfg)
            util_rows = store.build_util_rows(division_rollups, util_name, generated_at)
            util_collisions = store.check_collisions(
                conn, "util", [(r["period_index"], r["division"]) for r in util_rows],
            )
            collisions += [{"kind": "util", "period_or_month": k, "division": d} for k, d in util_collisions]

        if gm_data is not None:
            gm_rows = store.build_gm_rows(gm_data, cfg, gm_name, generated_at)
            gm_collisions = store.check_collisions(
                conn, "gm", [(r["month_key"], r["division"]) for r in gm_rows],
            )
            collisions += [{"kind": "gm", "period_or_month": k, "division": d} for k, d in gm_collisions]
    finally:
        conn.close()

    _log(job_id, f"[OK] {len(util_rows)} utilization + {len(gm_rows)} GM period/division row(s) "
                 f"staged for data/bcore.db"
                 + (f" -- {len(collisions)} already ingested" if collisions else ""))

    return {
        "util_rows": util_rows,
        "gm_rows": gm_rows,
        "collisions": collisions,
        "ingested_at": generated_at,
    }


def _run_analysis(job_id: str, gm_bytes: bytes | None, gm_name: str,
                   util_bytes: bytes | None, util_name: str):
    try:
        _set_progress(job_id, 15, "Loading config...")
        with open(ROOT / "config.yaml", "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)

        from src.common import validate_config
        validate_config(cfg, need_util=util_bytes is not None, need_gm=gm_bytes is not None)

        gm_data = None
        util_bundle = None
        emp_stats = None

        if gm_bytes is not None:
            _set_progress(job_id, 25, "Parsing GM workbook...")
            gm_data = _run_gm_job(job_id, cfg, gm_bytes, gm_name)

        if util_bytes is not None:
            _set_progress(job_id, 50, "Parsing utilization workbook (this can take 1-2 min for large files)...")
            data, corp_roles, views, emp_stats = _run_util_job(job_id, cfg, util_bytes, util_name)
            util_bundle = (data, corp_roles, views)

        _set_progress(job_id, 70, "Checking for existing data...")
        store_inputs = _build_store_inputs(job_id, gm_data, gm_name, util_bundle, emp_stats, util_name, cfg)

        with JOBS_LOCK:
            JOBS[job_id]["_pending"] = {
                "cfg": cfg,
                "gm_data": gm_data,
                "gm_name": gm_name,
                "util_bundle": util_bundle,
                "util_name": util_name,
                **store_inputs,
            }

        if store_inputs["collisions"]:
            preview = {
                "new_count": len(store_inputs["util_rows"]) + len(store_inputs["gm_rows"])
                             - len(store_inputs["collisions"]),
                "collision_count": len(store_inputs["collisions"]),
                "collisions": store_inputs["collisions"][:20],
            }
            _log(job_id, f'[!!] {preview["collision_count"]} period/division combo(s) already '
                         f'ingested — confirm to overwrite, or cancel to keep the existing data')
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "awaiting_confirmation"
                JOBS[job_id]["status_text"] = "Waiting for confirmation..."
                JOBS[job_id]["preview"] = preview
            return

        _finish_analysis(job_id)
        return
    except Exception as exc:
        traceback.print_exc()
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(exc)
        _log(job_id, f"[XX] {exc}")


def _finish_analysis(job_id: str):
    """Commits staged rows to data/bcore.db, runs cross-dataset flagging,
    renders the dashboard, exports insights, and saves to run history --
    the same final sequence whether or not a confirmation was needed."""
    try:
        with JOBS_LOCK:
            job = JOBS[job_id]
            pending = job.pop("_pending", None)
        if pending is None:
            raise RuntimeError("No pending analysis found for this job")

        cfg = pending["cfg"]
        gm_data = pending["gm_data"]
        gm_name = pending["gm_name"]
        util_bundle = pending["util_bundle"]
        util_name = pending["util_name"]

        from src import store
        from src.cross_flagger import evaluate_all

        util_avg_dict = {}
        gm_variance_dict = {}
        try:
            conn = store.connect()
            try:
                if pending["util_rows"]:
                    store.write_util_periods(conn, pending["util_rows"])
                if pending["gm_rows"]:
                    store.write_gm_periods(conn, pending["gm_rows"])
                # Read back the persisted, cumulative state (not just this
                # job's rows) so cross-flagging sees GM/utilization data
                # committed in earlier, separate uploads too.
                util_avg_dict = store.latest_util_avg_by_division_db(conn, cfg)
                gm_variance_dict = store.latest_gm_variance_by_division_db(conn)
            finally:
                conn.close()
            _log(job_id, "[OK] Written to data/bcore.db")
        except Exception as exc:
            _log(job_id, f"[!!] Could not write to data/bcore.db: {exc}")

        cross_flags = evaluate_all(util_avg_dict, gm_variance_dict, cfg)
        crit = sum(1 for f in cross_flags if f.severity == "critical")
        warn = sum(1 for f in cross_flags if f.severity == "warning")
        _log(job_id, f"[OK] Cross-dataset flags: {crit} critical, {warn} warning across "
                     f"{len(cross_flags)} division(s)")

        # Root-cause analysis
        root_causes = []
        if util_bundle is not None:
            from src.root_cause import build_root_causes
            rc_data = util_bundle[0]
            try:
                root_causes = build_root_causes(rc_data, cross_flags, cfg)
                _log(job_id, f"[OK] Root-cause analysis: {len(root_causes)} division(s) drilled down")
            except Exception as exc:
                _log(job_id, f"[!!] Root-cause analysis skipped: {exc}")

        # Workforce health
        workforce = None
        if util_bundle is not None:
            from src.workforce import compute_workforce_health
            wf_data = util_bundle[0]
            try:
                workforce = compute_workforce_health(wf_data, cfg, date.today().year)
                _log(job_id,
                     f"[OK] Workforce: {workforce.headcount_current} headcount, "
                     f"{workforce.hires_ytd} hires / {workforce.departures_ytd} departures YTD")
            except Exception as exc:
                _log(job_id, f"[!!] Workforce health skipped: {exc}")

        if cfg.get("ai_commentary", {}).get("enabled"):
            _log(job_id, "[OK] AI commentary enabled -- narrating trends via local `claude` CLI "
                         "(sends only already-aggregated numbers, never raw rows; silently "
                         "omitted if `claude` is not installed or a call fails)")
        else:
            _log(job_id, "[!!] AI commentary disabled (ai_commentary.enabled: false in config.yaml)")

        # Open-ended AI discovery — cross-dataset pattern analysis
        discoveries = []
        if cfg.get("ai_commentary", {}).get("enabled") and util_bundle is not None:
            try:
                from src.trend_explorer import generate_discoveries
                _log(job_id, "[OK] AI discovery -- scanning cross-dataset patterns...")
                _disc_views = util_bundle[2]
                discoveries = generate_discoveries(_disc_views, gm_data, workforce, cfg)
                _log(job_id, f"[OK] AI discovery: {len(discoveries)} pattern(s) found")
            except Exception as exc:
                _log(job_id, f"[!!] AI discovery skipped: {exc}")

        _set_progress(job_id, 90, "Rendering dashboard...")
        from src.renderer import combine_sections, render_gm_section, render_utilization_section

        gm_html = None
        util_html = None
        if gm_data is not None:
            gm_html = render_gm_section(gm_data, cfg, ROOT / "templates", discoveries=discoveries)
        if util_bundle is not None:
            data, corp_roles, views = util_bundle
            util_html = render_utilization_section(
                data, corp_roles, views, cfg, ROOT / "templates",
                workforce=workforce, discoveries=discoveries,
            )

        html = combine_sections(
            gm_html, util_html, ROOT / "templates", date.today(), cross_flags, root_causes
        )

        _log(job_id, f'[OK] Dashboard ready — {len(html) // 1024} KB')

        try:
            from src.insights_exporter import build_insights, write_insights
            insights = build_insights(
                gm_data, None, util_bundle, None, cross_flags, cfg, pending["ingested_at"],
                root_causes=root_causes, workforce=workforce,
            )
            write_insights(insights)
            _log(job_id, "[OK] Insights exported to exports/insights_latest.json")
        except OSError as exc:
            _log(job_id, f"[!!] Could not write insights export: {exc}")

        label_parts = [n for n in (gm_name, util_name) if n]
        stats = {"gm_name": gm_name, "util_name": util_name}
        if util_bundle is not None:
            _, _, views = util_bundle
            ytd = views[-1]
            stats.update({
                "employees_analyzed": ytd["employees_analyzed"],
                "critical_count": len(ytd["critical_flags"]),
                "warning_count": len(ytd["warning_flags"]),
            })
        try:
            _save_run_to_history(job_id, html, " + ".join(label_parts) or "Dashboard", stats)
            _log(job_id, "[OK] Saved to run history — reopen it later from the upload page")
        except OSError as exc:
            _log(job_id, f"[!!] Could not save to run history: {exc}")

        with JOBS_LOCK:
            JOBS[job_id]["html"] = html
            JOBS[job_id]["progress"] = 100
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["status_text"] = "Done"

    except Exception as exc:
        traceback.print_exc()
        err_msg = str(exc)
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = err_msg
        _log(job_id, f'[XX] {err_msg}')


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(UPLOAD_PAGE, history=_load_history())


@app.route("/history/<run_id>")
def history_view(run_id):
    try:
        uuid.UUID(run_id)  # run_id feeds a filesystem path — reject anything but a bare UUID
    except ValueError:
        abort(404)
    path = HISTORY_DIR / f"{run_id}.html"
    if not path.is_file():
        abort(404)
    return path.read_text(encoding="utf-8"), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/clear-data", methods=["DELETE"])
def clear_data():
    """Wipes data/bcore.db (the structured per-period store) and
    exports/insights_latest.json. Deliberately does NOT touch output/history/
    — that's the separate "browse past rendered dashboards" archive."""
    from src import store

    try:
        conn = store.connect()
        try:
            store.clear_all(conn)
        finally:
            conn.close()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    insights_path = ROOT / "exports" / "insights_latest.json"
    if insights_path.exists():
        insights_path.unlink()

    return jsonify({"ok": True})


def _validate_upload(file_storage) -> str | None:
    """Returns an error message, or None if the upload is acceptable."""
    filename = file_storage.filename or ""
    if not filename.lower().endswith(ALLOWED_EXTENSIONS):
        return f"'{filename}' is not a .xlsx/.xls file"
    return None


@app.route("/upload", methods=["POST"])
def upload():
    _sweep_expired_jobs()

    gm_file = request.files.get("gm_file")
    util_file = request.files.get("util_file")

    if (not gm_file or not gm_file.filename) and (not util_file or not util_file.filename):
        return jsonify({"error": "No file received — upload at least one workbook"}), 400

    gm_bytes = gm_name = None
    util_bytes = util_name = None

    if gm_file and gm_file.filename:
        err = _validate_upload(gm_file)
        if err:
            return jsonify({"error": err}), 400
        gm_bytes = gm_file.read()
        if not gm_bytes:
            return jsonify({"error": "GM workbook upload was empty"}), 400
        if len(gm_bytes) > MAX_UPLOAD_BYTES:
            return jsonify({"error": f"GM workbook exceeds {MAX_UPLOAD_BYTES // (1024*1024)} MB limit"}), 400
        gm_name = gm_file.filename

    if util_file and util_file.filename:
        err = _validate_upload(util_file)
        if err:
            return jsonify({"error": err}), 400
        util_bytes = util_file.read()
        if not util_bytes:
            return jsonify({"error": "Utilization workbook upload was empty"}), 400
        if len(util_bytes) > MAX_UPLOAD_BYTES:
            return jsonify({"error": f"Utilization workbook exceeds {MAX_UPLOAD_BYTES // (1024*1024)} MB limit"}), 400
        util_name = util_file.filename

    # Cache for settings re-run
    with _LAST_LOCK:
        if util_bytes:
            _LAST["util_bytes"] = util_bytes
            _LAST["util_name"]  = util_name
        if gm_bytes:
            _LAST["gm_bytes"] = gm_bytes
            _LAST["gm_name"]  = gm_name
    threading.Thread(
        target=_persist_last_uploads,
        args=(util_bytes, util_name, gm_bytes, gm_name),
        daemon=True,
    ).start()

    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "status_text": "Starting...",
            "progress": 10,
            "log": [],
            "log_sent": 0,
            "html": "",
            "error": "",
            "created_at": time.time(),
        }

    thread = threading.Thread(
        target=_run_analysis,
        args=(job_id, gm_bytes, gm_name, util_bytes, util_name),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404

    with JOBS_LOCK:
        sent = job["log_sent"]
        new_lines = job["log"][sent:]
        job["log_sent"] = len(job["log"])

    return jsonify({
        "status":      job["status"],
        "status_text": job["status_text"],
        "progress":    job["progress"],
        "new_lines":   new_lines,
        "error":       job.get("error", ""),
        "preview":     job.get("preview"),
    })


@app.route("/confirm/<job_id>", methods=["POST"])
def confirm(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job"}), 404
        if job["status"] != "awaiting_confirmation":
            return jsonify({"error": "This job isn't waiting for confirmation"}), 400
        job["status"] = "running"
        job["status_text"] = "Confirmed — finishing up..."

    thread = threading.Thread(target=_finish_analysis, args=(job_id,), daemon=True)
    thread.start()
    return jsonify({"ok": True})


@app.route("/result/<job_id>")
def result(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        return "Not ready", 404
    html = job["html"]
    with JOBS_LOCK:
        JOBS[job_id]["html"] = ""  # free memory — result claimed
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# ── Settings API ──────────────────────────────────────────────────────────────

_THRESH_KEYS = [
    "billable_utilization_benchmark", "billable_utilization_warning",
    "billable_utilization_critical",  "direct_utilization_benchmark",
    "direct_utilization_warning",     "direct_utilization_critical",
    "persistence_threshold",
]


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    with _LAST_LOCK:
        can_rerun = _LAST.get("util_bytes") is not None
    settings = {k: cfg.get(k) for k in _THRESH_KEYS}
    settings["division_thresholds"] = cfg.get("division_thresholds") or {}
    settings["divisions"] = cfg.get("gm", {}).get("divisions", [])
    settings["can_rerun"] = can_rerun
    return jsonify(settings)


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    with open(ROOT / "config.yaml", "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    for k in _THRESH_KEYS:
        if k in data:
            cfg[k] = data[k]
    if "division_thresholds" in data:
        cfg["division_thresholds"] = data["division_thresholds"]

    with open(ROOT / "config.yaml", "w", encoding="utf-8") as fh:
        yaml.dump(cfg, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)

    with _LAST_LOCK:
        util_bytes = _LAST.get("util_bytes")
        util_name  = _LAST.get("util_name", "util.xlsx")
        gm_bytes   = _LAST.get("gm_bytes")
        gm_name    = _LAST.get("gm_name",  "gm.xlsx")

    if not util_bytes and not gm_bytes:
        return jsonify({"ok": True, "job_id": None, "saved_only": True})

    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running", "status_text": "Starting...",
            "progress": 10, "log": [], "log_sent": 0,
            "html": "", "error": "", "created_at": time.time(),
        }
    threading.Thread(
        target=_run_analysis,
        args=(job_id, gm_bytes, gm_name, util_bytes, util_name),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/latest")
def latest_dashboard():
    """Serve the most recently generated dashboard HTML file."""
    output_dir = ROOT / "output"
    htmls = sorted(output_dir.glob("dashboard_*.html"), reverse=True)
    if not htmls:
        return "No dashboard generated yet. Upload files first.", 404
    return htmls[0].read_text(encoding="utf-8"), 200, {"Content-Type": "text/html; charset=utf-8"}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5003))
    _load_last_uploads()
    url = f"http://localhost:{port}"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"\n  BCore Performance Dashboard")
    print(f"  Open: {url}")
    print(f"  Stop: Ctrl+C\n")
    app.run(host="localhost", port=port, debug=False, threaded=True)
