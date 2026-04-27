"""Git operations for the agent loop.

All git interactions — commit, push, tag, diff — live here so that
``agent_loop.py`` contains only orchestration logic.  Every public
function is ``async`` and accepts resolved paths (no config objects).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def resolve_repo_paths(workspace: str | Path, commit_repos: list[str]) -> list[Path]:
    """Turn ``commit_repos`` entries into absolute ``Path`` objects."""
    ws = Path(workspace)
    paths: list[Path] = []
    for repo in commit_repos:
        p = Path(repo) if Path(repo).is_absolute() else ws / repo
        if p.is_dir():
            paths.append(p)
        else:
            log.warning("agent_git: commit_repos entry not found: %s", p)
    return paths


def _primary_repo(repo_paths: list[Path], workspace: str | Path) -> Path:
    """Return the first repo path, or fall back to workspace."""
    if repo_paths:
        return repo_paths[0]
    return Path(workspace)


# ---------------------------------------------------------------------------
# Diff / hash queries
# ---------------------------------------------------------------------------

async def get_staged_diff(repo_path: Path) -> str:
    """Return ``git diff --cached`` (staged changes), truncated to 30 k chars.

    Use this *before* committing to feed the evaluator without requiring
    a commit to exist first.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--cached",
            cwd=str(repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        diff = stdout.decode(errors="replace")
        if len(diff) > 30_000:
            log.debug("agent_git: staged diff truncated from %d to 30k chars", len(diff))
            diff = diff[:30_000] + "\n\n... (diff truncated at 30k chars)"
        return diff
    except Exception as exc:
        log.warning("agent_git: get_staged_diff failed: %s", exc)
        return ""


async def get_head_hash(repo_path: Path) -> str:
    """Return short HEAD hash (10 chars)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--short=10", "HEAD",
            cwd=str(repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


async def get_review_git_delta(repo_path: Path, since_hash: str) -> str:
    """Return git log + diff stat since *since_hash*."""
    parts: list[str] = []
    try:
        log_proc = await asyncio.create_subprocess_exec(
            "git", "log", "--oneline", f"{since_hash}..HEAD",
            cwd=str(repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await log_proc.communicate()
        if log_proc.returncode == 0:
            parts.append(
                "### Commits\n```\n"
                + stdout.decode(errors="replace")[:3000]
                + "\n```"
            )

        stat_proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--stat", f"{since_hash}..HEAD",
            cwd=str(repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await stat_proc.communicate()
        if stat_proc.returncode == 0:
            parts.append(
                "### File Stats\n```\n"
                + stdout.decode(errors="replace")[:3000]
                + "\n```"
            )
    except Exception as exc:
        parts.append(f"(git delta unavailable: {exc})")
    return "\n\n".join(parts) if parts else "(no git delta available)"


async def diff_summary(workspace: Path, changed_paths: list[str]) -> str:
    """Generate a one-line commit summary from ``git diff --stat``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--cached", "--stat",
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode(errors="replace").strip().splitlines()
        if lines:
            stat_line = lines[-1].strip()
            file_names = [Path(p).name for p in changed_paths[:5]] if changed_paths else []
            parts = ", ".join(file_names)
            if len(changed_paths) > 5:
                parts += f" +{len(changed_paths) - 5} more"
            summary = f"{parts} ({stat_line})" if parts else stat_line
            return summary[:80]
    except Exception as exc:
        log.debug("agent_git: diff_summary failed: %s", exc)
    return f"{len(changed_paths)} file(s) changed"


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

async def build_commit_message(
    cycle: int,
    agent_text: str,
    changed_paths: list[str],
    workspace: Path,
    *,
    metrics_line: str = "",
    eval_line: str = "",
    hooks_line: str = "",
) -> str:
    """Build a rich commit message with structured cycle data.

    Format::

        agent: cycle 5 — fix calendar handler

        metrics: tools=23 success=85% files=3 elapsed=45s
        eval: basic=7.2 diffusion=6.8 combined=7.0
        hooks: syntax=pass static=pass import_smoke=pass
    """
    summary_line = (
        agent_text.strip().splitlines()[0][:80] if agent_text.strip() else ""
    )
    if "unknown (tool loop was cut off)" in summary_line:
        summary_line = await diff_summary(workspace, changed_paths)
    title = f"[harness] agent: cycle {cycle + 1}"
    if summary_line:
        title += f" — {summary_line}"

    body_parts: list[str] = []
    if metrics_line:
        body_parts.append(f"metrics: {metrics_line}")
    if eval_line:
        body_parts.append(f"eval: {eval_line}")
    if hooks_line:
        body_parts.append(f"hooks: {hooks_line}")

    if body_parts:
        return title + "\n\n" + "\n".join(body_parts)
    return title


async def stage_changes(
    repo_paths: list[Path],
    changed_paths: list[str],
) -> bool:
    """``git add -- <paths>`` in each repo.  Returns True if all repos staged OK."""
    if not changed_paths:
        return True
    ok = True
    for repo_path in repo_paths:
        try:
            add = await asyncio.create_subprocess_exec(
                "git", "add", "--", *changed_paths,
                cwd=str(repo_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, add_err = await add.communicate()
            if add.returncode != 0:
                log.warning(
                    "agent_git: git add failed in %s: %s",
                    repo_path, add_err.decode(errors="replace")[:200],
                )
                ok = False
        except Exception as exc:
            log.warning("agent_git: stage error in %s: %s", repo_path, exc)
            ok = False
    return ok


async def commit_staged(
    repo_paths: list[Path],
    cycle: int,
    commit_msg: str,
) -> bool:
    """Commit already-staged changes in each repo.  Returns True if all succeeded."""
    ok = True
    for repo_path in repo_paths:
        try:
            commit = await asyncio.create_subprocess_exec(
                "git", "commit", "--allow-empty", "-m", commit_msg,
                cwd=str(repo_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await commit.communicate()
            if commit.returncode == 0:
                log.info(
                    "agent_git: committed cycle %d in %s",
                    cycle + 1, repo_path,
                )
            else:
                log.warning(
                    "agent_git: commit failed in %s: %s",
                    repo_path, stderr.decode(errors="replace")[:200],
                )
                ok = False
        except Exception as exc:
            log.warning("agent_git: commit error in %s: %s", repo_path, exc)
            ok = False
    return ok


# ---------------------------------------------------------------------------
# Push / tag
# ---------------------------------------------------------------------------

async def push_head(
    repo_paths: list[Path],
    remote: str,
    branch: str,
    cycle: int,
) -> bool:
    """``git push <remote> <branch>`` from each repo.  Returns True if all succeeded."""
    ok = True
    for repo_path in repo_paths:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "push", remote, branch,
                cwd=str(repo_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0:
                log.info("agent_git: pushed cycle %d in %s", cycle + 1, repo_path)
            else:
                log.warning(
                    "agent_git: push failed in %s: %s",
                    repo_path, stderr.decode(errors="replace")[:200],
                )
                ok = False
        except Exception as exc:
            log.warning("agent_git: push error in %s: %s", repo_path, exc)
            ok = False
    return ok


async def tag_cycle(
    repo_paths: list[Path],
    cycle: int,
    interval: int,
    prefix: str,
    push_remote: str,
    push_tag: bool,
) -> None:
    """Tag HEAD after cycle *cycle* when ``(cycle+1) % interval == 0``."""
    if interval <= 0 or (cycle + 1) % interval != 0:
        return
    for repo_path in repo_paths:
        try:
            sha_proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--short=7", "HEAD",
                cwd=str(repo_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            sha_out, _ = await sha_proc.communicate()
            if sha_proc.returncode != 0:
                log.warning("agent_git: tag rev-parse failed in %s", repo_path)
                continue
            short_sha = sha_out.decode().strip()
            tag_name = f"{prefix}-{cycle + 1}-{short_sha}"

            tag_proc = await asyncio.create_subprocess_exec(
                "git", "tag", "-f", tag_name,
                cwd=str(repo_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, tag_err = await tag_proc.communicate()
            if tag_proc.returncode != 0:
                log.warning(
                    "agent_git: tag create failed in %s: %s",
                    repo_path, tag_err.decode(errors="replace")[:200],
                )
                continue
            log.info(
                "agent_git: created tag %r in %s (cycle=%d)",
                tag_name, repo_path, cycle + 1,
            )

            if push_tag:
                push_proc = await asyncio.create_subprocess_exec(
                    "git", "push", push_remote, tag_name,
                    cwd=str(repo_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, push_err = await push_proc.communicate()
                if push_proc.returncode == 0:
                    log.info(
                        "agent_git: pushed tag %r to %s",
                        tag_name, push_remote,
                    )
                else:
                    log.warning(
                        "agent_git: tag push failed for %r: %s",
                        tag_name, push_err.decode(errors="replace")[:200],
                    )
        except Exception as exc:
            log.warning("agent_git: tag error in %s: %s", repo_path, exc)
