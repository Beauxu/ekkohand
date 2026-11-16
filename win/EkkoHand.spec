# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：EkkoHand（Windows，onedir）。

在 Windows 上执行：
    pyinstaller --noconfirm --clean EkkoHand.spec
产物：dist/EkkoHand/EkkoHand.exe（整个 dist/EkkoHand 目录一起分发）
"""
try:
    import certifi

    # PyInstaller 的 hook 偶尔收不到 certifi 数据；https 配对全靠它，显式带上。
    _certifi_datas = [(certifi.where(), 'certifi')]
except Exception:
    _certifi_datas = []

a = Analysis(
    ['ekkohand_win.py'],
    pathex=['..'],                      # ../agent.py
    binaries=[],
    datas=[
        ('assets/tray.png', '.'),
        ('assets/EkkoHand.ico', '.'),
    ] + _certifi_datas,
    hiddenimports=[
        'agent', 'certifi',
        'pystray', 'pystray._win32',
        'PIL.ImageGrab', 'PIL.ImageTk', 'PIL._tkinter_finder',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['numpy', 'matplotlib', 'scipy', 'pandas', 'test', 'unittest', 'pydoc'],
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
    icon='assets/EkkoHand.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='EkkoHand',
)
