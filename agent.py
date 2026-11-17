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

DEFAULT_HUB = os.environ.get("HAND_HUB_URL", "https://47.88.53.79/hand")
DEFAULT_STATE = os.path.expanduser("~/.backon/device.json")
LEGACY_STATE = os.path.expanduser("~/.ekkohand/device.json")  # 旧名时期的位置，只读兼容
STATE_PATH = DEFAULT_STATE
POLL_WAIT = 25.0
# 心跳里上报的版本号：CI 打包时用 BACKON_VERSION 注入 tag，本地跑就用默认值。
APP_VERSION = os.environ.get("BACKON_VERSION") or "1.1.9"


def _default_log_path() -> str:
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~/Library/Logs"), "Backon.log")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
        return os.path.join(base, "Backon", "Backon.log")
    return os.path.join(tempfile.gettempdir(), "backon.log")


LOG_PATH = _default_log_path()


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

# ── Windows 输入注入 ─────────────────────────────────────────
# 直接调 user32.SendInput：不依赖 pyautogui / pywinauto，打包体积和运行时都小。
_WIN_VK = {
    "return": 0x0D, "enter": 0x0D, "tab": 0x09, "space": 0x20,
    "escape": 0x1B, "esc": 0x1B, "delete": 0x08, "backspace": 0x08,
    "forwarddelete": 0x2E, "insert": 0x2D, "capslock": 0x14,
    "printscreen": 0x2C, "pause": 0x13,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}
_WIN_MODS = {"cmd": 0x5B, "command": 0x5B, "win": 0x5B, "super": 0x5B,
             "shift": 0x10, "alt": 0x12, "option": 0x12,
             "ctrl": 0x11, "control": 0x11}


