"""qbt data: 数据管道（导出 / 校验 / 转 qlib 格式）"""
import json
import os
import subprocess
import sys
from pathlib import Path

import typer

from qbt.config import load_config, project_root, resolve
from qbt.pools import get_pool, pool_names
from qbt.state import write_state

app = typer.Typer(help="数据管道：导出成分股日线 / 交叉校验 / 转 qlib bin 格式")

# 缓存元数据文件名（fetch 产物旁，记录下载口径；dump/训练不受影响）
FETCH_META = ".fetch_meta.json"
# 字段口径版本：v2 = 后复权 + 真实 factor + turn（P2-3/P2-5）
FIELDS_VERSION = "v2"


def _out_dir(cfg: dict, pool: str) -> Path:
    """C1: 输出目录来自股票池注册表（可被 qbt.yaml 的 <pool>_out 覆盖）"""
    p = get_pool(pool)
    return project_root() / cfg["data"].get(f"{pool}_out", p["data_out"])


def _load_cache_meta(out: Path, pool: str, start: str, end: str, adjust: str) -> dict | None:
    """读取 fetch 缓存元数据；口径（pool/区间/复权/字段版本）不一致则视为无缓存。

    缓存命中 = 该目录的 CSV 就是当前配置口径下载的，可跳过网络请求。
    """
    meta_path = out / FETCH_META
    if not meta_path.exists():
        return None
    try:
        m = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (m.get("pool") == pool and m.get("start") == start
            and m.get("end") == end and m.get("adjust") == str(adjust)
            and m.get("fields_version") == FIELDS_VERSION):
        return m
    return None


def _write_cache_meta(out: Path, pool: str, start: str, end: str, adjust: str, stocks: int) -> None:
    """fetch 成功后记录口径，供下次 fetch 复用本地缓存"""
    meta = {
        "pool": pool, "start": start, "end": end,
        "adjust": str(adjust), "fields_version": FIELDS_VERSION,
        "stocks": stocks, "note": "本地缓存（qbt data fetch 复用，--force 强制重下）",
    }
    (out / FETCH_META).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


