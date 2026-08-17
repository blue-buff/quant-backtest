"""qbt 主入口"""
import subprocess
import sys

import typer

from qbt import __version__
from qbt.commands import backtest, data, init, plan, report, status, train
from qbt.config import project_root

app = typer.Typer(
    help="A股/港股量化回测流水线：数据导出 → qlib 训练 → 调仓计划 → 真实规则回测 → 报告",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(data.app, name="data", help="数据管道：导出 / 校验 / 转 qlib 格式")
app.command()(train.train)
app.command()(plan.plan)
app.command()(backtest.backtest)
app.command()(report.report)
app.command()(status.status)
app.command()(init.init)


@app.command()
def all(
    pool: str = typer.Option("hs300", help="股票池: hs300 / zz500"),
    model: str = typer.Option("lgb", help="模型: lgb / linear"),
    capital: float = typer.Option(None, help="回测本金"),
) -> None:
    """一键全链路: 数据导出 → dump → 训练 → 调仓计划 → 真实规则回测 → 报告"""
    steps = [
        ["data", "fetch", "--pool", pool],
        ["data", "dump", "--pool", pool],
        ["train", "--model", model, "--pool", pool],
        ["plan", "--pool", pool],
        ["backtest", "--pool", pool] + (["--capital", str(capital)] if capital else []),
        ["report"],
    ]
    for i, args in enumerate(steps, 1):
        typer.secho(f"\n[{i}/{len(steps)}] qbt {' '.join(args)}", fg="cyan", bold=True)
        r = subprocess.run([sys.executable, "-m", "qbt", *args], cwd=str(project_root()))
        if r.returncode != 0:
            typer.secho(f"❌ 步骤失败: qbt {' '.join(args)}", fg="red")
            raise typer.Exit(1)
    typer.secho("\n🎉 全链路完成！查看 qbt status 或 results/report.html", fg="green")


@app.command()
def version():
    """显示版本"""
    typer.echo(f"qbt {__version__}")


if __name__ == "__main__":
    app()
