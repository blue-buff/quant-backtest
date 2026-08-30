"""P7 T7: 自动 advisory review 从 metrics.json 取数（含嵌套展平与窗口）。"""
import json

from pipeline import review


def test_from_file_window_and_p_value_pass(tmp_path):
    m = {"sample_window": ["2025-01-01", "2026-08-20"], "p_le0": 0.03,
         "n_days": 385, "mean_ic": 0.01,
         "bootstrap_rankic": {"p_le0": 0.03},
         "quarters": {"2025Q1": {"n_days": 60, "ic": 0.02}}}
    p = tmp_path / "metrics.json"
    p.write_text(json.dumps(m))
    checks = review.from_file(str(p))
    by = {c["check"]: c for c in checks}
    assert len(checks) == 6
    assert by["window_reported"]["pass"] is True      # sample_window -> extra.window
    assert by["p_value_reported"]["pass"] is True     # bootstrap_rankic.p_le0 展平可见
    assert by["sample_size_reported"]["pass"] is True
    assert by["quarters_reported"]["pass"] is True    # quarters.2025Q1.* 展平可见
    assert by["subsets_reported"]["pass"] is False    # advisory：记录但不拦
    assert by["multiplicity_reported"]["pass"] is False


def test_from_file_nested_p_value_only(tmp_path):
    # metrics.json 真实形态：p 值只藏在 bootstrap_rankic 里
    m = {"sample_window": ["a", "b"], "n_days": 100,
         "bootstrap_rankic": {"p_le0": 0.01}}
    p = tmp_path / "metrics.json"
    p.write_text(json.dumps(m))
    by = {c["check"]: c for c in review.from_file(str(p))}
    assert by["p_value_reported"]["pass"] is True
    assert by["window_reported"]["pass"] is True


def test_from_file_no_window_fails_window_check(tmp_path):
    p = tmp_path / "metrics.json"
    p.write_text(json.dumps({"p_le0": 0.01, "n_days": 10}))
    by = {c["check"]: c for c in review.from_file(str(p))}
    assert by["window_reported"]["pass"] is False


def test_check_metrics_always_six_checks():
    checks = review.check_metrics({})
    assert len(checks) == 6
    assert all("pass" in c and "note" in c for c in checks)


def test_from_file_flattens_period_lists(tmp_path):
    # 真实 metrics.json：quarters/monthly_ic 是 period-dict 列表 -> quarters_reported 应过
    m = {"sample_window": ["2025-01-01", "2026-08-20"], "n_days": 385,
         "bootstrap_rankic": {"p_le0": 0.01},
         "quarters": [{"quarter": "2025Q1", "n_days": 60, "ic": 0.02}],
         "monthly_ic": [{"month": "2025-01", "ic": 0.03, "n": 21}]}
    p = tmp_path / "metrics.json"
    p.write_text(json.dumps(m))
    by = {c["check"]: c for c in review.from_file(str(p))}
    assert by["quarters_reported"]["pass"] is True
    assert by["p_value_reported"]["pass"] is True
