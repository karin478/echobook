#!/bin/bash
# Echobook启动器：确认后台服务在跑（没跑就用 launchd 拉起来），再用 Chrome 打开无地址栏的独立窗口。
# 环境变量只给测试用：KOKORO_URL / KOKORO_CHROME / KOKORO_LAUNCHCTL / KOKORO_OSASCRIPT / KOKORO_WAIT_STEPS
URL="${KOKORO_URL:-http://127.0.0.1:7788/}"
CHROME="${KOKORO_CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
LAUNCHCTL="${KOKORO_LAUNCHCTL:-launchctl}"
OSASCRIPT="${KOKORO_OSASCRIPT:-osascript}"
WAIT_STEPS="${KOKORO_WAIT_STEPS:-40}"   # 每步 0.5 秒
LABEL="com.kokoro-reader"
DOMAIN="gui/$(id -u)"

up() { curl -fs -o /dev/null --max-time 2 "${URL}api/books"; }

if ! up; then
  "$LAUNCHCTL" kickstart "$DOMAIN/$LABEL" >/dev/null 2>&1 \
    || "$LAUNCHCTL" bootstrap "$DOMAIN" "$HOME/Library/LaunchAgents/$LABEL.plist" >/dev/null 2>&1
  for _ in $(seq 1 "$WAIT_STEPS"); do up && break; sleep 0.5; done
fi

if ! up; then
  "$OSASCRIPT" -e 'display alert "Echobook没能启动" message "后台服务没有响应。日志在 ~/Library/Logs/kokoro-reader.log"' >/dev/null 2>&1
  exit 1
fi

# 直接调 Chrome 主程序：Chrome 已在运行时会把 --app 交给现有进程，开一个独立窗口
"$CHROME" --app="$URL" >/dev/null 2>&1 &
disown