class _WinInput:
    """Windows 鼠标/键盘注入（SendInput）。坐标按虚拟桌面归一化，多屏也对得上。

    截图走 ImageGrab(all_screens=True)，坐标原点同样是虚拟桌面左上角，
    所以模型看图和点坐标是同一套坐标系。
    """

    INPUT_MOUSE = 0
    INPUT_KEYBOARD = 1
    MOVE, DOWN, UP, WHEEL = 0x0001, 0x0002, 0x0004, 0x0800
    ABSOLUTE, VIRTUALDESK = 0x8000, 0x4000
    KEYUP, UNICODE = 0x0002, 0x0004

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        ulong_ptr = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ulong_ptr)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                        ("wParamH", wintypes.WORD)]

        class INPUT(ctypes.Structure):
            class _U(ctypes.Union):
                _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        self._ct = ctypes
        self._INPUT, self._MOUSEINPUT, self._KEYBDINPUT = INPUT, MOUSEINPUT, KEYBDINPUT
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        self.user32.SendInput.restype = wintypes.UINT

    def _send(self, *inputs) -> None:
        batch = (self._INPUT * len(inputs))(*inputs)
        sent = self.user32.SendInput(len(inputs), batch, self._ct.sizeof(self._INPUT))
        if sent != len(inputs):
            err = self._ct.get_last_error()
            raise RuntimeError(
                f"SendInput 只送达 {sent}/{len(inputs)}（WinError={err}；"
                "目标程序以管理员运行时，本程序也需要同样权限）")

    def _mouse(self, flags: int, x: int = 0, y: int = 0, data: int = 0) -> None:
        inp = self._INPUT()
        inp.type = self.INPUT_MOUSE
        inp.mi = self._MOUSEINPUT(x, y, data & 0xFFFFFFFF, flags, 0, 0)
        self._send(inp)

    def _kb(self, vk: int, flags: int) -> None:
        inp = self._INPUT()
        inp.type = self.INPUT_KEYBOARD
        inp.ki = self._KEYBDINPUT(vk & 0xFFFF, 0, flags, 0, 0)
        self._send(inp)

    def _norm(self, x: int, y: int) -> tuple[int, int]:
        u = self.user32
        vx, vy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)      # 虚拟桌面原点
        vw, vh = u.GetSystemMetrics(78), u.GetSystemMetrics(79)      # 虚拟桌面尺寸
        if vw <= 1 or vh <= 1:
            vw, vh, vx, vy = u.GetSystemMetrics(0), u.GetSystemMetrics(1), 0, 0
        nx = int(round((x - vx) * 65535 / max(vw - 1, 1)))
        ny = int(round((y - vy) * 65535 / max(vh - 1, 1)))
        return max(0, min(nx, 65535)), max(0, min(ny, 65535))

    def click(self, x: int, y: int) -> None:
        nx, ny = self._norm(x, y)
        base = self.MOVE | self.ABSOLUTE | self.VIRTUALDESK
        self._mouse(base, nx, ny)
        self._mouse(base | self.DOWN, nx, ny)
        self._mouse(base | self.UP, nx, ny)

    def drag(self, x1: int, y1: int, x2: int, y2: int, steps: int) -> None:
        base = self.MOVE | self.ABSOLUTE | self.VIRTUALDESK
        nx, ny = self._norm(x1, y1)
        self._mouse(base, nx, ny)
        self._mouse(base | self.DOWN, nx, ny)
        for i in range(1, steps + 1):
            mx = int(round(x1 + (x2 - x1) * i / steps))
            my = int(round(y1 + (y2 - y1) * i / steps))
            nx, ny = self._norm(mx, my)
            self._mouse(base, nx, ny)
            time.sleep(0.012)
        nx, ny = self._norm(x2, y2)
        self._mouse(base | self.UP, nx, ny)

    def scroll(self, amount: int) -> None:
        # 协议：正数向下。Windows 正 delta 向上，所以取负。
        self._mouse(self.WHEEL, 0, 0, (-120 * amount) & 0xFFFFFFFF)

    def type_text(self, text: str) -> None:
        data = text.encode("utf-16-le")
        batch: list = []
        for i in range(0, len(data), 2):
            code = int.from_bytes(data[i:i + 2], "little")
            for flags in (self.UNICODE, self.UNICODE | self.KEYUP):
                inp = self._INPUT()
                inp.type = self.INPUT_KEYBOARD
                inp.ki = self._KEYBDINPUT(0, code, flags, 0, 0)
                batch.append(inp)
        for i in range(0, len(batch), 200):     # 一次别塞太多，避免 SendInput 拒绝
            self._send(*batch[i:i + 200])

    def key(self, vk: int, mods: list[int]) -> None:
        for m in mods:
            self._kb(m, 0)
        self._kb(vk, 0)
        self._kb(vk, self.KEYUP)
        for m in reversed(mods):
            self._kb(m, self.KEYUP)


_WIN_INPUT: _WinInput | None = None


def _win_input() -> _WinInput:
    global _WIN_INPUT
    if _WIN_INPUT is None:
        _WIN_INPUT = _WinInput()
    return _WIN_INPUT

