"""Hub: python -m pipeline {queue|board|backup|kb|review|spec|harness} ..."""
import importlib, sys

MODULES = {"queue": "pipeline.queue", "board": "pipeline.board", "backup": "pipeline.backup",
           "kb": "pipeline.kb", "review": "pipeline.review", "spec": "pipeline.spec",
           "harness": "pipeline.harness"}

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in MODULES:
        print("usage: python -m pipeline {queue|board|backup|kb|review|spec|harness} ...")
        sys.exit(1)
    m = importlib.import_module(MODULES[sys.argv[1]])
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    m.main()

if __name__ == "__main__":
    main()
