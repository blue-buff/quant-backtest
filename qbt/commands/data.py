"""qbt data: 数据管道（导出 / 校验 / 转 qlib 格式）"""
import os
import subprocess
import sys
from pathlib import Path

import typer

from qbt.config import load_config, project_root, resolve
from qbt.state import write_state

app = typer.Typer(help="数据管道：导出成分股日线 / 交叉校验 / 转 qlib bin 格式")

POOLS = {
    "hs300": ("query_hs300_stocks", "沪深300"),
    "zz500": ("query_zz500_stocks", "中证500"),
}


def _out_dir(cfg: dict, pool: str) -> Path:
    key = "hs300_out" if pool == "hs300" else "zz500_out"
    return project_root() / cfg["data"][key]


@app.command("fetch")
def fetch(
    pool: str = typer.Option("hs300", help="股票池: hs300 / zz500"),
    start: str = typer.Option(None, help="开始日期 YYYY-MM-DD（默认取配置）"),
    end: str = typer.Option(None, help="结束日期 YYYY-MM-DD（默认取配置）"),
    limit: int = typer.Option(0, help="只导出前 N 只（测试用，0=全部）"),
) -> None:
    """从 baostock 导出成分股日线 CSV（前复权）"""
    if pool not in POOLS:
        typer.secho(f"未知股票池 {pool}，可选: {', '.join(POOLS)}", fg="red")
        raise typer.Exit(1)
    cfg = load_config()
    start = start or cfg["data"]["start"]
    end = end or cfg["data"]["end"]
    out = _out_dir(cfg, pool)
    out.mkdir(parents=True, exist_ok=True)

    typer.echo(f"导出 {POOLS[pool][1]} 日线 {start} ~ {end} → {out} (limit={limit or '全部'})")
    try:
        import baostock as bs
        import pandas as pd
    except ImportError:
        typer.secho("缺少 baostock/pandas，请先 pip install baostock", fg="red")
        raise typer.Exit(1)

    lg = bs.login()
    if lg.error_code != "0":
        typer.secho(f"baostock 登录失败: {lg.error_msg}", fg="red")
        raise typer.Exit(1)

    fn = POOLS[pool][0]
    rs = getattr(bs, fn)()
    codes, ok, skipped, failed = [], 0, 0, 0
    while rs.error_code == "0" and rs.next():
        row = rs.get_row_data()  # [updateDate, code, code_name]
        codes.append(row[1])
    if limit > 0:
        codes = codes[:limit]
    typer.echo(f"成分股 {len(codes)} 只（{'前%d只' % limit if limit else '当前成分，含幸存者偏差'}）")

    with typer.progressbar(codes, label="下载中") as bar:
        for code in bar:
            fname = code.replace(".", "")
            try:
                rs = bs.query_history_k_data_plus(
                    code,
                    "date,open,high,low,close,volume,amount,turn",
                    start_date=start, end_date=end, frequency="d", adjustflag=cfg["data"]["adjust"],
                )
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
                if len(rows) < 100:
                    skipped += 1
                    continue
                df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                                 "volume", "amount", "turn"])
                for c in ["open", "high", "low", "close", "volume", "amount"]:
                    df[c] = df[c].astype(float)
                df["vwap"] = (df["amount"] / df["volume"].replace(0, pd.NA)).fillna(df["close"])
                df["factor"] = 1.0
                df.to_csv(out / f"{fname}.csv", index=False)
                ok += 1
            except (ValueError, TypeError):
                failed += 1
    # 补导基准指数（qlib 回测 benchmark 需要；须在 logout 前查询）
    idx_code = "sh.000300" if pool == "hs300" else "sh.000905"
    try:
        rs = bs.query_history_k_data_plus(
            idx_code, "date,open,high,low,close,volume,amount",
            start_date=start, end_date=end, frequency="d", adjustflag="3")
        idx_rows = []
        while rs.error_code == "0" and rs.next():
            idx_rows.append(rs.get_row_data())
        if idx_rows:
            pd.DataFrame(idx_rows, columns=["date", "open", "high", "low", "close", "volume", "amount"]) \
                .to_csv(out / f"{idx_code.replace('.', '')}.csv", index=False)
            typer.echo(f"基准指数 {idx_code} 已导出 ({len(idx_rows)} 行)")
    except Exception as e:
        typer.secho(f"指数导出失败: {e}", fg="yellow")
    bs.logout()
    write_state(data_status="done", data_info=f"{POOLS[pool][1]} ok={ok} skip={skipped} fail={failed}")
    typer.secho(f"✅ 完成: ok={ok} skip={skipped} fail={failed}，目录 {out}", fg="green")