# ── Windows 控件级定位（UIA）──────────────────────────────────
# 坐标点击在 DPI/缩放变化时会飘，也不认识“按钮叫什么”。UIA 能按名字 /
# AutomationId 找到控件，拿准确矩形再点，或者直接 Invoke。
# 实现借系统自带的 .NET UIA（PowerShell 5.1 里 Add-Type 即可），
# 不引 pywinauto / pywin32，打包体积不变。
_PS_PRELUDE = r"""$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function Get-Target([int]$procId, [string]$title) {
  $root = [System.Windows.Automation.AutomationElement]::RootElement
  if ($procId -gt 0) {
    $cond = New-Object System.Windows.Automation.PropertyCondition(
      [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $procId)
    $hit = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)
    if ($hit) { return $hit }
  }
  if ($title) {
    foreach ($w in $root.FindAll([System.Windows.Automation.TreeScope]::Children,
             [System.Windows.Automation.Condition]::TrueCondition)) {
      if ($w.Current.Name -like "*$title*") { return $w }
    }
  }
  return $null
}

function Get-MatchCond([string]$name, [string]$aid) {
  $list = New-Object System.Collections.ArrayList
  if ($name) {
    [void]$list.Add((New-Object System.Windows.Automation.PropertyCondition(
      [System.Windows.Automation.AutomationElement]::NameProperty, $name)))
  }
  if ($aid) {
    [void]$list.Add((New-Object System.Windows.Automation.PropertyCondition(
      [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $aid)))
  }
  if ($list.Count -eq 0) { return [System.Windows.Automation.Condition]::TrueCondition }
  if ($list.Count -eq 1) { return $list[0] }
  $arr = $list.ToArray()
  return New-Object System.Windows.Automation.AndCondition([System.Windows.Automation.Condition[]]$arr)
}

function Convert-El($el) {
  $c = $el.Current
  $r = $c.BoundingRectangle
  $o = [ordered]@{
    name = $c.Name
    control_type = ($c.ControlType.ProgrammaticName -replace '^ControlType\.', '')
    automation_id = $c.AutomationId
    class = $c.ClassName
    enabled = $c.IsEnabled
    x = [int]$r.X; y = [int]$r.Y; w = [int]$r.Width; h = [int]$r.Height
    cx = [int]($r.X + $r.Width / 2); cy = [int]($r.Y + $r.Height / 2)
    patterns = @()
  }
  $pat = @()
  foreach ($p in $c.GetSupportedPatterns()) {
    $pat += ($p.ProgrammaticName -replace '^Pattern\.', '')
  }
  $o.patterns = $pat
  return $o
}
"""

_PS_TREE = _PS_PRELUDE + r"""
$out = $env:UIA_OUT
$procId = 0; [void][int]::TryParse($env:UIA_PID, [ref]$procId)
$depth = 3; [void][int]::TryParse($env:UIA_DEPTH, [ref]$depth)
$maxNodes = 400; [void][int]::TryParse($env:UIA_MAXNODES, [ref]$maxNodes)

$target = Get-Target $procId $env:UIA_TITLE
if (-not $target) {
  @{ error = '没找到窗口：先用 window.list 看 pid/title（标题支持模糊匹配）' } |
    ConvertTo-Json -Compress | Out-File -LiteralPath $out -Encoding utf8
  exit 0
}

$script:count = 0
function Walk($el, $d) {
  if ($script:count -ge $maxNodes) { return $null }
  $script:count++
  $node = Convert-El $el
  if ($d -lt $depth) {
    $kids = @()
    foreach ($ch in $el.FindAll([System.Windows.Automation.TreeScope]::Children,
             [System.Windows.Automation.Condition]::TrueCondition)) {
      $child = Walk $ch ($d + 1)
      if ($child) { $kids += $child }
    }
    if ($kids.Count -gt 0) { $node['children'] = $kids }
  }
  return $node
}

$tree = Walk $target 0
@{ window = @{ name = $target.Current.Name; pid = $target.Current.ProcessId };
   nodes = $script:count; tree = $tree } |
  ConvertTo-Json -Depth 24 -Compress | Out-File -LiteralPath $out -Encoding utf8
"""

_PS_FIND = _PS_PRELUDE + r"""
$out = $env:UIA_OUT
$procId = 0; [void][int]::TryParse($env:UIA_PID, [ref]$procId)
$limit = 20; [void][int]::TryParse($env:UIA_LIMIT, [ref]$limit)
$wantType = $env:UIA_CT

$target = Get-Target $procId $env:UIA_TITLE
if (-not $target) {
  @{ error = '没找到窗口：先用 window.list 看 pid/title' } |
    ConvertTo-Json -Compress | Out-File -LiteralPath $out -Encoding utf8
  exit 0
}

$cond = Get-MatchCond $env:UIA_NAME $env:UIA_AID
$hits = $target.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
$rows = @()
foreach ($h in $hits) {
  $o = Convert-El $h
  if ($wantType -and $o.control_type -ne $wantType) { continue }
  $rows += $o
  if ($rows.Count -ge $limit) { break }
}
@{ window = @{ name = $target.Current.Name; pid = $target.Current.ProcessId };
   count = $rows.Count; matches = $rows } |
  ConvertTo-Json -Depth 6 -Compress | Out-File -LiteralPath $out -Encoding utf8
"""

