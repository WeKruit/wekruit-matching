"""WeKruit Matching Engine — backend pipeline for ranked job matching."""

__version__ = "0.1.0"

from wekruit_matching.matching.matcher import get_matches

__all__ = ["get_matches", "__version__"]
