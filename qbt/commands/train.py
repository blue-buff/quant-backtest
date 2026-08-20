"""qbt train: qlib 训练 + 简化规则回测"""
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import typer
import yaml

from qbt.config import load_config, project_root, resolve
from qbt.pools import get_pool
from qbt.state import log_dir, write_state

MODELS = {
    "lgb": {"class": "LGBModel", "module_path": "qlib.contrib.model.gbdt"},
    "linear": {"class": "LinearModel", "module_path": "qlib.contrib.model.linear",
               "kwargs": {"fit_intercept": True}},
}

# mlruns 中持久化的指标文件名 → state 字段
METRIC_KEYS = {
    "ic": "IC",
    "icir": "ICIR",
    "rank_ic": "Rank IC",
    "rank_icir": "Rank ICIR",
    "excess_ann": "1day.excess_return_with_cost.annualized_return",
    "excess_ir": "1day.excess_return_with_cost.information_ratio",
    "excess_mdd": "1day.excess_return_with_cost.max_drawdown",
}

_MODEL_LABEL = {"LGBModel": "lgb", "LinearModel": "linear"}


def _model_kwargs(model: str) -> dict:
    return dict(MODELS[model])


def _newest_pred_path(root: Path) -> Path | None:
    """当前 qrun 刚写完的 pred.pkl（mlruns 内最新，用于提取 run_id）"""
    cands = sorted((root / "qlib_examples" / "mlruns").glob("*/*/artifacts/pred.pkl"),
                   key=lambda p: p.stat().st_mtime)
    return cands[-1] if cands else None