_PS_INVOKE = _PS_PRELUDE + r"""
$out = $env:UIA_OUT
$procId = 0; [void][int]::TryParse($env:UIA_PID, [ref]$procId)

$target = Get-Target $procId $env:UIA_TITLE
if (-not $target) {
  @{ error = '没找到窗口：先用 window.list 看 pid/title' } |
    ConvertTo-Json -Compress | Out-File -LiteralPath $out -Encoding utf8
  exit 0
}

$cond = Get-MatchCond $env:UIA_NAME $env:UIA_AID
$el = $target.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
if (-not $el) {
  @{ error = '没找到控件：ui.find 里能看到候选，Name/AutomationId 要精确匹配' } |
    ConvertTo-Json -Compress | Out-File -LiteralPath $out -Encoding utf8
  exit 0
}

$action = $env:UIA_ACTION
$value = $env:UIA_VALUE
try {
  switch ($action) {
    'invoke'    { [System.Windows.Automation.InvokePattern](
                    $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)).Invoke() }
    'toggle'    { [System.Windows.Automation.TogglePattern](
                    $el.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)).Toggle() }
    'select'    { [System.Windows.Automation.SelectionItemPattern](
                    $el.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)).Select() }
    'expand'    { [System.Windows.Automation.ExpandCollapsePattern](
                    $el.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)).Expand() }
    'collapse'  { [System.Windows.Automation.ExpandCollapsePattern](
                    $el.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)).Collapse() }
    'set_value' { [System.Windows.Automation.ValuePattern](
                    $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)).SetValue($value) }
    'focus'     { $el.SetFocus() }
    default    { throw "不认识的 action：$action（invoke/toggle/select/expand/collapse/set_value/focus）" }
  }
  @{ ok = $true; action = $action; target = (Convert-El $el) } |
    ConvertTo-Json -Depth 6 -Compress | Out-File -LiteralPath $out -Encoding utf8
} catch {
  @{ error = $_.Exception.Message } |
    ConvertTo-Json -Compress | Out-File -LiteralPath $out -Encoding utf8
}
"""


def _ps_uia(script: str, env: dict[str, str], timeout: int = 45) -> dict:
    """跑一段 PowerShell 并读回它写的 JSON。powershell.exe 是 5.1（.NET UIA 就在里面）。"""
    fd, ps1 = tempfile.mkstemp(suffix=".ps1")
    os.close(fd)
    out_fd, out_json = tempfile.mkstemp(suffix=".json")
    os.close(out_fd)
    try:
        with open(ps1, "w", encoding="utf-8-sig") as fh:   # BOM：PowerShell 5.1 才按 UTF-8 解析
            fh.write(script)
        full_env = {**os.environ, **env, "UIA_OUT": out_json}
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", ps1],
            env=full_env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        try:
            with open(out_json, encoding="utf-8-sig") as fh:
                raw = fh.read().strip()
        except OSError:
            raw = ""
        if not raw:
            tail = [ln for ln in (proc.stderr or "").strip().splitlines() if ln][-3:]
            raise RuntimeError("UIA 没有返回结果：" + ("; ".join(tail) or "PowerShell 无输出"))
        data = json.loads(raw)
        if data.get("error"):
            raise RuntimeError(str(data["error"]))
        return data
    finally:
        for path in (ps1, out_json):
            try:
                os.unlink(path)
            except OSError:
                pass


def _uia_target_env(params: dict) -> dict[str, str]:
    env: dict[str, str] = {}
    pid = params.get("pid")
    if pid:
        env["UIA_PID"] = str(int(pid))
    title = params.get("title") or params.get("window_title")
    if title:
        env["UIA_TITLE"] = str(title)
    if not env:
        raise ValueError("需要 pid 或 title 指定窗口；先用 window.list 看有哪些窗口")
    return env


