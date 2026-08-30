"""入库完备性（审计专项）：commit 钉死、全文件归档、参数齐全、幂等复用。"""
from pipeline import harness


def test_collect_artifacts_includes_everything(tmp_path):
    for name in ("pred.pkl", "label_matrix.pkl", "metrics.json", "work.json",
                 "executor_config.json", "executor.log", "contract_report.json",
                 "review.json", "run_info.json", "portfolio.pkl", "spec.json",
                 "sub/model_seed42.txt"):
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    arts = harness._collect_artifacts(tmp_path)
    for name in ("run_info.json", "portfolio.pkl", "spec.json",
                 "sub/model_seed42.txt", "pred_matrix.pkl"):
        assert name in arts, name
    assert arts["pred_matrix.pkl"].endswith("pred.pkl")


def test_build_params_quant_complete():
    work = {"task": "regression", "executor": "executors/_example_lgb",
            "data_key": "dk", "test_key": "tk", "tester_seed": 42,
            "tester_seconds": 3.1, "executor_seconds": 9.9, "changes": "c",
            "cfg": {"pool": "hs300", "handler_class": "Alpha158",
                    "instruments": "csi300", "label_formula": "f", "horizon": 10,
                    "fit_start_time": "a", "fit_end_time": "b", "seeds": [42],
                    "ensemble": "rank_mean(seeds)", "rounds": 100,
                    "early_stopping": 50, "num_threads": 8, "model": {"x": 1},
                    "train": ["a", "b"], "valid": ["c", "d"],
                    "test_start": "e", "test_end": "f",
                    "price_pq": "/x/prices_hs300_abc.parquet"}}
    rep = {"n_dates": 10, "date_frac": 1, "n_inst": 20, "inst_frac": 1,
           "nan_frac": 0}
    p = harness._build_params(work, rep)
    for k in ("pool", "handler", "task", "executor", "instruments", "label",
              "horizon", "fit_window", "seeds", "ensemble", "rounds",
              "early_stopping", "num_threads", "model", "train_window",
              "valid_window", "test_window", "data_key", "price_key",
              "tester_seed", "tester_seconds", "executor_seconds", "changes",
              "coverage"):
        assert k in p, k
    assert p["price_key"] == "prices_hs300_abc.parquet"
    assert p["changes"] == "c"
    assert p["seeds"] == "[42]"
    assert p["tester_seed"] == "42"


def test_find_prior_run_reuses_only_exact_match(monkeypatch):
    class FakeClient:
        def search_experiments(self):
            return [type("E", (), {"experiment_id": "e1"})()]

        def search_runs(self, eid, max_results=5000):
            r = type("R", (), {
                "info": type("I", (), {"run_id": "rid9", "status": "FINISHED"})(),
                "data": type("D", (), {"tags": {
                    "qlab.spec_hash": "h1", "qlab.git": "g1",
                    "qlab.data_key": "dk1", "qlab.test_key": "tk1"}})()})()
            return [r]
    monkeypatch.setattr(harness.registry, "client", lambda: FakeClient())
    work = {"git_commit": "g1", "data_key": "dk1", "test_key": "tk1"}
    assert harness._find_prior_run(work, "h1") == "rid9"
    # commit 不同 = 代码不同 = 新 run，绝不误复用
    assert harness._find_prior_run(
        {"git_commit": "g2", "data_key": "dk1", "test_key": "tk1"}, "h1") is None
    assert harness._find_prior_run(work, "h2") is None
    assert harness._find_prior_run(
        {"git_commit": "g1", "data_key": "dkX", "test_key": "tk1"}, "h1") is None


def test_requirements_txt_is_code():
    assert ".txt" in harness.CODE_EXTENSIONS


def test_tester_seed_fixed_and_reproducible():
    from pipeline import metrics
    assert metrics.TESTER_BOOTSTRAP_SEED == 42
    b1 = metrics.bootstrap_ci([1.0, 2.0, 3.0, -1.0], n_boot=200)
    b2 = metrics.bootstrap_ci([1.0, 2.0, 3.0, -1.0], n_boot=200)
    assert b1 == b2
