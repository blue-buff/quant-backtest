"""生成 10d 标签 + 多种子的集成实验 yaml（e08_off_10d_seed100/200/300）。"""
import copy
from pathlib import Path

import yaml

ROOT = Path("/root/quant")
OUT = ROOT / "qlib_examples" / "experiments"
BASE = {p: yaml.safe_load((OUT / f"e03_off_10d_{p}.yaml").read_text(encoding="utf-8"))
        for p in ("hs300", "zz500")}
for pool, base in BASE.items():
    for i, seed in enumerate([100, 200, 300], start=1):
        cfg = copy.deepcopy(base)
        cfg["task"]["model"]["kwargs"].update({"seed": seed, "bagging_seed": seed + 1,
                                               "feature_fraction_seed": seed + 2})
        out = OUT / f"e08{i}_off_10d_seed{seed}_{pool}.yaml"
        out.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
        print(out)
