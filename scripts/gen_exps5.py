"""10d_21 种子 yaml（e12 基础 + seed 100/200/300）。"""
import copy
from pathlib import Path

import yaml

ROOT = Path("/root/quant")
OUT = ROOT / "qlib_examples" / "experiments"
for pool in ("hs300", "zz500"):
    base = yaml.safe_load((OUT / f"e12_off_10d_21_{pool}.yaml").read_text(encoding="utf-8"))
    for i, seed in enumerate([100, 200, 300], start=1):
        cfg = copy.deepcopy(base)
        cfg["task"]["model"]["kwargs"].update({"seed": seed, "bagging_seed": seed + 1,
                                               "feature_fraction_seed": seed + 2})
        out = OUT / f"e15{i}_10d21_seed{seed}_{pool}.yaml"
        out.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
        print(out)
