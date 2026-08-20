"""e18: 60 日标签（调研：大盘股价格修正慢，试更长周期）。"""
import copy
from pathlib import Path

import yaml

OUT = Path("/root/quant/qlib_examples/experiments")
L60 = ["Ref($close, -61)/Ref($close, -1) - 1"]
for pool in ("hs300", "zz500"):
    cfg = yaml.safe_load((OUT / f"e12_off_10d_21_{pool}.yaml").read_text(encoding="utf-8"))
    cfg["task"]["dataset"]["kwargs"]["handler"]["kwargs"]["label"] = L60
    out = OUT / f"e18_off_60d_21_{pool}.yaml"
    out.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(out)
