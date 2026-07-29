#!/usr/bin/env python3
"""
BCore Performance Dashboard
Entry point: loads config, runs the GM and/or utilization pipelines, writes
one combined (or single-section) HTML report.

Usage:
  python analyze.py --gm path/to/gm_workbook.xlsx --util path/to/utilization_workbook.xlsx
  python analyze.py --gm path/to/gm_workbook.xlsx      # revenue/GM only
  python analyze.py --util path/to/utilization_workbook.xlsx   # utilization only
  python analyze.py                                     # auto-discover from input/
"""

import argparse
import sys
import warnings
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).parent

OK   = "[OK]"
WARN = "[!!]"
ERR  = "[XX]"


def _load_config() -> dict:
    cfg_path = ROOT / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _probe_workbook_kind(path: Path, cfg: dict) -> str:
    """Peek at sheet names to classify a workbook as 'util', 'gm', or 'unknown'."""
    from src.gm_loader import is_aop_sheet, is_compare_sheet, is_gm_sheet

    try:
        xl = pd.ExcelFile(path, engine="openpyxl")
    except Exception:
        return "unknown"

    names = xl.sheet_names
    export_sheet = cfg.get("sheets", {}).get("export")
    if export_sheet and export_sheet in names:
        return "util"
    if any(is_gm_sheet(n) or is_aop_sheet(n) or is_compare_sheet(n) for n in names):
        return "gm"
    return "unknown"


def _find_inputs(args, cfg: dict) -> tuple[Path | None, Path | None]:
    gm_path = Path(args.gm) if args.gm else None
    util_path = Path(args.util) if args.util else None

    if gm_path and not gm_path.exists():
        raise FileNotFoundError(f"--gm file not found: {gm_path}")
    if util_path and not util_path.exists():
        raise FileNotFoundError(f"--util file not found: {util_path}")

    if gm_path or util_path:
        return gm_path, util_path

    candidates = sorted((ROOT / "input").glob("*.xlsx"))
    if not candidates:
        raise FileNotFoundError(
            "No .xlsx files found in input/, and neither --gm nor --util was given. "
            "Pass one or both paths explicitly, or drop workbook(s) into input/."
        )

    for c in candidates:
        kind = _probe_workbook_kind(c, cfg)
        if kind == "util" and util_path is None:
            util_path = c
        elif kind == "gm" and gm_path is None:
            gm_path = c

    if gm_path is None and util_path is None:
        raise FileNotFoundError(
            f"Found {len(candidates)} file(s) in input/ but could not classify any of them "
            "as a GM or utilization workbook. Pass --gm/--util explicitly."
        )
    return gm_path, util_path


def _run_gm(gm_path: Path, cfg: dict):
    from src.gm_loader import load_gm_workbook

    print(f'  {OK} Loading GM workbook: {gm_path.name}')
    data = load_gm_workbook(gm_path, cfg)

    issues = [s for s in data.sheet_log if s.issues and s.type != "ignored"]
    for s in data.sheet_log:
        if s.type == "ignored":
            continue
        tag = OK if not s.issues else WARN
        print(f'  {tag} [{s.type.upper()}] "{s.name}": {s.rows} row(s)'
              + (f' -- {"; ".join(s.issues)}' if s.issues else ""))

    if not data.actuals:
        print(f'  {WARN} No GM Report data parsed — dashboard will show the Workbook Scan only')
    else:
        print(f'  {OK} {len(data.actuals)} GM actual rows across {len(data.months)} month(s), '
              f'{len(data.projects)} project(s)')
    print(f'  {OK if data.has_aop else WARN} AOP data: {"found" if data.has_aop else "not found"}')
    if issues:
        print(f'  {WARN} {len(issues)} sheet(s) with issues — see Workbook Scan in the report')

    return data


