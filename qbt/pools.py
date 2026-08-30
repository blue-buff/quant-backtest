"""股票池注册表（OPTIMIZATION.md C1）。

所有池相关配置收敛到此处：新增股票池只需加一段配置，
无需再改 data/train/plan/backtest 四个命令的 if pool == "..." 分支。
"""
from __future__ import annotations

DEFAULT_POOLS: dict[str, dict] = {
    "hs300": {
        "label": "沪深300",
        "query_fn": "query_hs300_stocks",
        "index_code": "sh.000300",
        "index_sym": "SH000300",
        "universe": "csi300",
        "yaml": "qlib_examples/lightgbm_alpha158_full.yaml",
        "plan_out": "qlib_examples/rebalance_plan.csv",
        "strategy": "qlib_examples/rq_strategy_qlib.py",
        "data_out": "qlib_data_src",
        "qlib_dir": "~/.qlib/qlib_data/cn_data",
        "benchmark": "SH000300",
    },
    "zz500": {
        "label": "中证500",
        "query_fn": "query_zz500_stocks",
        "index_code": "sh.000905",
        "index_sym": "SH000905",
        "universe": "csi500",
        "yaml": "qlib_examples/lightgbm_alpha158_zz500.yaml",
        "plan_out": "qlib_examples/rebalance_plan_zz500.csv",
        "strategy": "qlib_examples/rq_strategy_qlib_zz500.py",
        "data_out": "qlib_data_src_zz500",
        "qlib_dir": "~/.qlib/qlib_data/cn_data_zz500",
        "benchmark": "SH000905",
    },
}

# 每个池必须具备的键（防止注册表缺字段导致运行期 KeyError）
REQUIRED_KEYS = {
    "label", "query_fn", "index_code", "index_sym", "universe",
    "yaml", "plan_out", "strategy", "data_out", "qlib_dir", "benchmark",
}


def pool_names() -> list[str]:
    return list(DEFAULT_POOLS)


def get_pool(name: str) -> dict:
    """按名字取池配置；未知池显式报错（替代散落的 if/else 与 POOLS 字典）"""
    if name not in DEFAULT_POOLS:
        raise ValueError(f"未知股票池 {name}，可选: {', '.join(pool_names())}")
    pool = DEFAULT_POOLS[name]
    missing = REQUIRED_KEYS - set(pool)
    if missing:
        raise ValueError(f"股票池 {name} 注册表缺字段: {sorted(missing)}")
    return pool
