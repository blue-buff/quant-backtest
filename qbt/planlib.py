"""调仓计划纯函数库：交易日对齐 / T+1 执行 / 代码转换 / lineage 校验。

按 docs/OPTIMIZATION.md 修复：
- P0-1: resample('ME').last() 的标签是日历月末（周末/节假日被 rqalpha 静默跳过、
  整月不调仓）→ 改为每月最后一个『有数据的交易日』
- P1-4: T 日收盘信号 T 日成交（轻微前视）→ 默认 T+1 交易日执行
- P0-2: plan 必须按 train 写入 state 的 run_id 定位 pred.pkl，pool 不匹配显式报错

qbt plan 与 qlib_examples/make_plan*.py 共用；pytest 直接覆盖（tests/test_planlib.py）。
"""
from __future__ import annotations

import bisect
from pathlib import Path

import pandas as pd


def to_rqalpha(symbol: str) -> str:
    """SH600000 → 600000.XSHG；SZ000001 → 000001.XSHE；其余原样返回"""
    if symbol.startswith("SH"):
        return symbol[2:] + ".XSHG"
    if symbol.startswith("SZ"):
        return symbol[2:] + ".XSHE"
    return symbol


def month_end_trading_days(wide: pd.DataFrame, freq: str = "ME") -> list[pd.Timestamp]:
    """每月最后一个『有数据的交易日』（宽表索引只含交易日）。

    原实现 resample('ME').last() 的索引标签是日历月末：2025-05-31(周六)、
    2025-08-31(周日) 等日期 rqalpha 永远不会有 bar，导致整月不调仓。
    这里按周期取索引最大值，保证每个信号日都是真实交易日。
    """
    if wide is None or wide.empty:
        return []
    period = "M" if freq in ("ME", "M") else freq
    idx = wide.index
    if not idx.is_monotonic_increasing:
        idx = idx.sort_values()
        wide = wide.loc[idx]
    return list(idx.to_series().groupby(idx.to_period(period)).max())


def shift_next_trading_day(dates, calendar) -> list[pd.Timestamp]:
    """把每个信号日向后平移一个交易日（T 日信号 → T+1 执行）。

    calendar 可以是 DataFrame 索引 / 序列 / 字符串日期列表；返回与原顺序一致、
    且存在后继交易日的日期；末尾无后继的日期被丢弃。
    """
    cal = sorted({pd.Timestamp(x) for x in calendar})
    out = []
    for d in dates:
        i = bisect.bisect_right(cal, pd.Timestamp(d))
        if i < len(cal):
            out.append(cal[i])
    return out


def build_plan(
    wide: pd.DataFrame,
    topk: int = 50,
    freq: str = "ME",
    execute_next_day: bool = True,
    calendar=None,
) -> list[list[str]]:
    """分数宽表 → [[执行日期, rqalpha 代码...], ...]

    - 信号日：每月最后一个交易日（P0-1）
    - 执行日：默认 T+1 交易日（P1-4）；不传 calendar 时退化为信号日当日
    - 组合：每信号日分数 top-K，顺序即等权持仓列表
    """
    wide = wide.sort_index()
    signal_dates = month_end_trading_days(wide, freq)
    cal = None
    if execute_next_day:
        source = wide.index if calendar is None else calendar
        cal = sorted({pd.Timestamp(x) for x in source})
    rows: list[list[str]] = []
    for sig in signal_dates:
        top = wide.loc[sig].dropna().sort_values(ascending=False).head(topk).index.tolist()
        if not top:
            continue
        if execute_next_day:
            i = bisect.bisect_right(cal, sig)
            if i >= len(cal):
                continue  # 信号后没有可执行的交易日（数据末尾）
            exec_date = cal[i]
        else:
            exec_date = sig
        rows.append([exec_date.strftime("%Y-%m-%d")] + [to_rqalpha(s) for s in top])
    return rows


