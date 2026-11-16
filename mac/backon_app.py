#!/usr/bin/env python3
"""Backon 菜单栏 App —— 把 agent.py 包成 macOS 常驻应用。

设计约束
- 无 Dock 图标（LSUIElement），只在菜单栏有一个状态项
- 不内置任何 AI：AI 在服务器，这里只是「手」
- 不弹任何自建窗口做常驻界面，配对/诊断只用系统对话框
- 退出必须由用户在菜单里点，避免被误杀后静默失联

首次使用
1. 在 https://work.backpeng.com 的「设备」页生成 6 位配对码
2. 菜单栏图标 → 输入配对码… → 粘贴 → 自动连接
3. 系统会弹「屏幕录制」授权，勾上本程序（否则截图只有壁纸）
"""
from __future__ import annotations

import os
import platform
import plistlib
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HAND_DIR = os.path.dirname(HERE)
if HAND_DIR not in sys.path:
    sys.path.insert(0, HAND_DIR)

import agent  # noqa: E402  hand/agent.py

import rumps  # noqa: E402

def _res(name: str) -> str:
    """打包后在 _MEIPASS 根目录；开发态在 assets/ 下。"""
    base = getattr(sys, "_MEIPASS", None) or os.path.join(HERE, "assets")
    for cand in (os.path.join(base, name), os.path.join(HERE, "assets", name)):
        if os.path.exists(cand):
            return cand
    return ""


MENUBAR_ICON = _res("MenuBar.png")

DEFAULT_HUB = os.environ.get("HAND_HUB_URL", "http://47.88.53.79/hand")
LOGIN_AGENT = os.path.expanduser("~/Library/LaunchAgents/cv.backpeng.backon.plist")
BUNDLE_ID = "cv.backpeng.backon"
APP_VERSION = "1.1.4"
LEGACY_AGENT_PLIST = os.path.expanduser("~/Library/LaunchAgents/cv.backpeng.ekkohand.plist")


def open_privacy(pane: str) -> None:
    subprocess.run(
        ["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"],
        check=False,
    )


# ── 权限探测 ──────────────────────────────────────────────────
# macOS 的屏幕录制授权跟「可执行文件的签名」绑定：ad-hoc 重签一次，旧勾选就作废，
# 系统设置里那一行却还打着勾 —— 于是「我明明给了权限」和「就是截不到」同时成立。
# 所以这里不信界面，直接问系统要答案。
def _quartz():
    try:
        import Quartz  # noqa: PLC0415  PyObjC
        return Quartz
    except Exception:  # noqa: BLE001
        return None


def screen_capture_allowed() -> bool | None:
    """None 表示问不出来（非 macOS / API 缺失）。"""
    q = _quartz()
    fn = getattr(q, "CGPreflightScreenCaptureAccess", None) if q else None
    if fn is None:
        return None
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001
        return None


def request_screen_capture() -> bool | None:
    """触发系统授权弹窗（把本程序加进屏幕录制列表）。"""
    q = _quartz()
    fn = getattr(q, "CGRequestScreenCaptureAccess", None) if q else None
    if fn is None:
        return None
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001
        return None


def accessibility_allowed() -> bool | None:
    q = _quartz()
    fn = getattr(q, "AXIsProcessTrusted", None) if q else None
    if fn is None:
        return None
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001
        return None


def request_accessibility() -> bool | None:
    """向系统「请求」辅助功能授权。

    只调 AXIsProcessTrusted() 是纯查询，不会让条目出现在列表里；
    系统要看到 prompt 选项才把当前这个二进制登记进去。之前只打开了设置页，
    用户在列表里勾到的是旧版本 Backon —— 所以「勾了却不生效」。
    """
    q = _quartz()
    fn = getattr(q, "AXIsProcessTrustedWithOptions", None) if q else None
    key = getattr(q, "kAXTrustedCheckOptionPrompt", None) if q else None
    if fn is None or key is None:
        try:
            import ApplicationServices as _as  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return None
        fn = getattr(_as, "AXIsProcessTrustedWithOptions", None)
        key = getattr(_as, "kAXTrustedCheckOptionPrompt", None)
    if fn is None or key is None:
        return None
    try:
        return bool(fn({key: True}))
    except Exception:  # noqa: BLE001
        return None


