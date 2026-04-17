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

    # ---- path validation ----

    def _validate_path_segments(self, *segments: str) -> None:
        """Validate path segments for security and directory traversal.
        
        Args:
            *segments: Path segments to validate.
            
        Raises:
            ValueError: If segments contain '..', empty strings, or security issues.
        """
        # Check for directory traversal attempts
        for segment in segments:
            if segment == "..":
                raise ValueError(f"Path segment '..' not allowed: {segments}")
            if segment == "":
                raise ValueError(f"Empty path segment not allowed: {segments}")
        
        # Build the full path and ensure it's within the run directory
        full_path = self.store.path(*segments)
        try:
            # This will raise ValueError if path escapes run_dir
            full_path.relative_to(self.store.run_dir)
        except ValueError:
            raise ValueError(f"Path attempts to escape artifact store: {segments}")
        
        # Use comprehensive security validation
        from harness.core.security import validate_path_security
        path_str = str(full_path)
        if error := validate_path_security(path_str):
            raise ValueError(error)

    # ---- basic markers ----

    def is_done(self, *segments: str) -> bool:
        """Check if ``.done`` marker exists at the given path."""
        return self.store.exists(*segments, ".done")

    def mark_done(self, *segments: str) -> None:
        """Write ``.done`` marker at the given path."""
        # Validate path segments for security
        self._validate_path_segments(*segments)
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
        # Validate path segments
        self._validate_path_segments(*segments)
        validated_segments = segments
        
        # Validate synthesis_specificity_score range
        if not (0 <= metadata.synthesis_specificity_score <= 10):
            raise ValueError(
                f"synthesis_specificity_score must be between 0 and 10, got {metadata.synthesis_specificity_score}"
            )
        
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
        json_path = self.store.path(*validated_segments, "checkpoint_metadata.json")
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(metadata_dict, indent=2), encoding="utf-8")

    def read_checkpoint_metadata(
        self,
        *segments: str
    ) -> CheckpointMetadata | None:
        """Read checkpoint metadata if it exists."""
        import json
        import logging
        from datetime import datetime
        
        logger = logging.getLogger(__name__)
        
        # Validate path segments
        self._validate_path_segments(*segments)
        validated_segments = segments
        
        json_path = self.store.path(*validated_segments, "checkpoint_metadata.json")
        if not json_path.exists():
            return None
        
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            # Convert timestamp string back to datetime
            data["timestamp"] = datetime.fromisoformat(data["timestamp"])
            return CheckpointMetadata(**data)
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(
                "Failed to read checkpoint metadata from %s: %s",
                json_path,
                e
            )
            return None

    # ---- hash-based incremental review ----

    def read_last_review_hash(self) -> str:
        """Read the last reviewed commit hash, or '' if none."""
        return (self.store.read("meta_review_hash.txt") or "").strip()

    def write_last_review_hash(self, commit_hash: str) -> None:
        """Persist the commit hash after a meta-review completes."""
        self.store.write(commit_hash, "meta_review_hash.txt")
