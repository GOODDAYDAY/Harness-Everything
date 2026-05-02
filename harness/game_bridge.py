"""GameBridge — async TCP client for the Godot TestHarness autoload.

Manages the Godot game process lifecycle and communicates with the
in-game TestHarness via a newline-delimited JSON protocol on TCP port
19840.

Usage:

    bridge = GameBridge(godot_path="/usr/local/bin/godot",
                        project_path="/path/to/game")
    await bridge.launch()
    await bridge.screenshot("/tmp/shot.png")
    state = await bridge.get_state()
    await bridge.stop()
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Track all active bridge instances for cleanup on interpreter exit.
_active_bridges: set["GameBridge"] = set()

log = logging.getLogger(__name__)

DEFAULT_PORT = 19840
DEFAULT_HOST = "127.0.0.1"


@dataclass
class BridgeResponse:
    """Parsed response from the TestHarness."""

    ok: bool
    data: dict[str, Any]
    error: str = ""


class GameBridge:
    """Manages a Godot game process and communicates via TCP."""

    def __init__(
        self,
        godot_path: str,
        project_path: str,
        port: int = DEFAULT_PORT,
        host: str = DEFAULT_HOST,
    ) -> None:
        self.godot_path = godot_path
        self.project_path = str(Path(project_path).resolve())
        self.port = port
        self.host = host
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        _active_bridges.add(self)

    # ── Process management ────────────────────────────────────────────────

    async def launch(self, timeout: float = 15.0) -> bool:
        """Start the game and wait for the TCP server to be ready.

        Returns True if the game launched and responded to ping.
        """
        if self._process is not None and self._process.returncode is None:
            log.warning("Game already running (PID %d)", self._process.pid)
            return True

        # Wait for the port to be free (TIME_WAIT cleanup on Windows)
        self._wait_port_free(self.port, timeout=5.0)

        game_type = os.environ.get("HARNESS_GAME_TYPE", "godot")

        if game_type == "python":
            self._process = await self._launch_python()
        else:
            self._process = await self._launch_godot()
        if self._process is None:
            return False
        log.info("Godot started with PID %d", self._process.pid)

        # Wait for TestHarness TCP server to accept connections
        connected = await self._wait_for_connection(timeout)
        if not connected:
            log.error("Failed to connect to TestHarness within %.1fs", timeout)
            await self.stop()
            return False

        # Verify with a ping
        resp = await self.ping()
        if not resp.ok:
            log.error("Ping failed after connection: %s", resp.error)
            await self.stop()
            return False

        log.info("Game launched and responding (engine: %s)",
                 resp.data.get("version", "?"))
        return True

    async def _launch_python(self) -> asyncio.subprocess.Process | None:
        """Launch the Python game entry point."""
        entry = os.environ.get("HARNESS_GAME_ENTRY", "main.py")
        python = sys.executable
        cwd = self.project_path
        log.info("Launching Python: %s %s (cwd=%s)", python, entry, cwd)
        return await asyncio.create_subprocess_exec(
            python, entry,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _launch_godot(self) -> asyncio.subprocess.Process | None:
        """Launch the Godot project."""
        godot = self._resolve_godot_path()
        if godot is None:
            log.error("Godot executable not found: %s", self.godot_path)
            return None
        log.info("Launching Godot: %s --path %s", godot, self.project_path)
        return await asyncio.create_subprocess_exec(
            godot, "--path", self.project_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def stop(self) -> None:
        """Stop the game process gracefully."""
        self._disconnect()

        if self._process is None:
            return

        if self._process.returncode is not None:
            log.info("Game process already exited (code=%d)", self._process.returncode)
            self._process = None
            return

        # Try sending quit via TCP first
        try:
            await self._connect()
            resp = await self._send({"cmd": "quit", "exit_code": 0})
            if resp.ok:
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                    log.info("Game quit gracefully")
                    self._process = None
                    return
                except asyncio.TimeoutError:
                    pass
        except Exception:
            pass
        finally:
            self._disconnect()

        # Fallback: force-kill (Windows: SIGTERM unreliable, use taskkill)
        pid = self._process.pid
        log.warning("Force-killing game (PID %d)", pid)
        try:
            self._process.kill()
            await asyncio.wait_for(self._process.wait(), timeout=3.0)
        except (asyncio.TimeoutError, OSError):
            # Windows fallback: direct taskkill
            import platform
            if platform.system() == "Windows":
                try:
                    subprocess.run(
                        ["taskkill", "/f", "/pid", str(pid)],
                        capture_output=True, timeout=10,
                    )
                except Exception:
                    pass
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass

        self._process = None
        _active_bridges.discard(self)

    def _kill_sync(self) -> None:
        """Synchronous kill — used by atexit when the event loop is gone."""
        if self._process is None or self._process.returncode is not None:
            return
        pid = self._process.pid
        try:
            os.kill(pid, signal.SIGTERM)
            log.debug("atexit: sent SIGTERM to game PID %d", pid)
        except OSError:
            pass

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    # ── Commands ──────────────────────────────────────────────────────────

    async def ping(self) -> BridgeResponse:
        """Health check."""
        return await self._send({"cmd": "ping"})

    async def screenshot(self, path: str) -> BridgeResponse:
        """Capture a screenshot and save to *path*.

        Returns BridgeResponse with path and size on success.
        """
        abs_path = str(Path(path).resolve())
        return await self._send({"cmd": "screenshot", "path": abs_path})

    async def send_click(
        self, x: int, y: int, button: str = "left",
    ) -> BridgeResponse:
        """Send a mouse click at viewport coordinates (x, y)."""
        return await self._send({
            "cmd": "input_click", "x": x, "y": y, "button": button,
        })

    async def send_key(self, key: str, pressed: bool = True) -> BridgeResponse:
        """Send a key press (auto-releases unless pressed=False)."""
        return await self._send({
            "cmd": "input_key", "key": key, "pressed": pressed,
        })

    async def send_motion(self, x: int, y: int) -> BridgeResponse:
        """Send a mouse motion event."""
        return await self._send({"cmd": "input_motion", "x": x, "y": y})

    async def get_state(self) -> BridgeResponse:
        """Query the game state from GameState autoload."""
        return await self._send({"cmd": "state"})

    async def quit_game(self, exit_code: int = 0) -> BridgeResponse:
        """Ask the game to quit."""
        return await self._send({"cmd": "quit", "exit_code": exit_code})

    # ── Recording ─────────────────────────────────────────────────────────

    async def record_frames(
        self,
        count: int,
        interval: float = 0.1,
        output_dir: str = "/tmp/game_frames",
    ) -> list[str]:
        """Capture *count* screenshots at *interval* seconds apart.

        Returns list of saved file paths.
        """
        os.makedirs(output_dir, exist_ok=True)
        paths: list[str] = []
        for i in range(count):
            path = os.path.join(output_dir, f"frame_{i:04d}.png")
            resp = await self.screenshot(path)
            if resp.ok:
                paths.append(path)
            else:
                log.warning("Frame %d capture failed: %s", i, resp.error)
            if i < count - 1:
                await asyncio.sleep(interval)
        return paths

    async def record_video(
        self,
        duration: float,
        fps: int = 10,
        output: str = "/tmp/game_recording.mp4",
        frame_dir: str = "/tmp/game_frames",
    ) -> str | None:
        """Record frames and stitch into a video with ffmpeg.

        Returns output path on success, None on failure.
        """
        count = int(duration * fps)
        interval = 1.0 / fps
        frames = await self.record_frames(count, interval, frame_dir)
        if not frames:
            log.error("No frames captured, cannot create video")
            return None

        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            log.error("ffmpeg not found, cannot create video")
            return None

        frame_pattern = os.path.join(frame_dir, "frame_%04d.png")
        cmd = [
            ffmpeg, "-y",
            "-framerate", str(fps),
            "-i", frame_pattern,
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            output,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error("ffmpeg failed: %s", stderr.decode(errors="replace")[:500])
            return None

        log.info("Video saved: %s (%d frames)", output, len(frames))
        return output

    # ── GDScript syntax validation (headless, no game launch needed) ──────

    async def validate_scripts(self, timeout: float = 30.0) -> tuple[bool, str]:
        """Validate all .gd files in the project using godot --headless.

        Returns (passed, output_text).
        """
        godot = self._resolve_godot_path()
        if godot is None:
            return False, f"Godot executable not found: {self.godot_path}"

        # Godot --headless --check-only can only check one script at a time
        # via --script. But running the project headless with --quit will
        # catch parse errors on all loaded scripts.
        proc = await asyncio.create_subprocess_exec(
            godot, "--headless", "--path", self.project_path, "--quit",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, "GDScript validation timed out"

        output = stdout.decode(errors="replace") + stderr.decode(errors="replace")

        # Godot prints errors to stderr; look for parser/script errors
        error_indicators = ["SCRIPT ERROR", "Parse Error", "ERROR:", "error("]
        has_errors = any(ind in output for ind in error_indicators)

        if proc.returncode != 0 or has_errors:
            return False, output[:2000]

        return True, "All GDScript files OK"

    # ── Internal TCP helpers ──────────────────────────────────────────────

    def _resolve_godot_path(self) -> str | None:
        """Find the Godot executable."""
        # Try the configured path first
        if os.path.isfile(self.godot_path) and os.access(self.godot_path, os.X_OK):
            return self.godot_path

        # Try well-known locations
        candidates = [
            shutil.which("godot"),
            shutil.which("godot4"),
            "/usr/local/bin/godot",
            "/opt/homebrew/bin/godot",
            # macOS .app bundle
            "/Applications/Godot.app/Contents/MacOS/Godot",
        ]
        for c in candidates:
            if c is not None and os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        return None

    @staticmethod
    def _wait_port_free(port: int, timeout: float = 5.0) -> None:
        """Block until *port* is not in TIME_WAIT or other bound state."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                sock.close()
                return  # port is free
            except OSError:
                sock.close()
                time.sleep(0.5)
        log.warning("Port %d still in use after %.1fs — proceeding anyway", port, timeout)

    async def _wait_for_connection(self, timeout: float) -> bool:
        """Retry TCP connection until success or timeout."""
        deadline = asyncio.get_event_loop().time() + timeout
        attempt = 0
        while asyncio.get_event_loop().time() < deadline:
            attempt += 1
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port),
                    timeout=2.0,
                )
                log.debug("Connected to TestHarness on attempt %d", attempt)
                return True
            except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
                pass
            # Check if process died
            if self._process is not None and self._process.returncode is not None:
                log.error("Godot process exited with code %d before we could connect",
                          self._process.returncode)
                return False
            await asyncio.sleep(0.5)
        return False

    async def _connect(self) -> None:
        """Ensure we have an active TCP connection."""
        if self._writer is not None and not self._writer.is_closing():
            return
        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.port,
        )

    def _disconnect(self) -> None:
        """Close TCP connection."""
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    async def _send(self, msg: dict, timeout: float = 10.0) -> BridgeResponse:
        """Send a JSON command and wait for the response."""
        try:
            await self._connect()
        except (ConnectionRefusedError, OSError) as e:
            return BridgeResponse(ok=False, data={}, error=f"Connection failed: {e}")

        assert self._writer is not None
        assert self._reader is not None

        payload = json.dumps(msg) + "\n"
        try:
            self._writer.write(payload.encode())
            await self._writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            self._disconnect()
            return BridgeResponse(ok=False, data={}, error=f"Send failed: {e}")

        # Read response line
        try:
            line = await asyncio.wait_for(
                self._reader.readline(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            self._disconnect()
            return BridgeResponse(ok=False, data={}, error="Response timeout")
        except (ConnectionResetError, OSError) as e:
            self._disconnect()
            return BridgeResponse(ok=False, data={}, error=f"Read failed: {e}")

        if not line:
            self._disconnect()
            return BridgeResponse(ok=False, data={}, error="Connection closed")

        try:
            data = json.loads(line.decode())
        except json.JSONDecodeError as e:
            return BridgeResponse(ok=False, data={}, error=f"Invalid JSON: {e}")

        ok = data.get("ok", False)
        error = data.get("error", "")
        return BridgeResponse(ok=ok, data=data, error=error)


def _cleanup_bridges() -> None:
    """Kill any lingering Godot processes on interpreter exit."""
    for bridge in list(_active_bridges):
        bridge._kill_sync()
    _active_bridges.clear()


atexit.register(_cleanup_bridges)
