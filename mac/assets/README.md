# hand/mac/assets —— 品牌图标（用户设计稿派生产物）

源稿：`工作台项目/tools/icons/app-icon-source.svg`（1254×1254，用户 2026-11-09 提供）
生成：`tools/icons/gen_icons_bbox.js` → `gen_icons_render.js`（node + @resvg/resvg-js + pngjs）

| 文件 | 用途 |
|---|---|
| AppIcon.icns | 打包进 .app（BUNDLE icon），16→1024 全尺寸 PNG-in-ICNS |
| AppIcon-1024.png | 同图位图版，文档/预览用 |
| MenuBar.png / MenuBar@2x.png | 菜单栏模板图标（只取深色 b 字形，32/64px，template=True 自动反色） |

重做方法：
    mkdir -p /tmp/iconwork && cd /tmp/iconwork && npm i @resvg/resvg-js pngjs
    node <repo>/工作台项目/tools/icons/gen_icons_bbox.js <源svg> /tmp/iconwork
    node <repo>/工作台项目/tools/icons/gen_icons_render.js <源svg> /tmp/iconwork
    # 再把 app_*.png 拼成 icns（见 memory 2026-11-16）

注意：菜单栏只保留 b 字形，橙光在 template 模式下会变黑糊成一团，故剔除。