def sign_identity() -> str:
    """自己签出来的是什么（ad-hoc / 证书名），排查权限问题时很关键。"""
    try:
        out = subprocess.run(["codesign", "-dvv", sys.executable],
                             capture_output=True, text=True, timeout=5).stderr
    except Exception:  # noqa: BLE001
        return "未知"
    for line in out.splitlines():
        if line.startswith("Authority="):
            return line.split("=", 1)[1]
    return "ad-hoc"


def probe_screen_capture() -> tuple[bool, str]:
    """真拍一张，返回 (是否成功, 错误)。比 preflight 更硬。"""
    try:
        raw = agent.grab_png()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc).strip() or type(exc).__name__
    return bool(raw), ""



# ── 权限引导窗（拖拽授权，对齐豆包/Code 的交互）─────────────────
# 实测痛点：用户在「辅助功能/屏幕录制」列表里找不到正确的条目、勾错副本、
# 或勾完忘了重启。豆包/Code 的做法是弹一个浮窗，里面放可拖拽的 app 图标，
# 拖进系统设置的列表区域 = 自动添加并勾选，比手动翻列表可靠得多。
# 窗口底部带一行实时诊断（权限原始值/Quartz 是否加载/签名身份），
# 授权一旦生效立刻变 ✅ 并给出「重启」按钮。
#
# 注意：PyObjC 的类一旦定义就会注册进 Objective-C runtime，同名类
# 注册第二次直接报「overriding existing Objective-C class」。
# 所以下面两个类必须在模块顶层，绝不能放进函数里（v1.1.3 的教训）。
try:
    from AppKit import NSImageView as _NSImageView  # noqa: PLC0415
    from Foundation import NSObject as _NSObject  # noqa: PLC0415
except Exception:  # noqa: BLE001
    _NSImageView = object
    _NSObject = object


class _GuideDragIcon(_NSImageView):
    """可拖拽的 app 图标。"""

    def mouseDown_(self, event):
        from AppKit import NSDraggingItem  # noqa: PLC0415
        from Foundation import NSURL  # noqa: PLC0415
        url = NSURL.fileURLWithPath_(self.app_path)
        item = NSDraggingItem.alloc().initWithPasteboardWriter_(url)
        item.setDraggingFrame_contents_(self.bounds(), self.image())
        self.beginDraggingSessionWithItems_event_source_([item], event, self)

    def draggingSourceOperationMaskForLocal_(self, _local):
        return 1  # copy


class _GuideCtrl(_NSObject):
    def openPane_(self, _sender):
        open_privacy(self.pane)

    def restart_(self, _sender):
        argv = ([sys.executable] if getattr(sys, "frozen", False)
                else [sys.executable, os.path.join(HERE, "backon_app.py")])
        subprocess.Popen(argv, close_fds=True, start_new_session=True)
        rumps.quit_application()

    def tick_(self, _timer):
        val = self.check()
        mark = {True: "✅ 已授权", False: "❌ 未授权", None: "？问不出来"}[val]
        qtxt = "已加载" if _quartz() is not None else "缺失（打包问题，截图这行发我）"
        self.status.setStringValue_(
            f"{self.title}：{mark}    Quartz 模块：{qtxt}\n"
            f"版本 {APP_VERSION}    签名：{sign_identity()}\n"
            f"授权对象：{self.app_path}"
        )
        if val is True and not self.granted:
            self.granted = True
            self.rbtn.setHidden_(False)
            self.hint.setStringValue_("✅ 已检测到授权！点「重启 Backon」生效。")
            rumps.notification("Backon", f"{self.title}已授权", "点引导窗里的「重启 Backon」完成")

    def windowWillClose_(self, _note):
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None
        for obj in (self.panel, self):
            if obj in _GUIDE_KEEP:
                _GUIDE_KEEP.remove(obj)


_GUIDE_KEEP = []


