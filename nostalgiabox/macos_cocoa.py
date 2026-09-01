"""macOS Cocoa run-loop helpers for libmpv.

libmpv's video output on macOS is Cocoa-based. Audio still plays if the
process never pumps NSApplication events; the picture window does not
appear, and the Python Dock icon bounces. The Raspberry Pi path never
imports this module.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import c_double, c_int32, c_void_p

log = logging.getLogger(__name__)

_ready = False
_cf = None
_k_default_mode = None


def is_macos() -> bool:
    return sys.platform == "darwin"


def _objc_msg(objc, receiver: int, selector: bytes, *extra_argtypes, extra_args=()):
    sel = objc.sel_registerName(selector)
    send = objc.objc_msgSend
    send.restype = c_void_p
    send.argtypes = [c_void_p, c_void_p, *extra_argtypes]
    return send(c_void_p(receiver), c_void_p(sel), *extra_args)


def ensure_nsapplication() -> None:
    """Create the process-wide NSApplication on the main thread (once)."""
    global _ready, _cf, _k_default_mode
    if _ready or not is_macos():
        return
    try:
        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        objc.sel_registerName.restype = c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.objc_getClass.restype = c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]

        ns_app_cls = objc.objc_getClass(b"NSApplication")
        app = _objc_msg(objc, ns_app_cls, b"sharedApplication")
        if not app:
            raise RuntimeError("NSApplication.sharedApplication returned nil")

        # NSApplicationActivationPolicyRegular == 0 so we get a Dock icon + window.
        _objc_msg(
            objc,
            app,
            b"setActivationPolicy:",
            ctypes.c_long,
            extra_args=(ctypes.c_long(0),),
        )
        _objc_msg(
            objc,
            app,
            b"activateIgnoringOtherApps:",
            ctypes.c_bool,
            extra_args=(ctypes.c_bool(True),),
        )

        _cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        _k_default_mode = c_void_p.in_dll(_cf, "kCFRunLoopDefaultMode")
        _cf.CFRunLoopRunInMode.argtypes = [c_void_p, c_double, c_int32]
        _cf.CFRunLoopRunInMode.restype = c_int32
        _ready = True
        log.info("macOS Cocoa run loop attached (libmpv needs this to show video)")
    except Exception:  # noqa: BLE001
        log.exception("could not initialise Cocoa for libmpv; video window may stay hidden")


def pump(seconds: float = 0.02) -> None:
    """Give Cocoa a timeslice so the mpv window can map and redraw."""
    if not is_macos():
        return
    ensure_nsapplication()
    if not _ready or _cf is None:
        return
    try:
        _cf.CFRunLoopRunInMode(_k_default_mode, float(max(0.0, seconds)), 1)
    except Exception:  # noqa: BLE001
        log.debug("Cocoa pump failed", exc_info=True)
