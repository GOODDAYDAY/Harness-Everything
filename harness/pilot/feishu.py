"""Feishu client — WebSocket connection, message send/receive, interactive card management.

Wraps lark-oapi to provide a clean async interface for the pilot's Feishu interactions.
lark-oapi is lazy-imported at connect() time so the module can be imported without
the SDK installed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

_RECONNECT_BASE_DELAY: float = 5.0
_RECONNECT_MAX_DELAY: float = 60.0
_DEDUP_CACHE_SIZE: int = 1000


@dataclass
class FeishuMessage:
    """Parsed incoming Feishu message."""

    chat_id: str
    sender_id: str
    text: str
    message_id: str
    chat_type: str  # "private" or "group"


@dataclass
class CardAction:
    """Parsed Feishu card button click."""

    action: str  # value from button's value.action field
    chat_id: str
    sender_id: str
    message_id: str
    raw_value: dict[str, Any]


# Type aliases for callbacks
MessageCallback = Callable[[FeishuMessage], Awaitable[None]]
CardActionCallback = Callable[[CardAction], Awaitable[None]]


class FeishuClient:
    """Async Feishu client with WebSocket event reception and REST message sending.

    Lifecycle: create → connect() → use send_* methods → close().
    Incoming messages and card actions are dispatched to registered callbacks.
    """

    def __init__(self, app_id: str, app_secret: str) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._rest_client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._dedup_cache: OrderedDict[str, bool] = OrderedDict()

        # Callbacks — set before connect()
        self._on_message: MessageCallback | None = None
        self._on_card_action: CardActionCallback | None = None

    def on_message(self, callback: MessageCallback) -> None:
        """Register handler for incoming text messages."""
        self._on_message = callback

    def on_card_action(self, callback: CardActionCallback) -> None:
        """Register handler for card button clicks."""
        self._on_card_action = callback

    async def connect(self) -> None:
        """Establish Feishu REST client and WebSocket connection.

        Starts a background thread for the WebSocket event loop.
        Raises ImportError with install instructions if lark-oapi is missing.
        """
        # 1. Lazy-import lark-oapi
        lark = _import_lark_oapi()

        # 2. Capture the running event loop for cross-thread dispatch
        self._loop = asyncio.get_running_loop()
        self._running = True

        # 3. Build REST client
        self._rest_client = self._build_rest_client(lark)

        # 4. Build and start WebSocket client
        self._ws_client = self._build_ws_client(lark)
        self._ws_thread = self._start_ws_thread()

        log.info("FeishuClient connected, app_id=%s", self._app_id)

    async def send_card(self, chat_id: str, card: dict[str, Any]) -> str | None:
        """Send an interactive card message to a chat.

        Returns the message_id on success, None on failure.
        """
        content = json.dumps(card, ensure_ascii=False)
        return await self._send_message(chat_id, "interactive", content)

    async def send_text(self, chat_id: str, text: str) -> str | None:
        """Send a plain text message to a chat."""
        content = json.dumps({"text": text}, ensure_ascii=False)
        return await self._send_message(chat_id, "text", content)

    async def send_markdown(self, chat_id: str, markdown: str) -> str | None:
        """Send a markdown message wrapped in an interactive card."""
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "💬"},
                "template": "grey",
            },
            "elements": [{"tag": "markdown", "content": markdown}],
        }
        return await self.send_card(chat_id, card)

    async def update_card(self, message_id: str, card: dict[str, Any]) -> bool:
        """Update an existing card message's content via PATCH API.

        Returns True on success, False on failure.
        """
        if not self._rest_client:
            log.error("Cannot update card: REST client not initialized")
            return False

        def _do_update() -> bool:
            return self._update_card_sync(message_id, card)

        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, _do_update)
        except Exception as exc:
            log.error("Failed to update Feishu card: %s", exc)
            return False

    def _update_card_sync(self, message_id: str, card: dict[str, Any]) -> bool:
        """Synchronous REST call to PATCH a card message."""
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        content = json.dumps(card, ensure_ascii=False)
        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(content)
                .build()
            )
            .build()
        )
        resp = self._rest_client.im.v1.message.patch(req)
        if resp.success():
            log.info("Feishu card updated, msg_id=%s", message_id)
            return True
        log.error("Feishu card update failed, code=%s, msg=%s", resp.code, resp.msg)
        return False

    async def add_reaction(self, message_id: str, emoji: str = "OK") -> bool:
        """Add an emoji reaction to a message. Returns True on success."""
        if not self._rest_client:
            return False

        def _do_react() -> bool:
            return self._add_reaction_sync(message_id, emoji)

        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, _do_react)
        except Exception as exc:
            log.debug("Failed to add reaction: %s", exc)
            return False

    def _add_reaction_sync(self, message_id: str, emoji: str) -> bool:
        """Synchronous REST call to add a reaction."""
        import lark_oapi as lark
        req = lark.RawRequest()
        req.method = "POST"
        req.uri = f"/open-apis/im/v1/messages/{message_id}/reactions"
        req.body = {"reaction_type": {"emoji_type": emoji}}
        resp = self._rest_client.request(req)
        if resp.success():
            log.debug("Reaction added to %s, emoji=%s", message_id, emoji)
            return True
        log.debug("Reaction failed, code=%s", resp.code)
        return False

    async def close(self) -> None:
        """Disconnect WebSocket and clean up resources."""
        log.info("FeishuClient closing")
        self._running = False
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=5.0)
        self._rest_client = None
        self._ws_client = None
        log.info("FeishuClient closed")

    # ══════════════════════════════════════════════════════════════════════
    #  REST message sending
    # ══════════════════════════════════════════════════════════════════════

    async def _send_message(
        self, chat_id: str, msg_type: str, content: str
    ) -> str | None:
        """Send a message via Feishu REST API in a thread executor."""
        if not self._rest_client:
            log.error("Cannot send message: REST client not initialized")
            return None

        def _do_send() -> str | None:
            return self._send_message_sync(chat_id, msg_type, content)

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _do_send)
            return result
        except Exception as exc:
            log.error("Failed to send Feishu message: %s", exc)
            return None

    def _send_message_sync(
        self, chat_id: str, msg_type: str, content: str
    ) -> str | None:
        """Synchronous REST API call to create a message."""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        receive_id_type = "chat_id" if chat_id.startswith("oc_") else "open_id"
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        resp = self._rest_client.im.v1.message.create(req)
        if resp.success():
            msg_id = resp.data.message_id
            log.info("Feishu message sent, chat_id=%s, msg_type=%s, msg_id=%s", chat_id, msg_type, msg_id)
            return msg_id
        log.error("Feishu send failed, code=%s, msg=%s", resp.code, resp.msg)
        return None

    # ══════════════════════════════════════════════════════════════════════
    #  WebSocket setup and event handling
    # ══════════════════════════════════════════════════════════════════════

    def _build_rest_client(self, lark: Any) -> Any:
        """Create the lark REST API client."""
        client = (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )
        log.debug("Feishu REST client built")
        return client

    def _build_ws_client(self, lark: Any) -> Any:
        """Create the lark WebSocket client with event handlers registered."""
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._handle_message_sync)
            .register_p2_card_action_trigger(self._handle_card_action_sync)
            .build()
        )
        ws_client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.WARNING,
        )
        log.debug("Feishu WebSocket client built")
        return ws_client

    def _start_ws_thread(self) -> threading.Thread:
        """Start WebSocket client in a daemon thread with auto-reconnect."""
        thread = threading.Thread(target=self._ws_loop, daemon=True, name="feishu-ws")
        thread.start()
        log.debug("Feishu WebSocket thread started")
        return thread

    def _ws_loop(self) -> None:
        """WebSocket event loop running in a dedicated thread.

        Implements exponential backoff reconnection on disconnect.
        """
        import lark_oapi.ws.client as _ws_mod
        _ws_mod.loop = asyncio.new_event_loop()

        delay = _RECONNECT_BASE_DELAY
        while self._running:
            try:
                log.info("Feishu WebSocket connecting")
                self._ws_client.start()
            except Exception as exc:
                log.warning("Feishu WebSocket disconnected: %s", exc)

            if not self._running:
                break

            log.info("Feishu WebSocket reconnecting in %.0fs", delay)
            time.sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX_DELAY)

        log.info("Feishu WebSocket loop exited")

    # ── Event dispatch (sync → async bridge) ─────────────────────────────

    def _handle_message_sync(self, data: Any) -> None:
        """Sync callback from WebSocket thread; dispatches to async handler."""
        if self._loop and self._on_message:
            asyncio.run_coroutine_threadsafe(
                self._handle_message_async(data), self._loop
            )

    def _handle_card_action_sync(self, data: Any) -> Any:
        """Sync callback for card button clicks; dispatches to async handler."""
        if self._loop and self._on_card_action:
            asyncio.run_coroutine_threadsafe(
                self._handle_card_action_async(data), self._loop
            )
        return self._build_card_ack()

    async def _handle_message_async(self, data: Any) -> None:
        """Parse and dispatch an incoming Feishu message."""
        try:
            msg = self._parse_message_event(data)
            if msg is None:
                return
            if self._is_duplicate(msg.message_id):
                return
            log.info(
                "Feishu message received, chat_id=%s, sender=%s, text_len=%d",
                msg.chat_id, msg.sender_id, len(msg.text),
            )
            if self._on_message:
                await self._on_message(msg)
        except Exception as exc:
            log.error("Error handling Feishu message: %s", exc)

    async def _handle_card_action_async(self, data: Any) -> None:
        """Parse and dispatch a Feishu card action (button click)."""
        try:
            action = self._parse_card_action_event(data)
            if action is None:
                return
            log.info(
                "Feishu card action received, action=%s, chat_id=%s, sender=%s",
                action.action, action.chat_id, action.sender_id,
            )
            if self._on_card_action:
                await self._on_card_action(action)
        except Exception as exc:
            log.error("Error handling Feishu card action: %s", exc)

    # ── Event parsing ────────────────────────────────────────────────────

    @staticmethod
    def _parse_message_event(data: Any) -> FeishuMessage | None:
        """Extract a FeishuMessage from a raw WebSocket event."""
        event = data.event
        message = event.message
        sender = event.sender

        # Skip bot messages
        if sender.sender_type == "bot":
            return None

        msg_type = message.message_type
        if msg_type != "text" or not message.content:
            log.debug("Skipping non-text message, type=%s", msg_type)
            return None

        content_json = json.loads(message.content)
        text = content_json.get("text", "").strip()
        if not text:
            return None

        sender_id = ""
        if sender.sender_id:
            sender_id = sender.sender_id.open_id or ""

        return FeishuMessage(
            chat_id=message.chat_id,
            sender_id=sender_id,
            text=text,
            message_id=message.message_id,
            chat_type=message.chat_type or "private",
        )

    @staticmethod
    def _parse_card_action_event(data: Any) -> CardAction | None:
        """Extract a CardAction from a raw WebSocket card action event."""
        event = data.event
        action = getattr(event, "action", None)
        if not action:
            return None

        raw_value = dict(getattr(action, "value", None) or {})
        action_name = raw_value.get("action", "")
        if not action_name:
            return None

        ctx = getattr(event, "context", None)
        chat_id = getattr(ctx, "open_chat_id", "") or ""
        message_id = getattr(ctx, "open_message_id", "") or ""

        operator = getattr(event, "operator", None)
        sender_id = getattr(operator, "open_id", "") or ""

        return CardAction(
            action=action_name,
            chat_id=chat_id,
            sender_id=sender_id,
            message_id=message_id,
            raw_value=raw_value,
        )

    @staticmethod
    def _build_card_ack() -> Any:
        """Build the required acknowledgement response for card actions."""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
            CallBackCard,
        )
        card = CallBackCard()
        card.type = "raw"
        card.data = {}
        resp = P2CardActionTriggerResponse()
        resp.card = card
        return resp

    # ── Deduplication ────────────────────────────────────────────────────

    def _is_duplicate(self, message_id: str) -> bool:
        """Check and record message_id to prevent duplicate processing."""
        if message_id in self._dedup_cache:
            log.debug("Duplicate message skipped, msg_id=%s", message_id)
            return True
        self._dedup_cache[message_id] = True
        while len(self._dedup_cache) > _DEDUP_CACHE_SIZE:
            self._dedup_cache.popitem(last=False)
        return False


# ══════════════════════════════════════════════════════════════════════════
#  Card builders
# ══════════════════════════════════════════════════════════════════════════

def build_pilot_card(
    proposal: str,
    branch: str,
    status: str = "discussing",
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build or rebuild the single pilot card for all lifecycle phases.

    Args:
        proposal: The improvement proposal markdown text.
        branch: Source branch name (e.g. "main", "feat/harness").
        status: One of "discussing", "approved", "executing", "done",
                "rejected", "expired", "no_action".
        result: Execution result dict with keys: cycles_run, tool_calls,
                mission_status, summary, exec_branch.
    """
    if status == "no_action":
        return _build_no_action_card(proposal)

    # Header varies by status
    header_map = {
        "discussing": ("每日改进提案", "blue"),
        "approved": ("每日改进提案 — 已批准", "wathet"),
        "executing": ("每日改进提案 — 执行中", "orange"),
        "done": ("每日改进提案 — 已完成", "green"),
        "rejected": ("每日改进提案 — 已拒绝", "red"),
        "expired": ("每日改进提案 — 已过期", "grey"),
    }
    title, template = header_map.get(status, ("每日改进提案", "blue"))

    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**分支**: `{branch}`"},
        {"tag": "hr"},
        {"tag": "markdown", "content": _sanitize_feishu_markdown(proposal)},
    ]

    # Status-specific footer
    if status == "discussing":
        elements.append({"tag": "hr"})
        elements.append(
            {"tag": "markdown", "content": "直接发消息可以讨论方案，讨论完再点按钮决定。"}
        )
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "批准执行"},
                    "type": "primary",
                    "value": {"action": "approve"},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "拒绝"},
                    "type": "danger",
                    "value": {"action": "reject"},
                },
            ],
        })
    elif status == "approved":
        elements.append({"tag": "hr"})
        elements.append(
            {"tag": "markdown", "content": "已批准，正在创建执行分支..."}
        )
    elif status == "executing":
        exec_branch = (result or {}).get("exec_branch", "")
        elements.append({"tag": "hr"})
        elements.append(
            {"tag": "markdown", "content": (
                f"正在执行改进\n"
                f"原分支：`{branch}` → 执行分支：`{exec_branch}`"
            )}
        )
    elif status == "done" and result:
        exec_branch = result.get("exec_branch", "")
        mission_status = result.get("mission_status", "")
        status_text = "已完成" if mission_status == "complete" else mission_status
        elements.append({"tag": "hr"})
        summary = result.get("summary", "")[:2000]
        elements.append({"tag": "markdown", "content": (
            f"**执行结果**\n\n"
            f"- 状态：{status_text}\n"
            f"- 执行轮次：{result.get('cycles_run', 0)}\n"
            f"- 工具调用：{result.get('tool_calls', 0)}\n"
            f"- 原分支：`{branch}` → 执行分支：`{exec_branch}`\n\n"
        )})
        if summary:
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": _sanitize_feishu_markdown(summary)})
    elif status in ("rejected", "expired"):
        elements.append({"tag": "hr"})
        label = "已拒绝" if status == "rejected" else "已过期（未在规定时间内回复）"
        elements.append({"tag": "markdown", "content": label})

    return {
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "elements": elements,
    }


