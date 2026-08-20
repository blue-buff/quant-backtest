"""特征精选（P1-8）：从已训练模型取 top-N 特征重要性，生成带 FilterCol 的 yaml。

用法:
  python scripts/make_topk_yaml.py --base qlib_examples/experiments/e03_off_10d_zz500.yaml \
      --model-pkl qlib_examples/mlruns/<exp>/<run>/artifacts/model.pkl --topk 50 --name e09_top50_10d_zz500

产出: qlib_examples/experiments/e09_top50_10d_zz500.yaml（learn/infer 处理器加 FilterCol）
"""
import argparse
import copy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "qlib_examples" / "experiments"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True)
    ap.add_argument("--model-pkl", required=True)
    ap.add_argument("--topk", type=int, default=50)
    ap.add_argument("--name", required=True)
    args = ap.parse_args()
    import pandas as pd
    model = pd.read_pickle(ROOT / args.model_pkl)
    imp = model.get_feature_importance()
    imp = imp.sort_values(ascending=False)
    top = list(imp.index[: args.topk])
    print(f"特征重要性: 总数 {len(imp)}，保留 top{args.topk}")
    print("前 12:", top[:12])

    cfg = yaml.safe_load((ROOT / args.base).read_text(encoding="utf-8"))
    hk = cfg["task"]["dataset"]["kwargs"]["handler"]["kwargs"]
    filt = {"class": "FilterCol", "module_path": "qlib.data.dataset.processor",
            "kwargs": {"fields_group": "feature", "col_list": top}}
    # learn/infer 处理器都加 FilterCol（默认处理器缺失时用官方默认）
    hk["learn_processors"] = [
        {"class": "DropnaLabel", "module_path": "qlib.data.dataset.processor"},
        {"class": "CSZScoreNorm", "module_path": "qlib.data.dataset.processor",
         "kwargs": {"fields_group": "label"}},
        filt,
    ]
    hk["infer_processors"] = [
        {"class": "ProcessInf", "module_path": "qlib.data.dataset.processor"},
        {"class": "ZScoreNorm", "module_path": "qlib.data.dataset.processor"},
        {"class": "Fillna", "module_path": "qlib.data.dataset.processor"},
        filt,
    ]
    out = OUT / f"{args.name}.yaml"
    out.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print("written", out)


if __name__ == "__main__":
    main()
