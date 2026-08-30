"""qbt status: 查看各阶段产物与最新结果"""
import typer
from rich.console import Console
from rich.table import Table

from qbt.state import read_state

console = Console()


def status() -> None:
    """查看各阶段产物与最新结果摘要"""
    st = read_state()
    if not st:
        typer.echo("还没有任何运行记录。先跑 qbt init 或 qbt data fetch --pool hs300")
        return

    table = Table(title="qbt 运行状态")
    table.add_column("阶段", style="cyan", no_wrap=True)
    table.add_column("状态", style="bold")
    table.add_column("关键信息", style="green")

    rows = [
        ("数据", st.get("data_status", "—"), st.get("data_info", "")),
        ("训练", st.get("train_status", "—"), st.get("train_info", "")),
        ("计划", st.get("plan_status", "—"), st.get("plan_info", "")),
        ("回测", st.get("backtest_status", "—"), st.get("backtest_info", "")),
        ("报告", st.get("report_status", "—"), st.get("report_info", "")),
    ]
    for name, s, info in rows:
        mark = "✅" if s == "done" else ("⏳" if s == "running" else "⬜")
        table.add_row(name, f"{mark} {s}", info)

    console.print(table)
    if st.get("_updated"):
        typer.echo(f"最近更新：{st['_updated']}")
