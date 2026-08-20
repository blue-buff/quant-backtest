"""滚动（walk-forward）训练与评估：真实模拟"每个重训点只用到当时为止的数据"。

每次窗口：
  train: [2022-01-01, fit_end]
  valid: [fit_end, fit_end+3M]
  test:  [fit_end+3M, min(fit_end+6M, 2026-08-14)]
跑 qrun 后收集 pred.pkl；所有窗口的预测拼接后交给 eval_pred 评估。
与一次性训练（训练止于 2024-06 却预测到 2026-08）的区别就是"没有时滞泄漏"。

用法:
  python scripts/run_rolling.py --pool hs300 --yaml qlib_examples/experiments/e01_off_1d_hs300.yaml \
      --fits 2024-06-30,2024-12-31,2025-06-30,2025-12-31,2026-03-31 --tag roll1
产物:
  results/exps/<tag>_wNNNN/  每个窗口的 work.yaml + qrun.log + meta.json
  results/eval/<tag>.json    拼接后的全窗口评估（run_rolling 最后调用 eval_pred 逻辑）
"""
import argparse
import copy
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MLRUNS = ROOT / "qlib_examples" / "mlruns"
EXPS = ROOT / "results" / "exps"

METRIC_FILES = ["IC", "ICIR", "Rank IC", "Rank ICIR", "l2.train", "l2.valid"]


def next_quarter(d: str) -> str:
    y, m, day = int(d[:4]), int(d[5:7]), int(d[8:10])
    m += 3
    if m > 12:
        m -= 12
        y += 1
    return f"{y:04d}-{m:02d}-{day:02d}"


def read_metrics(run_dir: Path) -> dict:
    out = {}
    mdir = run_dir / "metrics"
    if not mdir.is_dir():
        return out
    for name in METRIC_FILES:
        f = mdir / name
        if f.exists():
            try:
                parts = f.read_text(encoding="utf-8").splitlines()[-1].split()
                out[name] = float(parts[1] if len(parts) >= 3 else parts[-1])
            except (OSError, ValueError, IndexError):
                pass
    return out


def run_window(base_cfg: dict, pool: str, fit_end: str, tag: str, wi: int):
    cfg = copy.deepcopy(base_cfg)
    valid_end = next_quarter(fit_end)
    test_end = next_quarter(valid_end)
    hc = cfg["task"]["dataset"]["kwargs"]["handler"]["kwargs"]
    hc["fit_start_time"] = "2022-01-01"
    hc["fit_end_time"] = fit_end
    seg = cfg["task"]["dataset"]["kwargs"]["segments"]
    seg["train"] = ["2022-01-01", fit_end]
    seg["valid"] = [fit_end, valid_end]
    seg["test"] = [valid_end, test_end]
    wname = f"{tag}_w{wi:02d}"
    exp_name = f"predbump_{wname}"
    cfg["experiment_name"] = exp_name

    exp_dir = EXPS / wname
    exp_dir.mkdir(parents=True, exist_ok=True)
    (exp_dir / "work.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    t0 = time.time()
    env = dict(os.environ, MLFLOW_ALLOW_FILE_STORE="true")
    r = subprocess.run([sys.executable, "-m", "qlib.cli.run", str(exp_dir / "work.yaml")],
                       capture_output=True, text=True, env=env, cwd=str(ROOT / "qlib_examples"))
    (exp_dir / "qrun.log").write_text((r.stdout or "") + (r.stderr or ""), encoding="utf-8")
    ok = r.returncode == 0
    cands = sorted(MLRUNS.glob("*/*/artifacts/pred.pkl"), key=lambda q: q.stat().st_mtime)
    run_dir = cands[-1].parent.parent if cands else None
    m = read_metrics(run_dir) if run_dir else {}
    meta = {"window": wname, "fit_end": fit_end, "test": [valid_end, test_end],
            "ok": ok, "seconds": round(time.time() - t0, 1), "metrics": m,
            "run_id": run_dir.name if run_dir else None,
            "pred": str((run_dir / "artifacts" / "pred.pkl").relative_to(ROOT)) if run_dir else None,
            "label": str((run_dir / "artifacts" / "label.pkl").relative_to(ROOT)) if run_dir else None}
    (exp_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    status = "OK " if ok else "FAIL"
    print(f"[{status}] {wname} fit_end={fit_end} test={valid_end}~{test_end} "
          f"IC={m.get('IC')} RankIC={m.get('Rank IC')} {meta['seconds']:.0f}s", flush=True)
    return meta


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pool", default="hs300")
    ap.add_argument("--yaml", required=True)
    ap.add_argument("--fits", default="2024-06-30,2024-12-31,2025-06-30,2025-12-31,2026-03-31")
    ap.add_argument("--tag", default="rolling")
    ap.add_argument("--eval-h", type=int, default=1)
    args = ap.parse_args()
    base = yaml.safe_load(Path(args.yaml).read_text(encoding="utf-8"))
    fits = [x.strip() for x in args.fits.split(",")]
    metas = []
    for i, fe in enumerate(fits):
        metas.append(run_window(base, args.pool, fe, args.tag, i))
    print("\n===== 全窗口完成，拼接评估 =====")
    preds = sorted(set(m["pred"] for m in metas if m.get("pred")))
    if preds:
        cmd = [sys.executable, str(ROOT / "scripts" / "eval_pred.py"), "--name", args.tag, "--h", str(args.eval_h)]
        for p in preds:
            cmd += ["--run-dir", str(ROOT / p).replace("artifacts" + os.sep + "pred.pkl", "")]
        print("eval cmd:", " ".join(cmd[1:]))
        r = subprocess.run(cmd, cwd=ROOT)
        sys.exit(r.returncode)
    print("无可用 pred，跳过评估")


if __name__ == "__main__":
    main()
