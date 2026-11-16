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

import fcntl
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

DEFAULT_HUB = os.environ.get("HAND_HUB_URL", "https://47.88.53.79/hand")
# 长轮询 25 秒一轮 + 2 秒刷新，45 秒内有过心跳就算连得上；
# 不这么设的话单次网络抖动会立刻把菜单栏打成「!」，看起来像掉线但其实没断。
STALE_AFTER = 45.0
LOGIN_AGENT = os.path.expanduser("~/Library/LaunchAgents/cv.backpeng.backon.plist")
BUNDLE_ID = "cv.backpeng.backon"
LEGACY_AGENT_PLIST = os.path.expanduser("~/Library/LaunchAgents/cv.backpeng.ekkohand.plist")


# 同一用户只允许一个实例：否则两个进程会抢同一台设备的任务，同一件事被执行两遍
_LOCK_HANDLE = None


def acquire_single_instance() -> bool:
    """拿 ~/.backon/app.lock 的排他锁。进程退出时由内核释放，无需手工清理。"""
    global _LOCK_HANDLE
    lock_path = os.path.join(os.path.dirname(agent.STATE_PATH), "app.lock")
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fh = open(lock_path, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    _LOCK_HANDLE = fh  # 故意持有到进程结束
    return True


def open_privacy(pane: str) -> None:
    subprocess.run(
        ["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"],
        check=False,
    )


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
        # 勾选状态要按 plist 是否真的存在来还原，否则每次重启勾都会消失
        self.login_item = rumps.MenuItem("开机启动", callback=self.act_login)
        self.login_item.state = 1 if os.path.exists(LOGIN_AGENT) else 0
        self.menu = [
            self.status_item,
            self.device_item,
            None,
            rumps.MenuItem("输入配对码…", callback=self.act_pair),
            rumps.MenuItem("断开连接…", callback=self.act_disconnect),
            rumps.MenuItem("自检截图", callback=self.act_selftest),
            rumps.MenuItem("连接诊断", callback=self.act_diag),
            None,
            rumps.MenuItem("屏幕录制权限…", callback=lambda _: open_privacy("Privacy_ScreenCapture")),
            rumps.MenuItem("辅助功能权限…", callback=lambda _: open_privacy("Privacy_Accessibility")),
            None,
            self.login_item,
            None,
        ]
        self.refresh()
        threading.Thread(target=self.poll_loop, daemon=True).start()
        # 只由主线程的 Timer 刷 UI：轮询线程直接改 NSStatusItem 在多线程下不保证生效，
        # 这正是「三点点阵/感叹号」与真实状态对不上的根源。
        rumps.Timer(self.refresh, 2).start()

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
        age = (time.time() - self.last_seen) if self.last_seen else None
        if self.busy:
            self.status_item.title = f"◐ 执行中… {self.err}".strip()
            self.title = "•"
        elif age is None:
            # 刚启动、第一轮长轮询还没回来：这是「连接中」，不是故障
            self.status_item.title = "◌ 连接中…"
            self.title = ""
        elif age <= STALE_AFTER:
            self.status_item.title = f"● 已连接 · {int(age)}s 前心跳"
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
            # 本轮使用的地址与凭据取快照：用户可能在轮询途中点「断开连接」，
            # 那样 self.state 会被清空，直接读会造成 KeyError 或上报到错误的 hub。
            hub, tok, dev = self.hub, self.state.get("token"), self.state.get("device_id")
            if not tok or not dev:
                time.sleep(1)
                continue
            try:
                status, body = agent.post(
                    hub,
                    "/poll",
                    {
                        "device_id": dev,
                        "token": tok,
                        "wait": agent.POLL_WAIT,
                    },
                    agent.POLL_WAIT + 10,
                )
                backoff = 2.0
                self.connected = True
                self.last_seen = time.time()
                self.err = ""
                if status == 204 or status == 200 and not body.get("task_id"):
                    continue
                if status == 401:
                    if self.state.get("token") != tok:
                        # 是我们自己刚断开的，安静退场，别报「配对已失效」
                        time.sleep(2)
                        continue
                    self.connected = False
                    self.err = "配对已失效，需重新配对"
                    self.state = {}
                    agent.save_state({})
                    time.sleep(2)
                    continue
                if status != 200:
                    self.connected = False
                    self.err = f"poll {status}"
                    time.sleep(3)
                    continue

                task_id = body.get("task_id")
                method = body.get("method") or ""
                if self.state.get("token") != tok:
                    # 任务到手前后用户点了「断开连接」：直接丢弃，不上报
                    continue
                self.busy = True
                ok, result, err = agent.run_task(method, body.get("params") or {})
                agent.post(
                    hub,
                    "/result",
                    {
                        "device_id": dev,
                        "token": tok,
                        "task_id": task_id,
                        "ok": ok,
                        "result": result,
                        "error": err,
                    },
                    30,
                )
                self.busy = False
            except Exception as exc:  # noqa: BLE001  网络波动不该杀掉 app
                self.connected = False
                self.err = type(exc).__name__
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
            self._ui(lambda: rumps.notification(
                "Backon", "配对成功", f"设备名：{state.get('name')}"))

        threading.Thread(target=worker, daemon=True).start()

    def act_disconnect(self, _sender) -> None:
        """清掉本机配对信息，回到未配对状态。

        只动本地：服务端的设备记录保留，重新配对时会复用同一台设备（fingerprint 不变）。
        """
        token = self.state.get("token")
        if not token:
            rumps.alert("尚未配对", "当前没有配对信息，不需要断开。\n\n"
                                  "想连上就点「输入配对码…」。")
            return
        name = self.state.get("name") or platform.node()
        if not rumps.alert(
            title="断开连接？",
            message=(f"将清除本机保存的配对信息（设备：{name}）。\n\n"
                     f"服务端的设备列表不受影响，重新配对会复用同一台设备。\n"
                     f"断开后本程序继续待在菜单栏，随时可点「输入配对码…」重连。\n\n"
                     f"下次配对使用的入口：{DEFAULT_HUB}"),
            ok="断开", cancel="取消",
        ):
            return
        # 先清内存凭据，让轮询线程下一轮就停下，再落盘
        self.state = {}
        self.hub = DEFAULT_HUB.rstrip("/")
        self.connected = False
        self.busy = False
        self.last_seen = 0.0
        self.err = ""
        agent.save_state({})
        self.refresh()
        rumps.notification("Backon", "已断开连接", "本机配对信息已清除")

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
                f"{err}\n\n请在「屏幕录制权限…」里勾上 Backon，然后退出重开本程序。",
            )

    def act_diag(self, _sender) -> None:
        age = (time.time() - self.last_seen) if self.last_seen else None
        if age is None:
            state_text = "尚未收到心跳（刚启动或一直连不上）"
        elif age <= STALE_AFTER:
            state_text = f"已连接（{int(age)}s 前心跳）"
        else:
            state_text = f"未连接（已 {int(age)}s 无心跳）"
        lines = [
            f"hub：{self.hub}",
            f"状态：{state_text}" + (f"（{self.err}）" if self.err else ""),
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

    def act_login(self, sender=None) -> None:
        if os.path.exists(LOGIN_AGENT):
            subprocess.run(["launchctl", "unload", LOGIN_AGENT], check=False)
            try:
                os.remove(LOGIN_AGENT)
            except OSError:
                pass
            self.login_item.state = 0
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
        # RunAtLoad=True 时 launchctl load 会立刻拉起一份进程；
        # 单实例锁会让那份秒退，不会出现两个菜单栏图标抢同一台设备。
        subprocess.run(["launchctl", "load", LOGIN_AGENT], check=False)
        self.login_item.state = 1
        rumps.notification("Backon", "已加入开机启动", "")

    @staticmethod
    def _app_exec() -> str:
        # 打包后在 .app/Contents/MacOS/Backon；直接跑脚本时退回 python
        if getattr(sys, "frozen", False):
            return sys.executable
        return f"{sys.executable} {os.path.join(HERE, 'backon_app.py')}"


def main() -> None:
    if not acquire_single_instance():
        # 已经有一个 Backon 在跑（典型场景：点「开机启动」时 launchctl 立刻拉起了一份）
        # 静默退出，绝不能变成第二个菜单栏图标去抢任务
        sys.exit(0)
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
