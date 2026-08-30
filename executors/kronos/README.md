# Kronos executor（零样本金融时序基础模型）

执行器 `executors/kronos`：用开源基础模型 Kronos（shiyu-coder/Kronos，MIT）
对每只股票的日线 OHLCV 做零样本次日预测，输出截面可比的 1 日收益预测
`score = pred_close(t+1)/close(t) - 1`，交给固定测试器跑 IC 族指标。

## 上游与 vendor

- 上游仓库：github.com/shiyu-coder/Kronos（gitee 镜像
  gitee.com/mirrors/Kronos），MIT License。vendor commit `67b630e`。
- vendor 范围：`model/{__init__,module,kronos}.py` 三个文件原样拷贝，
  唯一修改：`kronos.py` 中 `sys.path.append("../")` 删除、
  `from model.module import *` 改为 `from .module import *`（包内相对导入，
  适配本执行器目录布局）。模型数学零改动。
- 权重：Hugging Face `NeoQuasar/Kronos-base`（102M，上下文 512）+
  `NeoQuasar/Kronos-Tokenizer-base`（24.7M）。huggingface.co 在容器内
  不可达，下载走 `HF_ENDPOINT=https://hf-mirror.com`（部署时预缓存到
  `~/.cache/huggingface`；main.py 内 setdefault 同一端点）。

## 输入数据（extra 特征旁路，契约第 8 条）

- `data/extra/kronos_ohlcv.parquet`：(datetime, instrument) MultiIndex，
  open/high/low/close/volume/amount float32。由 `qlib_data_src` 的 hfq 价格
  CSV（与管线 price cache 同源）一次性构建，**未进 git**（.gitignore 排除），
  构建脚本见 `scratch_probe/kronos_build_extra.py`（主机侧），manifest 在
  `data/extra/kronos_ohlcv.manifest.json`。
- 执行器在 `<out>/run_info.json` 声明 `extra_features: ["kronos_ohlcv"]`，
  管线自动记入 contract_report.json 与 tag `qlab.extra_features`。
- 口径纪律：raw OHLCV 是基础模型的输入而非自造特征，且与价格 cache 同源；
  board 对比时该实验超出固定菜单口径，与 Alpha158 实验不可直接并列。

## spec.params（透明透传）

| 参数 | 默认 | 说明 |
|---|---|---|
| kronos_model | NeoQuasar/Kronos-base | 主模型 HF id |
| kronos_tokenizer | NeoQuasar/Kronos-Tokenizer-base | tokenizer HF id |
| lookback | 512 | 每个预测日的 K 线回看窗口（交易日数） |
| max_context | 512 | 模型上下文上限（Kronos-base=512） |
| min_history | 60 | 历史不足该值的股票当日跳过 |
| temperature / top_p / sample_count | 1.0 / 0.9 / 1 | 采样参数（上游 README 默认） |
| seed | 42 | torch 采样种子 |
| max_dates | 0 | 只预测前 N 个测试日（0=全部；smoke 用） |
| torch_threads | 8 | CPU 线程数 |

## 部署清单（新环境重建）

1. `python scratch_probe/kronos_build_extra.py`（或主机侧对应脚本）生成
   data/extra/kronos_ohlcv.parquet。
2. `HF_ENDPOINT=https://hf-mirror.com python -c "from huggingface_hub import snapshot_download; snapshot_download('NeoQuasar/Kronos-base'); snapshot_download('NeoQuasar/Kronos-Tokenizer-base')"`
   （用本执行器 venv 的 python）。
3. venv 由管线按 requirements.txt 自动构建（stamp 防陈旧）。

## 已知限制

- 纯 CPU 推理，逐日批预测（B=当日股票数 × lookback × 6），吞吐随
  lookback×股票数线性增长；大窗口全池跑需按基准实测排期（见
  knowledge/notes 对应条目）。
- 采样温度>0 时预测非确定性（run_info.json 记录 seed）。
- 零样本模型无训练期；train parquet 仅用于管线口径对齐，执行器不读特征。

