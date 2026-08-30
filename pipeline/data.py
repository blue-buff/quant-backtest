"""Data layer (P5 executor contract): fixed-menu feature matrices.

The pipeline owns the data menu: pool x handler (Alpha158/Alpha360) x label x
windows. Features are built once into cached float32 parquet via a sliced,
memory-friendly fetch; executors only READ these parquet files -- they never
build features themselves. That fixed menu is what keeps every experiment
comparable on the board.

Cache integrity (P7 T2):
- cache/manifest.json maps filename -> {pool, key, built_at} (atomic write).
- cache/data_revision.json is THE data-changed signal; bumped only by
  invalidate() (called by scripts/update_tail.py after data updates).
- parquet writes go to <file>.part then os.replace (no torn files).
- reads are light-validated (num_rows>0, schema has y); broken files are
  rebuilt. Legacy caches without a manifest entry are registered with
  built_at="unknown", never rebuilt, never bump the revision.

CLI: python -m pipeline.data ensure <spec_path> [--rebuild]
     python -m pipeline.data invalidate [--pool hs300|zz500|all] [--all]
"""
import argparse, hashlib, json, os, time
from pathlib import Path

import numpy as np
import pandas as pd

from . import QLAB_ROOT, DATA_VERSION

CACHE_DIR = QLAB_ROOT / "cache"

def _qlib_base():
    """qlib bins 根目录：env QLAB_QLIB_DATA 优先；容器默认 /root/.qlib（存在时
    行为不变）；本机（无容器，如 macOS 开发机）探测迁移过来的 bins
    （~/projects/.qlib 或 ~/.qlib），都找不到时回落容器路径便于报错定位。"""
    env = os.environ.get("QLAB_QLIB_DATA")
    if env:
        return env.rstrip("/")
    if Path("/root/.qlib").exists():
        return "/root/.qlib"
    for cand in (Path.home() / "projects" / ".qlib", Path.home() / ".qlib"):
        if cand.exists():
            return str(cand)
    return "/root/.qlib"


def _pool_map():
    """Pool -> (provider_uri, default instruments). The data root follows
    QLAB_QLIB_DATA so remote containers (no /root access) can point elsewhere."""
    base = _qlib_base()
    return {
        "all": (base + "/qlib_data/cn_data_all", "all"),
        "hs300": (base + "/qlib_data/cn_data", "csi300"),
        "zz500": (base + "/qlib_data/cn_data_zz500", "csi500"),
    }


POOL_MAP = _pool_map()

HANDLER_CLASSES = ("Alpha158", "Alpha360")
TASKS = ("regression", "classification")

DEFAULTS = {
    "fit_start_time": "2021-06-01",
    "fit_end_time": "2024-06-30",
    "train": ["2021-06-01", "2024-06-30"],
    "valid": ["2024-07-01", "2024-12-31"],
    "test_start": "2025-01-01",
    "test_end": "2026-08-20",
    "rounds": 1000,
    "early_stopping": 50,
    # container cgroup pids.max=256: keep the default thread budget small
    "num_threads": 8,
}

DEFAULT_LEARN_PROCESSORS = [
    {"class": "DropnaLabel", "module_path": "qlib.data.dataset.processor"},
    {"class": "CSZScoreNorm", "module_path": "qlib.data.dataset.processor",
     "kwargs": {"fields_group": "label"}},
]


