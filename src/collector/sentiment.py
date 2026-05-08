"""
FinBERT sentiment inference for filing headlines and body summaries.

Model: ProsusAI/finbert (loaded locally from /opt/boomer/models/finbert/).
Runs during the parse phase (Layer 1 → Layer 2), not inline during collection.
Batch inference: up to 32 filings per call.
Confidence < threshold (default 0.60) → stored as 'unclassified'.

CPU inference: ~80-120ms per filing on a modern VPS CPU.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from collector.models import SentimentLabel

logger = logging.getLogger(__name__)

_DEFAULT_MODEL_DIR = Path("/opt/boomer/models/finbert")
_BATCH_SIZE = 32
_FINBERT_VERSION = "prosus-v1"


class SentimentPipeline:
    def __init__(self, model_dir: Path = _DEFAULT_MODEL_DIR) -> None:
        self._model_dir = model_dir
        self._pipeline = None  # lazy-loaded on first call

    def _load(self) -> None:
        try:
            from transformers import pipeline
        except ImportError as exc:
            raise RuntimeError(
                "transformers library is required for FinBERT: pip install transformers torch"
            ) from exc
        model_path = str(self._model_dir)
        logger.info("Loading FinBERT from %s", model_path)
        self._pipeline = pipeline(
            "text-classification",
            model=model_path,
            tokenizer=model_path,
            top_k=1,
            truncation=True,
            max_length=512,
        )
        logger.info("FinBERT loaded")

    def infer(self, texts: list[str]) -> list[tuple[SentimentLabel, float]]:
        """
        Run inference on a batch of texts.
        Returns list of (SentimentLabel, confidence) tuples, same length as input.
        """
        if not texts:
            return []
        if self._pipeline is None:
            self._load()

        results = []
        for i in range(0, len(texts), _BATCH_SIZE):
            batch = texts[i : i + _BATCH_SIZE]
            try:
                outputs = self._pipeline(batch)
                for out in outputs:
                    item = out[0] if isinstance(out, list) else out
                    label = _map_label(item["label"])
                    score = float(item["score"])
                    results.append((label, score))
            except Exception as exc:
                logger.error("FinBERT inference failed on batch starting at %d: %s", i, exc)
                # Append unclassified for each failed item in batch.
                results.extend([(SentimentLabel.UNCLASSIFIED, 0.0)] * len(batch))

        return results

    def infer_one(self, text: str) -> tuple[SentimentLabel, float]:
        return self.infer([text])[0]


def apply_sentiment_to_filings(
    db: sqlite3.Connection,
    pipeline: SentimentPipeline,
    confidence_threshold: float = 0.60,
    limit: int = 500,
) -> int:
    """
    Find filings with NULL sentiment_label and run FinBERT inference.
    Updates filing rows in-place. Returns count of rows updated.

    Call this from the parser after filing rows are inserted.
    """
    rows = db.execute(
        """
        SELECT filing_id, headline, body_summary
        FROM filings
        WHERE sentiment_label IS NULL
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        return 0

    ids = [r[0] for r in rows]
    texts = [_build_inference_text(r[1] or "", r[2] or "") for r in rows]

    results = pipeline.infer(texts)
    updated = 0

    for filing_id, (label, score) in zip(ids, results, strict=False):
        if score < confidence_threshold:
            label = SentimentLabel.UNCLASSIFIED

        db.execute(
            "UPDATE filings SET sentiment_label=?, sentiment_confidence=?, finbert_version=? "
            "WHERE filing_id=?",
            (label.value, score, _FINBERT_VERSION, filing_id),
        )
        updated += 1

    db.commit()
    return updated


def _build_inference_text(headline: str, body_summary: str) -> str:
    """Combine headline and body summary into a single inference input (max ~600 chars)."""
    combined = headline.strip()
    if body_summary:
        combined = combined + " " + body_summary.strip()[:500]
    return combined[:600]


def _map_label(raw_label: str) -> SentimentLabel:
    raw = raw_label.strip().lower()
    if raw == "positive":
        return SentimentLabel.POSITIVE
    if raw == "negative":
        return SentimentLabel.NEGATIVE
    if raw == "neutral":
        return SentimentLabel.NEUTRAL
    return SentimentLabel.UNCLASSIFIED
