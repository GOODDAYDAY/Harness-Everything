"""CheckpointManager — .done marker tracking and resume support."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from harness.core.artifacts import ArtifactStore


@dataclass
class CheckpointMetadata:
    """Structured metadata for checkpoint evaluation tracking."""
    checkpoint_type: str  # "phase", "inner", "synthesis", "meta_review"
    outer_round: int
    phase_label: str = ""
    inner_index: int = -1
    basic_score: float = 0.0
    diffusion_score: float = 0.0
    critique_count: int = 0
    actionable_critiques: int = 0
    synthesis_specificity_score: int = 0  # 0-10 scale
    timestamp: datetime = field(default_factory=datetime.now)


class CheckpointManager:
    """Manages ``.done`` markers on top of an ArtifactStore.

    Each checkpoint is a zero-byte file written inside the relevant artifact
    directory.  The presence of this file tells the runner to skip re-executing
    that step on resume.
    """

    def __init__(self, store: ArtifactStore) -> None:
        self.store = store

    # ---- basic markers ----

    def is_done(self, *segments: str) -> bool:
        """Check if ``.done`` marker exists at the given path."""
        return self.store.exists(*segments, ".done")

    def mark_done(self, *segments: str) -> None:
        """Write ``.done`` marker at the given path."""
        self.store.write("", *segments, ".done")

    def is_skipped(self, *segments: str) -> bool:
        """Check if ``skipped.done`` marker exists."""
        return self.store.exists(*segments, "skipped.done")

    def mark_skipped(self, *segments: str) -> None:
        """Write ``skipped.done`` marker."""
        self.store.write("", *segments, "skipped.done")

    # ---- convenience for phase pipeline ----

    def is_inner_done(self, outer: int, phase_label: str, inner: int) -> bool:
        segs = self.store.inner_dir(outer, phase_label, inner)
        return self.is_done(*segs)

    def mark_inner_done(self, outer: int, phase_label: str, inner: int) -> None:
        segs = self.store.inner_dir(outer, phase_label, inner)
        self.mark_done(*segs)

    def is_phase_done(self, outer: int, phase_label: str) -> bool:
        segs = self.store.phase_dir(outer, phase_label)
        return self.is_done(*segs)

    def mark_phase_done(self, outer: int, phase_label: str) -> None:
        segs = self.store.phase_dir(outer, phase_label)
        self.mark_done(*segs)

    def is_phase_skipped(self, outer: int, phase_label: str) -> bool:
        segs = self.store.phase_dir(outer, phase_label)
        return self.is_skipped(*segs)

    def mark_phase_skipped(self, outer: int, phase_label: str) -> None:
        segs = self.store.phase_dir(outer, phase_label)
        self.mark_skipped(*segs)

    def is_synthesis_done(self, outer: int, phase_label: str) -> bool:
        segs = self.store.phase_dir(outer, phase_label)
        return self.store.exists(*segs, "synthesis.done")

    def mark_synthesis_done(self, outer: int, phase_label: str) -> None:
        segs = self.store.phase_dir(outer, phase_label)
        self.store.write("", *segs, "synthesis.done")

    # ---- meta-review ----

    def is_meta_review_done(self, outer: int) -> bool:
        return self.store.exists(f"round_{outer + 1}", "meta_review.done")

    def mark_meta_review_done(self, outer: int) -> None:
        self.store.write("", f"round_{outer + 1}", "meta_review.done")

    # ---- structured checkpoint metadata ----

    def write_checkpoint_metadata(
        self,
        metadata: CheckpointMetadata,
        *segments: str
    ) -> None:
        """Write structured checkpoint metadata as JSON alongside .done marker."""
        import json
        metadata_dict = {
            "checkpoint_type": metadata.checkpoint_type,
            "outer_round": metadata.outer_round,
            "phase_label": metadata.phase_label,
            "inner_index": metadata.inner_index,
            "basic_score": metadata.basic_score,
            "diffusion_score": metadata.diffusion_score,
            "critique_count": metadata.critique_count,
            "actionable_critiques": metadata.actionable_critiques,
            "synthesis_specificity_score": metadata.synthesis_specificity_score,
            "timestamp": metadata.timestamp.isoformat()
        }
        json_path = self.store.path(*segments, "checkpoint_metadata.json")
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(metadata_dict, indent=2), encoding="utf-8")

    def read_checkpoint_metadata(
        self,
        *segments: str
    ) -> CheckpointMetadata | None:
        """Read checkpoint metadata if it exists."""
        import json
        from datetime import datetime
        
        json_path = self.store.path(*segments, "checkpoint_metadata.json")
        if not json_path.exists():
            return None
        
        data = json.loads(json_path.read_text(encoding="utf-8"))
        return CheckpointMetadata(
            checkpoint_type=data["checkpoint_type"],
            outer_round=data["outer_round"],
            phase_label=data.get("phase_label", ""),
            inner_index=data.get("inner_index", -1),
            basic_score=data["basic_score"],
            diffusion_score=data["diffusion_score"],
            critique_count=data["critique_count"],
            actionable_critiques=data["actionable_critiques"],
            synthesis_specificity_score=data["synthesis_specificity_score"],
            timestamp=datetime.fromisoformat(data["timestamp"])
        )

    # ---- hash-based incremental review ----

    def read_last_review_hash(self) -> str:
        """Read the last reviewed commit hash, or '' if none."""
        return (self.store.read("meta_review_hash.txt") or "").strip()

    def write_last_review_hash(self, commit_hash: str) -> None:
        """Persist the commit hash after a meta-review completes."""
        self.store.write(commit_hash, "meta_review_hash.txt")
