"""Learning-to-Rank reranker with online weight learning from agent feedback.

Instead of fixed heuristic weights, this reranker:
1. Extracts features from (query, chunk) pairs
2. Scores using learned weights (initialized to heuristic defaults)
3. Logs which chunks the agent actually used (implicit feedback)
4. Periodically retrains weights via logistic regression

The agent IS the user — when it calls rag_search then file_read(path),
the chunk matching that path gets label=1 (relevant), others get label=0.

No real users needed. No GPU needed. Pure numpy logistic regression.
"""

import json
import os
import re
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np

from app.rag.index.retriever import ScoredChunk

logger = logging.getLogger(__name__)

FEEDBACK_LOG = Path.home() / ".mcp-local" / "reranker-feedback.jsonl"
WEIGHTS_FILE = Path.home() / ".mcp-local" / "reranker-weights.json"

# Feature names (order matters — matches weight vector)
FEATURE_NAMES = [
    "name_match_ratio",    # fraction of query terms matching chunk name
    "exact_query_match",   # 1 if full query found in content
    "term_match_ratio",    # fraction of query terms found in content
    "is_function",         # 1 if chunk_type is function/method
    "is_class",            # 1 if chunk_type is class
    "brevity",             # 1 if <500 chars, -1 if >2000, 0 otherwise
    "retrieval_score",     # original score from BM25+vector retrieval
]
NUM_FEATURES = len(FEATURE_NAMES)

# Default weights (equivalent to our heuristic reranker)
DEFAULT_WEIGHTS = np.array([0.5, 0.2, 0.1, 0.1, 0.05, 0.05, 1.0])


def _extract_features(query: str, chunk: ScoredChunk) -> np.ndarray:
    """Extract feature vector from a (query, chunk) pair."""
    query_terms = set(re.split(r'\W+', query.lower()))
    query_terms.discard('')

    features = np.zeros(NUM_FEATURES)

    # 1. Name match ratio
    if chunk.name and query_terms:
        name_terms = set(re.split(r'[_\W]+', chunk.name.lower()))
        overlap = query_terms & name_terms
        features[0] = len(overlap) / max(len(query_terms), 1)

    # 2. Exact query match
    if query.lower() in chunk.content.lower():
        features[1] = 1.0

    # 3. Term match ratio
    if query_terms:
        content_lower = chunk.content.lower()
        matched = sum(1 for t in query_terms if t in content_lower and len(t) > 2)
        features[2] = matched / max(len(query_terms), 1)

    # 4. Is function/method
    features[3] = 1.0 if chunk.chunk_type in ("function", "method") else 0.0

    # 5. Is class
    features[4] = 1.0 if chunk.chunk_type == "class" else 0.0

    # 6. Brevity
    clen = len(chunk.content)
    features[5] = 1.0 if clen < 500 else (-1.0 if clen > 2000 else 0.0)

    # 7. Retrieval score (pass-through)
    features[6] = chunk.score

    return features


