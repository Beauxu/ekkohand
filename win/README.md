# EkkoHand · Windows

把这台 Windows 电脑交给工作台当手：截屏、点击、拖拽、滚动、打字、按快捷键、
读写文件、跑命令、列出并打开已安装的应用。

## 用户怎么用

1. 解压 `EkkoHand-win64.zip`，双击 `EkkoHand.exe`（首次会有 SmartScreen 提示：
   「更多信息」→「仍要运行」；程序没有买代码签名证书，仅此而已）。
2. 托盘出现图标，面板里输入工作台「设备」页生成的 6 位配对码。
3. 回工作台确认设备在线，然后在 Ekko 会话里让它截图 —— 每次操作都要你在会话里批准。

关掉面板 = 缩到托盘，不断线。托盘菜单「退出」才真退出。
`开机启动` 勾选后写 `HKCU\...\Run`，不需要管理员权限。

## 和 macOS 版的差别

同一份 `agent.py` 协议，配对码、令牌、`/poll`、`/result` 完全一致。

| 能力 | Windows 实现 |
| --- | --- |
| 截图 | Pillow `ImageGrab.grab(all_screens=True)`，多屏拼成一张虚拟桌面 |
| 鼠标 | `user32.SendInput`，坐标按虚拟桌面归一化（多屏不错位） |
| 键盘 | `KEYEVENTF_UNICODE`，中文 / emoji 直接输入；组合键走 VK 码 |
| 应用 | `Get-StartApps` 列开始菜单应用，`Start-Process shell:AppsFolder\<AppID>` 启动 |
| 命令 | 默认 `cmd.exe /c`（`chcp 65001` 防中文乱码），可传 `shell=powershell` |

Windows 没有 macOS 的 TCC 授权，装好就能用。唯一限制：**目标程序以管理员身份运行时，
EkkoHand 也必须以管理员身份运行**，否则 `SendInput` 会被 UIPI 拦掉（报错会明确说明）。

## 开发与打包

```powershell
# 源码直跑（需要 Python 3.11 + pillow + pystray）
python ekkohand_win.py

# 出包
powershell -ExecutionPolicy Bypass -File build.ps1
```

CI：推送 `win/**` 或 `agent.py` 到 `Beauxu/ekkohand`，`.github/workflows/build-windows.yml`
在 `windows-latest` 上出 zip，打 tag `ekkohand-win-v*` 时挂到 Release。

## 安全

- 令牌只存本机 `%LOCALAPPDATA%\Backon\device.json`，服务器只存哈希。
- 不做浏览器插件、不常驻系统服务、不加计划任务；只有你勾的「开机启动」一个自启项。
- 所有动作仍由 Ekko 会话审批把关；hand 通道本身禁止 kill Ekko / hand_hub。
