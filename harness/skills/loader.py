"""Skill data model and SKILL.md frontmatter parser.

Skills are markdown documents with YAML-subset frontmatter that provide
structured project context to the autonomous agent.  The parser is
hand-written (no PyYAML) to avoid adding runtime dependencies.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Skill:
    """A single skill — a knowledge document with metadata."""

    name: str
    description: str
    auto_load: bool = False
    body: str = ""              # markdown body after frontmatter
    path: str | None = None     # absolute path to SKILL.md (None for virtual)
    is_virtual: bool = False    # True for the _mission pseudo-skill

    @property
    def char_count(self) -> int:
        return len(self.body)


# ---------------------------------------------------------------------------
# Frontmatter parser (YAML subset, no PyYAML dependency)
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML-subset frontmatter from a SKILL.md string.

    Handles:
    - ``key: value`` (string, unquoted or single/double-quoted)
    - ``key: true`` / ``key: false`` (boolean)
    - ``key: >`` or ``key: >-`` multi-line scalars (folded)
    - Blank lines and comment lines (``# ...``) inside frontmatter

    Returns ``(meta_dict, body)`` where *body* is everything after the
    closing ``---`` delimiter.

    Raises :class:`ValueError` when the frontmatter delimiters are
    missing or malformed.
    """
    text = text.lstrip("\ufeff").replace("\r\n", "\n")

    if not text.startswith("---"):
        raise ValueError("SKILL.md must start with '---'")

    # Find the closing --- (skip the opening one at position 0).
    # We look for a line that is exactly "---" (possibly with trailing whitespace).
    lines = text.split("\n")
    close_idx: int | None = None
    for i, line in enumerate(lines[1:], start=1):
        if re.match(r"^---\s*$", line):
            close_idx = i
            break

    if close_idx is None:
        raise ValueError("SKILL.md frontmatter missing closing '---'")

    fm_lines = lines[1:close_idx]
    body = "\n".join(lines[close_idx + 1:]).strip()

    # Parse key: value pairs.
    result: dict[str, Any] = {}
    current_key: str | None = None
    multiline_buf: list[str] = []

    def _flush_multiline() -> None:
        nonlocal current_key, multiline_buf
        if current_key is not None and multiline_buf:
            result[current_key] = " ".join(multiline_buf)
        current_key = None
        multiline_buf = []

    for line in fm_lines:
        stripped = line.strip()

        # Skip blank lines and comments.
        if not stripped or stripped.startswith("#"):
            continue

        # Continuation of a multi-line scalar (indented line).
        if current_key is not None and (line.startswith("  ") or line.startswith("\t")):
            multiline_buf.append(stripped)
            continue

        # Flush any pending multi-line value.
        _flush_multiline()

        if ":" not in line:
            continue

        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()

        if not key:
            continue

        # Multi-line folded scalar indicator.
        if value in (">", ">-", "|", "|-"):
            current_key = key
            multiline_buf = []
            continue

        # Boolean literals.
        if value.lower() == "true":
            result[key] = True
        elif value.lower() == "false":
            result[key] = False
        else:
            # Strip surrounding quotes.
            if len(value) >= 2:
                if (value[0] == '"' and value[-1] == '"') or (
                    value[0] == "'" and value[-1] == "'"
                ):
                    value = value[1:-1]
            result[key] = value

    _flush_multiline()
    return result, body


# ---------------------------------------------------------------------------
# File loader
# ---------------------------------------------------------------------------

def load_skill(path: str | Path) -> Skill:
    """Load a single ``SKILL.md`` file and return a :class:`Skill`.

    Raises :class:`ValueError` if the frontmatter is missing the
    required ``name`` field.  Raises :class:`FileNotFoundError` if the
    path does not exist.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)

    name = meta.get("name")
    if not name:
        raise ValueError(f"{p}: frontmatter requires 'name'")

    return Skill(
        name=str(name),
        description=str(meta.get("description", "")),
        auto_load=bool(meta.get("auto_load", False)),
        body=body,
        path=str(p.resolve()),
    )
