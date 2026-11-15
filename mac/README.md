# EkkoHand（macOS 菜单栏 App）

把 [../agent.py](../agent.py) 包成常驻应用，让 Ekko 能用你这台 Mac 当「手」。

## 你看到的

- 菜单栏一个 `EH` 图标，无 Dock 图标
- 点开：状态、设备名、输入配对码、自检截图、连接诊断、两项系统授权入口、开机启动

## 构建（必须在 Mac 上）

Linux 容器无法交叉打包 `arm64` 的 `.app`，需要一台 Mac（你的 M2 即可）：

```bash
cd hand/mac
bash build_app.sh          # 首次会自动建 venv 并装 pyobjc / rumps / pyinstaller
```

产物：
- `dist/EkkoHand.app`
- `dist/EkkoHand-macOS.zip`（可分发，内部已 ad-hoc 签名）

## 安装与配对

1. 把 `EkkoHand.app` 拖进「应用程序」
2. 双击打开 → 菜单栏出现 `EH`
3. 首次会直接弹配对窗口；没弹就点图标 →「输入配对码…」
4. 在工作台 https://work.backpeng.com 「设备」页生成 6 位码，粘进去
5. 连接成功后建议点「开机启动」，并在两项系统授权里勾上 EkkoHand

## 已知限制

- **屏幕录制权限必须手动勾**（系统设置 → 隐私与安全性 → 屏幕录制），
  勾完要完全退出 app 再打开才生效。这是 macOS TCC 的限制，绕不过。
- 辅助功能权限同理，只有未来做「输入/点击」时才需要。
- 默认连 `https://work.backpeng.com/hand`，可用环境变量 `HAND_HUB_URL` 覆盖。
- 设备身份用 `machine-id / IOPlatformUUID` 做指纹，改名或换主机名不会产生新设备。

## 开发时直接跑（不打包）

```bash
cd hand/mac
python3 -m pip install pyobjc-framework-Cocoa rumps
python3 ekkohand_app.py
```
