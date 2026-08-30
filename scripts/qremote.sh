#!/usr/bin/env bash
# qremote.sh —— 主机→DGX Spark 远端 脚本文件执行封装（B3：工具链引号税根治）
# 依赖环境变量: QLAB_SPARK_SSH（必填）/ QLAB_SPARK_SSH_PORT(默认2223) /
#               QLAB_SPARK_JUMP / QLAB_SPARK_WORKDIR(默认/home/dev/quant)
# 用法: qremote.sh <脚本文件> [args...]
#   脚本 scp 到远端 /tmp/qremote_<rand>/，按扩展名推断解释器执行；
#   args 逐个 printf %q 转义后拼进远端命令，杜绝远端二次解析；执行后清理临时文件。
set -euo pipefail
export MSYS_NO_PATHCONV=1
: "${QLAB_SPARK_SSH:?qremote 需要 QLAB_SPARK_SSH 环境变量}"
PORT="${QLAB_SPARK_SSH_PORT:-2223}"
JUMP="${QLAB_SPARK_JUMP:-}"
WORKDIR="${QLAB_SPARK_WORKDIR:-/home/dev/quant}"
f="${1:-}"; shift || true
[ -n "$f" ] && [ -f "$f" ] || { echo "qremote: 用法 qremote.sh <脚本文件> [args...]"; exit 2; }
common=(-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new)
ssh_opts=("${common[@]}" -p "$PORT")
scp_opts=("${common[@]}" -P "$PORT")
[ -n "$JUMP" ] && { ssh_opts+=(-J "$JUMP"); scp_opts+=(-J "$JUMP"); }
remote_dir="/tmp/qremote_$RANDOM"
remote="$remote_dir/$(basename "$f")"
ssh "${ssh_opts[@]}" "$QLAB_SPARK_SSH" "mkdir -p $remote_dir"
scp "${scp_opts[@]}" "$f" "${QLAB_SPARK_SSH}:$remote"
case "${f##*.}" in
  py) interp=python ;;
  sh) interp=bash ;;
  *)  interp=sh ;;
esac
escaped=""
for a in "$@"; do
  escaped="$escaped $(printf %q "$a")"
done
if ssh "${ssh_opts[@]}" "$QLAB_SPARK_SSH" "cd $WORKDIR && $interp $remote$escaped"; then
  rc=0
else
  rc=$?
fi
ssh "${ssh_opts[@]}" "$QLAB_SPARK_SSH" "rm -rf $remote_dir" 2>/dev/null || true
exit $rc
