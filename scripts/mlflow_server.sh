#!/bin/sh
# QLab MLflow UI server (on-demand, for human browser only).
# Agent/ledger operations use sqlite direct access and do NOT need this.
# NOTE: MLflow 3.x server startup is slow (~40s) and spawns background workers
# (~2GB RAM). Start it only when the human wants the UI; stop it afterwards.
# Usage: mlflow_server.sh start|stop|status|heal
set -u
ROOT=/root/quant
DIR=$ROOT/mlflow-server
PIDFILE=$DIR/server.pid
LOG=$DIR/server.log
DB=sqlite:////root/quant/mlflow-server/mlflow.db
PORT=5000

mkdir -p $DIR

health() {
  python - $PORT <<'PYEOF'
import sys, urllib.request
try:
    with urllib.request.urlopen('http://127.0.0.1:' + sys.argv[1] + '/health', timeout=3) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PYEOF
}

case "$1" in
start)
  if health; then
    echo "already healthy"; exit 0
  fi
  if [ -f $PIDFILE ] && kill -0 $(cat $PIDFILE) 2>/dev/null; then
    echo "process alive but not healthy yet; waiting..."
  else
    setsid nohup mlflow ui --backend-store-uri $DB \
      --artifacts-destination file://$DIR/artifacts \
      --host 127.0.0.1 --port $PORT >> $LOG 2>&1 &
    echo $! > $PIDFILE
  fi
  i=0
  while [ $i -lt 30 ]; do
    if health; then echo "ready (pid $(cat $PIDFILE))"; exit 0; fi
    sleep 2
    i=$((i + 1))
  done
  echo "FAILED after 60s, tail of $LOG:"; tail -5 $LOG; exit 1
  ;;
stop)
  if [ -f $PIDFILE ] && kill -0 $(cat $PIDFILE) 2>/dev/null; then
    kill -- -$(cat $PIDFILE) 2>/dev/null
    sleep 1
    kill -9 -- -$(cat $PIDFILE) 2>/dev/null
    rm -f $PIDFILE
    echo stopped
  else
    echo "not running"; rm -f $PIDFILE
  fi
  ;;
status)
  if health; then
    echo healthy
  elif [ -f $PIDFILE ] && kill -0 $(cat $PIDFILE) 2>/dev/null; then
    echo "pid $(cat $PIDFILE) alive, not healthy yet"
  else
    echo down
  fi
  ;;
heal)
  if ! health; then
    echo "unhealthy, starting..."; $0 start
  else
    echo healthy
  fi
  ;;
*)
  echo "usage: $0 start|stop|status|heal"; exit 1
  ;;
esac
