"""C4: 根 config.py 与 qbt.config/pools 收敛一致性测试"""
from pathlib import Path

import config as root_config
from qbt.config import project_root
from qbt.pools import get_pool


def test_root_config_points_to_project():
    assert root_config.PROJECT_ROOT == str(project_root())
    assert root_config.QLIB_EXAMPLES_DIR == str(project_root() / "qlib_examples")
    assert root_config.MLRUNS_DIR == str(project_root() / "qlib_examples" / "mlruns")


def test_root_config_plan_files_match_registry():
    root = project_root()
    assert root_config.PLAN_FILE == str(root / get_pool("hs300")["plan_out"])
    assert root_config.PLAN_FILE_ZZ500 == str(root / get_pool("zz500")["plan_out"])


def test_root_config_strategy_files_match_registry():
    root = project_root()
    assert root_config.STRATEGY_FILE == str(root / get_pool("hs300")["strategy"])
    assert root_config.STRATEGY_FILE_ZZ500 == str(root / get_pool("zz500")["strategy"])
