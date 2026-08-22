"""Remote runner (DGX Spark docker) -- v1 placeholder (user-approved: SSH blank).

The executor architecture already isolates compute into a subprocess; remote
dispatch reuses the SAME contract on another machine. Transport is intentionally
UNCONFIGURED until the DGX Spark details arrive:

  QLAB_SPARK_SSH      user@host            (blank = not configured)
  QLAB_SPARK_WORKDIR  remote working dir   (default /root/quant-spark)
  QLAB_SPARK_IMAGE    remote docker image  (default qlab:latest)

Planned flow (P6, once QLAB_SPARK_SSH is set):
  1. rsync the committed repo tree to <workdir> on the remote
  2. ssh 'docker exec <image> python -m pipeline.harness run <spec> --compute-only'
  3. rsync results/runs/<exp_id>/ back; the local harness imports the ledger row
     (remote computes, local ledger keeps the single-writer sqlite invariant)

dispatch(row) is called by queue._execute for runner="spark"; while unconfigured
it returns blocked=True and the queue records the job as blocked.
"""
import os


def spark_config():
    return {"ssh": os.environ.get("QLAB_SPARK_SSH", "").strip(),
            "workdir": os.environ.get("QLAB_SPARK_WORKDIR", "/root/quant-spark").strip(),
            "image": os.environ.get("QLAB_SPARK_IMAGE", "qlab:latest").strip()}


def configured():
    return bool(spark_config()["ssh"])


def dispatch(row):
    """Attempt remote dispatch of one job row. v1: transport blank -> blocked."""
    if not configured():
        return {"ok": False, "blocked": True,
                "reason": "spark runner not configured: QLAB_SPARK_SSH is blank (v1 placeholder)"}
    raise NotImplementedError("spark transport lands in P6 once machine details arrive")


def main():
    import json
    print(json.dumps({"configured": configured(), "config": spark_config()}))


if __name__ == "__main__":
    main()
