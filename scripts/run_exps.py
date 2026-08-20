"""实验运行器：qrun 一个实验 yaml → 收指标 → 写 results/experiments.csv + results/exps/<name>/ 留痕。

留痕内容（每次尝试都可复查）：
  results/exps/<name>/work.yaml  实际喂给 qrun 的配置（含 experiment_name）
  results/exps/<name>/qrun.log   qrun stdout+stderr
  results/exps/<name>/meta.json  指标、run_id、git commit、配置摘要
  results/experiments.csv        所有实验汇总表（追加）

用法:
  python scripts/run_exps.py --yaml qlib_examples/experiments/e01_off_1d_hs300.yaml
  python scripts/run_exps.py --dir qlib_examples/experiments --filter zz500   # 批量
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MLRUNS = ROOT / "qlib_examples" / "mlruns"
EXPS = ROOT / "results" / "exps"
CSV_PATH = ROOT / "results" / "experiments.csv"

METRIC_FILES = {"IC": "IC", "ICIR": "ICIR", "Rank IC": "Rank IC", "Rank ICIR": "Rank ICIR",
                "l2.train": "l2.train", "l2.valid": "l2.valid"}


def read_metrics(run_dir: Path) -> dict:
    out = {}
    mdir = run_dir / "metrics"
    if not mdir.is_dir():
        return out
    for name in METRIC_FILES.values():
        f = mdir / name
        if f.exists():
            try:
                parts = f.read_text(encoding="utf-8").splitlines()[-1].split()
                out[name] = float(parts[1] if len(parts) >= 3 else parts[-1])
            except (OSError, ValueError, IndexError):
                pass
    return out


def read_param(run_dir: Path, name: str) -> str:
    p = run_dir / "params" / name
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def git_head() -> str:
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT)
    return r.stdout.strip() or "?"


def run_one(yaml_path: Path, name: str | None = None) -> dict:
    name = name or yaml_path.stem
    cfg = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    pool = "hs300" if yaml_path.name.endswith("hs300.yaml") else "zz500"
    exp_dir = EXPS / name
    if exp_dir.exists():
        shutil.rmtree(exp_dir)
    exp_dir.mkdir(parents=True, exist_ok=True)

    exp_name = f"predbump_{name}"
    cfg["experiment_name"] = exp_name
    work_yaml = exp_dir / "work.yaml"
    work_yaml.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")

    # 记录该实验名下已有 run 数，跑完后找新增的 run
    before = set(p.name for p in (MLRUNS / exp_name).glob("*/meta.yaml")) if (MLRUNS / exp_name).exists() else set()

    t0 = time.time()
    env = dict(os.environ, MLFLOW_ALLOW_FILE_STORE="true")
    r = subprocess.run([sys.executable, "-m", "qlib.cli.run", str(work_yaml)],
                       capture_output=True, text=True, env=env, cwd=str(ROOT / "qlib_examples"))
    log = (r.stdout or "") + (r.stderr or "")
    (exp_dir / "qrun.log").write_text(log, encoding="utf-8")
    ok = r.returncode == 0
    took = time.time() - t0

    # 找本次新增的 run
    after = set(p.parent.name for p in (MLRUNS / exp_name).glob("*/meta.yaml")) if (MLRUNS / exp_name).exists() else set()
    new_runs = sorted(after - before)
    run_dir = (MLRUNS / exp_name / new_runs[-1]) if new_runs else None
    metrics = read_metrics(run_dir) if run_dir else {}
    # 模型标签/标签配置摘要
    try:
        label_cfg = cfg["task"]["dataset"]["kwargs"]["handler"]["kwargs"].get("label")
    except KeyError:
        label_cfg = None
    model_kwargs = cfg.get("task", {}).get("model", {}).get("kwargs", {})
    meta = {
        "name": name, "pool": pool, "ts": datetime.now().isoformat(timespec="seconds"),
        "git": git_head(), "yaml": str(yaml_path.relative_to(ROOT)), "ok": ok,
        "label": label_cfg or "default(1d)", "model_kwargs": model_kwargs,
        "segments": cfg["task"]["dataset"]["kwargs"]["segments"],
        "train_l2": metrics.get("l2.train"), "valid_l2": metrics.get("l2.valid"),
        "IC": metrics.get("IC"), "ICIR": metrics.get("ICIR"),
        "rank_IC": metrics.get("Rank IC"), "rank_ICIR": metrics.get("Rank ICIR"),
        "run_id": run_dir.name if run_dir else None,
        "seconds": round(took, 1),
    }
    if run_dir:
        meta["pred_path"] = str((run_dir / "artifacts" / "pred.pkl").relative_to(ROOT))
        meta["label_pkl"] = str((run_dir / "artifacts" / "label.pkl").relative_to(ROOT))
    (exp_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # 追加 CSV（表头首次创建）
    new_csv = not CSV_PATH.exists()
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(meta.keys()))
        if new_csv:
            w.writeheader()
        w.writerow(meta)

    status = "OK " if ok else "FAIL"
    print(f"[{status}] {name} ({pool}) IC={meta['IC']} ICIR={meta['ICIR']} "
          f"RankIC={meta['rank_IC']} RankICIR={meta['rank_ICIR']} "
          f"train_l2={meta['train_l2']} valid_l2={meta['valid_l2']} {took:.0f}s")
    if not ok:
        print("    tail:", "\n".join(log.splitlines()[-6:]))
    return meta


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yaml", type=str, default=None)
    ap.add_argument("--dir", type=str, default=None)
    ap.add_argument("--filter", type=str, default="", help="文件名子串过滤（批量模式）")
    args = ap.parse_args()
    if args.yaml:
        yamls = [Path(args.yaml).resolve()]
    elif args.dir:
        yamls = sorted(p for p in Path(args.dir).glob("*.yaml") if args.filter in p.name)
    else:
        ap.error("--yaml 或 --dir 二选一")
    print(f"待跑 {len(yamls)} 个实验")
    for y in yamls:
        run_one(y)


if __name__ == "__main__":
    main()
