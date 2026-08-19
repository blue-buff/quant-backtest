"""A2 历史成分导出脚本的纯函数测试（不依赖 baostock/网络）"""
import importlib.util
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def _load_module():
    path = PROJECT / "qlib_scripts" / "export_csv_hist.py"
    spec = importlib.util.spec_from_file_location("export_csv_hist", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_first_trading_days_of_month():
    mod = _load_module()
    days = ["2025-01-02", "2025-01-03", "2025-02-03",
            "2025-07-01", "2025-07-02", "2026-01-05"]
    assert mod.first_trading_days_of_month(days) == [
        "2025-01-02", "2025-07-01", "2026-01-05"]


def test_build_instruments_merges_contiguous_periods():
    mod = _load_module()
    members = {
        "2025-01-02": ["sh.600519", "sh.600000"],
        "2025-07-01": ["sh.600519", "sh.600036"],   # 600000 被剔除，600036 新进
        "2026-01-05": ["sh.600519", "sh.600036"],
    }
    rows = mod.build_instruments(members)
    # 连续在成分内的 600519 合并为一段；600000 只有第一段；600036 从第二段起
    assert "SH600519\t2025-01-02\t2099-12-31" in rows
    assert "SH600000\t2025-01-02\t2025-07-01" in rows
    assert "SH600036\t2025-07-01\t2099-12-31" in rows
    assert len(rows) == 3


def test_build_instruments_filters_symbols():
    mod = _load_module()
    members = {"2025-01-02": ["sh.600519", "sh.600000"]}
    rows = mod.build_instruments(members, symbols={"sh.600519"})
    assert rows == ["SH600519\t2025-01-02\t2099-12-31"]
