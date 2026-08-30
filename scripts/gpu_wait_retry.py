"""Wait for the DGX Spark memory to free up, then retry + drain the GPU batch.

The Spark GPU is shared: another tenant can hold the unified memory exclusively
(CUDA OOM / nvidia-smi N/A). This watcher polls the remote free memory and, once
there is enough, requeues the blocked GPU job and drains its batch once.

Usage (in container, with QLAB_SPARK_* env set):
  nohup python scripts/gpu_wait_retry.py --batch b-p8-torch-gpu --min-free-gb 6 \
      > results/queue/gpu_wait.log 2>&1 &
"""
import argparse
import json
import subprocess
import sys
import time

sys.path.insert(0, "/root/quant")


def remote_free_gb():
    from pipeline import remote
    cfg = remote.spark_config()
    if not remote.configured():
        return None
    r = remote._ssh(cfg, "free -m | awk '/Mem:/{print int($7/1024)}'", 60)
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch", required=True)
    ap.add_argument("--min-free-gb", type=int, default=6)
    ap.add_argument("--interval", type=int, default=600)
    ap.add_argument("--max-attempts", type=int, default=120)
    a = ap.parse_args()
    for attempt in range(1, a.max_attempts + 1):
        free = remote_free_gb()
        print("check %d: remote free ~%s GB" % (attempt, free), flush=True)
        if free is not None and free >= a.min_free_gb:
            subprocess.run([sys.executable, "-m", "pipeline.queue", "retry",
                            "--blocked", "--batch", a.batch], check=False)
            # --once now claims ONE wave; loop until the batch has no queued jobs
            guard = 0
            while True:
                r = subprocess.run([sys.executable, "-m", "pipeline.queue", "run",
                                    "--batch", a.batch, "--once", "--concurrency", "1"],
                                   check=False)
                s = subprocess.run([sys.executable, "-m", "pipeline.queue",
                                    "status", "--json"],
                                   capture_output=True, text=True, check=False)
                try:
                    jobs = json.loads(s.stdout)
                except ValueError:
                    jobs = []
                q = [j for j in jobs if j.get("batch_id") == a.batch
                     and j.get("status") == "queued"]
                if not q:
                    break
                guard += 1
                if guard >= 20:
                    print("batch still not drained after 20 waves, giving up", flush=True)
                    return
                time.sleep(5)
            print("dispatched after %d checks (free=%s GB, %d waves)"
                  % (attempt, free, guard + 1), flush=True)
            return
        time.sleep(a.interval)
    print("gave up after %d checks (memory never freed)" % a.max_attempts, flush=True)


if __name__ == "__main__":
    main()
