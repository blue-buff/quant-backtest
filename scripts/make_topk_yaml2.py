"""从数据直接训练 LGBM(MSE) 取特征重要性 → 生成 FilterCol topK yaml（无需 model.pkl）。

用法:
  python scripts/make_topk_yaml2.py --base qlib_examples/experiments/e03_off_10d_zz500.yaml \
      --topk 50 --name e09_top50_10d_zz500
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
    ap.add_argument("--topk", type=int, default=50)
    ap.add_argument("--name", required=True)
    args = ap.parse_args()

    import pandas as pd
    import lightgbm as lgb
    import qlib
    from qlib.data.dataset.handler import DataHandlerLP
    from qlib.contrib.data.handler import Alpha158

    cfg = yaml.safe_load((ROOT / args.base).read_text(encoding="utf-8"))
    qlib.init(provider_uri=cfg["qlib_init"]["provider_uri"], region="cn")
    hc = cfg["task"]["dataset"]["kwargs"]["handler"]["kwargs"]
    handler = Alpha158(
        instruments=hc["instruments"], start_time=hc["start_time"], end_time=hc["end_time"],
        fit_start_time=hc["fit_start_time"], fit_end_time=hc["fit_end_time"],
        learn_processors=[
            {"class": "DropnaLabel", "module_path": "qlib.data.dataset.processor"},
            {"class": "CSZScoreNorm", "module_path": "qlib.data.dataset.processor",
             "kwargs": {"fields_group": "label"}},
        ],
        label=hc.get("label"),
    )
    seg = cfg["task"]["dataset"]["kwargs"]["segments"]
    data = handler.fetch(col_set=["feature", "label"], data_key=DataHandlerLP.DK_L)
    feat = data["feature"]
    lab = data["label"].iloc[:, 0]
    df = feat.join(lab.rename("y"))
    tr = df[(df.index.get_level_values(0) >= seg["train"][0]) & (df.index.get_level_values(0) < seg["train"][1])]
    va = df[(df.index.get_level_values(0) >= seg["valid"][0]) & (df.index.get_level_values(0) < seg["valid"][1])]
    feats = [c for c in df.columns if c != "y"]
    X_tr, y_tr = tr[feats], tr["y"]
    X_va, y_va = va[feats], va["y"]
    d_tr = lgb.Dataset(X_tr, label=y_tr)
    d_va = lgb.Dataset(X_va, label=y_va, reference=d_tr)
    params = {
        "objective": "regression", "metric": "l2",
        "colsample_bytree": 0.8879, "learning_rate": 0.2, "subsample": 0.8789,
        "lambda_l1": 205.6999, "lambda_l2": 580.9768, "max_depth": 8, "num_leaves": 210,
        "num_threads": 20, "verbosity": -1, "seed": 42,
    }
    model = lgb.train(params, d_tr, num_boost_round=1000, valid_sets=[d_va],
                      callbacks=[lgb.early_stopping(50), lgb.log_evaluation(0)])
    imp = pd.Series(model.feature_importance("gain"), index=feats).sort_values(ascending=False)
    top = list(imp.index[: args.topk])
    print(f"特征总数 {len(imp)}，top{args.topk} 重要性占比 {imp.iloc[:args.topk].sum()/imp.sum():.2%}")
    print("前 15:", top[:15])

    base_cfg = yaml.safe_load((ROOT / args.base).read_text(encoding="utf-8"))
    hk = base_cfg["task"]["dataset"]["kwargs"]["handler"]["kwargs"]
    filt = {"class": "FilterCol", "module_path": "qlib.data.dataset.processor",
            "kwargs": {"fields_group": "feature", "col_list": top}}
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
    out.write_text(yaml.safe_dump(base_cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print("written", out)


if __name__ == "__main__":
    main()
