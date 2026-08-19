"""qbt plan: pred.pkl → 月度调仓计划 CSV

修复（OPTIMIZATION.md）：
- P0-1: 计划日期对齐每月最后一个交易日（不再用日历月末标签）
- P0-2: 按 train 写入 state 的 lineage（run_id/pool）定位 pred，pool 不匹配显式报错
- P1-3: rank-buffer 换手口径（入池 top-K，跌出 top-(K+N) 才卖，对齐 qlib 层）
- P1-4: T 日收盘信号 → T+1 交易日执行
"""
import pickle
from pathlib import Path

import typer

from qbt.config import load_config, project_root
from qbt.planlib import build_plan_with_buffer, load_calendar, resolve_plan_pred
from qbt.pools import get_pool
from qbt.state import read_state, write_state


def _calendar_from_cfg(cfg: dict):
    """优先用 qlib 交易日历（calendars/day.txt），缺失则回退到 pred 自身索引"""
    try:
        qlib_dir = Path(cfg["train"]["qlib_dir"]).expanduser()
        return load_calendar(qlib_dir / "calendars" / "day.txt")
    except (KeyError, FileNotFoundError):
        return None


def plan(
    topk: int = typer.Option(None, help="每月持仓数量（默认取配置 50）"),
    freq: str = typer.Option(None, help="调仓频率（默认 ME=月末）"),
    buffer: int = typer.Option(None, help="rank-buffer 大小 N（P1-3，默认取配置 10；0=关闭）"),
    out: str = typer.Option(None, help="输出 CSV 路径"),
    pool: str = typer.Option("hs300", help="股票池: hs300 / zz500（决定默认输出文件）"),
) -> None:
    """从训练 lineage 对应的 pred.pkl 生成月度调仓计划（rank-buffer + T+1）"""
    import pandas as pd

    cfg = load_config()
    root = project_root()
    topk = topk or cfg["plan"]["topk"]
    freq = freq or cfg["plan"]["freq"]
    buffer = buffer if buffer is not None else cfg["plan"].get("rank_buffer", 10)
    # C1: 默认输出路径来自股票池注册表
    try:
        pool_cfg = get_pool(pool)
    except ValueError as e:
        typer.secho(f"❌ {e}", fg="red")
        raise typer.Exit(1)
    if out is None:
        out = str(root / pool_cfg["plan_out"])

    # P0-2: 必须用 train 记录的 run_id 定位 pred；pool 不匹配直接报错
    try:
        pred_path = resolve_plan_pred(read_state(), pool, root)
    except (FileNotFoundError, ValueError) as e:
        typer.secho(f"❌ {e}", fg="red")
        raise typer.Exit(1)
    run_id = pred_path.parent.parent.name
    typer.echo(f"使用 pred: {pred_path}")
    pred = pickle.load(open(pred_path, "rb"))
    wide = pred["score"].unstack("instrument")
    rows = build_plan_with_buffer(wide, topk=topk, buffer_n=buffer, freq=freq,
                                   execute_next_day=True, calendar=_calendar_from_cfg(cfg))
    if not rows:
        typer.secho("❌ 未生成任何调仓计划：宽表为空或信号日无有效分数", fg="red")
        raise typer.Exit(1)
    pd.DataFrame(rows).to_csv(out, index=False, header=False)
    n_months = len(rows)
    typer.secho(f"✅ 调仓计划: {n_months} 个月 × top{topk} → {out}", fg="green")
    typer.echo(f"   日期区间: {rows[0][0]} ~ {rows[-1][0]}（月末交易日对齐 + T+1 执行）")
    typer.echo(f"   rank-buffer: N={buffer}（P1-3：入池 top{topk}，跌出 top{topk + buffer} 才卖）")
    write_state(
        plan_status="done",
        plan_info=f"{n_months} 个月 × top{topk}（N={buffer}）（{Path(out).name}）",
        plan_run={"pool": pool, "topk": topk, "freq": freq, "buffer": buffer,
                  "pred_run_id": run_id, "out": out},
    )
