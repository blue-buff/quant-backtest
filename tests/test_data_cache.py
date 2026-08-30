"""P7 T2: 缓存 manifest + 数据修订号 + 原子写 + 读侧校验。"""
import json

import pyarrow as pa
import pyarrow.parquet as pq

from pipeline import data as d


def _write_ok_parquet(path):
    pq.write_table(pa.table({"y": pa.array([1.0, 2.0, 3.0])}), str(path))


def test_record_cache_and_load_manifest(tmp_path):
    d.record_cache("train_a.parquet", "hs300", "keyA", tmp_path)
    mf = d._load_manifest(tmp_path)
    assert mf["train_a.parquet"]["pool"] == "hs300"
    assert mf["train_a.parquet"]["key"] == "keyA"
    assert mf["train_a.parquet"]["built_at"] != "unknown"
    # manifest file itself is json with the same content
    assert json.loads((tmp_path / "manifest.json").read_text())["train_a.parquet"]["pool"] == "hs300"


def test_record_cache_does_not_bump_revision(tmp_path):
    d.record_cache("x.parquet", "hs300", "k", tmp_path)
    assert d.data_revision(tmp_path) == 0


def test_invalidate_only_that_pool(tmp_path):
    (tmp_path / "train_a.parquet").write_bytes(b"x")
    (tmp_path / "test_b.parquet").write_bytes(b"x")
    (tmp_path / "train_c.parquet").write_bytes(b"x")
    mf = {"train_a.parquet": {"pool": "hs300", "key": "a", "built_at": "t"},
          "test_b.parquet": {"pool": "hs300", "key": "b", "built_at": "t"},
          "train_c.parquet": {"pool": "zz500", "key": "c", "built_at": "t"}}
    d._save_manifest(tmp_path, mf)
    rev0 = d.data_revision(tmp_path)
    res = d.invalidate("hs300", cache_dir=tmp_path)
    assert not (tmp_path / "train_a.parquet").exists()
    assert not (tmp_path / "test_b.parquet").exists()
    assert (tmp_path / "train_c.parquet").exists()
    assert set(d._load_manifest(tmp_path)) == {"train_c.parquet"}
    assert res["revision"] == rev0 + 1
    assert d.data_revision(tmp_path) == rev0 + 1


def test_invalidate_all(tmp_path):
    (tmp_path / "a.parquet").write_bytes(b"x")
    (tmp_path / "b.parquet").write_bytes(b"x")
    d._save_manifest(tmp_path, {
        "a.parquet": {"pool": "hs300", "key": "a", "built_at": "t"},
        "b.parquet": {"pool": "zz500", "key": "b", "built_at": "t"}})
    res = d.invalidate(None, cache_dir=tmp_path)
    assert not (tmp_path / "a.parquet").exists()
    assert not (tmp_path / "b.parquet").exists()
    assert res["removed"] == ["a.parquet", "b.parquet"]


def test_invalidate_unknown_pool_is_noop(tmp_path):
    (tmp_path / "a.parquet").write_bytes(b"x")
    d._save_manifest(tmp_path, {"a.parquet": {"pool": "hs300", "key": "a", "built_at": "t"}})
    rev0 = d.data_revision(tmp_path)
    res = d.invalidate("not-a-pool", cache_dir=tmp_path)
    assert res["removed"] == []
    assert res["revision"] == rev0  # no bump: nothing matched
    assert (tmp_path / "a.parquet").exists()


def test_revision_persists_and_defaults_zero(tmp_path):
    assert d.data_revision(tmp_path) == 0
    d.invalidate(None, cache_dir=tmp_path)
    assert d.data_revision(tmp_path) == 1


def test_cache_valid(tmp_path):
    ok = tmp_path / "ok.parquet"
    _write_ok_parquet(ok)
    assert d.cache_valid(ok)
    noy = tmp_path / "noy.parquet"
    pq.write_table(pa.table({"x": pa.array([1, 2])}), str(noy))
    assert not d.cache_valid(noy)
    zero = tmp_path / "zero.parquet"
    pq.write_table(pa.table({"y": pa.array([], type=pa.float64())}), str(zero))
    assert not d.cache_valid(zero)
    assert not d.cache_valid(tmp_path / "missing.parquet")
    (tmp_path / "garbage.parquet").write_bytes(b"not a parquet")
    assert not d.cache_valid(tmp_path / "garbage.parquet")


