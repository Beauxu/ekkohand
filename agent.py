#!/usr/bin/env python3
"""Backon agent — 装在用户自己电脑上的「手」。

单文件、**零第三方依赖**（只用标准库 + 系统自带截屏命令），Mac / Linux 同一份。

用法
    # 首次配对（配对码在工作台里点「添加我的电脑」得到）
    python3 agent.py --pair 123456 --name "Macbook Pro"

    # 之后常驻（无参即用已存的令牌）
    python3 agent.py

    # 冒烟测试：不连 hub，直接截一张看看
    python3 agent.py --selftest

通信：向 hub 长轮询（默认 http://47.88.53.79/hand，只放行配对/轮询/回传三端点），出站连接，不需要公网 IP、
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
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_HUB = os.environ.get("HAND_HUB_URL", "http://47.88.53.79/hand")
DEFAULT_STATE = os.path.expanduser("~/.backon/device.json")
LEGACY_STATE = os.path.expanduser("~/.ekkohand/device.json")  # 旧名时期的位置，只读兼容
STATE_PATH = DEFAULT_STATE
POLL_WAIT = 25.0


LOG_PATH = (
    os.path.join(os.path.expanduser("~/Library/Logs"), "Backon.log")
    if sys.platform == "darwin"
    else os.path.join(tempfile.gettempdir(), "backon.log")
)


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


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
# ── 本机能力：文件 / 命令 / 输入 ────────────────────────────────
# 全部在用户自己这台机器上执行；能不能做由 Ekko 会话审批把关（写文件/跑脚本/点击是 T2）。
_FS_READ_LIMIT = 400_000

_KEYCODES_MAC = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "escape": 53, "esc": 53,
    "delete": 51, "backspace": 51, "forwarddelete": 117,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}
_MODS_MAC = {"cmd": 55, "command": 55, "shift": 56, "alt": 58, "option": 58,
             "ctrl": 59, "control": 59, "fn": 63}


def _abs(path) -> str:
    raw = str(path or "~").strip() or "~"
    return os.path.abspath(os.path.expanduser(raw))


def _clip(text: str, limit: int = 6000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + f"\n…（省略 {len(text) - limit} 字符）…\n" + text[-limit // 2:]


def _input_backend() -> str | None:
    """本机可用的输入注入后端。Mac 优先 pyobjc(Quartz)，退而 osascript；Linux 用 xdotool。"""
    if platform.system() == "Darwin":
        try:
            import Quartz  # noqa: F401
            return "quartz"
        except Exception:
            return "osascript" if shutil.which("osascript") else None
    if platform.system() == "Linux" and shutil.which("xdotool"):
        return "xdotool"
    return None


def caps() -> list[str]:
    out = ["screenshot", "fs.list", "fs.read", "fs.write", "script.run"]
    if _input_backend():
        out += ["input.click", "input.type", "input.key"]
    return out


def op_fs_list(params: dict) -> dict:
    target = _abs(params.get("path"))
    if not os.path.isdir(target):
        raise ValueError(f"不是目录：{target}")
    rows = []
    with os.scandir(target) as it:
        for entry in it:
            if entry.name.startswith(".") and not params.get("hidden"):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            rows.append({
                "name": entry.name,
                "type": "dir" if entry.is_dir() else "file",
                "size": None if entry.is_dir() else st.st_size,
                "modified": int(st.st_mtime),
            })
    rows.sort(key=lambda r: (r["type"] != "dir", r["name"].lower()))
    total = len(rows)
    limit = max(1, min(int(params.get("limit") or 200), 1000))
    return {"path": target, "total": total, "entries": rows[:limit], "truncated": total > limit}


def op_fs_read(params: dict) -> dict:
    target = _abs(params.get("path"))
    limit = max(1024, min(int(params.get("max_bytes") or _FS_READ_LIMIT), 4_000_000))
    with open(target, "rb") as fh:
        blob = fh.read(limit + 1)
    truncated = len(blob) > limit
    blob = blob[:limit]
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return {"path": target, "binary": True, "bytes": len(blob), "truncated": truncated,
                "content_b64": base64.b64encode(blob).decode()}
    return {"path": target, "binary": False, "bytes": len(blob), "truncated": truncated,
            "content": _clip(text)}


def op_fs_write(params: dict) -> dict:
    target = _abs(params.get("path"))
    if params.get("content") is None:
        raise ValueError("content 不能为空")
    content = str(params.get("content"))
    mode = str(params.get("mode") or "overwrite")
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    existed = os.path.exists(target)
    with open(target, "a" if mode == "append" else "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    return {"path": target, "mode": mode, "existed": existed,
            "bytes": len(content.encode("utf-8"))}


def op_script_run(params: dict) -> dict:
    script = str(params.get("script") or "").strip()
    if not script:
        raise ValueError("script 为空")
    timeout = max(1.0, min(float(params.get("timeout") or 30), 120.0))
    cwd = _abs(params.get("cwd") or "~")
    if not os.path.isdir(cwd):
        cwd = _abs("~")
    shell = str(params.get("shell") or ("/bin/zsh" if platform.system() == "Darwin" else "/bin/sh"))
    started = time.time()
    proc = subprocess.run([shell, "-lc", script], capture_output=True, text=True,
                          timeout=timeout, cwd=cwd)
    return {"exit_code": proc.returncode, "cwd": cwd,
            "duration_ms": int((time.time() - started) * 1000),
            "stdout": _clip(proc.stdout), "stderr": _clip(proc.stderr)}


def op_input_click(params: dict) -> dict:
    backend = _input_backend()
    try:
        x, y = int(params.get("x")), int(params.get("y"))
    except (TypeError, ValueError):
        raise ValueError("需要整数坐标 x / y")
    if backend == "quartz":
        import Quartz as Q
        point = (x, y)
        for kind in (Q.kCGEventMouseMoved, Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp):
            Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateMouseEvent(None, kind, point, Q.kCGMouseButtonLeft))
        return {"x": x, "y": y, "backend": "quartz"}
    if backend == "xdotool":
        subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "1"], check=True, timeout=10)
        return {"x": x, "y": y, "backend": "xdotool"}
    raise RuntimeError("这台设备没有输入注入能力（Mac 需要 pyobjc 或 osascript，Linux 需要 xdotool）")


def op_input_type(params: dict) -> dict:
    text = str(params.get("text") or "")
    if not text:
        raise ValueError("text 为空")
    backend = _input_backend()
    if backend == "quartz":
        import Quartz as Q
        for ch in text:
            for down in (True, False):
                ev = Q.CGEventCreateKeyboardEvent(None, 0, down)
                Q.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
                Q.CGEventPost(Q.kCGHIDEventTap, ev)
        return {"typed": len(text), "backend": "quartz"}
    if backend == "xdotool":
        subprocess.run(["xdotool", "type", "--clearmodifiers", "--", text], check=True, timeout=60)
        return {"typed": len(text), "backend": "xdotool"}
    raise RuntimeError("这台设备没有输入注入能力（Mac 需要在系统设置里授予「辅助功能」）")


_XDOTOOL_KEYS = {"cmd": "super", "command": "super", "option": "alt", "return": "Return",
                 "enter": "Return", "escape": "Escape", "esc": "Escape", "delete": "BackSpace",
                 "pageup": "Page_Up", "pagedown": "Page_Down", "space": "space"}


def op_input_key(params: dict) -> dict:
    combo = str(params.get("keys") or "").strip().lower()
    if not combo:
        raise ValueError("keys 为空，例如 return / cmd+space / shift+tab")
    parts = [p for p in combo.replace(" ", "").split("+") if p]
    backend = _input_backend()
    if backend == "quartz":
        import Quartz as Q
        mods = [_MODS_MAC[p] for p in parts[:-1] if p in _MODS_MAC]
        name = parts[-1]
        code = _KEYCODES_MAC.get(name)
        if code is None and len(name) == 1:
            for ch in name:
                for down in (True, False):
                    ev = Q.CGEventCreateKeyboardEvent(None, 0, down)
                    Q.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
                    Q.CGEventPost(Q.kCGHIDEventTap, ev)
            return {"keys": combo, "backend": "quartz"}
        if code is None:
            raise ValueError(f"不认识的键名：{name}")
        for m in mods:
            Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateKeyboardEvent(None, m, True))
        Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateKeyboardEvent(None, code, True))
        Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateKeyboardEvent(None, code, False))
        for m in reversed(mods):
            Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateKeyboardEvent(None, m, False))
        return {"keys": combo, "backend": "quartz"}
    if backend == "xdotool":
        mapped = "+".join(_XDOTOOL_KEYS.get(p, p) for p in parts)
        subprocess.run(["xdotool", "key", "--clearmodifiers", mapped], check=True, timeout=10)
        return {"keys": combo, "backend": "xdotool"}
    raise RuntimeError("这台设备没有输入注入能力（Mac 需要在系统设置里授予「辅助功能」）")


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
        if method == "fs.list":
            return True, op_fs_list(params), None
        if method == "fs.read":
            return True, op_fs_read(params), None
        if method == "fs.write":
            return True, op_fs_write(params), None
        if method == "script.run":
            return True, op_script_run(params), None
        if method == "input.click":
            return True, op_input_click(params), None
        if method == "input.type":
            return True, op_input_type(params), None
        if method == "input.key":
            return True, op_input_key(params), None
        return False, {}, f"设备不支持的方法：{method}"
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace")[:300]
        return False, {}, f"{method} 命令失败：{detail or exc}"
    except Exception as exc:  # noqa: BLE001
        return False, {}, f"{method} 失败：{type(exc).__name__}: {exc}"


# ── HTTP ──────────────────────────────────────────────────────
_SSL_CTX = None


def ssl_context():
    """PyInstaller 打的包里没有 CA 证书，必须显式指定 certifi 的 bundle。

    否则 macOS 上 https 请求会在握手阶段就以
    CERTIFICATE_VERIFY_FAILED 失败，请求根本发不出去。
    """
    global _SSL_CTX
    if _SSL_CTX is not None:
        return _SSL_CTX
    try:
        import certifi  # 打包时随包带上

        _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX


def post(base: str, path: str, payload: dict, timeout: float) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(), method="POST",
                                headers={"Content-Type": "application/json",
                                         "Accept": "application/json",
                                         "User-Agent": "Backon/1.0.5 (device agent)"})
    kwargs = {"timeout": timeout}
    if base.lower().startswith("https"):
        kwargs["context"] = ssl_context()
    try:
        with urllib.request.urlopen(req, **kwargs) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": raw[:200]}
    except urllib.error.URLError as exc:
        log(f"POST {base}{path} 失败：{exc.reason!r}")
        raise ConnectionError(f"连不上 {base}：{exc.reason}") from exc
    except ssl.SSLError as exc:
        log(f"POST {base}{path} TLS 失败：{exc!r}")
        raise ConnectionError(f"TLS 握手失败（证书问题）：{exc}") from exc


def load_state() -> dict:
    for path in (STATE_PATH, LEGACY_STATE):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            if path == LEGACY_STATE:
                log(f"沿用旧目录的配对信息：{path}")
            return state
        except (OSError, ValueError):
            continue
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
    status, body = post(base, "/pair", {"code": code, "name": name,
                                       "fingerprint": fingerprint(), "caps": caps()}, 20)
    if status != 200:
        log(f"配对被拒：status={status} body={body}")
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
    out = os.path.join(tempfile.gettempdir(), "backon-selftest.png")
    with open(out, "wb") as fh:
        fh.write(data)
    log(f"截屏 OK：{w}×{h}，{len(raw)} → {len(data)} 字节，scaled={scaled}，已存 {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Backon — 把自己这台机器交给工作台当手")
    ap.add_argument("--hub", default=None, help="hub 地址，默认用配对时记录的地址")
    ap.add_argument("--pair", default=None, help="6 位配对码（首次配对）")
    ap.add_argument("--name", default=None, help="设备名，默认主机名")
    ap.add_argument("--selftest", action="store_true", help="只截一张图自检，不连 hub")
    ap.add_argument("--state", default=None, help="令牌/配置存放路径，默认 ~/.backon/device.json")
    args = ap.parse_args()
    global STATE_PATH
    if args.state:
        STATE_PATH = os.path.expanduser(args.state)

    if args.selftest:
        selftest()
        return

    state = load_state()
    # 已配对的沿用记录里的地址（避免换版本后掉线）；未配对的一律用当前默认入口
    hub = (args.hub or (state.get("hub") if state.get("token") else None) or DEFAULT_HUB).rstrip("/")
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