def build_plan_with_buffer(
    wide: pd.DataFrame,
    topk: int = 50,
    buffer_n: int = 10,
    freq: str = "ME",
    execute_next_day: bool = True,
    calendar=None,
) -> list[list[str]]:
    """P1-3: rank-buffer 月度调仓计划（对齐 qlib TopkDropoutStrategy 语义）。

    - 入池：每月 top-K 中不在上期持仓的新面孔 → 买入
    - 保留：上期持仓仍在前 top-(K+N) 的 → 继续持有（rank buffer，N 默认 10）
    - 卖出：上期持仓跌出 top-(K+N) 的 → 不在目标列表 → rqalpha 策略清仓

    输出每期【目标持仓列表】= 本期 top-K ∪ 上期持仓 ∩ top-(K+N)（按分数降序），
    与普通 build_plan 相同的 CSV 格式，rqalpha 策略无需感知 rank-buffer。
    """
    wide = wide.sort_index()
    signal_dates = month_end_trading_days(wide, freq)
    cal = None
    if execute_next_day:
        cal = sorted({pd.Timestamp(x) for x in (wide.index if calendar is None else calendar)})
    rows: list[list[str]] = []
    held: set[str] = set()
    for sig in signal_dates:
        scores = wide.loc[sig].dropna().sort_values(ascending=False)
        if scores.empty:
            continue
        topk_syms = scores.head(topk).index.tolist()
        keep = set(scores.head(topk + buffer_n).index) if buffer_n and buffer_n > 0 else set()
        # 上期持仓仍在前 top-(K+N) 的保留（按分数序），再拼本期新进 top-K
        held_in_buffer = [s for s in scores.index if s in held and s in keep and s not in topk_syms]
        target = topk_syms + held_in_buffer
        if not target:
            continue
        if execute_next_day:
            i = bisect.bisect_right(cal, sig)
            if i >= len(cal):
                continue
            exec_date = cal[i]
        else:
            exec_date = sig
        rows.append([exec_date.strftime("%Y-%m-%d")] + [to_rqalpha(s) for s in target])
        held = set(target)
    return rows


def find_pred_by_run_id(root: Path, run_id: str) -> Path:
    """按 mlflow run_id 定位 pred.pkl（P0-2：拒绝按 mtime 兜底）"""
    cands = sorted((root / "qlib_examples" / "mlruns").glob(f"*/{run_id}/artifacts/pred.pkl"))
    if not cands:
        raise FileNotFoundError(
            f"训练记录的 pred.pkl 不存在（run_id={run_id}）。"
            "产物可能已被清理，请重新运行 qbt train --pool <pool>"
        )
    return cands[0]


def resolve_plan_pred(state: dict, pool: str, root: Path) -> Path:
    """plan 的 lineage 校验：必须使用 train 写入 state 的 run_id，且 pool 一致。

    交叉序列（train zz500 → plan hs300）必须显式报错，而不是拿别的股票池的
    预测分数静默生成计划（OPTIMIZATION.md P0-2）。
    """
    run = state.get("train_run")
    if not run or not run.get("run_id"):
        raise FileNotFoundError(
            "未找到训练 lineage（results/status.json 中无 train_run）。"
            "请先运行 qbt train --pool <pool> 再执行 plan。"
        )
    if run.get("pool") != pool:
        raise ValueError(
            f"训练 lineage 与请求的股票池不匹配: 最近一次训练 pool={run.get('pool')!r}，"
            f"而本次 plan --pool={pool!r}。请先运行 qbt train --pool {pool}，"
            "或清除 results/status.json 中的 train_run 后重试。"
        )
    return find_pred_by_run_id(root, str(run["run_id"]))


def load_calendar(path) -> list[pd.Timestamp]:
    """读取 qlib 交易日历（calendars/day.txt）"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"交易日历不存在: {p}")
    return [
        pd.Timestamp(line.strip())
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
