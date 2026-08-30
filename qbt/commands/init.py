"""qbt init: 生成 qbt.yaml 配置与 results 目录"""
from pathlib import Path

import typer

from qbt.config import DEFAULT_CONFIG, project_root

import yaml


def init(
    force: bool = typer.Option(False, "--force", "-f", help="覆盖已有 qbt.yaml"),
) -> None:
    """初始化项目配置（qbt.yaml + results/）"""
    root = project_root()
    cfg_path = root / "qbt.yaml"
    if cfg_path.exists() and not force:
        typer.secho(f"qbt.yaml 已存在（用 --force 覆盖）：{cfg_path}", fg="yellow")
        raise typer.Exit(1)
    cfg_path.write_text(
        yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    (root / "results" / "logs").mkdir(parents=True, exist_ok=True)
    typer.secho(f"✅ 已生成 {cfg_path}", fg="green")
    typer.echo("下一步：qbt data fetch --pool hs300")
