#!/usr/bin/env bash
# 在 Mac 上构建 EkkoHand.app（必须在 macOS + arm64 上执行，Linux 无法交叉打包）
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
echo "== Python: $($PY -V)"

$PY -m venv .venv 2>/dev/null || true
# shellcheck disable=SC1091
. .venv/bin/activate

python -m pip install -q --upgrade pip
# Quartz/AppKit 用于鼠标与键盘注入（input.click / input.type / input.key）；
# 缺了它们 app 仍能跑，只是 hand.input_* 会回报「没有输入注入能力」。
python -m pip install -q pyobjc-framework-Cocoa pyobjc-framework-Quartz rumps pyinstaller

test -f assets/AppIcon.icns || { echo "缺少 assets/AppIcon.icns"; exit 1; }
test -f assets/MenuBar.png || { echo "缺少 assets/MenuBar.png"; exit 1; }

rm -rf build dist
pyinstaller --noconfirm --clean EkkoHand.spec

# ad-hoc 签名：固定 identifier，重构建后 TCC 授权不会失效
codesign --force --deep --sign - --identifier cv.backpeng.ekkohand dist/EkkoHand.app

ditto -c -k --keepParent dist/EkkoHand.app dist/EkkoHand-macOS.zip
echo
echo "== 完成"
ls -lh dist/EkkoHand-macOS.zip
echo "把 dist/EkkoHand.app 拖进「应用程序」，双击打开，菜单栏出现 b 字形图标即可。"
