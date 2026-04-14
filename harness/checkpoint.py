"""CheckpointManager — .done marker tracking and resume support."""

from __future__ import annotations

from harness.artifacts import ArtifactStore


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
