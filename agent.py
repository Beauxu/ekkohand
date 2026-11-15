#!/usr/bin/env python3
"""EkkoHand agent — 装在用户自己电脑上的「手」。

单文件、**零第三方依赖**（只用标准库 + 系统自带截屏命令），Mac / Linux 同一份。

用法
    # 首次配对（配对码在工作台里点「添加我的电脑」得到）
    python3 agent.py --pair 123456 --name "Macbook Pro"

    # 之后常驻（无参即用已存的令牌）
    python3 agent.py

    # 冒烟测试：不连 hub，直接截一张看看
    python3 agent.py --selftest

通信：向 hub 长轮询（默认 http://127.0.0.1:8770），出站连接，不需要公网 IP、
端口映射或 DDNS。心跳 = 轮询本身（首字节即证明活着），hub 判离线阈值 90s。

注意（macOS）：首次运行会弹「屏幕录制」授权，必须在
系统设置 → 隐私与安全性 → 屏幕录制 里勾上本程序（或 Terminal），否则截出来是桌面壁纸。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_HUB = os.environ.get("HAND_HUB_URL", "http://127.0.0.1:8770")
DEFAULT_STATE = os.path.expanduser("~/.ekkohand/device.json")
STATE_PATH = DEFAULT_STATE
POLL_WAIT = 25.0


def log(msg: str) -> None:
    sys.stderr.write(time.strftime("%H:%M:%S ") + msg + "\n")
    sys.stderr.flush()


def _machine_identity() -> str:
    """这台机器的稳定标识。hostname 会变（容器重建、用户改名），不能当身份。"""
    system = platform.system()
    try:
        if system == "Darwin":
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            found = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            if found:
                return f"uuid:{found.group(1)}"
        elif system == "Windows":
            out = subprocess.run(
                ["reg", "query", r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            found = re.search(r"MachineGuid\s+REG_SZ\s+([0-9a-fA-F-]{20,})", out)
            if found:
                return f"guid:{found.group(1)}"
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                with open(path, encoding="utf-8") as fh:
                    value = fh.read().strip()
            except OSError:
                continue
            if len(value) >= 8:
                return f"mid:{value}"
    except Exception:
        pass
    return f"{platform.node()}|{system}|{platform.machine()}"


def fingerprint() -> str:
    """配对指纹。早先用 hostname 拼，容器一改名就多出一台僵尸设备。"""
    return _machine_identity()


# ── 截屏 ──────────────────────────────────────────────────────
def grab_png() -> bytes:
    system = platform.system()
    if system == "Darwin":
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            subprocess.run(["screencapture", "-x", "-t", "png", path], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            with open(path, "rb") as fh:
                return fh.read()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    if system == "Windows":
        from PIL import ImageGrab  # 打包时随附 Pillow
        buf = io.BytesIO()
        ImageGrab.grab(all_screens=True).save(buf, format="PNG")
        return buf.getvalue()
    # Linux：需要 xwd 或 Pillow；(容器里用 DISPLAY=:1 的虚拟桌面做假设备)
    try:
        from PIL import ImageGrab
        im = ImageGrab.grab(xdisplay=os.environ.get("DISPLAY", ":0"))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Linux 截屏需要 Pillow（{exc}）") from exc


def png_size(data: bytes) -> tuple[int, int]:
    # PNG IHDR: 宽高各 4 字节，偏移 16/20
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
    return (0, 0)


def scale_png(data: bytes, max_edge: int) -> tuple[bytes, bool]:
    """长边缩到 max_edge；max_edge<=0 表示不缩放。优先用系统自带工具，缺则退回 Pillow。"""
    if not max_edge or max_edge <= 0:
        return data, False
    w, h = png_size(data)
    if not w or not h or max(w, h) <= max_edge:
        return data, False
    system = platform.system()
    if system == "Darwin" and shutil.which("sips"):
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            with open(path, "wb") as fh:
                fh.write(data)
            subprocess.run(["sips", "-Z", str(max_edge), path], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            with open(path, "rb") as fh:
                return fh.read(), True
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.thumbnail((max_edge, max_edge))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue(), True
    except Exception:  # noqa: BLE001 — 缩不了就原样给，别让任务失败
        log("缩放失败，返回原图")
        return data, False


# ── 任务执行 ──────────────────────────────────────────────────
def run_task(method: str, params: dict) -> tuple[bool, dict, str | None]:
    try:
        if method == "screenshot":
            raw = grab_png()
            data, scaled = scale_png(raw, int(params.get("max_edge") or 0))
            w, h = png_size(data)
            return True, {
                "png_b64": base64.b64encode(data).decode(),
                "width": w, "height": h, "scaled": scaled,
                "bytes": len(data),
            }, None
        return False, {}, f"设备不支持的方法：{method}"
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace")[:300]
        return False, {}, f"{method} 命令失败：{detail or exc}"
    except Exception as exc:  # noqa: BLE001
        return False, {}, f"{method} 失败：{type(exc).__name__}: {exc}"


# ── HTTP ──────────────────────────────────────────────────────
def post(base: str, path: str, payload: dict, timeout: float) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(), method="POST",
                                headers={"Content-Type": "application/json",
                                         "Accept": "application/json",
                                         "User-Agent": "EkkoHand/0.1 (device agent)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": raw[:200]}


def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    os.chmod(os.path.dirname(STATE_PATH), 0o700)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_PATH)


def do_pair(base: str, code: str, name: str) -> dict:
    status, body = post(base, "/pair", {"code": code, "name": name, "fingerprint": fingerprint()}, 20)
    if status != 200:
        raise SystemExit(f"配对失败（{status}）：{body.get('error') or body}")
    state = {"hub": base, "device_id": body["device_id"], "token": body["token"],
             "name": name, "paired_at": int(time.time())}
    save_state(state)
    log(f"配对成功：{body['device_id']} → {STATE_PATH}" + ("（复用已有设备）" if body.get("reused") else ""))
    return state


def selftest() -> None:
    raw = grab_png()
    data, scaled = scale_png(raw, 1024)
    w, h = png_size(data)
    out = os.path.join(tempfile.gettempdir(), "ekkohand-selftest.png")
    with open(out, "wb") as fh:
        fh.write(data)
    log(f"截屏 OK：{w}×{h}，{len(raw)} → {len(data)} 字节，scaled={scaled}，已存 {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description="EkkoHand — 把自己这台机器交给 Ekko 当手")
    ap.add_argument("--hub", default=None, help="hub 地址，默认用配对时记录的地址")
    ap.add_argument("--pair", default=None, help="6 位配对码（首次配对）")
    ap.add_argument("--name", default=None, help="设备名，默认主机名")
    ap.add_argument("--selftest", action="store_true", help="只截一张图自检，不连 hub")
    ap.add_argument("--state", default=None, help="令牌/配置存放路径，默认 ~/.ekkohand/device.json")
    args = ap.parse_args()
    global STATE_PATH
    if args.state:
        STATE_PATH = os.path.expanduser(args.state)

    if args.selftest:
        selftest()
        return

    state = load_state()
    hub = (args.hub or state.get("hub") or DEFAULT_HUB).rstrip("/")
    name = args.name or state.get("name") or platform.node()

    if args.pair:
        state = do_pair(hub, args.pair, name)
        hub = state["hub"]
    if not state.get("token"):
        raise SystemExit("还没有配对。先在工作台生成配对码，然后：python3 agent.py --pair <6位码> --name <设备名>")

    log(f"已连接 {hub}（{state['name']} / {state['device_id']}）。Ctrl-C 退出。")
    backoff = 2.0
    while True:
        try:
            status, body = post(hub, "/poll", {
                "device_id": state["device_id"], "token": state["token"], "wait": POLL_WAIT,
            }, POLL_WAIT + 10)
            backoff = 2.0
            if status == 204:
                continue
            if status == 401:
                raise SystemExit("令牌失效，请重新配对（--pair <新码>）")
            if status != 200:
                log(f"poll 异常 {status}: {body.get('error')}")
                time.sleep(3)
                continue
            task_id = body.get("task_id")
            method = body.get("method") or ""
            log(f"收到任务 {task_id} {method}")
            ok, result, err = run_task(method, body.get("params") or {})
            post(hub, "/result", {"device_id": state["device_id"], "token": state["token"],
                                  "task_id": task_id, "ok": ok, "result": result, "error": err}, 30)
            log(f"任务 {task_id} {'完成' if ok else '失败：' + str(err)}")
        except KeyboardInterrupt:
            log("退出")
            return
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError) as exc:
            log(f"连接断开（{exc}），{backoff:.0f}s 后重试")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


if __name__ == "__main__":
    main()