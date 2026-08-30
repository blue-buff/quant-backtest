"""Torch MLP example executor: requirements smoke + first GPU task (P8 7).

Trains a small 2-layer MLP (mse) on the pipeline feature parquet. Uses CUDA
when available (DGX Spark: NVIDIA GB10, sm_120) and reports the device in
run_info.json. Kept deliberately small: this executor proves the requirements
venv + GPU path, it is not a research model.

requirements.txt pins torch (CUDA aarch64 via aliyun mirror).
"""
import argparse, json, time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn


def load(cfg, train_pq, test_pq):
    tr = pd.read_parquet(train_pq)
    te = pd.read_parquet(test_pq)
    lv_tr = tr.index.get_level_values(0)
    trn = tr[(lv_tr >= cfg["train"][0]) & (lv_tr < cfg["train"][1])]
    val = tr[(lv_tr >= cfg["valid"][0]) & (lv_tr < cfg["valid"][1])]
    feats = [c for c in tr.columns if c != "y"]
    X_tr, y_tr = trn[feats].to_numpy(np.float32), trn["y"].to_numpy(np.float32)
    X_va, y_va = val[feats].to_numpy(np.float32), val["y"].to_numpy(np.float32)
    X_te = te[feats].to_numpy(np.float32)
    return X_tr, y_tr, X_va, y_va, X_te, te.index, feats


class MLP(nn.Module):
    def __init__(self, n_in, hidden):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(n_in, hidden), nn.ReLU(),
            nn.Linear(hidden, hidden), nn.ReLU(),
            nn.Linear(hidden, 1))

    def forward(self, x):
        return self.net(x).squeeze(-1)


def main():
    ap = argparse.ArgumentParser(prog="executors/_torch_mlp")
    ap.add_argument("--config", required=True)
    ap.add_argument("--train", required=True)
    ap.add_argument("--test", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cfg = json.loads(Path(a.config).read_text())
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    p = cfg.get("params") or {}
    device = "cuda" if torch.cuda.is_available() else "cpu"
    seed = int((cfg.get("seeds") or [42])[0])
    torch.manual_seed(seed)
    X_tr, y_tr, X_va, y_va, X_te, te_idx, feats = load(cfg, a.train, a.test)
    print("data: train %s valid %s test %s feats=%d device=%s torch=%s" % (
        X_tr.shape, X_va.shape, X_te.shape, len(feats), device, torch.__version__),
        flush=True)
    n_in = X_tr.shape[1]
    hidden = int(p.get("hidden", 64))
    epochs = int(p.get("epochs", 30))
    batch = int(p.get("batch", 4096))
    lr = float(p.get("lr", 1e-3))
    patience = int(p.get("patience", 4))
    # simple standardization from train stats
    mu = np.nanmean(X_tr, axis=0)
    sd = np.nanstd(X_tr, axis=0)
    sd[sd == 0] = 1.0
    X_tr = np.nan_to_num((X_tr - mu) / sd)
    X_va = np.nan_to_num((X_va - mu) / sd)
    X_te = np.nan_to_num((X_te - mu) / sd)
    model = MLP(n_in, hidden).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    lossf = nn.MSELoss()
    X_va_t = torch.from_numpy(X_va).to(device)
    y_va_t = torch.from_numpy(y_va).to(device)
    best_va, best_state, bad = float("inf"), None, 0
    rng = np.random.default_rng(seed)
    for ep in range(epochs):
        model.train()
        perm = rng.permutation(len(X_tr))
        tot = 0.0
        n_b = 0
        for s in range(0, len(X_tr), batch):
            idx = perm[s:s + batch]
            xb = torch.from_numpy(X_tr[idx]).to(device)
            yb = torch.from_numpy(y_tr[idx]).to(device)
            opt.zero_grad()
            loss = lossf(model(xb), yb)
            loss.backward()
            opt.step()
            tot += float(loss) * len(idx)
            n_b += len(idx)
        model.eval()
        with torch.no_grad():
            va = float(lossf(model(X_va_t), y_va_t))
        print("epoch %d/%d train_mse=%.6f valid_mse=%.6f" % (ep + 1, epochs,
              tot / n_b, va), flush=True)
        if va < best_va:
            best_va, bad = va, 0
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        else:
            bad += 1
            if bad >= patience:
                break
    if best_state is not None:
        model.load_state_dict(best_state)
    model.eval()
    scores = []
    with torch.no_grad():
        for s in range(0, len(X_te), 200000):
            scores.append(model(torch.from_numpy(X_te[s:s + 200000]).to(device)).cpu().numpy())
    ens = pd.Series(np.concatenate(scores), index=te_idx, name="score")
    ens.to_frame("score").to_pickle(out / "pred.pkl")
    info = {"seconds": round(time.time() - t0, 1), "device": device,
            "torch": torch.__version__,
            "cuda_available": bool(torch.cuda.is_available()),
            "cuda_device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
            "hidden": hidden, "epochs": ep + 1, "best_valid_mse": float(best_va)}
    (out / "run_info.json").write_text(json.dumps(info, indent=2))
    print(json.dumps({"done": True, "seconds": info["seconds"], "device": device,
                      "best_valid_mse": info["best_valid_mse"]}), flush=True)


if __name__ == "__main__":
    main()
