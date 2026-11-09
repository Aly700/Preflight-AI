"""Offline BAAI/bge-small-en-v1.5 (MIT), pinned to an immutable model revision.

Only the image build downloads files. This module reads local files and never
logs passage or query text. The same CPU implementation builds and queries the index.
"""

from collections.abc import Sequence
import hashlib
import os
from pathlib import Path
from threading import Lock

import numpy as np
from numpy.typing import NDArray
import onnxruntime as ort  # type: ignore[import-untyped]
from tokenizers import Tokenizer

MODEL_REVISION = '5c38ec7c405ec4b44b94cc5a9bb96e735b38267a'
MODEL_SHA256 = {
    'onnx/model.onnx': '828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35',
    'tokenizer.json': 'd241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66',
    'config.json': '094f8e891b932f2000c92cfc663bac4c62069f5d8af5b5278c4306aef3084750',
}
DEFAULT_MODEL_DIR = '/opt/models/bge-small-en-v1.5'
QUERY_PREFIX = 'Represent this sentence for searching relevant passages: '
DIMENSIONS = 384
BATCH_SIZE = 16


class Embedder:
    def __init__(self, model_dir: str | Path | None = None, *, intra_op_threads: int = 1) -> None:
        if intra_op_threads < 1:
            raise ValueError('intra_op_threads must be positive')
        directory = Path(
            model_dir if model_dir is not None else os.environ.get('PREFLIGHT_EMBEDDING_MODEL_DIR', DEFAULT_MODEL_DIR)
        )
        # Check every file before allowing either parser to load anything.
        for name, expected in MODEL_SHA256.items():
            with (directory / name).open('rb') as handle:
                actual = hashlib.file_digest(handle, 'sha256').hexdigest()
            if actual != expected:
                raise ValueError(f'Embedding model SHA-256 mismatch: {name}')
        self.tokenizer = Tokenizer.from_file(str(directory / 'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=512)
        pad_id = self.tokenizer.token_to_id('[PAD]')
        if pad_id is None:
            raise ValueError('Embedding tokenizer has no [PAD] token')
        self.tokenizer.enable_padding(pad_id=pad_id, pad_token='[PAD]')
        # ONNX Runtime ships with Microsoft telemetry hooks; this service sends nothing anywhere.
        ort.disable_telemetry_events()
        options = ort.SessionOptions()
        options.intra_op_num_threads = intra_op_threads
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(directory / 'onnx/model.onnx'), sess_options=options, providers=['CPUExecutionProvider']
        )

    def embed_passages(self, texts: Sequence[str]) -> NDArray[np.float32]:
        """CLS pooling and L2 normalisation; no passage prefix, at most 512 tokens."""
        vectors = np.empty((len(texts), DIMENSIONS), dtype=np.float32)
        for start in range(0, len(texts), BATCH_SIZE):
            encodings = self.tokenizer.encode_batch(list(texts[start : start + BATCH_SIZE]))
            feeds = {
                'input_ids': np.asarray([encoding.ids for encoding in encodings], dtype=np.int64),
                'attention_mask': np.asarray([encoding.attention_mask for encoding in encodings], dtype=np.int64),
                'token_type_ids': np.asarray([encoding.type_ids for encoding in encodings], dtype=np.int64),
            }
            hidden = np.asarray(self.session.run(['last_hidden_state'], feeds)[0], dtype=np.float32)
            pooled = hidden[:, 0, :]
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            if pooled.shape != (len(encodings), DIMENSIONS) or not np.isfinite(pooled).all() or (norms == 0).any():
                raise ValueError('Embedding model returned invalid CLS vectors')
            vectors[start : start + len(encodings)] = pooled / norms
        return vectors

    def embed_query(self, text: str) -> NDArray[np.float32]:
        return self.embed_passages([QUERY_PREFIX + text]).reshape(DIMENSIONS)


_embedder: Embedder | None = None
_embedder_lock = Lock()


def get_embedder() -> Embedder:
    """Load the configured local model on first use, once per service process."""
    global _embedder
    with _embedder_lock:
        if _embedder is None:
            _embedder = Embedder()
        return _embedder
