"""Smart commit squashing for the agent loop.

Periodically analyses recent harness commits, asks the LLM to group them
by logical task, and squashes each group into a single clean commit.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from harness.core.llm import LLM
from harness.agent import agent_git
from harness.prompts import agent_squash as squash_prompts

log = logging.getLogger(__name__)


def _build_commit_list(commits: list[dict[str, str]]) -> str:
    """Format commits for the LLM prompt."""
    return "\n".join(
        f"  {c['sha']} {c['message']}" for c in commits
    )


def _parse_groups(text: str, commits: list[dict[str, str]]) -> list[dict[str, Any]] | None:
    """Parse the LLM's JSON response into validated groups.

    Returns ``None`` if parsing or validation fails.
    """
    # Strip markdown fences if present
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        # Remove first and last fence lines
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    try:
        groups = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.warning("agent_squash: failed to parse LLM response as JSON: %s", exc)
        return None

    if not isinstance(groups, list):
        log.warning("agent_squash: LLM response is not a JSON array")
        return None

    # Validate structure
    all_shas = [c["sha"] for c in commits]
    seen_shas: list[str] = []
    for group in groups:
        if not isinstance(group, dict):
            log.warning("agent_squash: group is not a dict: %s", group)
            return None
        if "shas" not in group or "message" not in group:
            log.warning("agent_squash: group missing 'shas' or 'message'")
            return None
        if not isinstance(group["shas"], list) or not group["shas"]:
            log.warning("agent_squash: group has empty or non-list shas")
            return None
        if not group["message"].startswith("[harness]"):
            group["message"] = "[harness] " + group["message"]
        seen_shas.extend(group["shas"])

    # Validate contiguity: the flattened sha order must match the commit order
    # (LLM may return short or full SHAs — match by prefix)
    matched_order: list[str] = []
    for seen_sha in seen_shas:
        found = False
        for full_sha in all_shas:
            if full_sha.startswith(seen_sha) or seen_sha.startswith(full_sha):
                matched_order.append(full_sha)
                found = True
                break
        if not found:
            log.warning("agent_squash: SHA %s not found in commit list", seen_sha)
            return None

    if matched_order != all_shas:
        log.warning(
            "agent_squash: group order doesn't match commit order "
            "(non-contiguous grouping?)"
        )
        return None

    # Normalize SHAs to full hashes
    for group in groups:
        normalized: list[str] = []
        for sha in group["shas"]:
            for full_sha in all_shas:
                if full_sha.startswith(sha) or sha.startswith(full_sha):
                    normalized.append(full_sha)
                    break
        group["shas"] = normalized

    return groups


async def run_squash(
    llm: LLM,
    repo_path: Path,
    since_hash: str,
) -> str:
    """Analyze recent commits and squash related ones.

    Returns the new HEAD hash after squashing (SHAs change after rebase).
    If squash is skipped or fails, returns the current HEAD hash unchanged.
    The LLM decides whether the commits are worth squashing.
    """
    current_hash = await agent_git.get_head_hash(repo_path)

    # Gather commits since last squash
    commits = await agent_git.get_commits_since(repo_path, since_hash)
    if not commits:
        log.debug("agent_squash: no commits since %s, skipping", since_hash)
        return current_hash

    # Ask LLM to group commits
    commit_list = _build_commit_list(commits)
    user_content = squash_prompts.SQUASH_GROUPING_USER.replace(
        "$commit_list", commit_list,
    )

    try:
        response = await llm.call(
            [{"role": "user", "content": user_content}],
            system=squash_prompts.SQUASH_GROUPING_SYSTEM,
            max_tokens=2000,
        )
    except Exception as exc:
        log.warning("agent_squash: LLM call failed: %s", exc)
        return current_hash

    # Parse and validate groups
    groups = _parse_groups(response.text or "", commits)
    if groups is None:
        return current_hash

    # Filter out single-commit groups that don't need squashing
    needs_squash = [g for g in groups if len(g["shas"]) > 1]
    if not needs_squash:
        log.info("agent_squash: LLM says all commits are independent, no squash needed")
        return current_hash

    log.info(
        "agent_squash: squashing %d commits into %d groups (%d need squash)",
        len(commits), len(groups), len(needs_squash),
    )

    # Execute squash
    ok = await agent_git.squash_groups(repo_path, since_hash, groups)
    if not ok:
        return current_hash

    new_hash = await agent_git.get_head_hash(repo_path)
    log.info("agent_squash: done — HEAD moved from %s to %s", current_hash, new_hash)
    return new_hash
