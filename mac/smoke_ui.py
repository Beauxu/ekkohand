#!/usr/bin/env python3
"""CI 冒烟测试：确认引导窗用到的每个 AppKit API 在当前系统上真实存在。

背景：v1.1.3 用了 NSApplication.activateWithOptions_（这个 selector 只存在于
NSRunningApplication 上），本地无 Mac 可验证，结果用户在点菜单时才撞上崩溃。
这类错误必须让 CI 先发现，不能拿用户当测试机。

本脚本不创建窗口（无头 runner 也能跑），只做存在性检查。
"""
from __future__ import annotations

import sys

import objc  # noqa: F401  PyObjC

import AppKit
import Foundation

import backon_app as m  # noqa: F401  导入即校验模块可加载、类在顶层


FAILS: list[str] = []


def need(obj, name: str, label: str) -> None:
    if not hasattr(obj, name):
        FAILS.append(f"{label}.{name} 不存在")


def main() -> int:
    app = AppKit.NSApplication.sharedApplication()

    # 激活：只有这两个，没有 activateWithOptions:
    if not (hasattr(app, "activateIgnoringOtherApps_") or hasattr(app, "activate")):
        FAILS.append("NSApplication 既没有 activateIgnoringOtherApps_ 也没有 activate")
    if hasattr(app, "activateWithOptions_"):
        pass  # 有也无害；没有才是常态

    panel = AppKit.NSPanel.alloc()
    for name in ("initWithContentRect_styleMask_backing_defer_", "setFloatingPanel_",
                 "setLevel_", "setReleasedWhenClosed_", "contentView", "setTitle_",
                 "makeKeyAndOrderFront_", "setDelegate_", "center"):
        need(panel, name, "NSPanel")

    img = AppKit.NSImageView.alloc()
    for name in ("initWithFrame_", "setImage_", "bounds", "beginDraggingSessionWithItems_event_source_"):
        need(img, name, "NSImageView")

    btn = AppKit.NSButton.alloc()
    for name in ("initWithFrame_", "setTitle_", "setBezelStyle_", "setTarget_",
                 "setAction_", "setHidden_"):
        need(btn, name, "NSButton")

    tf = AppKit.NSTextField.alloc()
    for name in ("initWithFrame_", "setStringValue_", "setBezeled_", "setDrawsBackground_",
                 "setEditable_", "setSelectable_", "setFont_", "setTextColor_"):
        need(tf, name, "NSTextField")

    for name in ("boldSystemFontOfSize_", "systemFontOfSize_"):
        need(AppKit.NSFont, name, "NSFont")
    need(AppKit.NSColor, "secondaryLabelColor", "NSColor")
    need(AppKit.NSWorkspace.sharedWorkspace(), "iconForFile_", "NSWorkspace")
    need(AppKit.NSDraggingItem.alloc(), "initWithPasteboardWriter_", "NSDraggingItem")
    need(AppKit.NSDraggingItem.alloc(), "setDraggingFrame_contents_", "NSDraggingItem")
    need(Foundation.NSTimer, "scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_", "NSTimer")
    need(Foundation.NSURL, "fileURLWithPath_", "NSURL")

    # 引导窗的两个类是模块顶层类（PyObjC 重复注册会直接 crash）
    for cls in ("_GuideDragIcon", "_GuideCtrl"):
        if not hasattr(m, cls):
            FAILS.append(f"模块里缺类 {cls}")
    src = open(m.__file__, encoding="utf-8").read()
    head = src[:src.index("class BackonApp")]
    if "class _GuideDragIcon" not in head or "class _GuideCtrl" not in head:
        FAILS.append("引导窗类没在模块顶层定义（会触发 overriding existing Objective-C class）")

    if FAILS:
        print("SMOKE FAILED")
        for f in FAILS:
            print(" ::error::", f)
        return 1
    print("SMOKE OK：引导窗用到的 AppKit API 全部存在")
    return 0


if __name__ == "__main__":
    sys.exit(main())
