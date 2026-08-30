"""P7 T5: base_ref 分组多重检验修正（Bonferroni）。"""
from pipeline import board


def _row(status="FINISHED", base_ref="refA", p=0.02, source="harness"):
    return {"exp": "e", "run_id": "x", "status": status, "source": source,
            "pool": "", "seed": "", "batch": "", "legacy": "", "smoke": "",
            "git": "", "executor": "", "handler": "", "task": "",
            "rank_IC": None, "IC": None, "rankic_mean": 0.05, "p_le0": p,
            "start_time": 1, "base_ref": base_ref}


def test_three_variants_bonferroni():
    data = [_row(base_ref="refA", p=0.02) for _ in range(3)]
    for i, r in enumerate(data):
        r["exp"] = "v%d" % i  # 不同变体 = 不同 exp（同 exp 是重跑，见去重测试）
    out = board._multiplicity(data)
    for r in out:
        assert r["n_variants"] == 3
        assert abs(r["p_bonf"] - 0.06) < 1e-9
        assert r["multiplicity_risk"] is True


def test_legacy_rows_not_corrected():
    legacy = _row(base_ref="", p=0.01, source="legacy_backfill")
    out = board._multiplicity([legacy])
    assert "n_variants" not in out[0]
    assert "p_bonf" not in out[0]
    assert out[0]["p_le0"] == 0.01  # 原值不动


def test_failed_rows_excluded_from_group():
    data = [_row(status="FINISHED", p=0.02), _row(status="FAILED", p=0.02),
            _row(status="FINISHED", p=0.02)]
    for i, r in enumerate(data):
        r["exp"] = "v%d" % i
    board._multiplicity(data)
    assert data[0]["n_variants"] == 2
    assert data[2]["n_variants"] == 2
    assert "n_variants" not in data[1]  # FAILED 行不参与也不被修正


def test_missing_p_does_not_crash():
    data = [_row(p=0.01), _row(p=None), _row(p=0.01)]
    for i, r in enumerate(data):
        r["exp"] = "v%d" % i
    board._multiplicity(data)
    assert data[1]["n_variants"] == 3
    assert data[1]["p_bonf"] is None
    assert data[1]["multiplicity_risk"] == ""
    assert abs(data[0]["p_bonf"] - 0.03) < 1e-9


def test_different_base_refs_separate_groups():
    data = [_row(base_ref="A", p=0.01), _row(base_ref="A", p=0.01),
            _row(base_ref="B", p=0.01)]
    for i, r in enumerate(data):
        r["exp"] = "v%d" % i
    board._multiplicity(data)
    assert data[0]["n_variants"] == 2 and data[2]["n_variants"] == 1
    assert abs(data[2]["p_bonf"] - 0.01) < 1e-9
    assert data[2]["multiplicity_risk"] is False


def test_single_variant_no_correction():
    data = [_row(p=0.03)]
    board._multiplicity(data)
    assert data[0]["n_variants"] == 1
    assert abs(data[0]["p_bonf"] - 0.03) < 1e-9
    assert data[0]["multiplicity_risk"] is False


def test_smoke_rows_never_count():
    """回归（审计 #9）：smoke 行（接线检查）不计入 n_variants。"""
    data = [_row(p=0.02), _row(p=0.02), _row(p=0.02)]
    for i, r in enumerate(data):
        r["exp"] = "v%d" % i
    data[2]["smoke"] = "true"
    board._multiplicity(data)
    assert data[0]["n_variants"] == 2
    assert data[1]["n_variants"] == 2
    assert "n_variants" not in data[2]


def test_same_exp_deduped_to_latest():
    """回归（审计 #9）：同 exp 重复 FINISHED（重跑）只计一次取最新；旧行不被修正。"""
    data = [_row(p=0.02), _row(p=0.02), _row(p=0.02)]
    data[0]["exp"] = "dup"
    data[1]["exp"] = "dup"
    data[1]["start_time"] = 5  # 重跑（更新）
    data[2]["exp"] = "other"
    board._multiplicity(data)
    assert "n_variants" not in data[0]   # 旧行不参与计数
    assert data[1]["n_variants"] == 2
    assert data[2]["n_variants"] == 2
    assert abs(data[2]["p_bonf"] - 0.04) < 1e-9
