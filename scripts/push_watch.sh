#!/bin/bash
# GitHub push 自动守望：Steam++/网络恢复后自动把容器仓库 HEAD 推到 GitHub。
# 用法（主机 Git Bash）：nohup bash scripts/push_watch.sh >/dev/null 2>&1 &
STAGE="/d/quant_backup/push_stage.git"
STAGE_WIN="D:/quant_backup/push_stage.git"
LOG="/d/quant_backup/push_watch.log"
MAX=60
INTERVAL=300
TOKEN=$(MSYS_NO_PATHCONV=1 docker exec -i hermes-1679f5b2 cat /root/quant/.qlab_github_token 2>/dev/null | tr -d '\r\n')
if [ -z "$TOKEN" ]; then
  echo "$(date) FATAL: no token in container" >> "$LOG"
  exit 1
fi
for i in $(seq 1 $MAX); do
  rm -rf "$STAGE"
  MSYS_NO_PATHCONV=1 docker cp hermes-1679f5b2:/root/quant/.git "$STAGE_WIN" 2>/dev/null
  git --git-dir="$STAGE_WIN" remote remove origin 2>/dev/null
  git --git-dir="$STAGE_WIN" remote add origin "https://oauth2:${TOKEN}@github.com/blue-buff/quant-backtest.git" 2>/dev/null
  OUT=$(git --git-dir="$STAGE_WIN" push origin HEAD:main 2>&1)
  RC=$?
  echo "$(date) attempt $i rc=$RC lastline=$(echo "$OUT" | tail -1)" >> "$LOG"
  if [ $RC -eq 0 ]; then
    echo "$(date) PUSH OK" >> "$LOG"
    exit 0
  fi
  sleep $INTERVAL
done
echo "$(date) gave up after $MAX attempts" >> "$LOG"
