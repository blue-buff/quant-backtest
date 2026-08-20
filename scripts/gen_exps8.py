"""e19 15d hs300 种子 yaml（seed 100/200/300）。"""
import copy
from pathlib import Path

import yaml

OUT = Path("/root/quant/qlib_examples/experiments")
base = yaml.safe_load((OUT / "e19_off_15d_21_hs300.yaml").read_text(encoding="utf-8"))
for i, seed in enumerate([100, 200, 300], start=1):
    cfg = copy.deepcopy(base)
    cfg["task"]["model"]["kwargs"].update({"seed": seed, "bagging_seed": seed + 1, "feature_fraction_seed": seed + 2})
    out = OUT / f"e19{i}_15d21_seed{seed}_hs300.yaml"
    out.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(out)
