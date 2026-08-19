"""B4: 回测结论的统计严谨性检验（bootstrap 置信区间 + Sharpe 检验）

OPTIMIZATION.md B4：20 个月样本 + 9 组压力测试存在多重检验风险，
单点数字不可信，需 bootstrap 区间。

用法（在装有 qlib 的环境）:
  python scripts/analysis_bootstrap.py \
      --port-pkl qlib_examples/mlruns/<exp>/<run>/artifacts/portfolio_analysis/port_analysis_1day.pkl

纯函数 bootstrap_ci / sharpe_pvalue 不依赖 qlib，可直接测试。
"""
import argparse
import pickle
from pathlib import Path

import numpy as np


def bootstrap_ci(returns, n_boot=10000, seed=42, alpha=0.05):
    """对日收益序列做 bootstrap：返回 (下界, 上界, 重采样均值)。

    returns: 1-D 数组（日收益，如超额收益序列）
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size == 0:
        raise ValueError("returns 为空或全 NaN")
    rng = np.random.default_rng(seed)
    n = r.size
    means = np.empty(n_boot, dtype=float)
    for i in range(n_boot):
        means[i] = rng.choice(r, size=n, replace=True).mean()
    lo = float(np.quantile(means, alpha / 2))
    hi = float(np.quantile(means, 1 - alpha / 2))
    return lo, hi, float(means.mean())


def sharpe_pvalue(returns, n_boot=10000, seed=42):
    """H0: 策略真实均值收益 <= 0 的 bootstrap p 值（越小越可信）"""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size == 0:
        raise ValueError("returns 为空或全 NaN")
    rng = np.random.default_rng(seed)
    n = r.size
    obs = r.mean()
    count = 0
    for _ in range(n_boot):
        if rng.choice(r, size=n, replace=True).mean() <= 0:
            count += 1
    return float(count) / n_boot, float(obs)


def load_excess_returns(port_pkl):
    """从 qlib PortAnaRecord 的 port_analysis_1day.pkl 提取超额收益序列。

    pkl 内为 dict: {key: DataFrame}，超额收益在 'excess_return_with_cost' 的
    'return' 列（若结构不同则打印可用键并回退到 'excess_return_without_cost'）。
    """
    with open(port_pkl, "rb") as f:
        data = pickle.load(f)
    for key in ("excess_return_with_cost", "excess_return_without_cost"):
        if key in data:
            df = data[key]
            col = "return" if "return" in df.columns else df.columns[0]
            return df[col].to_numpy(), key
    raise KeyError(f"pkl 中未找到超额收益序列，可用键: {list(data)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port-pkl", required=True, help="port_analysis_1day.pkl 路径")
    ap.add_argument("--n-boot", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    returns, key = load_excess_returns(args.port_pkl)
    lo, hi, boot_mean = bootstrap_ci(returns, args.n_boot, args.seed)
    p, obs = sharpe_pvalue(returns, args.n_boot, args.seed)
    ann = returns.mean() * 252
    print(f"序列: {key}（{len(returns)} 个交易日）")
    print(f"样本日收益均值: {returns.mean():.5f}（年化 {ann * 100:.2f}%）")
    print(f"bootstrap 均值 95% CI: [{lo:.5f}, {hi:.5f}]（年化 [{lo * 252 * 100:.2f}%, {hi * 252 * 100:.2f}%]）")
    print(f"bootstrap p 值 (H0: 均值<=0): {p:.4f}  ->  {'显著' if p < 0.05 else '不显著'}")


if __name__ == "__main__":
    main()
