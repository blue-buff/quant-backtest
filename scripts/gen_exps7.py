"""e19: 15 日标签（10 与 20 之间插值验证）。"""
import copy
from pathlib import Path

import yaml

OUT = Path("/root/quant/qlib_examples/experiments")
L15 = ["Ref($close, -16)/Ref($close, -1) - 1"]
for pool in ("hs300", "zz500"):
    cfg = yaml.safe_load((OUT / f"e12_off_10d_21_{pool}.yaml").read_text(encoding="utf-8"))
    cfg["task"]["dataset"]["kwargs"]["handler"]["kwargs"]["label"] = L15
    out = OUT / f"e19_off_15d_21_{pool}.yaml"
    out.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(out)
