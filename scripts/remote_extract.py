"""远程解压 repo + 数据 tgz（python tarfile，兼容 Windows 路径）。"""
import tarfile
from pathlib import Path

base = Path("C:/Users/song/qbt_work")

for src, dst in [("qbt_transfer.tgz", "quant"), ("qlib_data_transfer.tgz", "qlib_data")]:
    p = base / src
    if not p.exists():
        print("缺失", src)
        continue
    d = base / dst
    d.mkdir(parents=True, exist_ok=True)
    with tarfile.open(p, "r:gz") as t:
        t.extractall(d)
    print("extracted", src, "->", d)
print("EXTRACT_DONE")
