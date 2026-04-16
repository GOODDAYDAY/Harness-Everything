"""harness.evaluation — evaluator components.

Re-exports the public API so that both old-style imports
(``from harness.evaluator import Evaluator``) and new-style imports
(``from harness.evaluation import Evaluator``) work transparently.
"""

from harness.evaluation.dual_evaluator import DualEvaluator, parse_score
from harness.evaluation.evaluator import Evaluator, Verdict

__all__ = [
    "DualEvaluator",
    "Evaluator",
    "Verdict",
    "parse_score",
]
