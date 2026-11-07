#!/bin/bash
# 生成「Echobook.app」：双击确认后台服务在跑，然后用 Chrome 打开独立窗口。
#   scripts/build-mac-app.sh            装到 ~/Applications
#   scripts/build-mac-app.sh <目录>      装到指定目录
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$HOME/Applications}"
APP="$DEST/Echobook.app"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 图标：1024 PNG → 各尺寸 → .icns
swift "$REPO/macapp/make_icon.swift" "$WORK/icon.png"
mkdir "$WORK/icon.iconset"
for s in 16 32 128 256 512; do
  sips -z $s $s "$WORK/icon.png" --out "$WORK/icon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$WORK/icon.png" --out "$WORK/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$WORK/icon.iconset" -o "$WORK/AppIcon.icns"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$REPO/macapp/launcher.sh" "$APP/Contents/MacOS/kokoro-launcher"
chmod +x "$APP/Contents/MacOS/kokoro-launcher"
cp "$WORK/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Echobook</string>
  <key>CFBundleDisplayName</key><string>Echobook</string>
  <key>CFBundleIdentifier</key><string>com.kokoro-reader.launcher</string>
  <key>CFBundleExecutable</key><string>kokoro-launcher</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
</dict>
</plist>
EOF
plutil -lint "$APP/Contents/Info.plist" >/dev/null
touch "$APP"   # 让 Finder 刷新图标
echo "已生成：$APP"
