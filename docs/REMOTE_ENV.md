# DGX Spark 远端环境（P8 §6.3 固定包清单）

> 记录 2026-08-23。远端 = ssh -J song@10.110.12.99 -p 2223 dev@10.0.0.5，
> 落点直接是计算容器（aarch64，20 核，NVIDIA GB10 / sm_120 / CUDA 13.0）。
> 修改远端环境后必须更新本文件。

## 运行环境

- OS: Ubuntu 24.04 容器（无 sudo、无 bzip2、无 rsync）
- Python: micromamba env `quant` = /home/dev/.local/share/mamba/envs/quant/bin/python
  （python 3.12.14）
- QLAB_SPARK_PYTHON 指向它（固定 venv，不在仓库内）
- 执行器私有依赖 venv：/home/dev/quant/executor_venvs/<executor>（QLAB_VENV_DIR，
  跨 dispatch 持久；repo 目录每次 dispatch 会重建）

## 关键包（quant env）

| 包 | 版本 | 备注 |
|---|---|---|
| numpy | 2.5.2 | aarch64 conda build |
| pandas | conda build | |
| scipy | 1.18.0 | aarch64 |
| pyarrow | conda build | |
| pyqlib | 0.9.7 | GitHub 源码编译（Cython rolling/expanding .so）；PyPI 上 qlib 项目停更在 0.0.2.dev*，0.9.x 以 pyqlib 包名发布且无 aarch64 wheel |
| python-redis-lock | 4.0.1 | qlib 导入时需要；conda-forge 无此包，从本地纯 py wheel 安装 |
| lightgbm | 4.7.0 | |
| mlflow | 3.15.1 | |
| Cython / setuptools | | 源码编译工具 |
| dill/fire/tqdm/loguru/ruamel.yaml/filelock/joblib/redis/pymongo/pydantic-settings | | qlib/mlflow 依赖 |

## pip 与镜像

- 默认 PyPI 走清华镜像（~/.config/pip/pip.conf；2026-08-23 起外网 PyPI/
  download.pytorch.org 被墙，直连 SSL reset）
- CUDA torch 轮子走阿里镜像：https://mirrors.aliyun.com/pytorch-wheels/cu128
  （torch 2.10.0+cu128 有 linux aarch64 wheel，支持 sm_120；约 13MB/s）

## 数据（QLAB_QLIB_DATA=/home/dev/quant）

- qlib_data/{cn_data,cn_data_zz500,cn_data_all}：三池 bins（2021-06-01~2026-08-20）
- repo/cache/：特征/价格缓存（跨 dispatch 持久）；特征缓存由远端从 bins 自建，
  价格缓存由本地从 qlib_data_src 构建后 scp 预放

## 建环境备忘（重建时参考）

1. micromamba 装 python 3.12 + conda-forge 常用包（无 sudo，root-prefix 用户级）
2. pyqlib：pip install https://github.com/microsoft/qlib/archive/refs/tags/v0.9.7.tar.gz
   --no-deps --no-build-isolation（需 Cython）
3. python-redis-lock：本地下载纯 py wheel 后 scp 安装
4. 配置 pip 镜像（清华），torch 走阿里 cu128
5. 数据 bins + 缓存按上述路径放置