def _run_util(util_path: Path, cfg: dict):
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        from src.loader import load, get_ul_row_count
        data = load(util_path, cfg)

    ul_count = get_ul_row_count()
    print(f'  {OK} Sheet "Export": {len(data.export_df) + ul_count:,} rows loaded')
    print(f'  {OK} Sheet "Lookups": {len(data.periods)} periods parsed '
          f"({data.report_start} to {data.report_end})")
    print(f"  {OK} Roster: {len(data.all_employees)} employees")
    print(f"  {OK} PT employees identified: {len(data.pt_employees)}")
    print(f"  {OK} Partial-period employees: {len(data.partial_period_employees)}")

    from src.classifier import identify_corporate_roles
    corp_roles = identify_corporate_roles(data, cfg)
    print(f"  {OK} Corporate roles excluded from trend analysis: {len(corp_roles)}")

    print(f"  {WARN} {len(data.excluded_no_pto)} employees excluded -- in Time Details, no PTO match")
    print(f"  {WARN} {len(data.flagged_no_timesheet)} employees flagged -- in PTO Balances, "
          f"no Time Details entries")

    from src.calculator import compute_all
    emp_stats = compute_all(data, cfg, corp_roles)
    print(f"  {OK} Per-employee utilization computed: {len(emp_stats)} employees")

    from src.view_builder import build_all_views
    views = build_all_views(data, emp_stats, corp_roles, cfg, date.today())
    for v in views:
        print(f"  {OK} [{v['view_label']}] {v['view_period_count']} period(s), "
              f"persistence={v['view_persistence']}, "
              f"{v['critical_count']} critical / {v['warning_count']} warning flags")

    return data, corp_roles, views, emp_stats


def _commit_and_flag(gm_data, gm_path, util_bundle, emp_stats, util_path, cfg):
    """Writes per-period rollups to data/bcore.db, then runs cross-dataset
    flagging, root-cause analysis, workforce health, and the insights export.

    The CLI has no confirm-click UI (that's a serve.py/browser concept), so a
    period/division collision here is just overwritten with a log line --
    the same INSERT OR REPLACE semantics as everywhere else in src/store.py.

    Returns (cross_flags, root_causes, workforce).
    """
    from src import store
    from src.aggregator import build_rollups
    from src.cross_flagger import evaluate_all
    from src.insights_exporter import build_insights, write_insights

    generated_at = date.today().isoformat()

    try:
        conn = store.connect()
        try:
            if util_bundle is not None:
                data, corp_roles, _views = util_bundle
                _, division_rollups = build_rollups(data, emp_stats, corp_roles, cfg)
                util_rows = store.build_util_rows(division_rollups, util_path.name, generated_at)
                collisions = store.check_collisions(
                    conn, "util", [(r["period_index"], r["division"]) for r in util_rows],
                )
                if collisions:
                    print(f"  {WARN} {len(collisions)} utilization period/division combo(s) "
                          f"already in data/bcore.db -- overwriting")
                store.write_util_periods(conn, util_rows)
                print(f"  {OK} {len(util_rows)} utilization period/division row(s) written to data/bcore.db")

            if gm_data is not None:
                gm_rows = store.build_gm_rows(gm_data, cfg, gm_path.name, generated_at)
                collisions = store.check_collisions(
                    conn, "gm", [(r["month_key"], r["division"]) for r in gm_rows],
                )
                if collisions:
                    print(f"  {WARN} {len(collisions)} GM month/division combo(s) "
                          f"already in data/bcore.db -- overwriting")
                store.write_gm_periods(conn, gm_rows)
                print(f"  {OK} {len(gm_rows)} GM month/division row(s) written to data/bcore.db")

            # Read back the persisted, cumulative state (not just this run's
            # rows) so cross-flagging sees GM/utilization data committed in
            # earlier, separate runs too.
            util_avg_dict = store.latest_util_avg_by_division_db(conn, cfg)
            gm_variance_dict = store.latest_gm_variance_by_division_db(conn)
        finally:
            conn.close()
    except Exception as exc:
        print(f"  {WARN} Structured data store unavailable ({exc}) -- cross-dataset "
              f"flagging and insights export skipped")
        return [], [], None

    cross_flags = evaluate_all(util_avg_dict, gm_variance_dict, cfg)
    crit = sum(1 for f in cross_flags if f.severity == "critical")
    warn = sum(1 for f in cross_flags if f.severity == "warning")
    print(f"  {OK} Cross-dataset flags: {crit} critical, {warn} warning across {len(cross_flags)} division(s)")

    # Root-cause analysis
    root_causes = []
    if util_bundle is not None:
        from src.root_cause import build_root_causes
        data, _corp, _views = util_bundle
        try:
            root_causes = build_root_causes(data, cross_flags, cfg)
            print(f"  {OK} Root-cause analysis: {len(root_causes)} division(s) drilled down")
        except Exception as exc:
            print(f"  {WARN} Root-cause analysis skipped: {exc}")

    # Workforce health
    workforce = None
    if util_bundle is not None:
        from src.workforce import compute_workforce_health
        data, _corp, _views = util_bundle
        try:
            workforce = compute_workforce_health(data, cfg, date.today().year)
            print(
                f"  {OK} Workforce: {workforce.headcount_current} headcount, "
                f"{workforce.hires_ytd} hires / {workforce.departures_ytd} departures YTD, "
                f"{len(workforce.high_pto_liability)} high-PTO, "
                f"{len(workforce.negative_pto_balances)} negative-PTO"
            )
        except Exception as exc:
            print(f"  {WARN} Workforce health skipped: {exc}")

    try:
        insights = build_insights(
            gm_data, None, util_bundle, None, cross_flags, cfg, generated_at,
            root_causes=root_causes, workforce=workforce,
        )
        write_insights(insights)
        print(f"  {OK} Insights exported to exports/insights_latest.json")
    except OSError as exc:
        print(f"  {WARN} Could not write insights export: {exc}")

    return cross_flags, root_causes, workforce


