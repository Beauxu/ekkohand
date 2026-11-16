#!/usr/bin/env python3
"""EkkoHand 菜单栏 App —— 把 agent.py 包成 macOS 常驻应用。

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

DEFAULT_HUB = os.environ.get("HAND_HUB_URL", "https://work.backpeng.com/hand")
LOGIN_AGENT = os.path.expanduser("~/Library/LaunchAgents/cv.backpeng.ekkohand.plist")
BUNDLE_ID = "cv.backpeng.ekkohand"


def open_privacy(pane: str) -> None:
    subprocess.run(
        ["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"],
        check=False,
    )


class EkkoHandApp(rumps.App):
    def __init__(self) -> None:
        super().__init__(
            "EkkoHand",
            title="",
            icon=MENUBAR_ICON or None,
            template=bool(MENUBAR_ICON),
            quit_button="退出",
        )
        self.state = agent.load_state()
        self.hub = (self.state.get("hub") or DEFAULT_HUB).rstrip("/")
        self.connected = False
        self.last_seen = 0.0
        self.busy = False
        self.pairing = False
        self.err = ""

        self.status_item = rumps.MenuItem("○ 未配对")
        self.device_item = rumps.MenuItem("设备：—")
        self.menu = [
            self.status_item,
            self.device_item,
            None,
            rumps.MenuItem("输入配对码…", callback=self.act_pair),
            rumps.MenuItem("自检截图", callback=self.act_selftest),
            rumps.MenuItem("连接诊断", callback=self.act_diag),
            None,
            rumps.MenuItem("屏幕录制权限…", callback=lambda _: open_privacy("Privacy_ScreenCapture")),
            rumps.MenuItem("辅助功能权限…", callback=lambda _: open_privacy("Privacy_Accessibility")),
            None,
            rumps.MenuItem("开机启动", callback=self.act_login),
            None,
        ]
        self.refresh()
        threading.Thread(target=self.poll_loop, daemon=True).start()
        rumps.Timer(self.refresh, 10).start()

    # ── 状态显示 ──────────────────────────────────────────────
    def refresh(self, _timer=None) -> None:
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
            title="配对 EkkoHand",
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
            self._ui(lambda: rumps.notification(
                "EkkoHand", "配对成功", f"设备名：{state.get('name')}"))

        threading.Thread(target=worker, daemon=True).start()

    def act_selftest(self, _sender) -> None:
        ok, result, err = agent.run_task("screenshot", {"max_edge": 0})
        if ok:
            rumps.alert(
                "自检成功",
                f"截到 {result.get('width')}×{result.get('height')}，{result.get('bytes')} 字节。\n"
                "说明屏幕录制权限已生效。",
            )
        else:
            rumps.alert(
                "自检失败",
                f"{err}\n\n请在「屏幕录制权限…」里勾上 EkkoHand，然后退出重开本程序。",
            )

    def act_diag(self, _sender) -> None:
        lines = [
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
            rumps.notification("EkkoHand", "已取消开机启动", "")
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
        rumps.notification("EkkoHand", "已加入开机启动", "")

    @staticmethod
    def _app_exec() -> str:
        # 打包后在 .app/Contents/MacOS/EkkoHand；直接跑脚本时退回 python
        if getattr(sys, "frozen", False):
            return sys.executable
        return f"{sys.executable} {os.path.join(HERE, 'ekkohand_app.py')}"


def main() -> None:
    app = EkkoHandApp()
    if not app.state.get("token"):
        # 首启弹一次配对窗；用户点取消就安静待在菜单栏，绝不重复弹
        def _ask_once(timer) -> None:
            timer.stop()
            app.act_pair(None)

        rumps.Timer(_ask_once, 0.8).start()
    app.run()


if __name__ == "__main__":
    main()