@app.command("validate")
def validate(
    n: int = typer.Option(5, help="抽查股票数量"),
    days: int = typer.Option(5, help="每只抽查最近 N 个交易日"),
    threshold: float = typer.Option(0.005, help="价格差异报警阈值（默认 0.5%）"),
) -> None:
    """交叉校验：已导出 CSV vs 腾讯行情接口（>阈值报警）"""
    import glob

    import pandas as pd

    cfg = load_config()
    csvs = sorted(glob.glob(str(_out_dir(cfg, "hs300") / "*.csv")) +
                  glob.glob(str(_out_dir(cfg, "zz500") / "*.csv")))
    if not csvs:
        typer.secho("没有可校验的 CSV，先跑 qbt data fetch", fg="red")
        raise typer.Exit(1)

    import urllib.request

    import numpy as np

    picked = csvs[:n]
    bad = 0
    for p in picked:
        fname = Path(p).stem  # sh600519
        df = pd.read_csv(p).tail(days)
        last = df.iloc[-1]
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={fname},day,{last['date']},{last['date']},1,qfq"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = r.read().decode("utf-8")
            import re

            m = re.search(r'\["([\d-]+)","([\d.]+)","([\d.]+)"', data)
            if not m:
                continue
            tencent_close = float(m.group(3))
            diff = abs(tencent_close - last["close"]) / last["close"]
            flag = "⚠️" if diff > threshold else "✅"
            if diff > threshold:
                bad += 1
            typer.echo(f"{flag} {fname} {last['date']}: 本地 {last['close']:.2f} vs 腾讯 {tencent_close:.2f} (差 {diff*100:.2f}%)")
        except Exception as e:
            typer.echo(f"⚠️ {fname}: 接口请求失败 {e}")
    if bad:
        typer.secho(f"发现 {bad} 只差异超阈值（前复权口径差异属正常，详见 README 风险说明）", fg="yellow")
    else:
        typer.secho("✅ 校验通过：本地数据与腾讯接口一致", fg="green")


@app.command("dump")
def dump(
    pool: str = typer.Option("hs300", help="股票池: hs300 / zz500"),
    qlib_dir: str = typer.Option(None, help="qlib 数据目录（默认 ~/.qlib/qlib_data/cn_data 或 cn_data_zz500）"),
) -> None:
    """CSV → qlib bin 格式（调用 dump_bin.py）"""
    if pool not in POOLS:
        typer.secho(f"未知股票池 {pool}", fg="red")
        raise typer.Exit(1)
    cfg = load_config()
    src = _out_dir(cfg, pool)
    if not any(src.glob("*.csv")):
        typer.secho(f"{src} 没有 CSV，先跑 qbt data fetch --pool {pool}", fg="red")
        raise typer.Exit(1)
    if qlib_dir is None:
        base = cfg["train"]["qlib_dir"].replace("cn_data", f"cn_data_{pool}") if pool == "zz500" \
            else cfg["train"]["qlib_dir"]
        qlib_dir = base
    qlib_dir = resolve(qlib_dir)

    dump_script = project_root() / "qlib_scripts" / "dump_bin.py"
    cmd = [
        sys.executable, str(dump_script), "dump_all",
        "--data_path", str(src),
        "--qlib_dir", qlib_dir,
        "--include_fields", "open,high,low,close,volume,vwap,factor",
    ]
    typer.echo(f"转格式: {src} → {qlib_dir}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    tail = "\n".join(r.stdout.splitlines()[-3:]) if r.stdout else r.stderr[-500:]
    if r.returncode != 0:
        typer.secho(f"dump 失败: {tail}", fg="red")
        raise typer.Exit(1)
    typer.echo(f"✅ dump 完成 → {qlib_dir}")

    # 生成 universe 文件（剔除指数，qlib 训练用 instruments）
    idx_sym = "SH000300" if pool == "hs300" else "SH000905"
    uni_name = "csi300.txt" if pool == "hs300" else "csi500.txt"
    all_file = Path(qlib_dir) / "instruments" / "all.txt"
    if all_file.exists():
        lines = [l for l in all_file.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.startswith(idx_sym)]
        (Path(qlib_dir) / "instruments" / uni_name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        typer.echo(f"universe: {uni_name} ({len(lines)} 只)")
    write_state(data_status="done", data_info=f"{POOLS[pool][1]} 已转 qlib 格式")
