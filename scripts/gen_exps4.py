"""e14：波动率缩放标签（Barroso & Santa-Clara 2015 / Moreira & Muir 2017 思路）
label = 未来10日收益 / Max(过去20日收益标准差, 0.005)
"""
import copy
from pathlib import Path

import yaml

ROOT = Path("/root/quant")
OUT = ROOT / "qlib_examples" / "experiments"
VOL_LABEL = ["(Ref($close, -11)/Ref($close, -1) - 1) / Max(Std($close/Ref($close, 1) - 1, 20), 0.005)"]
for pool in ("hs300", "zz500"):
    cfg = yaml.safe_load((OUT / f"e03_off_10d_{pool}.yaml").read_text(encoding="utf-8"))
    cfg["task"]["dataset"]["kwargs"]["handler"]["kwargs"]["label"] = VOL_LABEL
    out = OUT / f"e14_vol10d_{pool}.yaml"
    out.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(out)
