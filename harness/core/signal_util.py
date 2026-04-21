"""Shared SIGINT/SIGTERM handler installation for async loops.

AgentLoop and PipelineLoop both want the same "finish the current unit of
work, then exit cleanly on Ctrl-C" semantics. This module provides one
implementation they share — the try/except against NotImplementedError is
there because add_signal_handler is Unix-only and Windows local dev would
otherwise crash.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Callable

log = logging.getLogger(__name__)

_HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def install_shutdown_handlers(callback: Callable[[], None]) -> None:
    """Register ``callback`` for SIGINT and SIGTERM on the running event loop.

    Silent no-op on platforms where add_signal_handler isn't supported
    (Windows). Callers there rely on the default Ctrl-C → KeyboardInterrupt
    behaviour, which is acceptable for local dev but lacks graceful shutdown.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.debug("install_shutdown_handlers: no running loop — skipped")
        return
    try:
        for sig in _HANDLED_SIGNALS:
            loop.add_signal_handler(sig, callback)
    except NotImplementedError:
        log.debug("install_shutdown_handlers: not supported on this platform")


def uninstall_shutdown_handlers() -> None:
    """Restore default handlers for SIGINT/SIGTERM. Safe to call unconditionally."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for sig in _HANDLED_SIGNALS:
        try:
            loop.remove_signal_handler(sig)
        except (NotImplementedError, RuntimeError):
            pass
