# systems_topk

First systems-style portfolio executor. LightGBM makes the score; the cleanroom
implementation under `impl/` normalizes forecasts, sizes by inverse volatility,
constructs complete daily target vectors, and applies schedule/band/feasibility
constraints. `vendor/qstrader` is MIT-licensed source used for the rebalance
schedule and risk-hook interface.