def read_run_metrics(run_dir: Path) -> dict[str, float]:
    """读 mlruns/<exp>/<run>/metrics/* 文件（mlflow 落盘格式: 'timestamp value'）。

    替代正则解析 qrun stdout（OPTIMIZATION.md P1-5）：qlib 升级导致输出格式
    变化时指标仍可稳定读取；文件缺失时调用方回退到 stdout 解析。
    """
    metrics: dict[str, float] = {}
    mdir = run_dir / "metrics"
    if not mdir.is_dir():
        return metrics
    for f in sorted(mdir.iterdir()):
        try:
            lines = [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if lines:
                # mlflow 落盘格式: "<timestamp> <value> <step>"，取 value 列
                parts = lines[-1].split()
                metrics[f.name] = float(parts[1] if len(parts) >= 3 else parts[-1])
        except (OSError, ValueError):
            continue
    return metrics


def read_run_param(run_dir: Path, name: str) -> str | None:
    p = run_dir / "params" / name
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


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
    try:
        pool_cfg = get_pool(pool)
    except ValueError as e:
        typer.secho(f"❌ {e}", fg="red")
        raise typer.Exit(1)
    cfg = load_config()
    root = project_root()

    # 1. 选择/生成 yaml（C1: 模板来自股票池注册表）
    if yaml_path is None:
        yaml_path = pool_cfg["yaml"]
    yaml_path = (root / yaml_path).resolve()
    if not yaml_path.exists():
        typer.secho(f"yaml 不存在: {yaml_path}", fg="red")
        raise typer.Exit(1)

    with open(yaml_path, encoding="utf-8") as f:
        c = yaml.safe_load(f)

    # 1.5 数据目录统一：provider_uri 始终取配置的 qlib_dir（防硬编码偏差）
    qlib_dir = resolve(cfg["train"]["qlib_dir"])
    c.setdefault("qlib_init", {})["provider_uri"] = qlib_dir
    typer.echo(f"qlib 数据目录: {qlib_dir}")

    # 2. 模型配置：⚠️ qlib 实际训练用 task.model（顶层 model 不生效）
    if model == "linear" or c.get("task", {}).get("model", {}).get("class") != MODELS[model]["class"]:
        c["model"] = dict(_model_kwargs(model))
        c.setdefault("task", {})["model"] = dict(_model_kwargs(model))
    # 审计修复 D: 每次训练都用独立实验名（避免 run 混入 Default 实验，
    # 实验名与模型/池/时间绑定，mlflow 组织规范化）
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
    r = subprocess.run([sys.executable, "-m", "qlib.cli.run", str(work_yaml)],
                       capture_output=True, text=True, env=env, cwd=str(root / "qlib_examples"))
    logf.write_text(r.stdout + r.stderr, encoding="utf-8")
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).splitlines()[-8:])
        typer.secho(f"训练失败:\n{tail}", fg="red")
        write_state(train_status="failed", train_info=tail[:200])
        raise typer.Exit(1)

    # 4. 解析结果：优先读 mlruns metrics/ 文件（P1-5），缺失才回退 stdout 正则
    pred_path = _newest_pred_path(root)
    run_dir = pred_path.parent.parent if pred_path else None
    metrics = read_run_metrics(run_dir) if run_dir else {}
    if metrics:
        ic = metrics.get(METRIC_KEYS["ic"])
        icir = metrics.get(METRIC_KEYS["icir"])
        exc_ann = metrics.get(METRIC_KEYS["excess_ann"])
        exc_ir = metrics.get(METRIC_KEYS["excess_ir"])
        exc_mdd = metrics.get(METRIC_KEYS["excess_mdd"])
        model_class = read_run_param(run_dir, "model.class")
        model_used = _MODEL_LABEL.get(model_class or "", "?")
    else:
        txt = r.stdout
        ic = _grep_float(txt, r"'IC': np\.float64\(([\d.eE+-]+)\)")
        icir = _grep_float(txt, r"'ICIR': np\.float64\(([\d.eE+-]+)\)")
        exc_ann = _grep_float(txt, r"excess return with cost.*?annualized_return\s+([\d.-]+)", dotall=True)
        exc_ir = _grep_float(txt, r"excess return with cost.*?information_ratio\s+([\d.-]+)", dotall=True)
        exc_mdd = _grep_float(txt, r"excess return with cost.*?max_drawdown\s+([\d.-]+)", dotall=True)
        model_used = "linear" if "LinearModel" in txt else ("lgb" if "LGBModel" in txt else "?")

    info = (f"{model_used} IC={ic or '?'} ICIR={icir or '?'} | "
            f"超额年化={exc_ann * 100 if exc_ann is not None else '?'}% "
            f"IR={exc_ir or '?'} MDD={exc_mdd or '?'}")
    typer.secho(f"✅ 训练完成（实际模型: {model_used}）", fg="green")
    typer.echo(f"   IC={ic}  ICIR={icir}")
    typer.echo(f"   超额(含成本) 年化={exc_ann * 100 if exc_ann is not None else '?'}%  "
               f"IR={exc_ir}  MDD={exc_mdd}")
    if run_dir:
        typer.echo(f"   lineage: run_id={run_dir.name}  pred={pred_path}")
        write_state(train_status="done", train_info=info,
                    train_metrics={"ic": ic, "icir": icir, "excess_ann": exc_ann,
                                   "excess_ir": exc_ir, "excess_mdd": exc_mdd,
                                   "model": model_used, "pool": pool, "tag": tag},
                    train_run={"run_id": run_dir.name, "pool": pool,
                               "model": model_used, "pred_path": str(pred_path)})
    else:
        write_state(train_status="done", train_info=info + "（未找到 pred.pkl，无 lineage）",
                    train_metrics={"ic": ic, "icir": icir, "excess_ann": exc_ann,
                                   "excess_ir": exc_ir, "excess_mdd": exc_mdd,
                                   "model": model_used, "pool": pool, "tag": tag})


def _grep_float(txt: str, pattern: str, dotall: bool = False) -> float | None:
    m = re.search(pattern, txt, re.DOTALL if dotall else 0)
    return float(m.group(1)) if m else None
