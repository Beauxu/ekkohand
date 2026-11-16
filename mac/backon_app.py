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
APP_VERSION = "1.1.0"
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


def probe_screen_capture() -> tuple[bool, str]:
    """真拍一张，返回 (是否成功, 错误)。比 preflight 更硬。"""
    try:
        raw = agent.grab_png()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc).strip() or type(exc).__name__
    return bool(raw), ""


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
            self.perm_ax_item.title = "⚠️ 辅助功能：没生效（点这里查原因）"
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
        lines += ["", "程序路径：" + sys.executable, "版本：" + APP_VERSION]
        rumps.alert("屏幕录制：没生效", "\n".join(lines))

    def act_perm_ax(self, _sender) -> None:
        if accessibility_allowed() is True:
            rumps.alert("辅助功能：可用", "已授权，模拟点击/键盘输入可以正常工作。")
            return
        rumps.alert(
            "辅助功能：没生效",
            "在「系统设置 → 隐私与安全性 → 辅助功能」里勾上 Backon"
            "（列表里若有同名旧条目，删掉只留一条），然后点「重启 Backon（授权后必做）」。",
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
            f"屏幕录制：{screen_capture_allowed()}",
            f"辅助功能：{accessibility_allowed()}",
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