def resolve(spec, eff):
    """Effective data config from resolved spec. Bad config raises ValueError
    with QLAB_SPEC_INVALID (fail loud, never silently fall back)."""
    h = eff.get("dataset", {}).get("handler", {})
    lab = eff.get("label", {})
    act = spec.get("action", {})
    pool = str(eff.get("universe") or "all")
    if pool not in POOL_MAP:
        raise ValueError("QLAB_SPEC_INVALID: unknown pool %r (allowed: %s)"
                         % (pool, sorted(POOL_MAP)))
    handler_class = str(h.get("class") or "Alpha158")
    if handler_class not in HANDLER_CLASSES:
        raise ValueError("QLAB_SPEC_INVALID: unknown handler %r (allowed: %s)"
                         % (handler_class, list(HANDLER_CLASSES)))
    task = str(act.get("task") or "regression")
    if task not in TASKS:
        raise ValueError("QLAB_SPEC_INVALID: unknown task %r (allowed: %s)"
                         % (task, list(TASKS)))
    horizon = int(lab.get("horizon", 10))
    label_formula = lab.get("formula")
    if not label_formula:
        label_formula = "Ref($close,-%d)/Ref($close,-1)-1" % (horizon + 1)
    provider, inst_default = POOL_MAP[pool]
    learn = eff.get("processors", {}).get("learn")
    # infer processors (qlib official benchmark configs): applied to ALL
    # segments at fetch time, e.g. FilterCol/RobustZScoreNorm/Fillna on
    # features. Empty by default (historical behavior).
    infer = eff.get("processors", {}).get("infer")
    cfg = {
        "pool": pool,
        "provider_uri": provider,
        "instruments": h.get("instruments") or inst_default,
        "handler_class": handler_class,
        "fit_start_time": h.get("fit_start_time", DEFAULTS["fit_start_time"]),
        "fit_end_time": h.get("fit_end_time", DEFAULTS["fit_end_time"]),
        "train": list(DEFAULTS["train"]),
        "valid": list(DEFAULTS["valid"]),
        "test_start": act.get("test_start", DEFAULTS["test_start"]),
        "test_end": act.get("test_end", DEFAULTS["test_end"]),
        "label_formula": label_formula,
        "horizon": horizon,
        "learn_processors": learn if learn else DEFAULT_LEARN_PROCESSORS,
        "infer_processors": infer if infer else [],
        "process_type": str(eff.get("processors", {}).get("process_type") or "append"),
        "task": task,
    }
    return cfg


def _key(cfg, test=False):
    canon = {
        "pool": cfg["pool"],
        "instruments": cfg["instruments"],
        "handler_class": cfg["handler_class"],
        "fit": [cfg["fit_start_time"], cfg["fit_end_time"]],
        "label": cfg["label_formula"],
        "learn_processors": cfg["learn_processors"],
        "infer_processors": cfg["infer_processors"],
        "process_type": cfg["process_type"],
    }
    # train cache covers fit_start..valid[1]; test cache covers test_start..test_end
    if test:
        canon["window"] = [cfg["test_start"], cfg["test_end"]]
    else:
        canon["window"] = [cfg["fit_start_time"], cfg["valid"][1]]
    return hashlib.sha256(json.dumps(canon, sort_keys=True, ensure_ascii=False)
                          .encode()).hexdigest()[:16]


# ---------- cache manifest + data revision (P7 T2) ----------

def _manifest_path(cache_dir):
    return Path(cache_dir) / "manifest.json"


def _revision_path(cache_dir):
    return Path(cache_dir) / "data_revision.json"


def _load_manifest(cache_dir):
    p = _manifest_path(cache_dir)
    if not p.exists():
        return {}
    try:
        m = json.loads(p.read_text())
        return m if isinstance(m, dict) else {}
    except (ValueError, OSError):
        return {}


def _save_manifest(cache_dir, manifest):
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = _manifest_path(cache_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True))
    os.replace(tmp, p)


