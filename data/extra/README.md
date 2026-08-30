# data/extra/ —— 执行器自由特征旁路（P7 T3）

固定菜单（Alpha158/Alpha360 特征缓存）之外的扩展口：执行器可以自由读取
data/extra/<feature_name>.parquet 并自行 join（必须对齐 (datetime, instrument)
MultiIndex 索引）。

## 约定

- 文件：data/extra/<feature_name>.parquet，MultiIndex (datetime, instrument)，
  数值列若干。
- 声明：执行器用了哪些 extra 特征，必须在 <out>/run_info.json 写
  `"extra_features": ["<feature_name>", ...]`。
- 管线只记录不校验：声明进 contract_report.json 与台账 tag
  `qlab.extra_features`（空不写）；管线不读取、不验证这些 parquet 的内容。
- 仓库管理：*.parquet 被 .gitignore 排除，由 agent 手动放入本机或远端工作
  目录；远端执行时如用到 extra 特征，先自行 scp 到远端 data/extra/。
- 可比性纪律：extra 特征自造自由，但用了它就超出了固定菜单口径，
  记得在 spec.changes 里写明（board 对比只在同口径内进行）。