def _sanitize_feishu_markdown(text: str) -> str:
    """Convert standard markdown to Feishu card-compatible markdown.

    Feishu interactive cards do not render ## headers or --- horizontal rules.
    Converts headers to bold text and strips horizontal rules.
    """
    import re
    # Convert ## Header → **Header**  (any heading level)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"**\1**", text, flags=re.MULTILINE)
    # Remove standalone --- horizontal rules
    text = re.sub(r"^-{3,}\s*$", "", text, flags=re.MULTILINE)
    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _build_no_action_card(summary: str) -> dict[str, Any]:
    """Build a brief 'all clear' card when diagnosis finds no issues."""
    return {
        "header": {
            "title": {"tag": "plain_text", "content": "每日检查 — 一切正常"},
            "template": "green",
        },
        "elements": [{"tag": "markdown", "content": summary}],
    }


# ══════════════════════════════════════════════════════════════════════════
#  Module-level helpers
# ══════════════════════════════════════════════════════════════════════════

def _import_lark_oapi() -> Any:
    """Lazy-import lark_oapi; raise ImportError with install instructions if missing."""
    try:
        import lark_oapi
        return lark_oapi
    except ImportError:
        raise ImportError(
            "lark-oapi is not installed.  Install with: "
            "pip install 'harness-everything[pilot]'  "
            "(or: pip install lark-oapi)"
        )
