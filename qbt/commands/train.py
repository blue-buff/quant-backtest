"""qbt train: qlib 训练 + 简化规则回测"""
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import typer
import yaml

from qbt.config import load_config, project_root
from qbt.state import log_dir, write_state

MODELS = {
    "lgb": {"class": "LGBModel", "module_path": "qlib.contrib.model.gbdt"},
    "linear": {"class": "LinearModel", "module_path": "qlib.contrib.model.linear",
               "kwargs": {"fit_intercept": True}},
}


def _model_kwargs(model: str) -> dict:
    return dict(MODELS[model])


def train(
    model: str = typer.Option("lgb", help="模型: lgb / linear"),
    pool: str = typer.Option("hs300", help="股票池: hs300 / zz500"),
    yaml_path: str = typer.Option(None, help="yaml 模板路径（默认取配置 train.yaml）"),
    tag: str = typer.Option(None, help="实验标签（用于结果记录）"),
) -> None:
    """训练 + 简化规则回测（qlib qrun），解析 IC 与超额指标"""
    if model not in MODELS:
        typer.secho(f"未知模型 {model}，可选: {', '.join(MODELS)}", fg="red")
        raise typer.Exit(1)
    cfg = load_config()
    root = project_root()

    # 1. 选择/生成 yaml
    if yaml_path is None:
        yaml_path = cfg["train"]["yaml"]
        if pool == "zz500":
            yaml_path = "qlib_examples/lightgbm_alpha158_zz500.yaml"
    yaml_path = (root / yaml_path).resolve()
    if not yaml_path.exists():
        typer.secho(f"yaml 不存在: {yaml_path}", fg="red")
        raise typer.Exit(1)

    with open(yaml_path, encoding="utf-8") as f:
        c = yaml.safe_load(f)

    # 2. 模型配置：⚠️ qlib 实际训练用 task.model（顶层 model 不生效）
    if model == "linear" or c.get("task", {}).get("model", {}).get("class") != MODELS[model]["class"]:
        c["model"] = dict(_model_kwargs(model))
        c.setdefault("task", {})["model"] = dict(_model_kwargs(model))
        # 实验名隔离，避免 mlflow 复用旧实验产物
        exp_name = f"{'linear' if model == 'linear' else 'workflow'}_{pool}_{datetime.now():%m%d%H%M}"
        c["experiment_name"] = exp_name

    work_yaml = root / "results" / f"train_{model}_{pool}_{datetime.now():%Y%m%d_%H%M%S}.yaml"
    work_yaml.parent.mkdir(parents=True, exist_ok=True)
    with open(work_yaml, "w", encoding="utf-8") as f:
        yaml.safe_dump(c, f, sort_keys=False, allow_unicode=True)

    # 3. qrun
    logf = log_dir() / f"train_{model}_{pool}_{datetime.now():%Y%m%d_%H%M%S}.log"
    env = dict(os.environ, MLFLOW_ALLOW_FILE_STORE="true")
    typer.echo(f"训练开始: model={model} pool={pool}（{work_yaml.name}）")
    typer.echo(f"日志: {logf}")
    write_state(train_status="running", train_info=f"{model}/{pool} 训练中")
    r = subprocess.run([sys.executable, "-m", "qlib.qrun", str(work_yaml)],
                       capture_output=True, text=True, env=env, cwd=str(root / "qlib_examples"))
    logf.write_text(r.stdout + r.stderr, encoding="utf-8")
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).splitlines()[-8:])
        typer.secho(f"训练失败:\n{tail}", fg="red")
        write_state(train_status="failed", train_info=tail[:200])
        raise typer.Exit(1)

    # 4. 解析结果
    txt = r.stdout
    ic = _grep_float(txt, r"'IC': np\.float64\(([\d.eE+-]+)\)")
    icir = _grep_float(txt, r"'ICIR': np\.float64\(([\d.eE+-]+)\)")
    exc_ann = _grep_float(txt, r"excess return with cost.*?annualized_return\s+([\d.-]+)", dotall=True)
    exc_ir = _grep_float(txt, r"excess return with cost.*?information_ratio\s+([\d.-]+)", dotall=True)
    exc_mdd = _grep_float(txt, r"excess return with cost.*?max_drawdown\s+([\d.-]+)", dotall=True)
    model_used = "linear" if "LinearModel" in txt else ("lgb" if "LGBModel" in txt else "?")

    info = (f"{model_used} IC={ic or '?'} ICIR={icir or '?'} | "
            f"超额年化={exc_ann or '?'}% IR={exc_ir or '?'} MDD={exc_mdd or '?'}")
    typer.secho(f"✅ 训练完成（实际模型: {model_used}）", fg="green")
    typer.echo(f"   IC={ic}  ICIR={icir}")
    typer.echo(f"   超额(含成本) 年化={exc_ann}  IR={exc_ir}  MDD={exc_mdd}")
    write_state(train_status="done", train_info=info,
                train_metrics={"ic": ic, "icir": icir, "excess_ann": exc_ann,
                               "excess_ir": exc_ir, "excess_mdd": exc_mdd,
                               "model": model_used, "pool": pool, "tag": tag})


def _grep_float(txt: str, pattern: str, dotall: bool = False) -> float | None:
    m = re.search(pattern, txt, re.DOTALL if dotall else 0)
    return float(m.group(1)) if m else None
