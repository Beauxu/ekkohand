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
python -m pip install -q pyobjc-framework-Cocoa rumps pyinstaller

rm -rf build dist
pyinstaller --noconfirm --clean EkkoHand.spec

# ad-hoc 签名：固定 identifier，重构建后 TCC 授权不会失效
codesign --force --deep --sign - --identifier cv.backpeng.ekkohand dist/EkkoHand.app

ditto -c -k --keepParent dist/EkkoHand.app dist/EkkoHand-macOS.zip
echo
echo "== 完成"
ls -lh dist/EkkoHand-macOS.zip
echo "把 dist/EkkoHand.app 拖进「应用程序」，双击打开，菜单栏出现 EH 图标即可。"
