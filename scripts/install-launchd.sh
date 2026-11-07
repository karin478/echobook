#!/bin/bash
# Echobook 开机自启。
#   scripts/install-launchd.sh            安装并立即启动（登录后自动运行）
#   scripts/install-launchd.sh uninstall  停止并卸载（plist 移到 ~/Library/LaunchAgents.disabled/）
set -euo pipefail

LABEL="com.kokoro-reader"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO/launchd/$LABEL.plist"
AGENTS="$HOME/Library/LaunchAgents"
DISABLED="$HOME/Library/LaunchAgents.disabled"
TARGET="$AGENTS/$LABEL.plist"
DOMAIN="gui/$(id -u)"
LAUNCHCTL="${KOKORO_LAUNCHCTL:-launchctl}"     # 只给测试用
RETRY_SLEEP="${KOKORO_RETRY_SLEEP:-0.5}"

# bootout 是异步的：旧服务还在收尾时 bootstrap 会报 "Bootstrap failed: 5"，所以先等它真正消失
wait_gone() {
  for _ in $(seq 1 40); do
    "$LAUNCHCTL" print "$DOMAIN/$LABEL" >/dev/null 2>&1 || return 0
    sleep "$RETRY_SLEEP"
  done
}

bootstrap_with_retry() {
  for _ in 1 2 3 4 5; do
    "$LAUNCHCTL" bootstrap "$DOMAIN" "$TARGET" 2>/dev/null && return 0
    sleep "$RETRY_SLEEP"
  done
  "$LAUNCHCTL" bootstrap "$DOMAIN" "$TARGET"   # 最后一次把错误打出来
}

case "${1:-install}" in
  install)
    [ -x "$REPO/.venv/bin/python" ] || { echo "缺少 $REPO/.venv，请先按 README 安装依赖" >&2; exit 1; }
    mkdir -p "$AGENTS" "$HOME/Library/Logs"
    "$LAUNCHCTL" bootout "$DOMAIN/$LABEL" 2>/dev/null || true   # 重装：先停掉旧的
    wait_gone
    sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" "$TEMPLATE" > "$TARGET"
    plutil -lint "$TARGET" >/dev/null
    bootstrap_with_retry
    echo "已安装并启动：http://127.0.0.1:7788 （日志：$HOME/Library/Logs/kokoro-reader.log）"
    ;;
  uninstall)
    "$LAUNCHCTL" bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    if [ -f "$TARGET" ]; then
      mkdir -p "$DISABLED"
      mv "$TARGET" "$DISABLED/$LABEL.plist"
      echo "已停止并卸载；plist 移到 $DISABLED/"
    else
      echo "未安装（$TARGET 不存在）"
    fi
    ;;
  *)
    echo "用法：$0 [install|uninstall]" >&2
    exit 2
    ;;
esac
