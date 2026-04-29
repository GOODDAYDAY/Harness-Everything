"""Game automation tools — launch, screenshot, input, and state query.

These tools communicate with the Godot TestHarness autoload via the
GameBridge TCP client. They are OPTIONAL tools — enable via
``extra_tools: ["game_launch", "game_screenshot", "game_input", "game_state"]``
in the agent config.

The tools share a module-level GameBridge singleton so the game process
persists across tool calls within a single agent run.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import os
import signal
import shutil
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

# Module-level bridge singleton — lazily initialized by GameLaunchTool
_bridge: Any = None  # type: GameBridge | None

# Module-level screen recording state (independent of bridge)
_recording_process: asyncio.subprocess.Process | None = None
_recording_output: str | None = None


def _kill_recording_sync() -> None:
    """Kill orphaned ffmpeg recording process on interpreter exit."""
    global _recording_process
    if _recording_process is not None and _recording_process.returncode is None:
        try:
            os.kill(_recording_process.pid, signal.SIGTERM)
        except OSError:
            pass
        _recording_process = None


atexit.register(_kill_recording_sync)


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

        tool_cfg = config.tool_config.get("game_screenshot", {})
        default_dir = tool_cfg.get("output_dir", "/tmp")
        default_path = str(Path(default_dir) / "game_screenshot.png")
        path = params.get("output_path", default_path)
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
            text_parts.append(f"Phase: {state.get('phase', '?')}")
            text_parts.append(f"Round: {state.get('round', 0)}  Day: {state.get('day', 0)}")
            text_parts.append(
                f"Alive: {state.get('alive_count', 0)} "
                f"(Good: {state.get('alive_good_count', 0)}, "
                f"Evil: {state.get('alive_evil_count', 0)})"
            )
            players = state.get("players", [])
            role_icons = {
                "werewolf": "🐺", "seer": "🔮", "witch": "🧪",
                "hunter": "🏹", "guard": "🛡️", "villager": "👤", "none": "❓",
            }
            player_summary = " ".join(
                f"[{role_icons.get(p.get('role','none'),'?')}{'💀' if not p.get('alive') else ''}]"
                for p in players[:12]
            )
            text_parts.append(f"Players: {player_summary}")

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
        "Query the internal state of the running game: current phase, "
        "round/day numbers, player list with roles/teams/status, "
        "alive counts by faction, sheriff, game log, and winner. "
        "Use this to verify game logic after making changes "
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

        # ── Werewolf game state formatting ───────────────────────────
        role_icons = {
            "werewolf": "🐺", "seer": "🔮", "witch": "🧪",
            "hunter": "🏹", "guard": "🛡️", "villager": "👤", "none": "❓",
        }
        phase_icons = {
            "setup": "⚙️", "day_sheriff_election": "⭐",
            "night_guard": "🛡️", "night_werewolf": "🐺",
            "night_witch": "🧪", "night_seer": "🔮",
            "day_announce": "📢", "day_discussion": "💬",
            "day_vote": "🗳️", "day_result": "🏆", "game_over": "🎮",
        }

        phase = state.get("phase", "?")
        pi = phase_icons.get(phase, "")
        lines = [
            f"{pi} Phase: {phase} (enum={state.get('phase_enum', '?')})",
            f"Round: {state.get('round', 0)}  Day: {state.get('day', 0)}",
            f"Alive: {state.get('alive_count', 0)}  "
            f"Good: {state.get('alive_good_count', 0)}  "
            f"Evil: {state.get('alive_evil_count', 0)}",
        ]

        sheriff = state.get("sheriff", -1)
        if sheriff >= 0:
            lines.append(f"Sheriff: Player {sheriff}")

        winner = state.get("winner", "")
        if winner:
            lines.append(f"Winner: {winner}")

        lines.append("")
        lines.append("Players:")
        for p in state.get("players", []):
            role = p.get("role", "none")
            icon = role_icons.get(role, "❓")
            status = "💀" if not p.get("alive") else "  "
            sheriff_flag = "⭐" if p.get("is_sheriff") else "  "
            human_flag = "👤" if p.get("is_human") else "  "
            lines.append(
                f"  [{p['index']:2d}] {icon} {status} {sheriff_flag} {human_flag} "
                f"{role:>10s} ({p.get('team','?'):>4s})  {p.get('name','')}"
            )

        game_log = state.get("game_log", [])
        if game_log:
            lines.append("")
            lines.append(f"Game Log ({len(game_log)} entries):")
            for entry in game_log[-10:]:  # Last 10 entries
                lines.append(f"  {entry}")

        return ToolResult(
            output="\n".join(lines),
            metadata={"state": state},
        )


class GameRecordTool(Tool):
    """Record gameplay video for sharing or agent verification."""

    name = "game_record"
    description = (
        "Record gameplay video. Two modes:\n"
        "  start/stop: Screen recording via ffmpeg (30fps, full screen, "
        "high quality for video platforms like Bilibili). Independent of "
        "the game bridge — works with any running Godot instance.\n"
        "  frames: Capture frames via TCP bridge and stitch with ffmpeg "
        "(lower fps, for quick agent verification). Requires game_launch."
    )
    tags = frozenset({"game"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "stop", "frames"],
                    "description": (
                        "start: begin screen recording (ffmpeg avfoundation). "
                        "stop: end recording and save video file. "
                        "frames: capture N frames via TCP bridge then stitch."
                    ),
                },
                "output": {
                    "type": "string",
                    "description": "Output file path.",
                    "default": "/tmp/game_recording.mp4",
                },
                "fps": {
                    "type": "integer",
                    "description": (
                        "Frames per second. Default 30 for screen mode, "
                        "10 for frames mode."
                    ),
                },
                "duration": {
                    "type": "number",
                    "description": "Duration in seconds (frames mode only).",
                    "default": 5.0,
                },
                "screen_id": {
                    "type": "string",
                    "description": (
                        "macOS avfoundation screen device index. Default '1'. "
                        "Run `ffmpeg -f avfoundation -list_devices true -i "
                        '""` to list available devices.'
                    ),
                    "default": "1",
                },
            },
            "required": ["action"],
        }

    async def execute(self, config: HarnessConfig, **params: Any) -> ToolResult:
        global _recording_process, _recording_output

        action = params.get("action", "")

        if action == "start":
            if _recording_process is not None:
                return ToolResult(
                    error="Recording already in progress. Stop it first.",
                    is_error=True,
                )

            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg is None:
                return ToolResult(
                    error="ffmpeg not found. Install with: brew install ffmpeg",
                    is_error=True,
                )

            output = params.get("output", "/tmp/game_recording.mp4")
            fps = params.get("fps", 30)
            screen_id = params.get("screen_id", "1")
            _recording_output = output

            cmd = [
                ffmpeg, "-y",
                "-f", "avfoundation",
                "-framerate", str(fps),
                "-capture_cursor", "1",
                "-i", f"{screen_id}:none",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-pix_fmt", "yuv420p",
                output,
            ]

            _recording_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            return ToolResult(
                output=(
                    f"Screen recording started (PID {_recording_process.pid})\n"
                    f"Output: {output}\n"
                    f"Settings: {fps}fps, screen device {screen_id}\n"
                    f"Stop with: game_record action=stop"
                ),
            )

        elif action == "stop":
            if _recording_process is None:
                return ToolResult(error="No active recording.", is_error=True)

            output = _recording_output
            proc = _recording_process
            _recording_process = None
            _recording_output = None

            # Send 'q' to ffmpeg stdin for graceful shutdown
            try:
                if proc.stdin:
                    proc.stdin.write(b"q")
                    await proc.stdin.drain()
            except (BrokenPipeError, OSError):
                try:
                    proc.send_signal(signal.SIGINT)
                except OSError:
                    pass

            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

            if output and os.path.isfile(output):
                size_mb = os.path.getsize(output) / (1024 * 1024)
                return ToolResult(
                    output=f"Recording saved: {output} ({size_mb:.1f} MB)",
                    metadata={"path": output, "size_mb": round(size_mb, 1)},
                )
            return ToolResult(
                error=f"Recording file not found: {output}",
                is_error=True,
            )

        elif action == "frames":
            bridge = _get_bridge()
            if bridge is None or not bridge.is_running:
                return ToolResult(
                    error="Game is not running. Call game_launch first.",
                    is_error=True,
                )
            duration = params.get("duration", 5.0)
            fps = params.get("fps", 10)
            output = params.get("output", "/tmp/game_recording.mp4")
            path = await bridge.record_video(
                duration=duration, fps=fps, output=output,
            )
            if path:
                return ToolResult(
                    output=(
                        f"Frame recording saved: {path}\n"
                        f"{int(duration * fps)} frames at {fps}fps"
                    ),
                    metadata={"path": path},
                )
            return ToolResult(
                error="Frame recording failed. Check ffmpeg installation.",
                is_error=True,
            )

        return ToolResult(
            error=f"Unknown action: {action}. Use start, stop, or frames.",
            is_error=True,
        )
