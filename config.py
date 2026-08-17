"""集中路径配置：环境变量优先，默认基于项目根自动推导，无需改代码。

用法：脚本开头加
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from config import BUNDLE_PATH, PLAN_FILE, ...
"""
import os

# 项目根目录（本文件所在目录）
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
QLIB_EXAMPLES_DIR = os.path.join(PROJECT_ROOT, "qlib_examples")

# rqalpha 行情库（3.3G，官方 download-bundle 获取；可用环境变量 RQALPHA_BUNDLE 覆盖）
BUNDLE_PATH = os.environ.get("RQALPHA_BUNDLE", "/root/.rqalpha/bundle")

# 数据导出目录（export_csv*.py 输出）
DATA_SRC_DIR = os.path.join(PROJECT_ROOT, "qlib_data_src")
DATA_SRC_ZZ500_DIR = os.path.join(PROJECT_ROOT, "qlib_data_src_zz500")

# 月度调仓计划文件（make_plan*.py 输出 / rq_strategy_qlib*.py 读取）
PLAN_FILE = os.path.join(QLIB_EXAMPLES_DIR, "rebalance_plan.csv")
PLAN_FILE_ZZ500 = os.path.join(QLIB_EXAMPLES_DIR, "rebalance_plan_zz500.csv")

# qlib 实验产物目录（qrun 输出，含 pred.pkl）
MLRUNS_DIR = os.path.join(QLIB_EXAMPLES_DIR, "mlruns")

# rqalpha 策略文件（rq_run_qlib*.py 引用）
STRATEGY_FILE = os.path.join(QLIB_EXAMPLES_DIR, "rq_strategy_qlib.py")
STRATEGY_FILE_ZZ500 = os.path.join(QLIB_EXAMPLES_DIR, "rq_strategy_qlib_zz500.py")
RQALPHA_STRATEGY_FILE = os.path.join(PROJECT_ROOT, "rqalpha", "strategy.py")
