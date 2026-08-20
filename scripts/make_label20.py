"""容器内计算 20d 标签矩阵（只取 label 列，供远程 20d 预测的深度评估）。"""
import time
from pathlib import Path

import pandas as pd


def main():
    import qlib
    from qlib.contrib.data.handler import Alpha158
    from qlib.data.dataset.handler import DataHandlerLP
    qlib.init(provider_uri="/root/.qlib/qlib_data/cn_data_all", region="cn")
    label = ["Ref($close, -21)/Ref($close, -1) - 1"]
    learn_proc = [
        {"class": "DropnaLabel", "module_path": "qlib.data.dataset.processor"},
        {"class": "CSZScoreNorm", "module_path": "qlib.data.dataset.processor",
         "kwargs": {"fields_group": "label"}},
    ]
    insts = []
    with open("/root/.qlib/qlib_data/cn_data_all/instruments/all.txt") as f:
        for ln in f:
            if ln.strip():
                insts.append(ln.split()[0])
    parts = []
    t0 = time.time()
    for i in range(0, len(insts), 500):
        ch = insts[i:i + 500]
        h = Alpha158(instruments=ch, start_time="2025-01-01", end_time="2026-08-14",
                     fit_start_time="2021-06-01", fit_end_time="2024-06-30",
                     learn_processors=learn_proc, label=label)
        data = h.fetch(col_set=["label"], data_key=DataHandlerLP.DK_L)
        parts.append(data["label"])
        if (i // 500) % 3 == 0:
            print(f"{i + len(ch)}/{len(insts)} ({time.time()-t0:.0f}s)", flush=True)
    lab = pd.concat(parts)
    out = Path("/root/quant/results/remote/e17r_label20d.pkl")
    out.parent.mkdir(parents=True, exist_ok=True)
    lab.to_pickle(out)
    print(f"20d label saved {out} {lab.shape}")


if __name__ == "__main__":
    main()