def main():
    parser = argparse.ArgumentParser(description="BCore Performance Dashboard generator")
    parser.add_argument("--gm", help="Path to the Revenue/GM workbook")
    parser.add_argument("--util", help="Path to the utilization workbook")
    args = parser.parse_args()

    cfg = _load_config()

    print(f"\n{'='*60}")
    print(f"  BCore Performance Dashboard")
    print(f"{'='*60}\n")

    try:
        gm_path, util_path = _find_inputs(args, cfg)
    except FileNotFoundError as exc:
        print(f"  {ERR} {exc}")
        sys.exit(1)

    if gm_path is None and util_path is None:
        print(f"  {ERR} No input provided.")
        sys.exit(1)

    from src.common import validate_config
    try:
        validate_config(cfg, need_util=util_path is not None, need_gm=gm_path is not None)
    except ValueError as exc:
        print(f"  {ERR} {exc}")
        sys.exit(1)

    gm_data = None
    util_bundle = None
    emp_stats = None

    if gm_path is not None:
        print(f"\n  --> Revenue & Gross Margin\n")
        gm_data = _run_gm(gm_path, cfg)

    if util_path is not None:
        print(f"\n  --> Utilization\n")
        print(f"  Input: {util_path.name}")
        data, corp_roles, views, emp_stats = _run_util(util_path, cfg)
        util_bundle = (data, corp_roles, views)

    print(f"\n  --> Structured data store & cross-dataset flags\n")
    cross_flags, root_causes, workforce = _commit_and_flag(
        gm_data, gm_path, util_bundle, emp_stats, util_path, cfg,
    )

    if cfg.get("ai_commentary", {}).get("enabled"):
        print(f'  {OK} AI commentary enabled -- narrating trends via local `claude` CLI '
              f'(each division/view sends only already-aggregated numbers, never raw rows; '
              f'silently omitted if `claude` is not installed or a call fails)')
    else:
        print(f'  {WARN} AI commentary disabled (ai_commentary.enabled: false in config.yaml)')

    print(f"\n  --> Rendering dashboard...\n")
    from src.renderer import render_dashboard

    today_str = date.today().strftime("%Y-%m-%d")
    output_file = ROOT / "output" / f"dashboard_{today_str}.html"
    render_dashboard(
        gm_data=gm_data,
        util_bundle=util_bundle,
        cfg=cfg,
        template_dir=ROOT / "templates",
        output_path=output_file,
        generated_date=date.today(),
        cross_flags=cross_flags,
        root_causes=root_causes,
        workforce=workforce,
    )

    size_kb = output_file.stat().st_size // 1024
    print(f"\n  [DONE] Report written to: {output_file}")
    print(f"         Size: {size_kb} KB")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