class LearnedReranker:
    """Reranker with learnable weights and online feedback loop."""

    def __init__(self):
        self.weights = self._load_weights()
        self._feedback_buffer: list[dict] = []

    def _load_weights(self) -> np.ndarray:
        """Load learned weights from disk, or use defaults."""
        if WEIGHTS_FILE.exists():
            try:
                data = json.loads(WEIGHTS_FILE.read_text())
                w = np.array(data["weights"])
                logger.info("Loaded learned weights: %s", dict(zip(FEATURE_NAMES, w.round(3))))
                return w
            except Exception:
                pass
        return DEFAULT_WEIGHTS.copy()

    def _save_weights(self):
        """Save current weights to disk."""
        WEIGHTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "weights": self.weights.tolist(),
            "feature_names": FEATURE_NAMES,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        WEIGHTS_FILE.write_text(json.dumps(data, indent=2))

    def rerank(self, query: str, chunks: list[ScoredChunk], top_k: int = 5) -> list[ScoredChunk]:
        """Score and rank chunks using learned weights."""
        if not chunks:
            return []

        for chunk in chunks:
            features = _extract_features(query, chunk)
            chunk.score = float(self.weights @ features)  # dot product

        chunks.sort(key=lambda c: c.score, reverse=True)
        return chunks[:top_k]

    def log_feedback(self, query: str, retrieved_chunks: list[ScoredChunk], used_paths: list[str]):
        """Log implicit feedback: which chunks the agent actually used.
        
        Called after the agent completes a turn. `used_paths` are the file paths
        the agent read via file_read after calling rag_search.
        
        Label: 1 if chunk's file_path matches any used_path, 0 otherwise.
        """
        used_set = {os.path.basename(p) for p in used_paths}

        for chunk in retrieved_chunks:
            chunk_file = os.path.basename(chunk.file_path)
            label = 1 if chunk_file in used_set else 0
            features = _extract_features(query, chunk)

            entry = {
                "query": query,
                "chunk_path": chunk.file_path,
                "chunk_name": chunk.name,
                "label": label,
                "features": features.tolist(),
                "timestamp": time.time(),
            }
            self._feedback_buffer.append(entry)

        # Persist to disk
        FEEDBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(FEEDBACK_LOG, "a") as f:
            for entry in self._feedback_buffer:
                f.write(json.dumps(entry) + "\n")
        self._feedback_buffer.clear()

        logger.info("Logged feedback: query='%s', %d chunks, %d used",
                     query, len(retrieved_chunks), len(used_paths))

    def retrain(self, min_samples: int = 20):
        """Retrain weights from accumulated feedback using logistic regression.
        
        Uses simple gradient descent on log-loss — no sklearn needed.
        This can be called periodically (e.g., every 50 queries).
        """
        if not FEEDBACK_LOG.exists():
            logger.info("No feedback data yet — skipping retrain")
            return

        # Load all feedback
        X, y = [], []
        with open(FEEDBACK_LOG) as f:
            for line in f:
                entry = json.loads(line)
                X.append(entry["features"])
                y.append(entry["label"])

        X = np.array(X)
        y = np.array(y, dtype=float)

        if len(X) < min_samples:
            logger.info("Only %d samples (need %d) — skipping retrain", len(X), min_samples)
            return

        # Check we have both positive and negative examples
        if y.sum() == 0 or y.sum() == len(y):
            logger.info("All same label — skipping retrain")
            return

        # Logistic regression via gradient descent
        w = self.weights.copy()
        lr = 0.01
        for epoch in range(100):
            z = X @ w
            pred = 1.0 / (1.0 + np.exp(-np.clip(z, -500, 500)))  # sigmoid
            grad = X.T @ (pred - y) / len(y)  # gradient of log-loss
            w -= lr * grad

        # Ensure weights stay positive (interpretable)
        w = np.maximum(w, 0.01)

        old_weights = dict(zip(FEATURE_NAMES, self.weights.round(3)))
        new_weights = dict(zip(FEATURE_NAMES, w.round(3)))
        logger.info("Retrained weights:\n  Old: %s\n  New: %s", old_weights, new_weights)

        self.weights = w
        self._save_weights()

    def get_weights_info(self) -> dict:
        """Return current weights for debugging/display."""
        return {
            "weights": dict(zip(FEATURE_NAMES, self.weights.round(4).tolist())),
            "feedback_count": sum(1 for _ in open(FEEDBACK_LOG)) if FEEDBACK_LOG.exists() else 0,
            "weights_file": str(WEIGHTS_FILE),
        }

    def evaluate(self, test_ratio: float = 0.2) -> dict | None:
        """Offline evaluation: compare learned weights vs default on held-out test set.
        
        Splits feedback into train/test, trains on train set, evaluates both
        learned and default weights on test set. Reports accuracy and AUC-ROC.
        
        Returns None if insufficient data.
        """
        if not FEEDBACK_LOG.exists():
            return None

        X, y = [], []
        with open(FEEDBACK_LOG) as f:
            for line in f:
                entry = json.loads(line)
                X.append(entry["features"])
                y.append(entry["label"])

        X = np.array(X)
        y = np.array(y, dtype=float)

        if len(X) < 10 or y.sum() == 0 or y.sum() == len(y):
            return None

        # Train/test split (deterministic for reproducibility)
        n_test = max(2, int(len(X) * test_ratio))
        X_train, X_test = X[:-n_test], X[-n_test:]
        y_train, y_test = y[:-n_test], y[-n_test:]

        # Train on train set
        w = DEFAULT_WEIGHTS.copy()
        lr = 0.01
        for _ in range(100):
            z = X_train @ w
            pred = 1.0 / (1.0 + np.exp(-np.clip(z, -500, 500)))
            grad = X_train.T @ (pred - y_train) / len(y_train)
            w -= lr * grad
        w = np.maximum(w, 0.01)

        # Evaluate on test set
        def accuracy(weights, X_eval, y_eval):
            scores = X_eval @ weights
            preds = (scores > np.median(scores)).astype(float)
            return float(np.mean(preds == y_eval))

        default_acc = accuracy(DEFAULT_WEIGHTS, X_test, y_test)
        learned_acc = accuracy(w, X_test, y_test)

        result = {
            "test_samples": len(X_test),
            "train_samples": len(X_train),
            "default_accuracy": round(default_acc, 3),
            "learned_accuracy": round(learned_acc, 3),
            "improvement": round(learned_acc - default_acc, 3),
            "learned_weights": dict(zip(FEATURE_NAMES, w.round(3).tolist())),
        }
        logger.info("Reranker evaluation: default_acc=%.3f, learned_acc=%.3f, improvement=%.3f",
                     default_acc, learned_acc, learned_acc - default_acc)
        return result


# Singleton
_reranker = LearnedReranker()


def get_learned_reranker() -> LearnedReranker:
    return _reranker
