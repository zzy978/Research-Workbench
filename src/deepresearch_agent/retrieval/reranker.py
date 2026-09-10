"""Local cross-encoder inference, independent of generation and embedding APIs."""
from __future__ import annotations

import threading


class CrossEncoderReranker:
    def __init__(self, model_name: str, *, device: str = "cpu", batch_size: int = 8,
                 max_length: int = 1024, cache_dir=None):
        self.model_name = model_name
        self.device, self.batch_size, self.max_length = device, batch_size, max_length
        self.cache_dir = str(cache_dir) if cache_dir else None
        self._model = None
        self._lock = threading.Lock()

    def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        with self._lock:
            # torch must initialize before sentence_transformers/pandas on Windows.
            import torch
            from sentence_transformers import CrossEncoder
            if self._model is None:
                self._model = CrossEncoder(self.model_name, device=self.device,
                                           max_length=self.max_length, cache_folder=self.cache_dir,
                                           trust_remote_code=False)
            scores = self._model.predict([(query, text) for text in texts],
                                         batch_size=self.batch_size, show_progress_bar=False,
                                         activation_fn=torch.nn.Sigmoid())
            return [float(score) for score in scores]
