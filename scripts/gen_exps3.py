"""生成 2021-06 起训练窗口的实验 yaml（e11_1d / e12_10d / e13_20d，官方处理器）。"""
import copy
from pathlib import Path

import yaml

ROOT = Path("/root/quant")
OUT = ROOT / "qlib_examples" / "experiments"
BASE = {}
for p in ("hs300", "zz500"):
    BASE[p] = yaml.safe_load((OUT / f"e01_off_1d_{p}.yaml").read_text(encoding="utf-8"))

LABELS = {
    "e11_off_1d": None,
    "e12_off_10d": ["Ref($close, -11)/Ref($close, -1) - 1"],
    "e13_off_20d": ["Ref($close, -21)/Ref($close, -1) - 1"],
}
for pool, base in BASE.items():
    for name, lab in LABELS.items():
        cfg = copy.deepcopy(base)
        hk = cfg["task"]["dataset"]["kwargs"]["handler"]["kwargs"]
        hk["start_time"] = "2021-06-01"
        hk["fit_start_time"] = "2021-06-01"
        if lab:
            hk["label"] = lab
        seg = cfg["task"]["dataset"]["kwargs"]["segments"]
        seg["train"] = ["2021-06-01", "2024-06-30"]
        out = OUT / f"{name}_21_{pool}.yaml"
        out.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
        print(out)
