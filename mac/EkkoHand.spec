# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：EkkoHand.app（菜单栏常驻，无 Dock 图标）。

在 Mac 上执行：
    bash build_app.sh
产物：dist/EkkoHand.app 与 dist/EkkoHand-macOS.zip
"""
try:
    import certifi

    # PyInstaller 的 hook 有时收集不到 certifi 的数据文件，显式带上，
    # 否则 macOS 上 https 会 CERTIFICATE_VERIFY_FAILED（配对必然失败）。
    _certifi_datas = [(certifi.where(), 'certifi')]
except Exception:
    _certifi_datas = []

a = Analysis(
    ['ekkohand_app.py'],
    pathex=['..'],          # ../agent.py
    binaries=[],
    datas=[('assets/MenuBar.png', '.'), ('assets/MenuBar@2x.png', '.')] + _certifi_datas,
    hiddenimports=['agent', 'rumps', 'Quartz', 'AppKit', 'Foundation', 'certifi'],
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
    icon='assets/AppIcon.icns',
    bundle_identifier='cv.backpeng.ekkohand',
    info_plist={
        'CFBundleName': 'EkkoHand',
        'CFBundleDisplayName': 'EkkoHand',
        'CFBundleShortVersionString': '1.0.4',
        'CFBundleVersion': '4',
        'LSUIElement': True,
        'LSMinimumSystemVersion': '11.0',
        'NSHighResolutionCapable': True,
        'NSAppleEventsUsageDescription': 'EkkoHand 需要通过系统事件操作其他应用。',
    },
)
