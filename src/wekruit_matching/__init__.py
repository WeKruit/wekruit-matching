"""WeKruit Matching Engine — backend pipeline for ranked job matching."""

__version__ = "0.1.0"

from wekruit_matching.matching.matcher import get_matches
from wekruit_matching.feedback.handler import record_feedback

__all__ = ["get_matches", "record_feedback", "__version__"]