def op_window_list(params: dict) -> dict:
    if platform.system() != "Windows":
        raise ValueError("window.list 只在 Windows 上可用")
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE

    def proc_name(pid: int) -> str:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            kernel32.CloseHandle(handle)
        return ""

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    rows: list[dict] = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        rows.append({
            "handle": int(hwnd),
            "title": title,
            "class": cls.value,
            "pid": int(pid.value),
            "process": proc_name(int(pid.value)),
            "rect": {"x": rect.left, "y": rect.top,
                     "w": rect.right - rect.left, "h": rect.bottom - rect.top},
            "minimized": bool(user32.IsIconic(hwnd)),
        })
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    fg = int(user32.GetForegroundWindow())
    for row in rows:
        row["foreground"] = row["handle"] == fg
    query = str(params.get("query") or "").lower()
    if query:
        rows = [r for r in rows
                if query in r["title"].lower() or query in r["process"].lower()]
    rows.sort(key=lambda r: (not r["foreground"], r["title"].lower()))
    return {"windows": rows, "total": len(rows)}


def op_ui_tree(params: dict) -> dict:
    if platform.system() != "Windows":
        raise ValueError("ui.tree 只在 Windows 上可用")
    env = _uia_target_env(params)
    env["UIA_DEPTH"] = str(max(1, min(int(params.get("depth") or 3), 8)))
    env["UIA_MAXNODES"] = str(max(20, min(int(params.get("max_nodes") or 400), 3000)))
    data = _ps_uia(_PS_TREE, env)
    return {"window": data.get("window"), "nodes": data.get("nodes"), "tree": data.get("tree")}


def op_ui_find(params: dict) -> dict:
    if platform.system() != "Windows":
        raise ValueError("ui.find 只在 Windows 上可用")
    name = str(params.get("name") or "")
    aid = str(params.get("automation_id") or "")
    if not name and not aid:
        raise ValueError("至少给 name 或 automation_id；不知道叫什么就先 ui.tree 看一眼")
    env = _uia_target_env(params)
    env["UIA_NAME"] = name
    env["UIA_AID"] = aid
    env["UIA_CT"] = str(params.get("control_type") or "")
    env["UIA_LIMIT"] = str(max(1, min(int(params.get("limit") or 20), 200)))
    data = _ps_uia(_PS_FIND, env)
    return {"window": data.get("window"), "count": data.get("count"),
            "matches": data.get("matches") or []}


def op_ui_invoke(params: dict) -> dict:
    if platform.system() != "Windows":
        raise ValueError("ui.invoke 只在 Windows 上可用")
    name = str(params.get("name") or "")
    aid = str(params.get("automation_id") or "")
    if not name and not aid:
        raise ValueError("至少给 name 或 automation_id，否则不知道点哪个")
    action = str(params.get("action") or "invoke").lower()
    env = _uia_target_env(params)
    env["UIA_NAME"] = name
    env["UIA_AID"] = aid
    env["UIA_ACTION"] = action
    env["UIA_VALUE"] = str(params.get("value") or "")
    return _ps_uia(_PS_INVOKE, env)


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
    if platform.system() == "Windows":
        return "win"          # user32.SendInput，系统自带，无需额外依赖
    if platform.system() == "Linux" and shutil.which("xdotool"):
        return "xdotool"
    return None