@app.command("fetch")
def fetch(
    pool: str = typer.Option("hs300", help="股票池: hs300 / zz500"),
    start: str = typer.Option(None, help="开始日期 YYYY-MM-DD（默认取配置）"),
    end: str = typer.Option(None, help="结束日期 YYYY-MM-DD（默认取配置）"),
    limit: int = typer.Option(0, help="只导出前 N 只（测试用，0=全部）"),
    force: bool = typer.Option(False, "--force", help="忽略本地缓存，强制重新下载"),
) -> None:
    """从 baostock 导出成分股日线 CSV（后复权 + 真实 factor + turn）

    本地缓存：已按相同口径（pool/区间/复权/字段版本）下载过的目录直接复用，
    不再访问 baostock（--force 强制重下）。缓存元数据为 <out>/.fetch_meta.json。
    P2-3: adjustflag=1 后复权，factor 取真实复权因子（避免前复权基准漂移）
    P2-5: 保留 turn（换手率），Alpha158 流动性类因子不再缺失
    """
    try:
        pool_cfg = get_pool(pool)
    except ValueError as e:
        typer.secho(f"❌ {e}", fg="red")
        raise typer.Exit(1)
    cfg = load_config()
    start = start or cfg["data"]["start"]
    end = end or cfg["data"]["end"]
    out = _out_dir(cfg, pool)
    out.mkdir(parents=True, exist_ok=True)

    # 缓存命中：跳过网络下载（qbt all 第二次起不再拉数据；--limit 调试模式除外）
    cached = _load_cache_meta(out, pool, start, end, cfg["data"]["adjust"])
    if cached and not force and limit <= 0:
        n_csv = len(list(out.glob("*.csv")))
        if n_csv >= int(cached.get("stocks", 0)):
            typer.secho(f"✅ 本地缓存命中：{cached['stocks']} 只（{out}），跳过下载", fg="green")
            typer.echo("   如需重新拉取: qbt data fetch --force --pool " + pool)
            write_state(data_status="done",
                        data_info=f"{pool_cfg['label']} 缓存命中 {cached['stocks']} 只（未访问网络）")
            return
        typer.secho(f"⚠️ 缓存元数据存在但 CSV 数量不足（{n_csv}），重新下载", fg="yellow")

    typer.echo(f"导出 {pool_cfg['label']} 日线 {start} ~ {end} → {out} (limit={limit or '全部'})")
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

    fn = pool_cfg["query_fn"]
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
                    "date,open,high,low,close,volume,amount,turn,factor",
                    start_date=start, end_date=end, frequency="d", adjustflag=cfg["data"]["adjust"],
                )
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
                if len(rows) < 100:
                    skipped += 1
                    continue
                df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                                 "volume", "amount", "turn", "factor"])
                for c in ["open", "high", "low", "close", "volume", "amount", "turn", "factor"]:
                    df[c] = df[c].astype(float)
                df["vwap"] = (df["amount"] / df["volume"].replace(0, pd.NA)).fillna(df["close"])
                # P2-3/P2-5: 真实复权因子 + 保留 turn
                df.to_csv(out / f"{fname}.csv", index=False)
                ok += 1
            except (ValueError, TypeError):
                failed += 1
    # 补导基准指数（qlib 回测 benchmark 需要；须在 logout 前查询）
    idx_code = pool_cfg["index_code"]
    try:
        rs = bs.query_history_k_data_plus(
            idx_code, "date,open,high,low,close,volume,amount",
            start_date=start, end_date=end, frequency="d", adjustflag="3")
        idx_rows = []
        while rs.error_code == "0" and rs.next():
            idx_rows.append(rs.get_row_data())
        if idx_rows:
            idx_df = pd.DataFrame(idx_rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
            idx_df["turn"] = pd.NA  # P2-5: 指数无换手率，补空列避免 dump 缺字段
            idx_df.to_csv(out / f"{idx_code.replace('.', '')}.csv", index=False)
            typer.echo(f"基准指数 {idx_code} 已导出 ({len(idx_rows)} 行)")
    except Exception as e:
        typer.secho(f"指数导出失败: {e}", fg="yellow")
    bs.logout()
    # 记录缓存口径（本目录 CSV 可被后续 fetch 复用）
    _write_cache_meta(out, pool, start, end, cfg["data"]["adjust"], ok)
    write_state(data_status="done", data_info=f"{pool_cfg['label']} ok={ok} skip={skipped} fail={failed}（已缓存）")
    typer.secho(f"✅ 完成: ok={ok} skip={skipped} fail={failed}，目录 {out}", fg="green")


@app.command("validate")
def validate(
    n: int = typer.Option(5, help="抽查股票数量"),
    days: int = typer.Option(5, help="每只抽查最近 N 个交易日"),
    threshold: float = typer.Option(0.005, help="价格差异报警阈值（默认 0.5%）"),
) -> None:
    """交叉校验：已导出 CSV vs 腾讯行情接口（对比日收益率，>阈值报警）

    本地数据为后复权（P2-3），不同数据源的后复权基准不同、绝对价不可直接比，
    因此对比【日收益率】（复权基准差异不影响收益），阈值默认 0.5 个百分点。
    """
    import glob
    import json

    import pandas as pd

    cfg = load_config()
    csvs = sorted(glob.glob(str(_out_dir(cfg, "hs300") / "*.csv")) +
                  glob.glob(str(_out_dir(cfg, "zz500") / "*.csv")))
    if not csvs:
        typer.secho("没有可校验的 CSV，先跑 qbt data fetch", fg="red")
        raise typer.Exit(1)

    import urllib.request

    picked = csvs[:n]
    bad = 0
    for p in picked:
        fname = Path(p).stem  # sh600519
        df = pd.read_csv(p).tail(days)
        if len(df) < 2:
            continue
        last = df.iloc[-1]
        # 腾讯 hfq（后复权）：取最近两天，对比日收益率（复权基准无关）
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={fname},day,{df['date'].iloc[-2]},{last['date']},2,hfq")
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                node = json.loads(r.read().decode("utf-8"))["data"][fname]
            klines = node.get("hfqday") or node.get("day")
            if not klines or len(klines) < 2:
                continue
            tx_ret = float(klines[-1][2]) / float(klines[-2][2]) - 1.0
            local_ret = float(df["close"].iloc[-1]) / float(df["close"].iloc[-2]) - 1.0
            diff = abs(local_ret - tx_ret)
            flag = "⚠️" if diff > threshold else "✅"
            if diff > threshold:
                bad += 1
            typer.echo(f"{flag} {fname} {last['date']}: 本地日收益 {local_ret*100:+.3f}% "
                       f"vs 腾讯 {tx_ret*100:+.3f}% (差 {diff*100:.3f}pct)")
        except Exception as e:
            typer.echo(f"⚠️ {fname}: 接口请求失败 {e}")
    if bad:
        typer.secho(f"发现 {bad} 只日收益差异超阈值（0.5pct），请检查数据", fg="yellow")
    else:
        typer.secho("✅ 校验通过：本地数据与腾讯接口日收益一致", fg="green")


@app.command("dump")
def dump(
    pool: str = typer.Option("hs300", help="股票池: hs300 / zz500"),
    qlib_dir: str = typer.Option(None, help="qlib 数据目录（默认 ~/.qlib/qlib_data/cn_data 或 cn_data_zz500）"),
) -> None:
    """CSV → qlib bin 格式（调用 dump_bin.py）"""
    try:
        pool_cfg = get_pool(pool)
    except ValueError as e:
        typer.secho(f"❌ {e}", fg="red")
        raise typer.Exit(1)
    cfg = load_config()
    src = _out_dir(cfg, pool)
    if not any(src.glob("*.csv")):
        typer.secho(f"{src} 没有 CSV，先跑 qbt data fetch --pool {pool}", fg="red")
        raise typer.Exit(1)
    if qlib_dir is None:
        # C1: qlib 数据目录来自注册表；z500 不再靠字符串替换推断
        qlib_dir = pool_cfg["qlib_dir"]
    qlib_dir = resolve(qlib_dir)

    dump_script = project_root() / "qlib_scripts" / "dump_bin.py"
    cmd = [
        sys.executable, str(dump_script), "dump_all",
        "--data_path", str(src),
        "--qlib_dir", qlib_dir,
        "--include_fields", "open,high,low,close,volume,vwap,turn,factor",
    ]
    typer.echo(f"转格式: {src} → {qlib_dir}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    tail = "\n".join(r.stdout.splitlines()[-3:]) if r.stdout else r.stderr[-500:]
    if r.returncode != 0:
        typer.secho(f"dump 失败: {tail}", fg="red")
        raise typer.Exit(1)
    typer.echo(f"✅ dump 完成 → {qlib_dir}")

    # 生成 universe 文件（剔除指数，qlib 训练用 instruments）
    idx_sym = pool_cfg["index_sym"]
    uni_name = f"{pool_cfg['universe']}.txt"
    all_file = Path(qlib_dir) / "instruments" / "all.txt"
    if all_file.exists():
        lines = [l for l in all_file.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.startswith(idx_sym)]
        (Path(qlib_dir) / "instruments" / uni_name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        typer.echo(f"universe: {uni_name} ({len(lines)} 只)")
    write_state(data_status="done", data_info=f"{pool_cfg['label']} 已转 qlib 格式")
