"""planlib 纯函数测试：P0-1 交易日对齐 / P1-4 T+1 执行 / P0-2 lineage"""
import pandas as pd
import pytest

from qbt import planlib


def test_to_rqalpha():
    assert planlib.to_rqalpha("SH600000") == "600000.XSHG"
    assert planlib.to_rqalpha("SZ000001") == "000001.XSHE"
    assert planlib.to_rqalpha("600000.XSHG") == "600000.XSHG"  # 已是 rqalpha 格式则原样


def _wide(scores_by_date):
    """{date: {symbol: score}} → 宽表（index=日期, columns=股票）"""
    df = pd.DataFrame(scores_by_date).T
    df.index = pd.to_datetime(df.index)
    return df


def test_month_end_aligns_to_last_trading_day():
    """P0-1: 2025-05-31 是周六，信号日必须是 2025-05-30 而非日历月末"""
    wide = _wide({
        "2025-05-28": {"SH600000": 1.0},
        "2025-05-29": {"SH600000": 2.0},
        "2025-05-30": {"SH600000": 3.0},
        "2025-06-02": {"SH600000": 1.0},
        "2025-06-30": {"SH600000": 2.0},
        "2025-07-01": {"SH600000": 1.0},
    })
    days = planlib.month_end_trading_days(wide)
    assert days == [pd.Timestamp("2025-05-30"), pd.Timestamp("2025-06-30"),
                    pd.Timestamp("2025-07-01")]  # 7 月数据只到 07-01
    # 所有信号日都必须是数据里真实存在的交易日
    assert all(d in wide.index for d in days)


def test_build_plan_topk_and_tplus1():
    """P1-4: 2025-05-30(信号) → 2025-06-03(下一交易日) 执行；topk 生效"""
    wide = _wide({
        "2025-05-30": {"SH600519": 3.0, "SH600000": 2.0, "SH600036": 1.0},
        "2025-06-30": {"SH600036": 5.0, "SH600000": 4.0, "SH600519": 3.0},
    })
    calendar = ["2025-05-30", "2025-06-03", "2025-06-30", "2025-07-01"]
    rows = planlib.build_plan(wide, topk=2, calendar=calendar)
    assert rows == [
        ["2025-06-03", "600519.XSHG", "600000.XSHG"],
        ["2025-07-01", "600036.XSHG", "600000.XSHG"],
    ]


def test_build_plan_execute_same_day_without_calendar():
    """不传 calendar 时退化为信号日当日（旧行为，兼容单脚本用法）"""
    wide = _wide({
        "2025-05-30": {"SH600519": 3.0, "SH600000": 2.0},
    })
    rows = planlib.build_plan(wide, topk=2, execute_next_day=False)
    assert rows == [["2025-05-30", "600519.XSHG", "600000.XSHG"]]


def test_build_plan_drops_tail_without_next_trading_day():
    """数据末尾的信号日无 T+1 交易日 → 该行丢弃，而不是产出无效日期"""
    wide = _wide({
        "2025-05-30": {"SH600519": 3.0},
        "2025-06-30": {"SH600000": 1.0},
    })
    calendar = ["2025-05-30", "2025-06-03", "2025-06-30"]  # 6-30 之后无后继
    rows = planlib.build_plan(wide, topk=1, calendar=calendar)
    assert rows == [["2025-06-03", "600519.XSHG"]]


def test_build_plan_drops_all_nan_month():
    wide = _wide({
        "2025-05-30": {"SH600519": 3.0},
        "2025-06-30": {"SH600000": float("nan")},
    })
    rows = planlib.build_plan(wide, topk=1, execute_next_day=False)
    assert rows == [["2025-05-30", "600519.XSHG"]]


def test_shift_next_trading_day():
    assert planlib.shift_next_trading_day(
        [pd.Timestamp("2025-05-30")],
        [pd.Timestamp("2025-05-30"), pd.Timestamp("2025-06-03")],
    ) == [pd.Timestamp("2025-06-03")]
    # 无后继 → 丢弃
    assert planlib.shift_next_trading_day(
        [pd.Timestamp("2025-06-30")], [pd.Timestamp("2025-06-30")]) == []


def test_resolve_plan_pred_ok(tmp_path):
    pred = (tmp_path / "qlib_examples" / "mlruns" / "exp1" / "run1" / "artifacts" / "pred.pkl")
    pred.parent.mkdir(parents=True)
    pred.write_bytes(b"x")
    state = {"train_run": {"run_id": "run1", "pool": "hs300"}}
    assert planlib.resolve_plan_pred(state, "hs300", tmp_path) == pred


def test_resolve_plan_pred_pool_mismatch(tmp_path):
    """P0-2 验收：train zz500 → plan hs300 必须显式报错"""
    pred = (tmp_path / "qlib_examples" / "mlruns" / "exp1" / "run1" / "artifacts" / "pred.pkl")
    pred.parent.mkdir(parents=True)
    pred.write_bytes(b"x")
    state = {"train_run": {"run_id": "run1", "pool": "zz500"}}
    with pytest.raises(ValueError, match="不匹配"):
        planlib.resolve_plan_pred(state, "hs300", tmp_path)


def test_resolve_plan_pred_missing_lineage(tmp_path):
    with pytest.raises(FileNotFoundError, match="train_run"):
        planlib.resolve_plan_pred({}, "hs300", tmp_path)


def test_resolve_plan_pred_missing_artifact(tmp_path):
    state = {"train_run": {"run_id": "run1", "pool": "hs300"}}
    with pytest.raises(FileNotFoundError, match="run_id=run1"):
        planlib.resolve_plan_pred(state, "hs300", tmp_path)


def test_load_calendar(tmp_path):
    f = tmp_path / "day.txt"
    f.write_text("2025-05-30\n2025-06-03\n", encoding="utf-8")
    assert planlib.load_calendar(f) == [pd.Timestamp("2025-05-30"), pd.Timestamp("2025-06-03")]
    with pytest.raises(FileNotFoundError):
        planlib.load_calendar(tmp_path / "missing.txt")