def _app_path() -> str:
    """当前运行中的 .app 路径（拖的就是它，保证授权对象 = 运行对象）。"""
    if getattr(sys, "frozen", False):
        # …/Backon.app/Contents/MacOS/Backon → …/Backon.app
        return os.path.abspath(os.path.join(sys.executable, "..", "..", ".."))
    return os.path.abspath(HERE)


def open_perm_guide(kind: str) -> None:
    try:
        _open_perm_guide(kind)
    except Exception as exc:  # noqa: BLE001
        rumps.alert(
            "引导窗打不开",
            f"{type(exc).__name__}: {exc}\n\n"
            "可以改用菜单里的「打开屏幕录制/辅助功能设置…」手动授权。",
        )


def _open_perm_guide(kind: str) -> None:
    from AppKit import (  # noqa: PLC0415
        NSApp, NSBackingStoreBuffered, NSButton, NSColor, NSFont,
        NSMakeRect, NSPanel, NSTextField, NSWindowStyleMaskClosable,
        NSWindowStyleMaskTitled, NSWorkspace,
    )
    from Foundation import NSTimer  # noqa: PLC0415

    is_ax = kind == "ax"
    title = "辅助功能" if is_ax else "屏幕录制"
    pane = "Privacy_Accessibility" if is_ax else "Privacy_ScreenCapture"
    check = accessibility_allowed if is_ax else screen_capture_allowed
    app_path = _app_path()

    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, 560, 320),
        NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
        NSBackingStoreBuffered, False)
    panel.setTitle_(f"给 Backon 授权：{title}")
    panel.setFloatingPanel_(True)
    panel.setLevel_(5)
    panel.setReleasedWhenClosed_(False)
    panel.center()
    v = panel.contentView()

    icon = _GuideDragIcon.alloc().initWithFrame_(NSMakeRect(28, 150, 128, 128))
    icon.app_path = app_path
    img = NSWorkspace.sharedWorkspace().iconForFile_(app_path)
    img.setSize_((128, 128))
    icon.setImage_(img)
    v.addSubview_(icon)

    def label(frame, text, size=13, bold=False):
        tf = NSTextField.alloc().initWithFrame_(NSMakeRect(*frame))
        tf.setStringValue_(text)
        tf.setBezeled_(False)
        tf.setDrawsBackground_(False)
        tf.setEditable_(False)
        tf.setSelectable_(True)
        tf.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
        v.addSubview_(tf)
        return tf

    label((184, 258, 360, 22), f"把左边的 Backon 图标拖进「{title}」列表", 14, True)
    label((184, 232, 360, 18), "① 点「打开设置页」（已自动打开）", 12)
    label((184, 210, 360, 18), "② 把图标拖进列表区域 —— 会自动添加并勾选", 12)
    label((184, 188, 360, 18), "③ 下面变 ✅ 后点「重启 Backon」", 12)
    hint = label((184, 158, 360, 18), "正在监听授权状态…", 12)

    btn = NSButton.alloc().initWithFrame_(NSMakeRect(184, 116, 150, 30))
    btn.setTitle_("打开设置页")
    btn.setBezelStyle_(1)

    rbtn = NSButton.alloc().initWithFrame_(NSMakeRect(348, 116, 150, 30))
    rbtn.setTitle_("重启 Backon")
    rbtn.setBezelStyle_(1)
    rbtn.setHidden_(True)

    status = label((28, 28, 520, 78), "状态读取中…", 11)
    status.setTextColor_(NSColor.secondaryLabelColor())

    ctrl = _GuideCtrl.alloc().init()
    ctrl.pane = pane
    ctrl.check = check
    ctrl.title = title
    ctrl.app_path = app_path
    ctrl.status = status
    ctrl.rbtn = rbtn
    ctrl.hint = hint
    ctrl.panel = panel
    ctrl.granted = False
    ctrl.timer = None
    btn.setTarget_(ctrl)
    btn.setAction_("openPane:")
    rbtn.setTarget_(ctrl)
    rbtn.setAction_("restart:")
    v.addSubview_(btn)
    v.addSubview_(rbtn)
    panel.setDelegate_(ctrl)

    _GUIDE_KEEP.append(ctrl)
    _GUIDE_KEEP.append(panel)

    NSApp.activateWithOptions_(2)  # NSApplicationActivateIgnoringOtherApps
    panel.makeKeyAndOrderFront_(None)
    open_privacy(pane)
    if is_ax:
        request_accessibility()
    else:
        request_screen_capture()
    ctrl.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        1.0, ctrl, "tick:", None, True)
    ctrl.tick_(None)


