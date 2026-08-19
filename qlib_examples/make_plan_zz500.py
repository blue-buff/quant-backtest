"""从 qlib pred.pkl 提取每月 top50 调仓计划（中证500），转为 rqalpha 可读格式

修复（OPTIMIZATION.md）：
- P0-1: 调仓日期对齐每月最后一个真实交易日
- P1-4: T 日收盘信号 → T+1 交易日执行（默认开启；--same-day 可关闭）
"""
import argparse
import glob
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import MLRUNS_DIR, PLAN_FILE_ZZ500  # noqa: E402
from qbt.planlib import build_plan  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--topk", type=int, default=50, help="每月持仓数量")
    ap.add_argument("--out", default=PLAN_FILE_ZZ500, help="输出 CSV 路径")
    ap.add_argument("--pred", default=None, help="指定 pred.pkl（默认取 mlruns 最新）")
    ap.add_argument("--same-day", action="store_true",
                    help="关闭 T+1 执行（不推荐，OPTIMIZATION.md P1-4）")
    args = ap.parse_args()

    pred_path = args.pred
    if not pred_path:
        files = sorted(glob.glob(f"{MLRUNS_DIR}/*/*/artifacts/pred.pkl"),
                       key=os.path.getmtime)
        if not files:
            sys.exit("未找到 pred.pkl，先运行 qrun")
        pred_path = files[-1]
    print("使用:", pred_path)
    pred = pickle.load(open(pred_path, "rb"))

    # 长表 -> 宽表: index=日期, columns=股票, values=score
    wide = pred["score"].unstack("instrument")
    print("宽表:", wide.shape, "| 日期:", wide.index[0], "~", wide.index[-1])

    rows = build_plan(wide, topk=args.topk, execute_next_day=not args.same_day)
    if not rows:
        sys.exit("未生成任何调仓计划：宽表为空或信号日无有效分数")

    import pandas as pd

    pd.DataFrame(rows).to_csv(args.out, index=False, header=False)
    print(f"调仓计划: {len(rows)} 个月, 每月 {len(rows[0]) - 1} 只")
    print("首行:", rows[0][0], rows[0][1:6])


if __name__ == "__main__":
    main()
