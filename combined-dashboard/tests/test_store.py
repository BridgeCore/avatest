from datetime import date
from pathlib import Path

import pytest
import yaml

from src import store
from src.aggregator import GroupPeriodStats, GroupStats
from src.gm_loader import AopEntry, GMActual, GMData, MonthKey
from src.loader import Period

ROOT = Path(__file__).parent.parent


def _cfg():
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    yield c
    c.close()


def _period(idx, start, end):
    return Period(index=idx, start=start, end=end, net_hours=80.0)


def _group_stats(division, period_index, billable_hours, net_available):
    period = _period(period_index, date(2026, 1, 1), date(2026, 1, 14))
    gps = GroupPeriodStats(period_index=period_index, period=period,
                            employee_count=5, billable_hours=billable_hours,
                            net_available=net_available)
    gs = GroupStats(group_key=division, group_type="division", member_count=5,
                     period_stats=[gps])
    return gs


def test_check_collisions_detects_existing_util_period(conn):
    rows = store.build_util_rows(
        {"MS1": _group_stats("MS1", 1, 700, 800)}, "util.xlsx", "2026-07-13T00:00:00",
    )
    store.write_util_periods(conn, rows)

    collisions = store.check_collisions(conn, "util", [(1, "MS1"), (2, "MS1")])
    assert collisions == [(1, "MS1")]


def test_write_and_read_back_util_rows(conn):
    rows = store.build_util_rows(
        {"MS1": _group_stats("MS1", 1, 700, 800)}, "util.xlsx", "2026-07-13T00:00:00",
    )
    store.write_util_periods(conn, rows)

    row = conn.execute("SELECT division, headcount, avg_billable_util FROM periods_util").fetchone()
    assert row == ("MS1", 5, pytest.approx(0.875))


def test_write_util_periods_insert_or_replace_overwrites(conn):
    rows_v1 = store.build_util_rows(
        {"MS1": _group_stats("MS1", 1, 700, 800)}, "util_v1.xlsx", "2026-07-13T00:00:00",
    )
    store.write_util_periods(conn, rows_v1)
    rows_v2 = store.build_util_rows(
        {"MS1": _group_stats("MS1", 1, 400, 800)}, "util_v2.xlsx", "2026-07-14T00:00:00",
    )
    store.write_util_periods(conn, rows_v2)

    count = conn.execute("SELECT COUNT(*) FROM periods_util").fetchone()[0]
    row = conn.execute("SELECT source_file, avg_billable_util FROM periods_util").fetchone()
    assert count == 1
    assert row == ("util_v2.xlsx", pytest.approx(0.5))


def _month(idx, label):
    return MonthKey(idx=idx, year=2026, label=label, key=2026 * 12 + idx)


def _gm_data_two_months():
    months = [_month(0, "Jan 2026"), _month(1, "Feb 2026")]
    actuals = [
        GMActual(month=months[0], project_name="Orion", division="MS1", revenue=100_000, gross_margin=30_000),
        GMActual(month=months[1], project_name="Orion", division="MS1", revenue=90_000, gross_margin=27_000),
    ]
    aop = {"MS1": AopEntry(division="MS1", revenue=[110_000, 110_000] + [0] * 10, gp=[33_000, 33_000] + [0] * 10)}
    return GMData(months=months, projects={}, actuals=actuals, aop=aop, compare=[], has_aop=True, sheet_log=[])


def test_build_gm_rows_computes_mtd_and_ytd_variance():
    data = _gm_data_two_months()
    rows = store.build_gm_rows(data, _cfg(), "gm.xlsx", "2026-07-13T00:00:00")

    ms1_rows = {r["month_label"]: r for r in rows if r["division"] == "MS1"}
    jan, feb = ms1_rows["Jan 2026"], ms1_rows["Feb 2026"]

    # Jan: 100k actual vs 110k AOP -> -9.09% both MTD and YTD (first month)
    assert jan["mtd_variance_pct"] == pytest.approx((100_000 - 110_000) / 110_000)
    assert jan["ytd_variance_pct"] == pytest.approx((100_000 - 110_000) / 110_000)

    # Feb: 90k actual vs 110k AOP (MTD); cumulative 190k vs 220k AOP (YTD)
    assert feb["mtd_variance_pct"] == pytest.approx((90_000 - 110_000) / 110_000)
    assert feb["ytd_variance_pct"] == pytest.approx((190_000 - 220_000) / 220_000)