def caps() -> list[str]:
    out = ["screenshot", "fs.list", "fs.read", "fs.write", "fs.delete", "fs.move",
           "script.run", "app.list", "app.launch"]
    if _input_backend():
        out += ["input.click", "input.type", "input.key", "input.drag", "input.scroll"]
    if platform.system() == "Windows":
        # 坐标点击会飘；这三个让模型能按控件名定位、直接触发
        out += ["window.list", "ui.tree", "ui.find", "ui.invoke"]
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
    started = time.time()
    if platform.system() == "Windows":
        shell = str(params.get("shell") or "cmd")
        low = os.path.basename(shell).lower()
        if low.startswith("powershell") or low == "pwsh":
            exe = shell if low != "cmd" else "powershell.exe"
            cmd = [exe, "-NoProfile", "-NonInteractive", "-Command",
                   "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script]
        else:
            # chcp 65001 让 cmd 的 stdout 变 UTF-8，中文不乱码
            cmd = ["cmd.exe", "/d", "/c", "chcp 65001>nul && " + script]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=cwd)
    else:
        shell = str(params.get("shell") or ("/bin/zsh" if platform.system() == "Darwin" else "/bin/sh"))
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
    if backend == "win":
        _win_input().click(x, y)
        return {"x": x, "y": y, "backend": "win"}
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
    if backend == "win":
        _win_input().type_text(text)      # KEYEVENTF_UNICODE，中文/emoji 都能打
        return {"typed": len(text), "backend": "win"}
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
    if backend == "win":
        mods = [_WIN_MODS[p] for p in parts[:-1] if p in _WIN_MODS]
        name = parts[-1]
        vk = _WIN_VK.get(name)
        if vk is None and len(name) == 1:
            _win_input().type_text(name)
            return {"keys": combo, "backend": "win"}
        if vk is None:
            raise ValueError(f"不认识的键名：{name}")
        _win_input().key(vk, mods)
        return {"keys": combo, "backend": "win"}
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


def _quad(points: list[tuple[float, float]], kinds: list[int], Q) -> None:
    for kind, point in zip(kinds, points):
        Q.CGEventPost(
            Q.kCGHIDEventTap,
            Q.CGEventCreateMouseEvent(None, kind, point, Q.kCGMouseButtonLeft),
        )


def op_input_drag(params: dict) -> dict:
    """从 (x1,y1) 按住拖到 (x2,y2)。Mac 走按住 + 分步 LeftMouseDragged，Linux 走 xdotool。"""
    try:
        x1, y1 = int(params.get("x1")), int(params.get("y1"))
        x2, y2 = int(params.get("x2")), int(params.get("y2"))
    except (TypeError, ValueError):
        raise ValueError("需要整数坐标 x1 / y1 / x2 / y2")
    steps = max(2, min(int(params.get("steps") or 12), 60))
    backend = _input_backend()
    if backend == "win":
        _win_input().drag(x1, y1, x2, y2, steps)
        return {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "steps": steps, "backend": "win"}
    if backend == "quartz":
        import Quartz as Q
        points: list[tuple[float, float]] = [(x1, y1)]
        points.append((x1, y1))
        for i in range(1, steps + 1):
            points.append((x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps))
        points.append((x2, y2))
        kinds = [Q.kCGEventMouseMoved, Q.kCGEventLeftMouseDown]
        kinds += [Q.kCGEventLeftMouseDragged] * steps
        kinds.append(Q.kCGEventLeftMouseUp)
        _quad(points, kinds, Q)
        return {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "steps": steps, "backend": "quartz"}
    if backend == "xdotool":
        subprocess.run(["xdotool", "mousemove", str(x1), str(y1), "mousedown", "1"],
                       check=True, timeout=10)
        subprocess.run(["xdotool", "mousemove", "--sync", str(x2), str(y2)],
                       check=True, timeout=10)
        subprocess.run(["xdotool", "mouseup", "1"], check=True, timeout=10)
        return {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "steps": 1, "backend": "xdotool"}
    raise RuntimeError("这台设备没有输入注入能力（Mac 需要 pyobjc，Linux 需要 xdotool）")


def op_input_scroll(params: dict) -> dict:
    """滚轮。amount 正数向下滚、负数向上滚，单位是「行」。"""
    if params.get("amount") is None:
        raise ValueError("需要 amount（正数向下、负数向上，单位：行）")
    try:
        amount = int(params.get("amount"))
    except (TypeError, ValueError):
        raise ValueError("amount 必须是整数")
    if amount == 0:
        raise ValueError("amount 不能为 0")
    amount = max(-100, min(amount, 100))
    backend = _input_backend()
    if backend == "win":
        _win_input().scroll(amount)
        return {"amount": amount, "unit": "line", "backend": "win"}
    if backend == "quartz":
        import Quartz as Q
        event = Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitLine, 1, amount)
        Q.CGEventPost(Q.kCGHIDEventTap, event)
        return {"amount": amount, "unit": "line", "backend": "quartz"}
    if backend == "xdotool":
        button = "5" if amount > 0 else "4"
        for _ in range(abs(amount)):
            subprocess.run(["xdotool", "click", button], check=True, timeout=10)
        return {"amount": amount, "unit": "line", "backend": "xdotool"}
    raise RuntimeError("这台设备没有输入注入能力（Mac 需要 pyobjc，Linux 需要 xdotool）")


