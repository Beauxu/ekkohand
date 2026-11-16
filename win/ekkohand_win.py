#!/usr/bin/env python3
"""EkkoHand for Windows —— 把这台 Windows 电脑交给工作台当手。

界面：tkinter 面板（配对 / 状态 / 日志），关掉窗口缩到系统托盘。
协议：和 macOS 版（Backon.app）共用同一份 agent.py，配对与轮询完全一致。

为什么要“可视化一键”：用户不需要开终端。双击 exe → 托盘出现图标 →
输入工作台生成的 6 位配对码 → 服务器就能让这台电脑截图、点击、打字、跑命令。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(HERE))      # 源码直跑时找得到 ../agent.py

import agent  # noqa: E402

APP_NAME = "EkkoHand"
APP_VERSION = agent.APP_VERSION
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# windowed 打包后 sys.stderr 是 None，agent.log 一写就崩；统一转到队列再进面板。
LOG_QUEUE: "queue.Queue[str]" = queue.Queue(maxsize=2000)


class _LogStream:
    def write(self, text: str) -> None:
        text = (text or "").rstrip()
        if not text:
            return
        try:
            LOG_QUEUE.put_nowait(text)
        except queue.Full:
            pass

    def flush(self) -> None:
        pass


def _res(name: str) -> str:
    base = getattr(sys, "_MEIPASS", HERE)
    return os.path.join(base, name)


def _exe_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{os.path.join(HERE, "ekkohand_win.py")}"'


def autostart_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
        return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _exe_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


class Runner:
    """轮询线程：所有网络请求和任务执行都在这里，UI 线程只负责画。"""

    def __init__(self, on_status, on_log) -> None:
        self._on_status = on_status          # (text, kind) kind: idle/ok/warn/err
        self._on_log = on_log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.busy = False

    # ── 生命周期 ──
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="hand-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    # ── 配对 ──
    def pair(self, code: str, name: str) -> None:
        def work() -> None:
            self._on_status("正在配对…", "warn")
            try:
                state = agent.do_pair(agent.DEFAULT_HUB.rstrip("/"), code, name)
                self._on_log(f"配对成功：{state.get('device_id')}（设备名 {state.get('name')}）")
                self._on_status("已配对，正在连接…", "warn")
                self.start()
            except SystemExit as exc:      # do_pair 用 SystemExit 报业务错误
                self._on_status("配对失败", "err")
                self._on_log(str(exc))
            except Exception as exc:       # noqa: BLE001
                self._on_status("配对失败", "err")
                self._on_log(f"{type(exc).__name__}: {exc}")

        threading.Thread(target=work, name="hand-pair", daemon=True).start()

    # ── 主循环 ──
    def _loop(self) -> None:
        state = agent.load_state()
        if not state.get("token"):
            self._on_status("未配对", "idle")
            return
        hub = (state.get("hub") or agent.DEFAULT_HUB).rstrip("/")
        device_name = state.get("name") or "Windows 电脑"
        backoff = 2.0
        self._on_log(f"连接 {hub}（{device_name}）")
        while not self._stop.is_set():
            try:
                status, body = agent.post(hub, "/poll", {
                    "device_id": state["device_id"],
                    "token": state["token"],
                    "wait": agent.POLL_WAIT,
                    "caps": agent.caps(),
                    "version": APP_VERSION,
                }, agent.POLL_WAIT + 10)
                backoff = 2.0
                if status == 204:
                    self._on_status(f"在线 · {device_name}", "ok")
                    continue
                if status == 401:
                    self._on_status("令牌失效，请重新配对", "err")
                    self._on_log("令牌失效：工作台里删掉了这台设备，或换过令牌。")
                    return
                if status != 200:
                    self._on_status(f"服务端异常 {status}", "warn")
                    time.sleep(3)
                    continue
                task_id = body.get("task_id")
                method = body.get("method") or ""
                self.busy = True
                self._on_status(f"执行中：{method}", "warn")
                self._on_log(f"收到任务 {task_id} {method}")
                ok, result, err = agent.run_task(method, body.get("params") or {})
                agent.post(hub, "/result", {
                    "device_id": state["device_id"], "token": state["token"],
                    "task_id": task_id, "ok": ok, "result": result, "error": err,
                }, 30)
                self.busy = False
                self._on_log(f"任务 {task_id} " + ("完成" if ok else f"失败：{err}"))
                self._on_status(f"在线 · {device_name}", "ok")
            except KeyboardInterrupt:
                return
            except Exception as exc:  # noqa: BLE001
                self.busy = False
                if self._stop.is_set():
                    return
                self._on_status(f"连接断开（{backoff:.0f}s 后重试）", "warn")
                self._on_log(f"连接断开：{type(exc).__name__}: {exc}")
                for _ in range(int(backoff * 2)):
                    if self._stop.is_set():
                        return
                    time.sleep(0.5)
                backoff = min(backoff * 2, 30.0)


class Panel:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.state = agent.load_state()
        self.status_text = "未配对"
        self.status_kind = "idle"
        self.runner = Runner(self._set_status, self._log)
        self.tray = None

        root.title(f"{APP_NAME} {APP_VERSION}")
        root.geometry("520x460")
        root.minsize(460, 400)
        root.protocol("WM_DELETE_WINDOW", self.hide)
        try:
            root.iconbitmap(_res("EkkoHand.ico"))
        except Exception:  # noqa: BLE001
            pass

        self._build()
        self._apply_state()
        self._setup_tray()
        self.root.after(120, self._drain_logs)

        if self.state.get("token"):
            self.runner.start()

    # ── 界面 ──
    def _build(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Sub.TLabel", font=("Microsoft YaHei UI", 9), foreground="#666")
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 11))
        style.configure("Code.TEntry", font=("Consolas", 20))
        style.configure("TButton", font=("Microsoft YaHei UI", 10))

        wrap = ttk.Frame(self.root, padding=18)
        wrap.pack(fill="both", expand=True)

        head = ttk.Frame(wrap)
        head.pack(fill="x")
        ttk.Label(head, text="EkkoHand", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text=f"v{APP_VERSION} · Windows", style="Sub.TLabel").pack(
            side="left", padx=(10, 0), pady=(8, 0))

        ttk.Label(
            wrap,
            text="让工作台里的 Ekko 用这台电脑：截屏、点击、打字、读文件、跑命令。\n"
                 "每次操作都要在 Ekko 会话里点「允许」，不给它自己留后门。",
            style="Sub.TLabel", justify="left",
        ).pack(anchor="w", pady=(6, 14))

        card = ttk.LabelFrame(wrap, text="连接", padding=12)
        card.pack(fill="x")

        self.status_var = tk.StringVar(value="未配对")
        self.status_lbl = ttk.Label(card, textvariable=self.status_var, style="Status.TLabel")
        self.status_lbl.pack(anchor="w")

        self.device_row = ttk.Frame(card)
        self.device_row.pack(fill="x", pady=(10, 0))
        ttk.Label(self.device_row, text="设备名").pack(side="left")
        self.name_var = tk.StringVar(value=self.state.get("name") or os.environ.get("COMPUTERNAME") or "Windows 电脑")
        self.name_entry = ttk.Entry(self.device_row, textvariable=self.name_var)
        self.name_entry.pack(side="left", fill="x", expand=True, padx=(10, 0))

        self.pair_row = ttk.Frame(card)
        self.pair_row.pack(fill="x", pady=(12, 0))
        ttk.Label(self.pair_row, text="配对码").pack(side="left")
        self.code_var = tk.StringVar()
        self.code_entry = ttk.Entry(self.pair_row, textvariable=self.code_var,
                                    style="Code.TEntry", width=10, justify="center")
        self.code_entry.pack(side="left", padx=(10, 8))
        self.code_entry.bind("<Return>", lambda _e: self.pair())
        self.pair_btn = ttk.Button(self.pair_row, text="配对并连接", command=self.pair)
        self.pair_btn.pack(side="left")
        self.hint = ttk.Label(card, text="配对码在工作台「设备」页生成，6 位数字。",
                              style="Sub.TLabel")
        self.hint.pack(anchor="w", pady=(6, 0))

        tools = ttk.Frame(wrap)
        tools.pack(fill="x", pady=(12, 0))
        ttk.Button(tools, text="自检截图", command=self.selftest).pack(side="left")
        ttk.Button(tools, text="重新连接", command=self.reconnect).pack(side="left", padx=8)
        ttk.Button(tools, text="打开日志", command=self.open_log).pack(side="left")
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(tools, text="开机启动", variable=self.autostart_var,
                        command=self.toggle_autostart).pack(side="right")

        log_frame = ttk.LabelFrame(wrap, text="日志", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(12, 0))
        self.log_text = tk.Text(log_frame, height=9, wrap="word", font=("Consolas", 9),
                                background="#fbfbfb", relief="flat")
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.configure(state="disabled")

    # ── 状态 ──
    def _set_status(self, text: str, kind: str = "idle") -> None:
        def apply() -> None:
            self.status_text, self.status_kind = text, kind
            self.status_var.set(text)
            color = {"ok": "#1a7f37", "warn": "#9a6700", "err": "#b42318"}.get(kind, "#555")
            self.status_lbl.configure(foreground=color)

        self.root.after(0, apply)

    def _log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S ")
        try:
            LOG_QUEUE.put_nowait(stamp + text)
        except queue.Full:
            pass

    def _drain_logs(self) -> None:
        lines = []
        while True:
            try:
                lines.append(LOG_QUEUE.get_nowait())
            except queue.Empty:
                break
        if lines:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", "\n".join(lines) + "\n")
            # 只保留最近 500 行，免得面板越开越卡
            if int(self.log_text.index("end-1c").split(".")[0]) > 500:
                self.log_text.delete("1.0", "100.0")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(200, self._drain_logs)

    def _apply_state(self) -> None:
        if self.state.get("token"):
            self.status_var.set(f"已配对 · {self.state.get('name') or '本机'}")
            self.pair_row.pack_forget()
            self.hint.configure(text="已配对。换机器或重装后，才需要重新输入配对码。")
        else:
            self.status_var.set("未配对")

    # ── 动作 ──
    def pair(self) -> None:
        code = "".join(ch for ch in self.code_var.get() if ch.isdigit())
        if len(code) != 6:
            messagebox.showwarning(APP_NAME, "配对码是 6 位数字，在工作台生成。")
            return
        name = (self.name_var.get() or "").strip() or "Windows 电脑"
        self.code_var.set("")
        self.runner.pair(code, name)

    def reconnect(self) -> None:
        self.runner.stop()
        self.state = agent.load_state()
        if not self.state.get("token"):
            messagebox.showinfo(APP_NAME, "还没有配对，先输入配对码。")
            return
        self._log("手动重连")
        self.runner.start()

    def selftest(self) -> None:
        def work() -> None:
            ok, result, err = agent.run_task("screenshot", {"max_edge": 0})
            if ok:
                self._log(f"自检截图成功：{result.get('width')}×{result.get('height')}，"
                          f"{result.get('bytes')} 字节")
                self.root.after(0, lambda: messagebox.showinfo(
                    APP_NAME, f"截图成功：{result.get('width')}×{result.get('height')}\n"
                              "这台电脑已经能被工作台看见。"))
            else:
                self._log(f"自检截图失败：{err}")
                self.root.after(0, lambda: messagebox.showerror(APP_NAME, f"截图失败：\n{err}"))

        threading.Thread(target=work, daemon=True).start()

    def open_log(self) -> None:
        try:
            os.startfile(agent.LOG_PATH)  # noqa: S606
        except OSError as exc:
            messagebox.showinfo(APP_NAME, f"日志在：{agent.LOG_PATH}\n（打不开：{exc}）")

    def toggle_autostart(self) -> None:
        try:
            set_autostart(self.autostart_var.get())
            self._log("开机启动：" + ("已开启" if self.autostart_var.get() else "已关闭"))
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"修改开机启动失败：{exc}")

    # ── 托盘 ──
    def _setup_tray(self) -> None:
        try:
            import pystray
            from PIL import Image
        except Exception as exc:  # noqa: BLE001
            self._log(f"托盘不可用（{exc}），关窗口就是退出")
            self.root.protocol("WM_DELETE_WINDOW", self.quit)
            return
        icon_path = _res("tray.png")
        image = Image.open(icon_path) if os.path.exists(icon_path) else Image.new("RGBA", (32, 32), "#2f6f4f")
        menu = pystray.Menu(
            pystray.MenuItem("显示面板", lambda: self.root.after(0, self.show), default=True),
            pystray.MenuItem(lambda _i: self.status_text, None, enabled=False),
            pystray.MenuItem("重新连接", lambda: self.root.after(0, self.reconnect)),
            pystray.MenuItem("退出", lambda: self.root.after(0, self.quit)),
        )
        self.tray = pystray.Icon("EkkoHand", image, f"EkkoHand · {self.status_text}", menu)
        threading.Thread(target=self.tray.run, name="hand-tray", daemon=True).start()

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self) -> None:
        if self.tray:
            self.root.withdraw()
        else:
            self.quit()

    def quit(self) -> None:
        self.runner.stop()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:  # noqa: BLE001
                pass
        self.root.after(0, self.root.destroy)


def _single_instance() -> bool:
    """同名互斥量：第二次双击不再起一个托盘，直接退出。"""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW(None, False, "Local\\EkkoHandSingleton")
        return kernel32.GetLastError() != 183      # ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001
        return True


def main() -> None:
    sys.stderr = _LogStream()
    sys.stdout = _LogStream()
    if not _single_instance():
        return
    root = tk.Tk()
    panel = Panel(root)
    root.mainloop()


if __name__ == "__main__":
    main()