def test_util_rollups_to_dict_exposes_avg_billable_utilization():
    division_rollups = {"MS1": _group_stats("MS1", 1, 700, 800)}
    result = store.util_rollups_to_dict(division_rollups)
    assert result == {"MS1": {"avg_billable_util": pytest.approx(0.875)}}


def test_util_rollups_to_dict_applies_configured_division_aliases():
    # Real workbooks: utilization tracker uses "BL"/"IS", GM workbook uses "BL1"/"IS1".
    division_rollups = {
        "BL": _group_stats("BL", 1, 700, 800),
        "MS1": _group_stats("MS1", 1, 400, 800),
        "Corp": _group_stats("Corp", 1, 100, 800),
    }
    cfg = {"cross_flags": {"util_division_aliases": {"BL": "BL1", "IS": "IS1"}}}

    result = store.util_rollups_to_dict(division_rollups, cfg)

    assert set(result) == {"BL1", "MS1", "Corp"}
    assert result["BL1"]["avg_billable_util"] == pytest.approx(0.875)


def test_util_rollups_to_dict_without_cfg_leaves_divisions_unaliased():
    division_rollups = {"BL": _group_stats("BL", 1, 700, 800)}
    result = store.util_rollups_to_dict(division_rollups, cfg=None)
    assert set(result) == {"BL"}


def test_latest_gm_variance_by_division_picks_most_recent_month():
    data = _gm_data_two_months()
    rows = store.build_gm_rows(data, _cfg(), "gm.xlsx", "2026-07-13T00:00:00")
    result = store.latest_gm_variance_by_division(rows)
    assert result["MS1"]["mtd_variance_pct"] == pytest.approx((90_000 - 110_000) / 110_000)
    assert result["MS1"]["ytd_variance_pct"] == pytest.approx((190_000 - 220_000) / 220_000)


def test_latest_util_avg_by_division_db_reads_persisted_state(conn):
    rows = store.build_util_rows({"BL": _group_stats("BL", 1, 700, 800)}, "util.xlsx", "2026-07-13T00:00:00")
    store.write_util_periods(conn, rows)

    cfg = {"cross_flags": {"util_division_aliases": {"BL": "BL1"}}}
    result = store.latest_util_avg_by_division_db(conn, cfg)
    assert set(result) == {"BL1"}
    assert result["BL1"]["avg_billable_util"] == pytest.approx(0.875)


def test_latest_util_avg_by_division_db_picks_max_period_per_division(conn):
    rows = (
        store.build_util_rows({"MS1": _group_stats("MS1", 1, 400, 800)}, "util_v1.xlsx", "2026-07-01T00:00:00")
        + store.build_util_rows({"MS1": _group_stats("MS1", 2, 700, 800)}, "util_v2.xlsx", "2026-07-13T00:00:00")
    )
    store.write_util_periods(conn, rows)

    result = store.latest_util_avg_by_division_db(conn)
    assert result["MS1"]["avg_billable_util"] == pytest.approx(0.875)  # period 2, not period 1


def test_latest_gm_variance_by_division_db_reads_persisted_state(conn):
    data = _gm_data_two_months()
    rows = store.build_gm_rows(data, _cfg(), "gm.xlsx", "2026-07-13T00:00:00")
    store.write_gm_periods(conn, rows)

    result = store.latest_gm_variance_by_division_db(conn)
    assert result["MS1"]["mtd_variance_pct"] == pytest.approx((90_000 - 110_000) / 110_000)
    assert result["MS1"]["ytd_variance_pct"] == pytest.approx((190_000 - 220_000) / 220_000)


def test_check_collisions_detects_existing_gm_month(conn):
    data = _gm_data_two_months()
    rows = store.build_gm_rows(data, _cfg(), "gm.xlsx", "2026-07-13T00:00:00")
    store.write_gm_periods(conn, rows)

    jan_key, feb_key = data.months[0].key, data.months[1].key
    collisions = store.check_collisions(conn, "gm", [(jan_key, "MS1"), (9999, "MS1")])
    assert collisions == [(jan_key, "MS1")]


def test_clear_all_empties_every_table(conn):
    store.write_util_periods(conn, store.build_util_rows(
        {"MS1": _group_stats("MS1", 1, 700, 800)}, "util.xlsx", "2026-07-13T00:00:00",
    ))
    store.write_discrepancies(conn, [{
        "kind": "missing_period", "ref": "Fuel", "detail": "no utilization data",
        "source_file": "util.xlsx", "ingested_at": "2026-07-13T00:00:00",
    }])

    store.clear_all(conn)

    assert conn.execute("SELECT COUNT(*) FROM periods_util").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM periods_gm").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM discrepancies").fetchone()[0] == 0
