# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：EkkoHand.app（菜单栏常驻，无 Dock 图标）。

在 Mac 上执行：
    bash build_app.sh
产物：dist/EkkoHand.app 与 dist/EkkoHand-macOS.zip
"""
a = Analysis(
    ['ekkohand_app.py'],
    pathex=['..'],          # ../agent.py
    binaries=[],
    datas=[],
    hiddenimports=['agent', 'rumps'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='EkkoHand',
    debug=False,
    strip=False,
    upx=False,
    console=False,
    target_arch='arm64',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='EkkoHand',
)
app = BUNDLE(
    coll,
    name='EkkoHand.app',
    icon=None,
    bundle_identifier='cv.backpeng.ekkohand',
    info_plist={
        'CFBundleName': 'EkkoHand',
        'CFBundleDisplayName': 'EkkoHand',
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion': '1',
        'LSUIElement': True,
        'LSMinimumSystemVersion': '11.0',
        'NSHighResolutionCapable': True,
        'NSAppleEventsUsageDescription': 'EkkoHand 需要通过系统事件操作其他应用。',
    },
)
