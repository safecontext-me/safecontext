"""Hook for the local sensitive-prose model (health, legal, personal matters).

The model itself is not chosen yet. It will run on-device and implement
`ProseClassifier`; until then the pipeline uses `NoProseClassifier`.
Flagged paragraphs are replaced with [REDACTED:<CATEGORY>].
"""
from __future__ import annotations

import re
from typing import List, Optional

try:  # Protocol is in typing from 3.8
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

CATEGORIES = ("HEALTH", "LEGAL", "PERSONAL")


class ProseClassifier(Protocol):
    def classify(self, paragraphs: List[str]) -> List[Optional[str]]:
        """Return one category from CATEGORIES (or None) per paragraph."""
        ...


class NoProseClassifier:
    def classify(self, paragraphs: List[str]) -> List[Optional[str]]:
        return [None] * len(paragraphs)


_PARA_SPLIT = re.compile(r"\n\s*\n")


def redact_prose(text: str, classifier: ProseClassifier):
    paragraphs = _PARA_SPLIT.split(text)
    labels = classifier.classify(paragraphs)
    counts = {}
    out = []
    for para, label in zip(paragraphs, labels):
        if label:
            out.append(f"[REDACTED:{label}]")
            counts[label] = counts.get(label, 0) + 1
        else:
            out.append(para)
    return "\n\n".join(out), counts
