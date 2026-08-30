"""B8: qlib_bench 执行器兼容 shim 回归（每点一条，vendor 钉死见 executors/qlib_bench/VENDOR.md）。"""
import importlib.util
from pathlib import Path

import pytest

MAIN = Path(__file__).resolve().parents[1] / "executors" / "qlib_bench" / "main.py"


def _load():
    spec = importlib.util.spec_from_file_location("qb_main", MAIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_torch_compat_reduce_lr_verbose_idempotent():
    """shim #1: torch>=2.2 的 ReduceLROnPlateau 不接受 verbose=；打补丁后可构造。
    （本地系统 python 无 torch 时跳过；torch 在 qlib_bench venv / 远端环境。）"""
    torch = pytest.importorskip("torch")
    mod = _load()
    mod._torch_compat()
    torch.optim.lr_scheduler.ReduceLROnPlateau(
        torch.optim.SGD([torch.zeros(1, requires_grad=True)], lr=0.1),
        verbose=True)  # 未打补丁时 torch 2.10 直接 TypeError
    mod._torch_compat()  # 幂等


def test_torch_load_default_weights_only_false(tmp_path):
    """shim #2: torch.load 默认 weights_only=False（官方旧版 pkl 可反序列化）。"""
    torch = pytest.importorskip("torch")
    mod = _load()
    mod._torch_compat()
    p = tmp_path / "t.pth"
    torch.save({"x": torch.zeros(2)}, str(p))
    d = torch.load(str(p))  # 不传 weights_only：shim 默认 False
    assert d["x"].shape == (2,)


class _FakeModelModule:
    class FakeModel:
        def __init__(self, **kw):
            self.kw = kw


def test_load_model_forces_n_jobs_zero(monkeypatch):
    """shim #3: ts 模型 n_jobs 强制 0（远端 /dev/shm=64MB）；其余 kwargs 原样。"""
    mod = _load()
    fake = _FakeModelModule()
    monkeypatch.setattr(mod.importlib, "import_module", lambda p: fake)
    m = mod._load_model({"model": {"module_path": "x.y", "class": "FakeModel",
                                  "kwargs": {"n_jobs": 20, "lr": 0.1}}})
    assert m.kw["n_jobs"] == 0
    assert m.kw["lr"] == 0.1


def test_load_model_keeps_kwargs_without_n_jobs(monkeypatch):
    mod = _load()
    fake = _FakeModelModule()
    monkeypatch.setattr(mod.importlib, "import_module", lambda p: fake)
    m = mod._load_model({"model": {"module_path": "x.y", "class": "FakeModel",
                                  "kwargs": {"lr": 0.1}}})
    assert m.kw == {"lr": 0.1}  # 不新增键


def test_parquet_handler_exposes_loader_fields():
    """shim #5: ParquetHandler 暴露 data_loader.fields / _learn（MTSDatasetH/TRA 直读）。"""
    mod = _load()
    import pandas as pd
    idx = pd.MultiIndex.from_product(
        [pd.to_datetime(["2025-01-02", "2025-01-03"]), ["S1", "S2"]],
        names=["datetime", "instrument"])
    df = pd.DataFrame({"F1": [1.0, 2.0, 3.0, 4.0], "LABEL0": [0.1, 0.2, 0.3, 0.4]},
                      index=idx)
    h = mod.ParquetHandler(df)
    assert "feature" in h.data_loader.fields
    assert "label" in h.data_loader.fields
    assert hasattr(h, "_learn")
    out = h.fetch(selector=slice(None, None), col_set=["feature", "label"])
    lvl0 = set(out.columns.get_level_values(0))
    assert "feature" in lvl0 and "label" in lvl0
