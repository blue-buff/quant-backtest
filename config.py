"""集中路径配置（OPTIMIZATION.md C4：与 qbt/config.py 合并收敛为一套）。

本文件保持向后兼容：qlib_examples/qlib_scripts 里的独立脚本
（make_plan*.py、export_csv*.py 等）继续用 from config import XXX 导入常量，
但取值统一来自 qbt.config（qbt.yaml + 默认值 + 环境变量），不再各自维护一份。
"""
import os

from qbt.config import load_config, project_root

PROJECT_ROOT = str(project_root())
QLIB_EXAMPLES_DIR = os.path.join(PROJECT_ROOT, "qlib_examples")

# rqalpha 行情库（3.3G，官方 download-bundle 获取；可用环境变量 RQALPHA_BUNDLE 覆盖）
BUNDLE_PATH = os.environ.get("RQALPHA_BUNDLE", "/root/.rqalpha/bundle")

# 数据导出目录（export_csv*.py 输出）
_cfg = load_config()
DATA_SRC_DIR = os.path.join(PROJECT_ROOT, _cfg["data"].get("hs300_out", "qlib_data_src"))
DATA_SRC_ZZ500_DIR = os.path.join(PROJECT_ROOT, _cfg["data"].get("zz500_out", "qlib_data_src_zz500"))

# 月度调仓计划文件（make_plan*.py 输出 / rq_strategy_qlib*.py 读取）
PLAN_FILE = os.path.join(QLIB_EXAMPLES_DIR, "rebalance_plan.csv")
PLAN_FILE_ZZ500 = os.path.join(QLIB_EXAMPLES_DIR, "rebalance_plan_zz500.csv")

# qlib 实验产物目录（qrun 输出，含 pred.pkl）
MLRUNS_DIR = os.path.join(QLIB_EXAMPLES_DIR, "mlruns")

# rqalpha 策略文件（rq_run_qlib*.py 引用）
STRATEGY_FILE = os.path.join(QLIB_EXAMPLES_DIR, "rq_strategy_qlib.py")
STRATEGY_FILE_ZZ500 = os.path.join(QLIB_EXAMPLES_DIR, "rq_strategy_qlib_zz500.py")
RQALPHA_STRATEGY_FILE = os.path.join(PROJECT_ROOT, "rqalpha", "strategy.py")
