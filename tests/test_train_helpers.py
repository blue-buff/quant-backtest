"""train 命令辅助函数测试：P1-5 mlruns metrics/ 文件读取 + 模型标签映射"""
import pytest

from qbt.commands.train import _MODEL_LABEL, read_run_metrics, read_run_param


def test_read_run_metrics(tmp_path):
    run = tmp_path / "run"
    (run / "metrics").mkdir(parents=True)
    # mlflow 真实格式: "<timestamp> <value> <step>" 三列，必须取中间 value 列
    (run / "metrics" / "IC").write_text("1710000000000 0.052 0\n", encoding="utf-8")
    (run / "metrics" / "1day.excess_return_with_cost.annualized_return").write_text(
        "1710000000000 0.128 0\n1710000000001 0.131 1\n", encoding="utf-8")
    # 兼容两列旧格式: "timestamp value"
    (run / "metrics" / "l2.valid").write_text("1710000000000 0.99\n", encoding="utf-8")
    # 非数值文件应被跳过而不是报错
    (run / "metrics" / "bad").write_text("not-a-float\n", encoding="utf-8")

    m = read_run_metrics(run)
    assert m["IC"] == 0.052  # 若误取 step 列会得到 0
    assert m["1day.excess_return_with_cost.annualized_return"] == 0.131  # 取最后一行
    assert m["l2.valid"] == 0.99
    assert "bad" not in m


def test_read_run_metrics_missing_dir(tmp_path):
    assert read_run_metrics(tmp_path / "no-such-dir") == {}


def test_read_run_param(tmp_path):
    run = tmp_path / "run"
    (run / "params").mkdir(parents=True)
    (run / "params" / "model.class").write_text("LGBModel\n", encoding="utf-8")
    assert read_run_param(run, "model.class") == "LGBModel"
    assert read_run_param(run, "missing") is None


def test_model_label_mapping():
    assert _MODEL_LABEL["LGBModel"] == "lgb"
    assert _MODEL_LABEL["LinearModel"] == "linear"
