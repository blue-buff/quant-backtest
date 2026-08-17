"""从 qlib pred.pkl 提取每月 top50 调仓计划，转为 rqalpha 可读格式"""
import pickle, glob, os
import pandas as pd

files = sorted(glob.glob("/root/quant/qlib_examples/mlruns/*/*/artifacts/pred.pkl"),
               key=os.path.getmtime)
pred_path = files[-1]
print("使用:", pred_path)
pred = pickle.load(open(pred_path, "rb"))

# 长表 -> 宽表: index=日期, columns=股票, values=score
wide = pred["score"].unstack("instrument")
print("宽表:", wide.shape, "| 日期:", wide.index[0], "~", wide.index[-1])

# 每月最后一个交易日取 top50
monthly = wide.resample("ME").last()
print("调仓月份数:", len(monthly))

def to_rqalpha(symbol):
    if symbol.startswith("SH"):
        return symbol[2:] + ".XSHG"
    if symbol.startswith("SZ"):
        return symbol[2:] + ".XSHE"
    return symbol

plan_rows = []
for dt, row in monthly.iterrows():
    top50 = row.dropna().sort_values(ascending=False).head(50).index.tolist()
    plan_rows.append([dt.strftime("%Y-%m-%d")] + [to_rqalpha(s) for s in top50])

plan = pd.DataFrame(plan_rows)
plan.to_csv("/root/quant/qlib_examples/rebalance_plan_zz500.csv", index=False, header=False)
print(f"调仓计划: {len(plan)} 个月, 每月 {len(plan.columns) - 1} 只")
print(plan.iloc[0].head().to_string())