def _mac_app_dirs() -> list[str]:
    home = os.path.expanduser("~")
    return ["/Applications", "/System/Applications",
            os.path.join(home, "Applications"),
            "/System/Applications/Utilities"]


def op_app_list(params: dict) -> dict:
    """列本机已安装的 App（名字 + 路径），可按 query 过滤名字。"""
    query = str(params.get("query") or "").strip().lower()
    limit = max(1, min(int(params.get("limit") or 200), 500))
    rows: list[dict] = []
    if platform.system() == "Darwin":
        seen = set()
        for folder in _mac_app_dirs():
            if not os.path.isdir(folder):
                continue
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for entry in entries:
                if not entry.name.endswith(".app"):
                    continue
                name = entry.name[:-4]
                if query and query not in name.lower():
                    continue
                if entry.path in seen:
                    continue
                seen.add(entry.path)
                rows.append({"name": name, "path": entry.path, "folder": folder})
    elif platform.system() == "Windows":
        # Get-StartApps 列出开始菜单里的所有应用（UWP 和桌面程序都算），
        # 输出重定向到临时文件再读，避免控制台代码页把中文名搞乱。
        fd, tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            ps = ("Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress "
                  f"| Out-File -LiteralPath '{tmp}' -Encoding utf8")
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=60)
            try:
                with open(tmp, encoding="utf-8-sig") as fh:
                    raw = fh.read().strip()
            except OSError:
                raw = ""
            items = json.loads(raw) if raw else []
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if isinstance(items, dict):
            items = [items]
        for item in items:
            name = str(item.get("Name") or "").strip()
            app_id = str(item.get("AppID") or "").strip()
            if not name or not app_id:
                continue
            if query and query not in name.lower():
                continue
            rows.append({"name": name, "path": "shell:AppsFolder\\" + app_id,
                         "folder": "start-menu"})
    else:
        for folder in ("/usr/share/applications", os.path.expanduser("~/.local/share/applications")):
            if not os.path.isdir(folder):
                continue
            for entry in os.scandir(folder):
                if not entry.name.endswith(".desktop"):
                    continue
                name = entry.name[:-8]
                try:
                    with open(entry.path, encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            if line.startswith("Name=") and name == entry.name[:-8]:
                                name = line.strip().split("=", 1)[1]
                            if line.startswith("NoDisplay=true"):
                                name = ""
                                break
                except OSError:
                    pass
                if not name:
                    continue
                if query and query not in name.lower():
                    continue
                rows.append({"name": name, "path": entry.path, "folder": folder})
    rows.sort(key=lambda r: r["name"].lower())
    total = len(rows)
    return {"apps": rows[:limit], "total": total, "truncated": total > limit}


def op_app_launch(params: dict) -> dict:
    """打开一个 App（已在运行时等于切到前台，所以不再单独提供 app.focus）。"""
    target = str(params.get("path") or "").strip()
    name = str(params.get("name") or "").strip()
    if not target and not name:
        raise ValueError("需要 name 或 path")
    if platform.system() == "Windows":
        if target.lower().startswith("shell:appsfolder"):
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                            f'Start-Process "{target}"'], check=True, timeout=30)
        elif target and os.path.exists(target):
            os.startfile(target)  # noqa: S606 — 用户点名要打开的程序
        elif target:
            subprocess.run(["cmd.exe", "/d", "/c", "start", "", target], check=True, timeout=30)
        else:
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                            f'Start-Process "{name}"'], check=True, timeout=30)
        return {"launched": name or target, "backend": "start"}
    if platform.system() == "Darwin":
        cmd = ["open", "-a", name] if name and not target else ["open", target]
        subprocess.run(cmd, check=True, timeout=30)
        return {"launched": name or target, "backend": "open"}
    if target:
        subprocess.run(["xdg-open", target], check=True, timeout=30)
    else:
        subprocess.run(["gtk-launch", name], check=True, timeout=30)
    return {"launched": name or target, "backend": "xdg-open"}