class BackonApp(rumps.App):
    def __init__(self) -> None:
        super().__init__(
            "Backon",
            title="",
            icon=MENUBAR_ICON or None,
            template=bool(MENUBAR_ICON),
            quit_button="退出",
        )
        self.state = agent.load_state()
        # 未配对时钉死当前默认入口（IP 直连），已配对的保留原地址
        if self.state.get("token"):
            self.hub = (self.state.get("hub") or DEFAULT_HUB).rstrip("/")
        else:
            self.hub = DEFAULT_HUB.rstrip("/")
        self.connected = False
        self.last_seen = 0.0
        self.busy = False
        self.pairing = False
        self.err = ""

        self.status_item = rumps.MenuItem("○ 未配对")
        self.device_item = rumps.MenuItem("设备：—")
        self.perm_screen_item = rumps.MenuItem("屏幕录制：检测中…", callback=self.act_perm_screen)
        self.perm_ax_item = rumps.MenuItem("辅助功能：检测中…", callback=self.act_perm_ax)
        self.menu = [
            self.status_item,
            self.device_item,
            None,
            self.perm_screen_item,
            self.perm_ax_item,
            rumps.MenuItem("重启 Backon（授权后必做）", callback=self.act_restart),
            None,
            rumps.MenuItem("输入配对码…", callback=self.act_pair),
            rumps.MenuItem("自检截图", callback=self.act_selftest),
            rumps.MenuItem("连接诊断", callback=self.act_diag),
            None,
            rumps.MenuItem("打开屏幕录制设置…", callback=lambda _: open_privacy("Privacy_ScreenCapture")),
            rumps.MenuItem("打开辅助功能设置…", callback=lambda _: open_privacy("Privacy_Accessibility")),
            rumps.MenuItem("重置并重新请求辅助功能", callback=self.act_fix_ax),
            None,
            rumps.MenuItem("开机启动", callback=self.act_login),
            None,
        ]
        self.refresh_perms()
        self.refresh()
        threading.Thread(target=self.poll_loop, daemon=True).start()
        rumps.Timer(self.refresh, 10).start()

    # ── 权限显示 ──────────────────────────────────────────────
    def refresh_perms(self, _timer=None) -> None:
        screen = screen_capture_allowed()
        if screen is True:
            self.perm_screen_item.title = "✅ 屏幕录制：已生效"
        elif screen is False:
            self.perm_screen_item.title = "⚠️ 屏幕录制：没生效（点这里查原因）"
        else:
            self.perm_screen_item.title = "？ 屏幕录制：问不出来（点这里实测）"
        ax = accessibility_allowed()
        if ax is True:
            self.perm_ax_item.title = "✅ 辅助功能：已生效"
        elif ax is False:
            self.perm_ax_item.title = "⚠️ 辅助功能：没生效（点这里请求授权）"
        else:
            self.perm_ax_item.title = "？ 辅助功能：问不出来"

    def act_perm_screen(self, _sender) -> None:
        """点一下实测：拍得出来就是真能用，拍不出来给出确切原因。"""
        ok, err = probe_screen_capture()
        pre = screen_capture_allowed()
        if ok:
            self.refresh_perms()
            rumps.alert(
                "屏幕录制：可用",
                "实测截图成功，权限是好的。\n\n"
                "如果服务器那边还说截不到，那就是别的环节（设备离线/选错终端），找我看日志。",
            )
            return
        lines = ["实测截图失败：", err, ""]
        if pre is True:
            lines += [
                "系统说已授权，但实际拍不出来 —— 这种情况几乎都是",
                "「授权之后没有重启过本程序」，重启一次即可。",
            ]
        elif pre is False:
            lines += [
                "系统说本程序没有屏幕录制权限。若你记得勾过了，多半是",
                "列表里留着旧版本 Backon 的条目：重新打包会换签名，",
                "旧勾选就作废了。删掉旧条目，只留一条并勾上。",
            ]
        else:
            lines += [
                "没能问出系统状态，按这两步做：",
                "① 在「屏幕录制」里勾上 Backon（删掉同名旧条目）",
                "② 点菜单里的「重启 Backon（授权后必做）」",
            ]
        lines += ["", "程序路径：" + sys.executable, "版本：" + APP_VERSION,
                  "", "接下来会弹出授权引导窗：把里面的 Backon 图标",
                  "直接拖进设置的「屏幕录制」列表即可。"]
        rumps.alert("屏幕录制：没生效", "\n".join(lines))
        open_perm_guide("screen")

    def act_perm_ax(self, _sender) -> None:
        if accessibility_allowed() is True:
            rumps.alert("辅助功能：可用", "已授权，模拟点击/键盘输入可以正常工作。")
            return
        open_perm_guide("ax")
        return
        granted = request_accessibility()
        self.refresh_perms()
        if granted:
            rumps.alert(
                "辅助功能：已登记",
                "系统已把当前这个 Backon 记进清单，但本进程还读不到授权。\n\n"
                "点菜单里的「重启 Backon（授权后必做）」，重启后就会变成 ✅。",
            )
            return
        rumps.alert(
            "辅助功能：还没授权",
            "已向系统发出授权请求（会弹一个窗）。\n\n"
            "若清单里有多条 Backon，只保留正在运行的这一条：\n"
            + sys.executable + "\n\n"
            "勾上后点「重启 Backon（授权后必做）」。",
        )
        open_privacy("Privacy_Accessibility")

    def act_fix_ax(self, _sender) -> None:
        """清掉 TCC 里的旧记录再重新请求。

        ad-hoc 时代留下的死条目（cdhash 对不上）在界面上删不干净，
        只能 tccutil 直接清，然后让系统按当前这个二进制重新登记。
        """
        try:
            subprocess.run(["tccutil", "reset", "Accessibility", BUNDLE_ID],
                           check=False, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass
        granted = request_accessibility()
        self.refresh_perms()
        if granted:
            rumps.alert(
                "已重置并重新登记",
                "系统已清掉旧记录，并把当前这个 Backon 重新登记。\n\n"
                "点「重启 Backon（授权后必做）」，重启后应该就是 ✅。",
            )
        else:
            rumps.alert(
                "已重置",
                "旧记录清掉了，但这次系统没有直接给出授权。\n\n"
                "请在「辅助功能」里重新勾上 Backon，再点「重启 Backon」。",
            )
            open_privacy("Privacy_Accessibility")

    def act_restart(self, _sender) -> None:
        # 屏幕录制授权只有在进程重新启动后才生效，所以给一个一键重启，
        # 免得用户为了「重开」去翻活动监视器。
        if getattr(sys, "frozen", False):
            argv = [sys.executable]
        else:
            argv = [sys.executable, os.path.join(HERE, "backon_app.py")]
        subprocess.Popen(argv, close_fds=True, start_new_session=True)
        rumps.quit_application()

    # ── 状态显示 ──────────────────────────────────────────────
    def refresh(self, _timer=None) -> None:
        # 每轮都顺手刷新权限：用户在系统设置里勾完，菜单自己就会变成 ✅
        self.refresh_perms()
        if self.pairing:
            self.status_item.title = "◐ 配对中…"
            self.title = "…"
            return
        if not self.state.get("token"):
            self.status_item.title = "○ 未配对"
            self.device_item.title = "设备：—"
            self.title = "?"  # 带问号：提示还需要配对
            return
        name = self.state.get("name") or platform.node()
        self.device_item.title = f"设备：{name}"
        if self.busy:
            self.status_item.title = f"◐ 执行中… {self.err}".strip()
            self.title = "•"
        elif self.connected:
            ago = int(time.time() - self.last_seen) if self.last_seen else 0
            self.status_item.title = f"● 已连接 · {ago}s 前心跳"
            self.title = ""
        else:
            self.status_item.title = f"○ 未连接{('：' + self.err) if self.err else ''}"
            self.title = "!"

    # ── 主循环：长轮询 ────────────────────────────────────────
    def poll_loop(self) -> None:
        backoff = 2.0
        while True:
            if not self.state.get("token"):
                time.sleep(2)
                continue
            try:
                status, body = agent.post(
                    self.hub,
                    "/poll",
                    {
                        "device_id": self.state["device_id"],
                        "token": self.state["token"],
                        "wait": agent.POLL_WAIT,
                    },
                    agent.POLL_WAIT + 10,
                )
                backoff = 2.0
                self.connected = True
                self.last_seen = time.time()
                self.err = ""
                if status == 204 or status == 200 and not body.get("task_id"):
                    self.refresh()
                    continue
                if status == 401:
                    self.connected = False
                    self.err = "配对已失效，需重新配对"
                    self.state = {}
                    agent.save_state({})
                    self.refresh()
                    time.sleep(2)
                    continue
                if status != 200:
                    self.connected = False
                    self.err = f"poll {status}"
                    self.refresh()
                    time.sleep(3)
                    continue

                task_id = body.get("task_id")
                method = body.get("method") or ""
                self.busy = True
                self.refresh()
                ok, result, err = agent.run_task(method, body.get("params") or {})
                agent.post(
                    self.hub,
                    "/result",
                    {
                        "device_id": self.state["device_id"],
                        "token": self.state["token"],
                        "task_id": task_id,
                        "ok": ok,
                        "result": result,
                        "error": err,
                    },
                    30,
                )
                self.busy = False
                self.refresh()
            except Exception as exc:  # noqa: BLE001  网络波动不该杀掉 app
                self.connected = False
                self.err = type(exc).__name__
                self.refresh()
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    # ── 菜单动作 ──────────────────────────────────────────────
    def _ui(self, fn) -> None:
        """把回调丢回主线程执行（rumps 的对话框应跑在主线程）。"""
        def fire(timer) -> None:
            timer.stop()
            fn()
        rumps.Timer(fire, 0.1).start()

    def act_pair(self, _sender) -> None:
        if self.pairing:
            return
        win = rumps.Window(
            title="配对 Backon",
            message="在工作台「设备」页生成 6 位配对码，粘贴到这里：\n\n"
                    "不想配对就直接点「取消」——本程序会安静待在菜单栏，"
                    "随时可从菜单点「退出」关掉它。",
            default_text="",
            ok="配对",
            cancel="取消",
            dimensions=(300, 24),
        )
        resp = win.run()
        code = (resp.text or "").strip()
        if not resp.clicked or not code:
            return
        # 网络请求必须离开主线程：否则配对期间菜单栏整只 app 无响应（最长 20s），
        # 用户会以为「关不掉」。
        self.pairing = True
        self.err = ""
        self.refresh()

        def worker() -> None:
            try:
                name = self.state.get("name") or platform.node()
                state = agent.do_pair(self.hub, code, name)
            except BaseException as exc:  # noqa: BLE001  SystemExit 也接住
                msg = str(exc) if isinstance(exc, SystemExit) else f"{type(exc).__name__}: {exc}"
                self.pairing = False
                self.err = "配对失败"
                self.refresh()
                agent.log(f"配对失败：{msg}")

                def show() -> None:
                    rumps.alert("配对失败", f"{msg}\n\n可以再点「输入配对码…」重试，"
                                         f"或者从菜单点「退出」直接关掉本程序。\n\n"
                                         f"日志：{agent.LOG_PATH}")
                self._ui(show)
                return
            self.state = state
            self.hub = state["hub"].rstrip("/")
            self.connected = False
            self.pairing = False
            self.err = ""
            self.refresh()
            def after_pair() -> None:
                rumps.notification("Backon", "配对成功", "设备名：" + str(state.get("name")))
                # 配对完正是最该确认权限的时刻：截图没权限，整个 P1 就白搭。
                if screen_capture_allowed() is False:
                    if rumps.alert(
                        "还差一个权限",
                        "截图需要 macOS 的「屏幕录制」权限，现在还没给本程序。\n\n"
                        "点「现在授权」按系统提示勾上 Backon，"
                        "然后点菜单里的「重启 Backon（授权后必做）」。",
                        ok="现在授权",
                        cancel="稍后",
                    ):
                        request_screen_capture()
                        self.refresh_perms()

            self._ui(after_pair)

        threading.Thread(target=worker, daemon=True).start()

    def act_selftest(self, _sender) -> None:
        ok, result, err = agent.run_task("screenshot", {"max_edge": 0})
        self.refresh_perms()
        if ok:
            rumps.alert(
                "自检成功",
                f"截到 {result.get('width')}×{result.get('height')}，{result.get('bytes')} 字节。\n"
                "说明屏幕录制权限已生效。",
            )
        else:
            rumps.alert(
                "自检失败",
                f"{err}\n\n"
                f"系统预检：{screen_capture_allowed()}\n"
                f"程序：{sys.executable}\n\n"
                "点菜单里的「屏幕录制：…」那一行看具体原因与下一步。",
            )

    def act_diag(self, _sender) -> None:
        lines = [
            f"程序：{sys.executable}",
            f"版本：{APP_VERSION}",
            f"签名：{sign_identity()}",
            f"屏幕录制：{screen_capture_allowed()}",
            f"辅助功能：{accessibility_allowed()}",
            f"Quartz 模块：{'已加载' if _quartz() is not None else '缺失（打包问题）'}",
            f"hub：{self.hub}",
            f"状态：{'已连接' if self.connected else '未连接'}"
            + (f"（{self.err}）" if self.err else ""),
        ]
        if self.state.get("token"):
            lines.append(f"设备 ID：{self.state.get('device_id')}")
            lines.append(f"设备名：{self.state.get('name')}")
            lines.append(f"指纹：{self.state.get('fingerprint') or agent.fingerprint()}")
        else:
            lines.append("尚未配对")
        try:
            status, _body = agent.post(self.hub, "/poll", {"wait": 0}, 8)
            lines.append(f"连通性：HTTP {status}（401/400 都算通，说明地址对）")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"连通性：失败（{type(exc).__name__}: {exc}）")
        rumps.alert("连接诊断", "\n".join(lines))

    def act_login(self, sender) -> None:
        if os.path.exists(LOGIN_AGENT):
            subprocess.run(["launchctl", "unload", LOGIN_AGENT], check=False)
            os.remove(LOGIN_AGENT)
            sender.state = 0
            rumps.notification("Backon", "已取消开机启动", "")
            return
        exe = self._app_exec()
        os.makedirs(os.path.dirname(LOGIN_AGENT), exist_ok=True)
        payload = {
            "Label": BUNDLE_ID,
            "ProgramArguments": [exe],
            "RunAtLoad": True,
            "KeepAlive": False,
        }
        with open(LOGIN_AGENT, "wb") as fh:
            plistlib.dump(payload, fh)
        subprocess.run(["launchctl", "load", LOGIN_AGENT], check=False)
        sender.state = 1
        rumps.notification("Backon", "已加入开机启动", "")

    @staticmethod
    def _app_exec() -> str:
        # 打包后在 .app/Contents/MacOS/Backon；直接跑脚本时退回 python
        if getattr(sys, "frozen", False):
            return sys.executable
        return f"{sys.executable} {os.path.join(HERE, 'backon_app.py')}"


def main() -> None:
    app = BackonApp()
    if not app.state.get("token"):
        # 首启弹一次配对窗；用户点取消就安静待在菜单栏，绝不重复弹
        def _ask_once(timer) -> None:
            timer.stop()
            app.act_pair(None)

        rumps.Timer(_ask_once, 0.8).start()
    app.run()


if __name__ == "__main__":
    main()
