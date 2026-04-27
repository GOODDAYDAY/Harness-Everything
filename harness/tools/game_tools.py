"""Game automation tools — launch, screenshot, input, and state query.

These tools communicate with the Godot TestHarness autoload via the
GameBridge TCP client. They are OPTIONAL tools — enable via
``extra_tools: ["game_launch", "game_screenshot", "game_input", "game_state"]``
in the agent config.

The tools share a module-level GameBridge singleton so the game process
persists across tool calls within a single agent run.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

# Module-level bridge singleton — lazily initialized by GameLaunchTool
_bridge: Any = None  # type: GameBridge | None


def _get_bridge() -> Any:
    return _bridge


class GameLaunchTool(Tool):
    """Launch the Godot game and wait for it to be ready."""

    name = "game_launch"
    description = (
        "Launch the Godot game project. Starts the game process and "
        "waits for the in-game test harness to respond. Must be called "
        "before using game_screenshot, game_input, or game_state. "
        "If the game is already running, returns its current status."
    )
    tags = frozenset({"game", "execution"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "stop", "restart", "status"],
                    "description": (
                        "start: launch the game (default). "
                        "stop: shut down the game. "
                        "restart: stop then start. "
                        "status: check if the game is running."
                    ),
                    "default": "start",
                },
            },
        }

    async def execute(self, config: HarnessConfig, **params: Any) -> ToolResult:
        global _bridge
        from harness.game_bridge import GameBridge

        action = params.get("action", "start")
        # Game config: env vars > defaults
        godot_path = os.environ.get("HARNESS_GODOT_PATH", "godot")
        game_path = os.environ.get("HARNESS_GAME_PATH", config.workspace)
        port = int(os.environ.get("HARNESS_GAME_PORT", "19840"))

        if action == "status":
            if _bridge is None or not _bridge.is_running:
                return ToolResult(output="Game is NOT running.")
            resp = await _bridge.ping()
            if resp.ok:
                return ToolResult(output="Game is running and responsive.")
            return ToolResult(output="Game process exists but not responding.")

        if action == "stop":
            if _bridge is None:
                return ToolResult(output="Game is not running.")
            await _bridge.stop()
            _bridge = None
            return ToolResult(output="Game stopped.")

        if action == "restart":
            if _bridge is not None:
                await _bridge.stop()
                _bridge = None

        # Start
        if _bridge is not None and _bridge.is_running:
            return ToolResult(output="Game is already running.")

        _bridge = GameBridge(
            godot_path=godot_path,
            project_path=game_path,
            port=port,
        )
        success = await _bridge.launch()
        if success:
            return ToolResult(output="Game launched successfully and is responding.")
        _bridge = None
        return ToolResult(
            error="Failed to launch game. Check Godot installation and project path.",
            is_error=True,
        )


class GameScreenshotTool(Tool):
    """Capture a screenshot of the running game."""

    name = "game_screenshot"
    description = (
        "Take a screenshot of the currently running game. Returns the "
        "file path and image dimensions. Use this to visually verify "
        "what the game looks like after making changes. The game must "
        "be launched first with game_launch."
    )
    tags = frozenset({"game"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "output_path": {
                    "type": "string",
                    "description": (
                        "Path to save the screenshot PNG. "
                        "Defaults to /tmp/game_screenshot.png"
                    ),
                    "default": "/tmp/game_screenshot.png",
                },
            },
        }

    async def execute(self, config: HarnessConfig, **params: Any) -> ToolResult:
        bridge = _get_bridge()
        if bridge is None or not bridge.is_running:
            return ToolResult(
                error="Game is not running. Call game_launch first.",
                is_error=True,
            )

        path = params.get("output_path", "/tmp/game_screenshot.png")
        resp = await bridge.screenshot(path)
        if not resp.ok:
            return ToolResult(error=f"Screenshot failed: {resp.error}", is_error=True)

        size = resp.data.get("size", [0, 0])

        # Read PNG and base64-encode for image content block
        images: list[dict[str, str]] = []
        png_path = Path(path)
        if png_path.exists():
            raw = png_path.read_bytes()
            # Safety cap: if > 500KB, skip embedding (too large for context)
            if len(raw) <= 500_000:
                b64 = base64.b64encode(raw).decode("ascii")
                images.append({"media_type": "image/png", "data": b64})

        # Query game state for text summary alongside image
        text_parts = [f"Screenshot saved to {path} ({size[0]}x{size[1]})"]
        state_resp = await bridge.get_state()
        if state_resp.ok:
            state = state_resp.data.get("state", {})
            text_parts.append(f"Score: {state.get('score', 0)}")
            text_parts.append(f"Round: {state.get('round', 0)}")
            text_parts.append(f"Trees placed: {state.get('total_trees', 0)}")
            text_parts.append(
                f"Weather: {state.get('weather', {}).get('name', 'none')} "
                f"{state.get('weather', {}).get('icon', '')}"
            )
            hand = state.get("hand", [])
            tree_names = {-1: ".", 0: "E", 1: "F", 2: "B", 3: "S", 4: "W"}
            hand_str = " ".join(tree_names.get(t, "?") for t in hand)
            text_parts.append(f"Hand: {hand_str} ({len(hand)} seeds)")

        return ToolResult(
            output="\n".join(text_parts),
            images=images,
            metadata={"path": path, "width": size[0], "height": size[1]},
        )


class GameInputTool(Tool):
    """Send mouse or keyboard input to the running game."""

    name = "game_input"
    description = (
        "Send simulated input to the running game. Can send mouse "
        "clicks, mouse movement, or keyboard presses. Coordinates are "
        "in viewport space (the game uses 480x270 viewport). The game "
        "must be launched first with game_launch."
    )
    tags = frozenset({"game", "execution"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["click", "key", "motion"],
                    "description": (
                        "click: mouse click at (x, y). "
                        "key: keyboard key press. "
                        "motion: move mouse to (x, y)."
                    ),
                },
                "x": {
                    "type": "integer",
                    "description": "X coordinate in viewport space (0-480).",
                },
                "y": {
                    "type": "integer",
                    "description": "Y coordinate in viewport space (0-270).",
                },
                "key": {
                    "type": "string",
                    "description": (
                        "Key name for 'key' action. Examples: space, enter, "
                        "escape, a-z, 0-9, up, down, left, right, f1-f12."
                    ),
                },
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "description": "Mouse button for 'click' action (default: left).",
                    "default": "left",
                },
            },
            "required": ["action"],
        }

    async def execute(self, config: HarnessConfig, **params: Any) -> ToolResult:
        bridge = _get_bridge()
        if bridge is None or not bridge.is_running:
            return ToolResult(
                error="Game is not running. Call game_launch first.",
                is_error=True,
            )

        action = params.get("action", "")

        if action == "click":
            x = params.get("x", 0)
            y = params.get("y", 0)
            button = params.get("button", "left")
            resp = await bridge.send_click(x, y, button)
            label = f"Clicked ({x}, {y}) with {button} button"

        elif action == "key":
            key = params.get("key", "")
            if not key:
                return ToolResult(
                    error="'key' parameter required for key action",
                    is_error=True,
                )
            resp = await bridge.send_key(key)
            label = f"Pressed key: {key}"

        elif action == "motion":
            x = params.get("x", 0)
            y = params.get("y", 0)
            resp = await bridge.send_motion(x, y)
            label = f"Moved mouse to ({x}, {y})"

        else:
            return ToolResult(
                error=f"Unknown action: {action}. Use click, key, or motion.",
                is_error=True,
            )

        if resp.ok:
            return ToolResult(output=label)
        return ToolResult(error=f"Input failed: {resp.error}", is_error=True)


class GameStateTool(Tool):
    """Query the current game state."""

    name = "game_state"
    description = (
        "Query the internal state of the running game: current score, "
        "round number, grid contents, player hand, active bonds, and "
        "weather. Use this to verify game logic after making changes "
        "or after sending input. The game must be launched first with "
        "game_launch."
    )
    tags = frozenset({"game"})

    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, config: HarnessConfig, **params: Any) -> ToolResult:
        bridge = _get_bridge()
        if bridge is None or not bridge.is_running:
            return ToolResult(
                error="Game is not running. Call game_launch first.",
                is_error=True,
            )

        resp = await bridge.get_state()
        if not resp.ok:
            return ToolResult(
                error=f"State query failed: {resp.error}", is_error=True,
            )

        state = resp.data.get("state", {})

        # Format for readability
        lines = [
            f"Score: {state.get('score', 0)}",
            f"Round: {state.get('round', 0)}",
            f"Trees placed: {state.get('total_trees', 0)}",
            f"Selected seed: {state.get('selected_seed', -1)}",
            f"Weather: {state.get('weather', {}).get('name', 'none')} "
            f"{state.get('weather', {}).get('icon', '')}",
            "",
            f"Grid ({state.get('grid_cols', 4)}x{state.get('grid_rows', 3)}):",
        ]

        tree_names = {-1: ".", 0: "E", 1: "F", 2: "B", 3: "S", 4: "W"}
        grid = state.get("grid", [])
        cols = state.get("grid_cols", 4)
        for row_idx in range(state.get("grid_rows", 3)):
            row_cells = []
            for col_idx in range(cols):
                idx = row_idx * cols + col_idx
                if idx < len(grid):
                    cell = grid[idx]
                    if cell.get("blocked"):
                        row_cells.append("X")
                    else:
                        row_cells.append(tree_names.get(cell.get("type", -1), "?"))
                else:
                    row_cells.append("?")
            lines.append("  " + " ".join(row_cells))

        hand = state.get("hand", [])
        hand_names = [tree_names.get(t, "?") for t in hand]
        lines.append(f"\nHand: {' '.join(hand_names)} ({len(hand)} seeds)")

        bonds = state.get("bonds", [])
        if bonds:
            lines.append(f"\nBonds ({len(bonds)}):")
            for b in bonds:
                lines.append(
                    f"  {b.get('bond_name', '?')}: "
                    f"({b['cell_a'][0]},{b['cell_a'][1]}) - "
                    f"({b['cell_b'][0]},{b['cell_b'][1]})"
                )

        return ToolResult(
            output="\n".join(lines),
            metadata={"state": state},
        )