def test_ensure_cache_rebuilds_broken_and_cleans_part(monkeypatch, tmp_path):
    """构造 part 残留 + 坏文件场景：验证 .part 原子写路径与损坏重建。"""
    cfg = {"pool": "hs300", "instruments": "csi300", "handler_class": "Alpha158",
           "fit_start_time": "2021-01-01", "fit_end_time": "2021-06-30",
           "label_formula": "x", "learn_processors": []}
    pq = tmp_path / "train_abc.parquet"
    pq.write_bytes(b"garbage")
    (tmp_path / "train_abc.parquet.part").write_bytes(b"stale part leftover")
    calls = []

    def fake_fetch(cfg, start, end, cache_path, data_key=None, load_start=None):
        calls.append(str(cache_path))
        _write_ok_parquet(cache_path)

    monkeypatch.setattr(d, "_fetch", fake_fetch)
    out = d._ensure_cache(cfg, "k1", "hs300", "2021-01-01", "2021-01-05", pq, tmp_path)
    assert out == pq
    assert calls and calls[0].endswith(".part")
    assert d.cache_valid(pq)
    assert not (tmp_path / "train_abc.parquet.part").exists()  # part 已被 replace 走
    mf = d._load_manifest(tmp_path)
    assert mf["train_abc.parquet"]["pool"] == "hs300"
    assert d.data_revision(tmp_path) == 0  # 损坏重建不推进修订号


def test_ensure_cache_registers_legacy_without_rebuild(monkeypatch, tmp_path):
    """旧缓存存在但 manifest 无记录：只登记 built_at=unknown，不重建。"""
    cfg = {}
    pq = tmp_path / "train_legacy.parquet"
    _write_ok_parquet(pq)
    calls = []
    monkeypatch.setattr(d, "_fetch", lambda *a, **k: calls.append(1))
    out = d._ensure_cache(cfg, "k", "hs300", "s", "e", pq, tmp_path)
    assert out == pq
    assert calls == []  # never rebuilt
    mf = d._load_manifest(tmp_path)
    assert mf["train_legacy.parquet"]["built_at"] == "unknown"
    assert d.data_revision(tmp_path) == 0


def test_concurrent_cold_build_single_builder(tmp_path):
    """回归（审计 #2）：两个线程同时冷构建同一缓存：只有一个 builder 真正执行，
    双方都拿到有效 parquet（无 FileNotFoundError/损坏）。"""
    import threading
    state = {"built": 0}
    gate = threading.Event()

    def builder(part):
        gate.wait(1.0)  # 让两个线程都先进入构建路径
        state["built"] += 1
        pq.write_table(pa.table({"y": pa.array([1.0, 2.0, 3.0])}), str(part))

    cache = tmp_path / "train_k.parquet"
    errs = []

    def worker():
        try:
            p = d._cache_ok_or_rebuild(cache, "hs300", "k", tmp_path, builder)
            assert str(p) == str(cache)
        except Exception as e:  # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    gate.set()
    for t in ts:
        t.join()
    assert errs == []
    assert state["built"] == 1
    assert d.cache_valid(cache, ("y",))
    assert "train_k.parquet" in d._load_manifest(tmp_path)


def test_ensure_returns_data_revision_at_build_time(tmp_path, monkeypatch):
    """回归（审计 #7）：ensure 把取数时的修订号带回来，供 work.json 入账。"""
    import qlib as _qlib
    from pipeline import data as datamod

    monkeypatch.setattr(datamod, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(_qlib, "init", lambda **kw: None)  # 不碰真实 qlib 数据目录
    datamod._write_revision(tmp_path, 7)
    monkeypatch.setattr(datamod, "resolve", lambda spec, eff: {
        "provider_uri": "file://x", "pool": "hs300", "handler_class": "Alpha158",
        "fit_start_time": "2021-06-01", "fit_end_time": "2024-05-31",
        "valid": ["2024-06-01", "2024-06-30"],
        "test_start": "2025-01-01", "test_end": "2025-01-10",
        "instruments": "csi300", "label_formula": "Ref($close,-10)/$close-1",
        "learn_processors": [], "infer_processors": [], "process_type": "full"})

    def fake_cache(cfg, key, pool, start, end, cache_path, cache_dir,
                    data_key=None, load_start=None):
        pq.write_table(pa.table({"y": pa.array([1.0, 2.0])}), str(cache_path))

    monkeypatch.setattr(datamod, "_ensure_cache", fake_cache)
    out = datamod.ensure({}, {})
    assert out["data_revision"] == 7
    assert out["train_pq"].exists()
