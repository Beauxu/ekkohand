# EkkoHand

macOS 菜单栏客户端 —— 让 Ekko 能把你这台 Mac 当「手」。

这个仓库只负责**打包**：公开仓库的 GitHub-hosted macOS runner 免费，
构建产物直接发在 [Releases](../../releases)。

## 下载安装

1. 打开 [Releases](../../releases)，下载 `EkkoHand-macOS.zip`
2. 解压，把 `EkkoHand.app` 拖进「应用程序」
3. 双击打开 → 菜单栏出现 `EH`
4. 在工作台 https://work.backpeng.com 「设备」页生成 6 位配对码，粘进去
5. 首次会弹「屏幕录制」授权，勾上后**完全退出再打开**

## 自己构建

```bash
bash mac/build_app.sh
```

产物：`mac/dist/EkkoHand.app`、`mac/dist/EkkoHand-macOS.zip`

## 说明

- 客户端不内置任何 AI，只执行服务器下发的截图任务
- 设备令牌存 `~/.ekkohand/device.json`（0600），服务器只存哈希
- 设备指纹用 `machine-id / IOPlatformUUID`，改名不产生新设备
- 默认连 `https://work.backpeng.com/hand`，可用 `HAND_HUB_URL` 覆盖
