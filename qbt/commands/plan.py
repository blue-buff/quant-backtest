"""qbt plan: pred.pkl → 月度调仓计划 CSV"""
import pickle
from datetime import datetime
from pathlib import Path

import typer

from qbt.config import load_config, project_root
from qbt.state import write_state


def _latest_pred(root: Path) -> Path:
    cands = sorted((root / "qlib_examples" / "mlruns").glob("*/*/artifacts/pred.pkl"),
                   key=lambda p: p.stat().st_mtime)
    if not cands:
        raise FileNotFoundError("未找到 pred.pkl，先跑 qbt train")
    return cands[-1]


def plan(
    topk: int = typer.Option(None, help="每月持仓数量（默认取配置 50）"),
    freq: str = typer.Option(None, help="调仓频率（默认 ME=月末）"),
    out: str = typer.Option(None, help="输出 CSV 路径"),
    pool: str = typer.Option("hs300", help="股票池: hs300 / zz500（决定默认输出文件）"),
) -> None:
    """从最新 pred.pkl 生成每月 top-K 调仓计划"""
    import pandas as pd

    cfg = load_config()
    root = project_root()
    topk = topk or cfg["plan"]["topk"]
    freq = freq or cfg["plan"]["freq"]
    if out is None:
        out = str(root / cfg["plan"]["out"]) if pool == "hs300" \
            else str(root / "qlib_examples" / "rebalance_plan_zz500.csv")

    pred_path = _latest_pred(root)
    typer.echo(f"使用 pred: {pred_path}")
    pred = pickle.load(open(pred_path, "rb"))
    wide = pred["score"].unstack("instrument")
    monthly = wide.resample(freq).last()
    typer.echo(f"调仓月份数: {len(monthly)}")

    def to_rqalpha(symbol: str) -> str:
        if symbol.startswith("SH"):
            return symbol[2:] + ".XSHG"
        if symbol.startswith("SZ"):
            return symbol[2:] + ".XSHE"
        return symbol

    plan_rows = []
    for dt, row in monthly.iterrows():
        top = row.dropna().sort_values(ascending=False).head(topk).index.tolist()
        plan_rows.append([dt.strftime("%Y-%m-%d")] + [to_rqalpha(s) for s in top])
    pd.DataFrame(plan_rows).to_csv(out, index=False, header=False)
    n_months = len(plan_rows)
    typer.secho(f"✅ 调仓计划: {n_months} 个月 × {topk} 只 → {out}", fg="green")
    write_state(plan_status="done", plan_info=f"{n_months} 个月 × top{topk}（{Path(out).name}）")