def _guard_destructive(target: str) -> None:
    """删/移之前挡一下明显不该动的路径：根目录、家目录本身和它的一级目录。"""
    if sys.platform == "win32":
        import string

        protected = {
            os.environ.get("SystemRoot", r"C:\Windows").rstrip("\\").lower(),
            os.environ.get("ProgramFiles", r"C:\Program Files").rstrip("\\").lower(),
            os.environ.get("ProgramFiles(x86)", "").rstrip("\\").lower(),
            os.environ.get("ProgramData", "").rstrip("\\").lower(),
            os.environ.get("USERPROFILE", "").rstrip("\\").lower(),
        }
        protected |= {f"{d}:\\".lower() for d in string.ascii_uppercase}
        shielded = {x for x in protected if x}
        low = target.rstrip("\\").lower()
        if low in shielded or low in {x.rstrip("\\") for x in shielded}:
            raise ValueError(f"拒绝操作受保护路径：{target}")
        return
    home = os.path.expanduser("~").rstrip("/")
    if target in ("/", home, "/Users", "/System", "/etc", "/usr", "/bin", "/sbin"):
        raise ValueError(f"拒绝操作受保护路径：{target}")


def op_fs_delete(params: dict) -> dict:
    """删除文件或目录。目录必须显式 recursive=true；返回删掉的条目数与字节数。"""
    target = _abs(params.get("path"))
    _guard_destructive(target)
    if not os.path.exists(target) and not os.path.islink(target):
        raise ValueError(f"路径不存在：{target}")
    recursive = bool(params.get("recursive"))
    if os.path.isdir(target) and not os.path.islink(target):
        if not recursive and any(os.scandir(target)):
            raise ValueError("目录非空：要递归删除必须显式传 recursive=true")
        count, size = 0, 0
        for root, dirs, files in os.walk(target):
            for fn in files:
                count += 1
                try:
                    size += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
            count += len(dirs)
        shutil.rmtree(target)
        return {"path": target, "kind": "dir", "removed": count, "bytes": size,
                "recursive": recursive}
    size = os.path.getsize(target) if os.path.isfile(target) else 0
    os.remove(target)
    return {"path": target, "kind": "file", "removed": 1, "bytes": size, "recursive": False}


def op_fs_move(params: dict) -> dict:
    """移动 / 重命名。目标已存在时拒绝，避免静默覆盖。"""
    src = _abs(params.get("from"))
    dst = _abs(params.get("to"))
    _guard_destructive(src)
    if not os.path.exists(src):
        raise ValueError(f"源路径不存在：{src}")
    if os.path.exists(dst) and not params.get("overwrite"):
        raise ValueError(f"目标已存在：{dst}（要覆盖请显式传 overwrite=true）")
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    shutil.move(src, dst)
    return {"from": src, "to": dst, "overwritten": bool(params.get("overwrite"))}


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
        if method == "input.drag":
            return True, op_input_drag(params), None
        if method == "input.scroll":
            return True, op_input_scroll(params), None
        if method == "window.list":
            return True, op_window_list(params), None
        if method == "ui.tree":
            return True, op_ui_tree(params), None
        if method == "ui.find":
            return True, op_ui_find(params), None
        if method == "ui.invoke":
            return True, op_ui_invoke(params), None
        if method == "fs.delete":
            return True, op_fs_delete(params), None
        if method == "fs.move":
            return True, op_fs_move(params), None
        if method == "app.list":
            return True, op_app_list(params), None
        if method == "app.launch":
            return True, op_app_launch(params), None
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
                                         "User-Agent": f"Backon/{APP_VERSION} (device agent)"})
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
                # 心跳顺带报能力：新装的 .app 无需重新配对，hub 就能刷新设备能力表。
                "caps": caps(), "version": APP_VERSION,
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