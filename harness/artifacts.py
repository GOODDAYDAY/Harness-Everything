"""ArtifactStore — hierarchical artifact persistence for harness runs."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


class ArtifactStore:
    """Manages the hierarchical artifact directory for a single run.

    Directory layout example::

        harness_output/
        └── run_20260414T120000/
            ├── round_1/
            │   ├── phase_1_requirements_analysis/
            │   │   ├── inner_1/
            │   │   │   ├── proposal.txt
            │   │   │   ├── basic_eval.txt
            │   │   │   └── diffusion_eval.txt
            │   │   ├── synthesis.txt
            │   │   └── phase_summary.txt
            │   └── summary.md
            └── final_summary.md
    """

    def __init__(self, base_dir: str | Path, run_id: str | None = None) -> None:
        base = Path(base_dir)
        if run_id is None:
            run_id = f"run_{datetime.now().strftime('%Y%m%dT%H%M%S')}"
        self.run_dir = base / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

    # ---- core operations ----

    def path(self, *segments: str) -> Path:
        """Build a path under run_dir.

        Example: ``store.path("round_1", "phase_2_dev", "inner_1", "proposal.txt")``
        """
        return self.run_dir.joinpath(*segments)

    def write(self, content: str, *segments: str) -> Path:
        """Write content to an artifact path, creating parents. Returns the path."""
        p = self.path(*segments)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def read(self, *segments: str) -> str:
        """Read an artifact, returning ``""`` if missing or unreadable."""
        p = self.path(*segments)
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return ""

    def exists(self, *segments: str) -> bool:
        """Check if an artifact file exists."""
        return self.path(*segments).exists()

    # ---- run-level markers ----

    def write_final_summary(self, content: str) -> Path:
        """Write ``final_summary.md`` — also serves as the 'run completed' marker."""
        return self.write(content, "final_summary.md")

    @property
    def is_complete(self) -> bool:
        """True if ``final_summary.md`` exists (run finished successfully)."""
        return self.exists("final_summary.md")

    # ---- convenience helpers ----

    def inner_dir(self, outer: int, phase_label: str, inner: int) -> str:
        """Return the directory path segments for an inner round.

        Args:
            outer: 0-based outer round index.
            phase_label: e.g. ``"1_requirements_analysis"``.
            inner: 0-based inner round index.
        """
        return f"round_{outer + 1}", f"phase_{phase_label}", f"inner_{inner + 1}"

    def phase_dir(self, outer: int, phase_label: str) -> str:
        """Return the directory path segments for a phase."""
        return f"round_{outer + 1}", f"phase_{phase_label}"

    # ---- class methods ----

    @classmethod
    def find_resumable(cls, base_dir: str | Path) -> ArtifactStore | None:
        """Find the most recent incomplete run directory, or None.

        A run is incomplete when it has started (has at least one ``round_*``
        directory) but has not finished (no ``final_summary.md``).
        """
        base = Path(base_dir)
        if not base.is_dir():
            return None
        for run_dir in sorted(base.glob("run_*"), reverse=True):
            if (run_dir / "final_summary.md").exists():
                continue
            if any(run_dir.glob("round_*")):
                # Reconstruct with existing run_id
                store = cls.__new__(cls)
                store.run_dir = run_dir
                return store
        return None
