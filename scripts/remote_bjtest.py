"""远程 BJ 股票加载测试：验证 BJ 稀疏数据是否导致崩溃。"""
import traceback
import sys
import time
from pathlib import Path

BASE = Path("C:/Users/song/qbt_work")
URI = str(BASE / "qlib_data/cn_data_all")


def main():
    import qlib
    qlib.init(provider_uri=URI, region="cn", kernels=1)
    from qlib.contrib.data.handler import Alpha158
    from qlib.data.dataset.handler import DataHandlerLP
    insts = []
    with open(BASE / "qlib_data/cn_data_all/instruments/all.txt") as f:
        for ln in f:
            if ln.strip():
                insts.append(ln.split()[0])
    bj = [s for s in insts if s.startswith("BJ")][:100]
    print(f"BJ 测试 {len(bj)} 只", flush=True)
    label = ["Ref($close, -11)/Ref($close, -1) - 1"]
    learn_proc = [
        {"class": "DropnaLabel", "module_path": "qlib.data.dataset.processor"},
        {"class": "CSZScoreNorm", "module_path": "qlib.data.dataset.processor",
         "kwargs": {"fields_group": "label"}},
    ]
    t0 = time.time()
    h = Alpha158(instruments=bj, start_time="2021-06-01", end_time="2024-12-31",
                 fit_start_time="2021-06-01", fit_end_time="2024-06-30",
                 learn_processors=learn_proc, label=label)
    data = h.fetch(col_set=["feature", "label"], data_key=DataHandlerLP.DK_L)
    print(f"BJ fetch OK {time.time()-t0:.0f}s, {data['feature'].shape}", flush=True)
    # 再测沪深股票对照
    sh_ = [s for s in insts if s.startswith("SH")][:100]
    h2 = Alpha158(instruments=sh_, start_time="2021-06-01", end_time="2024-12-31",
                  fit_start_time="2021-06-01", fit_end_time="2024-06-30",
                  learn_processors=learn_proc, label=label)
    data2 = h2.fetch(col_set=["feature", "label"], data_key=DataHandlerLP.DK_L)
    print(f"SH fetch OK {data2['feature'].shape}", flush=True)
    print("BJ_TEST_DONE", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
