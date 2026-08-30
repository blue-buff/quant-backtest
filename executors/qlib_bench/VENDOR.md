# qlib_bench 执行器 vendor 钉死（B8 加固）

- 运行环境：ambient interpreter（容器系统 python3.12；pyqlib 0.9.7 无 aarch64 wheel，
  源码构建后从 ambient site-packages 注入，见 main.py 顶部）。
- torch: 2.10.0+cu128（requirements.txt 钉死；venv stamp 保证 requirements 变更自动重装）。
- pyqlib: 0.9.7（升级需重编译，视为冻结）。
- 兼容 shim 清单（main.py `_torch_compat` / `_load_model` / ParquetHandler，
  每点一条回归测试见 tests/test_qlib_shim.py）：
  1. ReduceLROnPlateau verbose= 移除（torch>=2.2 不接受该参数）；
  2. torch.load 默认 weights_only=False（官方预训练 LSTM pkl 为旧格式）；
  3. ts 模型 n_jobs 强制 0（远端 /dev/shm=64MB，DataLoader worker 会挂死）；
  4. qlib R.start 是 contextmanager，fit 必须 with 包裹（裸调是空操作）；
  5. ParquetHandler 暴露 data_loader.fields / _learn（MTSDatasetH/TRA 直读）；
  6. TRA 显式 horizon=1（qlib 从 "LABEL0" 列名推不出 horizon）；
  7. IGMTF 补 metric=ic（官方 yaml 有，转写时易漏）。
- 变更纪律：任何一条 shim 修改必须同步更新本清单与对应测试；上游 qlib 停更，
  该补丁层视为永久（无平替，2026-08-24 已评估）。
