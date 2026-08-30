"""P8: harness import CLI 参数绑定（batch_id/job_id 不互换）。"""
import json
import sys


def test_import_run_keyword_binding(tmp_path, monkeypatch):
    sys.path.insert(0, "/root/quant")
    from pipeline import harness
    (tmp_path / "work.json").write_text(json.dumps(
        {"exp_id": "t", "cfg": {"pool": "hs300", "handler_class": "Alpha158",
                                "task": "regression", "label_formula": "x",
                                "horizon": 10, "seeds": [42],
                                "train": ["a", "b"], "valid": ["c", "d"],
                                "test_start": "e", "test_end": "f",
                                "model": {}, "metric_families": ["prediction"]},
         "task": "regression", "executor": "executors/_example_lgb",
         "data_key": "k", "test_key": "k2", "contract": {},
         "data_version": "v3", "runner": "spark",
         "expectation": None, "base_ref": "ref:x", "hypothesis": "",
         "claims": [], "metric_families": ["prediction"],
         "git_commit": "abcd1234"}))
    (tmp_path / "metrics.json").write_text(json.dumps(
        {"task": "regression", "n_days": 10, "n_inst": 50,
         "sample_window": ["2025-01-01", "2025-01-31"],
         "mean_ic": 0.01, "ic_std": 0.1, "icir": 0.1,
         "mean_rank_ic": 0.02, "rank_ic_std": 0.1, "rank_icir": 0.2,
         "hit_rate": 0.5, "n_nonoverlap": 10,
         "nonoverlap_mean_rank_ic": 0.03, "nonoverlap_rank_icir": 0.3,
         "bootstrap_rankic": {"mean": 0.03, "ci95_lo": -0.1, "ci95_hi": 0.1,
                              "p_le0": 0.01},
         "bootstrap_ic": {"mean": 0.01, "ci95_lo": -0.1, "ci95_hi": 0.1,
                          "p_le0": 0.1},
         "deciles": {"top_minus_bottom": 0.1},
         "monthly_ic": [], "quarters": []}))
    seen = {}

    def fake_import_run(work, full, run_dir, spec_hash, batch_id=None, job_id=None):
        seen.update({"batch_id": batch_id, "job_id": job_id})
        return "rid"
    monkeypatch.setattr(harness, "_import_run", fake_import_run)
    monkeypatch.setattr(harness, "git_dirty_code", lambda: [])  # 测试环境绕过脏门
    # 直接调用 import_run（等价于 CLI: --job-id 34 --batch-id b-p7-freeze）
    harness.import_run(str(tmp_path), "hash", job_id="34", batch_id="b-p7-freeze")
    assert seen == {"batch_id": "b-p7-freeze", "job_id": "34"}
