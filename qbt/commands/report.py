"""qbt report: 汇总各阶段结果 → HTML 报告"""
from datetime import datetime
from pathlib import Path

import typer

from qbt.config import project_root
from qbt.state import read_state, write_state

_CSS = """
body{background:#0f1420;color:#e8ecf4;font-family:"Microsoft YaHei",sans-serif;padding:32px 20px;max-width:960px;margin:0 auto}
h1{font-size:24px}h2{font-size:17px;margin-top:28px;border-left:4px solid #60a5fa;padding-left:10px}
table{width:100%;border-collapse:collapse;background:#171e2e;font-size:13.5px;border-radius:8px;overflow:hidden}
th{background:#1d2640;color:#8b95ab;padding:9px 12px;text-align:left}td{padding:8px 12px;border-top:1px solid #2a3550}
.pos{color:#34d399}.neg{color:#f87171}.dim{color:#8b95ab;font-size:12px}
.note{background:#171e2e;border:1px dashed #2a3550;border-radius:8px;padding:12px 16px;font-size:12.5px;line-height:1.8;color:#8b95ab;margin-top:24px}
"""


def _fmt_pct(v) -> str:
    if v is None:
        return "—"
    return f"{float(v) * 100:+.2f}%"


def report(
    out: str = typer.Option(None, help="输出 HTML 路径（默认 results/report.html）"),
) -> None:
    """汇总各阶段结果生成 HTML 报告"""
    st = read_state()
    if not st:
        typer.secho("没有运行记录，先跑数据→训练→回测", fg="red")
        raise typer.Exit(1)

    root = project_root()
    out_path = root / "results" / "report.html" if out is None else Path(out)

    tm = st.get("train_metrics", {})
    bm = st.get("backtest_metrics", {})

    # 数据概览：优先读 fetch 缓存元数据（数据来源/数量/区间/字段口径）
    import json as _json

    data_rows = ""
    meta_path = root / "qlib_data_src" / ".fetch_meta.json"
    if meta_path.exists():
        try:
            m = _json.loads(meta_path.read_text(encoding="utf-8"))
            n_csv = len(list((root / "qlib_data_src").glob("*.csv")))
            data_rows = "".join([
                f"<tr><td>数据来源</td><td>{m.get('source', '—')}（后复权 + 真实 factor + turn）</td></tr>",
                f"<tr><td>股票数 / CSV 文件</td><td>{m.get('stocks', '—')} / {n_csv}</td></tr>",
                f"<tr><td>日期区间</td><td>{m.get('start', '—')} ~ {m.get('end', '—')}</td></tr>",
                f"<tr><td>字段口径</td><td>{m.get('fields_version', '—')}（P2-3 后复权 / P2-5 turn）</td></tr>",
            ])
        except Exception:  # noqa: BLE001
            data_rows = ""

    rows = []
    rows.append(f"<tr><td>数据导出</td><td>{st.get('data_status', '—')}</td><td>{st.get('data_info', '')}</td></tr>")
    rows.append(f"<tr><td>模型训练</td><td>{st.get('train_status', '—')}</td><td>{st.get('train_info', '')}</td></tr>")
    rows.append(f"<tr><td>调仓计划</td><td>{st.get('plan_status', '—')}</td><td>{st.get('plan_info', '')}</td></tr>")
    rows.append(f"<tr><td>真实规则回测</td><td>{st.get('backtest_status', '—')}</td><td>{st.get('backtest_info', '')}</td></tr>")
    rows.append(f"<tr><td>报告</td><td>done</td><td>{out_path.name}</td></tr>")

    ic_html = f"<tr><td>IC / ICIR</td><td>{tm.get('ic', '—')} / {tm.get('icir', '—')}</td></tr>" if tm else ""
    metric_rows = "".join([
        f"<tr><td>总收益</td><td class='{('pos' if (bm.get('total_returns') or 0) > 0 else 'neg')}'>{_fmt_pct(bm.get('total_returns'))}</td></tr>",
        f"<tr><td>年化收益</td><td class='{('pos' if (bm.get('annualized_returns') or 0) > 0 else 'neg')}'>{_fmt_pct(bm.get('annualized_returns'))}</td></tr>",
        f"<tr><td>最大回撤</td><td>{_fmt_pct(bm.get('max_drawdown'))}</td></tr>",
        f"<tr><td>Sharpe</td><td>{bm.get('sharpe', '—')}</td></tr>",
        f"<tr><td>胜率 / 换手</td><td>{_fmt_pct(bm.get('win_rate'))} / {bm.get('turnover', '—')} 倍</td></tr>",
        f"<tr><td>交易笔数</td><td>{bm.get('trades', '—')}</td></tr>",
    ]) if bm else ""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>qbt 回测报告</title>
<style>{_CSS}</style></head><body>
<h1>📊 qbt 量化回测报告</h1>
<div class="dim">生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}（{root.name}）</div>

<h2>一、流水线状态</h2>
<table><tr><th>阶段</th><th>状态</th><th>信息</th></tr>{''.join(rows)}</table>

<h2>二、数据概览</h2>
<table><tr><th>项目</th><th>内容</th></tr>{data_rows or '<tr><td colspan="2" class="dim">无 fetch 缓存元数据（未记录数据来源）</td></tr>'}</table>

<h2>三、模型预测力（简化规则，含成本）</h2>
<table>{ic_html}<tr><td>超额年化（含成本）</td><td>{_fmt_pct(tm.get('excess_ann'))}</td></tr>
<tr><td>超额 IR</td><td>{tm.get('excess_ir', '—')}</td></tr>
<tr><td>超额回撤</td><td>{_fmt_pct(tm.get('excess_mdd'))}</td></tr>
<tr><td>模型 / 股票池</td><td>{tm.get('model', '—')} / {tm.get('pool', '—')}</td></tr></table>

<h2>四、真实规则回测（rqalpha：T+1/涨跌停/印花税/100股整数倍）</h2>
<table><tr><th>指标</th><th>数值</th></tr>{metric_rows}</table>

<div class="note"><b>风险提示：</b>回测≠实盘（未计滑点/冲击成本/停牌锁定）；成分股为当前快照，含幸存者偏差；
超额随时间/模型/股票池均可能衰减。数据口径与复现步骤见项目 README。</div>
</body></html>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    typer.secho(f"✅ 报告已生成: {out_path}", fg="green")
    write_state(report_status="done", report_info=str(out_path))
