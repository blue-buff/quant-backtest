"""qbt backtest: rqalpha 真实规则回测（T+1/涨跌停/印花税/100股整数倍）"""
import os
import warnings
from datetime import datetime

import typer

from qbt.config import load_config, project_root, resolve
from qbt.pools import get_pool
from qbt.state import write_state

# P2-6: 不再全局屏蔽警告；只忽略 rqalpha analyser 已知的 FutureWarning，
# 其余警告保持可见，避免掩盖潜在问题
warnings.filterwarnings("ignore", category=FutureWarning,
                        message=".*Downcasting object dtype arrays.*")


def backtest(
    capital: float = typer.Option(None, help="初始资金（默认取配置 1000000）"),
    start: str = typer.Option(None, help="回测开始日期（默认取配置）"),
    end: str = typer.Option(None, help="回测结束日期（默认取配置）"),
    bundle: str = typer.Option(None, help="rqalpha 行情库路径"),
    pool: str = typer.Option("hs300", help="股票池: hs300 / zz500（决定策略与计划文件）"),
) -> None:
    """按调仓计划跑真实规则回测（先跑 qbt train + qbt plan）"""
    cfg = load_config()
    root = project_root()
    capital = capital or cfg["backtest"]["capital"]
    start = start or cfg["backtest"]["start"]
    end = end or cfg["backtest"]["end"]
    bundle = bundle or cfg.get("backtest", {}).get("bundle_path", "/root/.rqalpha/bundle")

    # C1: 计划与策略路径来自股票池注册表
    try:
        pool_cfg = get_pool(pool)
    except ValueError as e:
        typer.secho(f"❌ {e}", fg="red")
        raise typer.Exit(1)
    plan_name = pool_cfg["plan_out"]
    strategy_name = pool_cfg["strategy"]
    plan_file = root / plan_name
    if not plan_file.exists():
        typer.secho(f"调仓计划不存在: {plan_file}，先跑 qbt plan --pool {pool}", fg="red")
        raise typer.Exit(1)
    strategy = root / strategy_name
    if not strategy.exists():
        typer.secho(f"策略文件不存在: {strategy}", fg="red")
        raise typer.Exit(1)

    try:
        from rqalpha import run
    except ImportError:
        typer.secho("缺少 rqalpha，请先 pip install rqalpha 并配置行情库", fg="red")
        raise typer.Exit(1)

    # rqalpha 用 exec 加载策略文件，参数经环境变量传入
    os.environ["QBT_PLAN_FILE"] = str(plan_file)
    os.environ["QBT_SLIPPAGE"] = str(cfg.get("backtest", {}).get("slippage", 0.0))
    os.environ["QBT_PARTICIPATION"] = str(cfg.get("backtest", {}).get("participation", 0.05))
    os.environ["QBT_RETRY_DAYS"] = str(cfg.get("backtest", {}).get("retry_days", 2))

    config = {
        "base": {
            "data_bundle_path": resolve(bundle),
            "start_date": start,
            "end_date": end,
            "accounts": {"stock": capital},
            "frequency": "1d",
            "run_type": "b",
            "strategy_file": str(strategy),
            "capital_gain_tax_rate": 0,  # A股无资本利得税，显式配置消除 rqalpha 警告
        },
        "extra": {"log_level": "error"},
        "mod": {"sys_progress": {"enabled": False}, "sys_analyser": {"enabled": True}},
    }
    typer.echo(f"真实规则回测: {start} ~ {end}，本金 {capital:,.0f}，计划 {plan_file.name}")
    typer.echo(f"   执行参数: 滑点 {os.environ['QBT_SLIPPAGE']} 参与率 {os.environ['QBT_PARTICIPATION']} "
               f"重试 {os.environ['QBT_RETRY_DAYS']} 天（A3/P2-4）")
    write_state(backtest_status="running", backtest_info=f"{start}~{end} 回测中")
    results = run(config)
    analyser = results["sys_analyser"]
    s = analyser["summary"]
    n_trades = len(analyser["trades"])

    typer.secho("✅ 回测完成", fg="green")
    typer.echo(f"   总收益   {s['total_returns'] * 100:+.2f}%")
    typer.echo(f"   年化收益 {s['annualized_returns'] * 100:+.2f}%")
    typer.echo(f"   最大回撤 {s['max_drawdown'] * 100:.2f}%")
    typer.echo(f"   Sharpe   {s['sharpe']:.2f}")
    typer.echo(f"   胜率     {s['win_rate'] * 100:.1f}%   换手 {s['turnover']:.1f} 倍")
    typer.echo(f"   交易     {n_trades} 笔   期末资产 {s['total_value']:,.0f}")

    info = (f"{s['total_returns']*100:+.2f}% 总收益 | 年化 {s['annualized_returns']*100:+.2f}% | "
            f"Sharpe {s['sharpe']:.2f} | MDD {s['max_drawdown']*100:.2f}% | {n_trades} 笔")
    write_state(backtest_status="done", backtest_info=info,
                backtest_metrics={
                    "total_returns": round(float(s["total_returns"]), 5),
                    "annualized_returns": round(float(s["annualized_returns"]), 5),
                    "max_drawdown": round(float(s["max_drawdown"]), 5),
                    "sharpe": round(float(s["sharpe"]), 4),
                    "win_rate": round(float(s["win_rate"]), 4),
                    "turnover": round(float(s["turnover"]), 3),
                    "trades": n_trades,
                    "capital": capital,
                })