def record_cache(pq_name, pool, key, cache_dir=CACHE_DIR):
    """Register a freshly built cache file in the manifest (atomic write)."""
    mf = _load_manifest(cache_dir)
    mf[str(pq_name)] = {"pool": str(pool), "key": str(key),
                        "built_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _save_manifest(cache_dir, mf)
    return mf[str(pq_name)]


def data_revision(cache_dir=CACHE_DIR):
    """Current data revision (0 when the file is missing)."""
    p = _revision_path(cache_dir)
    if not p.exists():
        return 0
    try:
        return int(p.read_text().strip() or 0)
    except (ValueError, OSError):
        return 0


def _write_revision(cache_dir, rev):
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    tmp = _revision_path(cache_dir).with_suffix(".json.tmp")
    tmp.write_text(str(rev))
    os.replace(tmp, _revision_path(cache_dir))
    return rev


def _bump_revision(cache_dir):
    return _write_revision(cache_dir, data_revision(cache_dir) + 1)


def invalidate(pool=None, cache_dir=CACHE_DIR):
    """Delete cached parquet for one pool (or everything when pool=None) and
    bump the data revision. The revision is the ONLY signal that the underlying
    data changed; corruption-triggered rebuilds do NOT bump it. An unknown pool
    matches nothing and is a no-op."""
    cache_dir = Path(cache_dir)
    mf = _load_manifest(cache_dir)
    removed = []
    for name, entry in list(mf.items()):
        if pool is None or entry.get("pool") == pool:
            p = cache_dir / name
            if p.exists():
                p.unlink()
            p.with_name(name + ".part").unlink(missing_ok=True)
            del mf[name]
            removed.append(name)
    rev = _bump_revision(cache_dir) if (removed or pool is None) else data_revision(cache_dir)
    _save_manifest(cache_dir, mf)
    _write_revision(cache_dir, rev)  # materialize the revision file on every call
    return {"removed": removed, "revision": rev}


def cache_valid(cache_path, required_cols=("y",)):
    """Light read-side validation: opens as parquet, num_rows>0, schema has the
    required columns (feature caches need y; price caches need close)."""
    try:
        import pyarrow.parquet as pq
        md = pq.ParquetFile(str(cache_path)).metadata
        return md.num_rows > 0 and all(c in md.schema.names for c in required_cols)
    except Exception:
        return False


# ---------- fetch / build ----------

def _fetch(cfg, start, end, cache_path, data_key=None, load_start=None):
    """Sliced fetch -> cached float32 parquet (writes to cache_path, which the
    caller passes as the .part file; the caller does the final os.replace).
    Memory-friendly: one handler per ~180-trading-day slice,
    chunked float32 + pyarrow append.

    data_key: qlib DK_L (learn view; default) for train+valid parquet, DK_I
    (inference view: infer processors only, raw label) for the test parquet --
    matches the official benchmark recorder, which scores test on DK_I.

    load_start: the handler's data window starts at load_start (default start).
    qlib fits infer processors (FilterCol/RobustZScoreNorm/Fillna) on the
    handler's fit range -- a per-slice handler whose window lies entirely
    outside the fit range (the test window!) fits them on EMPTY data, which
    NaNs the features and Fillna turns them into ZEROS (observed 2026-08-24,
    lin_a158 constant predictions). The official workflow uses ONE handler
    over the whole period, so infer stats come from the train period. Test
    caches therefore load [fit_start, end] and slice the fetch selector to
    [start, end]."""
    from qlib.contrib.data.handler import Alpha158, Alpha360
    from qlib.data.dataset.handler import DataHandlerLP
    import pyarrow as pa
    import pyarrow.parquet as pq
    if data_key is None:
        data_key = DataHandlerLP.DK_L
    if load_start is None:
        load_start = start
    handler_cls = {"Alpha158": Alpha158, "Alpha360": Alpha360}[cfg["handler_class"]]
    days = pd.bdate_range(start, end)
    if len(days) == 0:
        raise ValueError("QLAB_SPEC_INVALID: empty fetch window %s..%s" % (start, end))
    SLICE_DAYS = 180
    if load_start != start or cfg.get("infer_processors"):
        SLICE_DAYS = len(days)  # single-slice: infer stats over the whole window
    writer = None
    for i in range(0, len(days), SLICE_DAYS):
        s0 = days[i]
        s1 = days[min(i + SLICE_DAYS, len(days)) - 1]
        h = handler_cls(instruments=cfg["instruments"], start_time=load_start,
                        end_time=s1,
                        fit_start_time=cfg["fit_start_time"],
                        fit_end_time=cfg["fit_end_time"],
                        learn_processors=cfg["learn_processors"],
                        infer_processors=cfg.get("infer_processors") or [],
                        process_type=cfg.get("process_type") or "append",
                        label=[cfg["label_formula"]])
        data = h.fetch(selector=slice(s0, s1), col_set=["feature", "label"],
                       data_key=data_key)
        feat64 = data["feature"]
        lab = data["label"].iloc[:, 0]
        del data, h
        CHUNK = 400000
        for s in range(0, len(feat64), CHUNK):
            sub = feat64.iloc[s:s + CHUNK].astype(np.float32)
            chunk = sub.join(lab.iloc[s:s + CHUNK].rename("y"))
            table = pa.Table.from_pandas(chunk)
            if writer is None:
                writer = pq.ParquetWriter(cache_path, table.schema)
            writer.write_table(table)
            del sub, chunk, table
        del feat64, lab
        print("cache slice %s..%s done" % (s0.date(), s1.date()), flush=True)
    if writer is not None:
        writer.close()
    return cache_path


def _cache_ok_or_rebuild(cache_path, pool, key, cache_dir, builder,
                        required_cols=("y",)):
    """Validate-or-rebuild one cached parquet with atomic write + manifest.
    builder(part_path) must write the parquet to the .part file; the final
    os.replace happens here."""
    cache_path = Path(cache_path)
    mf = _load_manifest(cache_dir)
    if cache_path.exists():
        if cache_valid(cache_path, required_cols):
            if cache_path.name not in mf:
                # legacy cache (pre-manifest): register only, never rebuild
                mf[cache_path.name] = {"pool": pool, "key": key,
                                       "built_at": "unknown"}
                _save_manifest(cache_dir, mf)
            return cache_path
        cache_path.unlink()  # broken -> rebuild
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    part = cache_path.with_name(cache_path.name + ".part")
    # per-file build lock: two dispatcher threads must never write the same
    # .part / os.replace each other's file away (cold-cache race, audit #2)
    try:
        import fcntl
    except ImportError:
        fcntl = None
    lk = None
    if fcntl is not None:
        try:
            lk = open(str(cache_path.with_name(cache_path.name + ".lock")), "w")
            fcntl.flock(lk, fcntl.LOCK_EX)
        except OSError:
            if lk is not None:
                lk.close()
            lk = None
    try:
        # double-checked under the lock: another builder may have just finished
        if cache_path.exists() and cache_valid(cache_path, required_cols):
            if cache_path.name not in mf:
                mf = _load_manifest(cache_dir)
                mf[cache_path.name] = {"pool": pool, "key": key,
                                       "built_at": "unknown"}
                _save_manifest(cache_dir, mf)
            return cache_path
        part.unlink(missing_ok=True)
        builder(part)
        os.replace(part, cache_path)
        record_cache(cache_path.name, pool, key, cache_dir)
        return cache_path
    finally:
        if lk is not None:
            try:
                fcntl.flock(lk, fcntl.LOCK_UN)
            except OSError:
                pass
            lk.close()


def _ensure_cache(cfg, key, pool, start, end, cache_path, cache_dir,
                  data_key=None, load_start=None):
    """Feature cache via the shared validate-or-rebuild path."""
    return _cache_ok_or_rebuild(cache_path, pool, key, cache_dir,
                                lambda p: _fetch(cfg, start, end, p,
                                                 data_key=data_key,
                                                 load_start=load_start))


# ---------- price cache (P8 T1): the single price source for backtests ----------

PRICE_COLUMNS = (
    "open_raw", "high_raw", "low_raw", "close_raw", "close",
    "volume", "amount", "vwap", "turn", "factor",
    "limit_up", "limit_down", "suspended",
)
PRICE_SCHEMA_VERSION = 2

PRICE_SOURCE_DIRS = {
    "hs300": QLAB_ROOT / "qlib_data_src",
    "zz500": QLAB_ROOT / "qlib_data_src_zz500",
    "all": QLAB_ROOT / "qlib_data_src_all",
}


def price_key(pool, start, end):
    # Include the schema/column list: old close-only caches must never be
    # silently reused after execution fields (raw OHLC + trade flags) are added.
    canon = {"kind": "prices", "pool": pool, "window": [start, end],
             "schema_version": PRICE_SCHEMA_VERSION,
             "columns": list(PRICE_COLUMNS)}
    return hashlib.sha256(json.dumps(canon, sort_keys=True, ensure_ascii=False)
                          .encode()).hexdigest()[:16]


def _price_limit_threshold(instrument):
    """A-share daily price limit by exchange/board prefix. Returns None for
    indices/unsupported listings (no limit flag is inferred)."""
    code = str(instrument).upper()
    body = code[2:]
    if code.startswith("SH"):
        if body.startswith(("688", "689")):
            return 0.20
        if body.startswith(("600", "601", "603", "605")):
            return 0.10
    elif code.startswith("SZ"):
        if body.startswith("300"):
            return 0.20
        if body.startswith(("000", "001", "002", "003")):
            return 0.10
    return None


def _build_prices(pool, start, end, cache_path):
    """Pool CSVs -> cached float32 parquet with (datetime, instrument) index.

    Source CSVs store prices in hfq units; ``factor`` maps them back to
    exchange raw prices. We keep both views because returns need hfq while
    A-share limit checks need raw prices. Suspension is inferred from zero
    volume; rows omitted upstream cannot be reconstructed here. Index CSVs
    (sh000300/sh000905) are included: they serve as benchmarks."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    src = PRICE_SOURCE_DIRS[pool]
    if not src.exists():
        raise ValueError("QLAB_SPEC_INVALID: price source missing: %s" % src)
    files = sorted(f for f in src.glob("*.csv") if not f.name.startswith("_"))
    if not files:
        raise ValueError("QLAB_SPEC_INVALID: no price CSVs in %s" % src)
    writer = None

    def flush(pieces):
        nonlocal writer
        df = pd.concat(pieces, ignore_index=True)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index(["date", "instrument"])
        df.index = df.index.set_names(["datetime", "instrument"])
        numeric = [c for c in PRICE_COLUMNS if not c.startswith(("limit_", "suspended"))]
        df[numeric] = df[numeric].astype(np.float32)
        table = pa.Table.from_pandas(df[list(PRICE_COLUMNS)])
        if writer is None:
            writer = pq.ParquetWriter(cache_path, table.schema)
        writer.write_table(table)
        del df, table

    pieces, n = [], 0
    for f in files:
        try:
            df = pd.read_csv(f, usecols=[
                "date", "open", "high", "low", "close",
                "volume", "amount", "vwap", "turn", "factor"])
        except Exception:
            continue
        df["date"] = df["date"].astype(str)
        df = df[(df["date"] >= start) & (df["date"] <= end)]
        if df.empty:
            continue
        # CSV 文件名是小写（sh600000），qlib 仪器代码是大写（SH600000）——统一大写
        df = df.assign(instrument=f.stem.upper())
        factor = pd.to_numeric(df["factor"], errors="coerce")
        if not factor.gt(0).all():
            raise ValueError("QLAB_DATA_INVALID: non-positive factor in %s" % f)
        for c in ("open", "high", "low", "close"):
            df[c + "_raw"] = pd.to_numeric(df[c], errors="coerce") / factor
        df["factor"] = factor
        prev_raw = df["close_raw"].shift(1)
        threshold = _price_limit_threshold(f.stem.upper())
        if threshold is None:
            df["limit_up"] = False
            df["limit_down"] = False
        else:
            df["limit_up"] = df["close_raw"] >= prev_raw * (1.0 + threshold) - 1e-6
            df["limit_down"] = df["close_raw"] <= prev_raw * (1.0 - threshold) + 1e-6
        volume = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
        df["suspended"] = volume <= 0
        pieces.append(df)
        n += len(df)
        if n >= 500000:
            flush(pieces)
            pieces, n = [], 0
        if len(pieces) % 500 == 0:
            print("prices %s: %d files, %d rows so far" % (pool, len(pieces), n),
                  flush=True)
    if pieces:
        flush(pieces)
    if writer is not None:
        writer.close()
    return cache_path


def price_ensure(cfg, rebuild=False):
    """Ensure the price cache exists for cfg['pool'] over the full fit..test
    span. Same manifest/revision/atomic-write mechanism as feature caches.
    Returns the parquet Path (executors read close prices ONLY from here)."""
    start, end = cfg["fit_start_time"], cfg["test_end"]
    key = price_key(cfg["pool"], start, end)
    pq_path = CACHE_DIR / ("prices_%s_%s.parquet" % (cfg["pool"], key))
    if rebuild:
        pq_path.unlink(missing_ok=True)
        pq_path.with_name(pq_path.name + ".part").unlink(missing_ok=True)
    return _cache_ok_or_rebuild(
        pq_path, cfg["pool"], key, CACHE_DIR,
        lambda p: _build_prices(cfg["pool"], start, end, p),
        required_cols=PRICE_COLUMNS)


def ensure(spec, eff, rebuild=False):
    """Ensure cached feature matrices exist for the spec's data config.
    Returns {"train_pq": Path, "test_pq": Path, "train_key": str,
             "test_key": str, "cfg": dict}."""
    import qlib
    cfg = resolve(spec, eff)
    # Local container cgroup pids.max=256: the default 14-process loky fetch
    # forks 14 workers whose threads push the task count over the limit, and a
    # single dying worker stalls the whole pool (observed twice: fork EAGAIN,
    # then a zombie worker deadlocking the fetch). Cap the fetch kernels to a
    # small, env-overridable number; spark dispatch may raise QLAB_KERNELS.
    qlib.init(provider_uri=cfg["provider_uri"], region="cn",
              kernels=int(os.environ.get("QLAB_KERNELS", "4")))
    train_key = _key(cfg, test=False)
    test_key = _key(cfg, test=True)
    train_pq = CACHE_DIR / ("train_%s.parquet" % train_key)
    test_pq = CACHE_DIR / ("test_%s.parquet" % test_key)
    if rebuild:
        for p in (train_pq, test_pq):
            p.unlink(missing_ok=True)
            p.with_name(p.name + ".part").unlink(missing_ok=True)
    t0 = time.time()
    _ensure_cache(cfg, train_key, cfg["pool"], cfg["fit_start_time"],
                  cfg["valid"][1], train_pq, CACHE_DIR)
    from qlib.data.dataset.handler import DataHandlerLP
    _ensure_cache(cfg, test_key, cfg["pool"], cfg["test_start"],
                  cfg["test_end"], test_pq, CACHE_DIR, data_key=DataHandlerLP.DK_I,
                  load_start=cfg["fit_start_time"])
    print("data ready %.0fs | %s %s | %s %s" % (
        time.time() - t0, cfg["pool"], cfg["handler_class"],
        train_pq.name, test_pq.name), flush=True)
    return {"train_pq": train_pq, "test_pq": test_pq,
            "train_key": train_key, "test_key": test_key, "cfg": cfg,
            "data_revision": data_revision(CACHE_DIR)}


def main():
    from . import spec as specmod
    ap = argparse.ArgumentParser(prog="pipeline.data")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("ensure")
    p1.add_argument("spec_path")
    p1.add_argument("--rebuild", action="store_true")
    p2 = sub.add_parser("invalidate")
    p2.add_argument("--pool", default=None)
    p2.add_argument("--all", action="store_true")
    a = ap.parse_args()
    if a.cmd == "ensure":
        spec = specmod.load_spec(a.spec_path)
        eff = specmod.resolve(spec)
        d = ensure(spec, eff, rebuild=a.rebuild)
        print(json.dumps({k: str(v) for k, v in d.items() if k != "cfg"},
                         ensure_ascii=False, indent=1))
    elif a.cmd == "invalidate":
        if a.all and a.pool:
            raise SystemExit("--all and --pool are mutually exclusive")
        print(json.dumps(invalidate(None if a.all else a.pool),
                         ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
