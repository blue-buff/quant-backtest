#!/usr/bin/env bash
# qexec.sh —— 脚本文件执行封装（B3：工具链引号税根治）
# 默认【本机直跑】（不加 docker）：cwd = QLAB_ROOT（默认仓库根），参数 argv 直传，
# 全程无内层引号/管道转义（pwsh→docker→sh 的引号吞噬与 CRLF 污染从此绕开）。
# 容器模式仅在显式 QLAB_QEXEC_CONTAINER=1 时启用（Windows 主机等仍有容器的环境）。
# 用法:
#   qexec.sh [-p python3] [-s] <脚本文件> [args...]
#     -p <解释器> 指定解释器（默认按扩展名推断: .py→python, .sh→bash, 其他→sh）
#     -s          用 bash 执行
# 示例:
#   qexec.sh probe.py --batch b1
#   qexec.sh mycheck.sh -x 1
set -euo pipefail
export MSYS_NO_PATHCONV=1
CONTAINER="${QLAB_CONTAINER:-hermes-1679f5b2}"
interp=""
while [ $# -gt 0 ]; do
  case "$1" in
    -p|--python) interp="$2"; shift 2 ;;
    -s|--shell)  interp="bash"; shift ;;
    *) break ;;
  esac
done
f="${1:-}"
[ -n "$f" ] && [ -f "$f" ] || { echo "qexec: 用法 qexec.sh [-p python3|-s] <脚本文件> [args...]"; exit 2; }
shift
if [ -z "$interp" ]; then
  case "${f##*.}" in
    py) interp=python ;;
    sh) interp=bash ;;
    *)  interp=sh ;;
  esac
fi
# 本机直跑为默认；容器模式仅 QLAB_QEXEC_CONTAINER=1 显式开启（且 docker 可达）。
# QLAB_QEXEC_LOCAL=1 仍可强制本机（兼容旧用法）。
if [ "${QLAB_QEXEC_CONTAINER:-0}" != "1" ] || [ "${QLAB_QEXEC_LOCAL:-0}" = "1" ] \
   || ! docker exec -i "$CONTAINER" true 2>/dev/null; then
  root="${QLAB_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
  cd "$root" || exit 2
  exec "$interp" "$f" "$@"
fi
tmp="/tmp/qexec_$RANDOM/$(basename "$f")"
docker exec -i "$CONTAINER" sh -c "mkdir -p ${tmp%/*} && cat > $tmp" < "$f"
rc=0
docker exec -i -w /root/quant "$CONTAINER" "$interp" "$tmp" "$@" || rc=$?
docker exec "$CONTAINER" sh -c "rm -rf ${tmp%/*}" 2>/dev/null || true
exit $rc
