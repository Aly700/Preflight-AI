"""Offline embedding contracts, including the pinned model when cached locally."""

import hashlib
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from tokenizers import Tokenizer, models, pre_tokenizers, processors

ROOT = Path(__file__).resolve().parents[1]
REVISION = '5c38ec7c405ec4b44b94cc5a9bb96e735b38267a'
MODEL = Path.home() / '.cache/preflight-oss/bge-small-en-v1.5' / REVISION
HASHES = {
    'onnx/model.onnx': '828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35',
    'tokenizer.json': 'd241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66',
    'config.json': '094f8e891b932f2000c92cfc663bac4c62069f5d8af5b5278c4306aef3084750',
}
PREFIX = 'Represent this sentence for searching relevant passages: '


def embedding_module(test):
    test.assertTrue((ROOT / 'mcp_service/embeddings.py').is_file(), 'Implement the offline Embedder')
    return importlib.import_module('mcp_service.embeddings')


class EmbedderTests(unittest.TestCase):
    def setUp(self):
        self.module = embedding_module(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        (self.directory / 'onnx').mkdir()
        (self.directory / 'onnx/model.onnx').write_bytes(b'fake model')
        (self.directory / 'config.json').write_text('{}')
        tokenizer = Tokenizer(models.WordLevel({'[UNK]': 0, '[CLS]': 1, '[SEP]': 2, '[PAD]': 3, 'desk': 4}))
        tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
        tokenizer.post_processor = processors.TemplateProcessing(
            single='[CLS] $A [SEP]', special_tokens=[('[CLS]', 1), ('[SEP]', 2)]
        )
        tokenizer.save(str(self.directory / 'tokenizer.json'))
        self.hashes = {name: hashlib.sha256((self.directory / name).read_bytes()).hexdigest() for name in HASHES}

    def test_pinned_hashes(self):
        self.assertEqual(self.module.MODEL_SHA256, HASHES)

    def test_each_tampered_file_is_refused_before_loading(self):
        for name in HASHES:
            with self.subTest(file=name), patch.object(self.module, 'MODEL_SHA256', self.hashes):
                path = self.directory / name
                original = path.read_bytes()
                path.write_bytes(original + b'tampered')
                with (
                    patch.object(self.module.ort, 'InferenceSession') as session,
                    patch.object(self.module, 'Tokenizer') as tokenizer,
                    self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'),
                ):
                    self.module.Embedder(self.directory)
                session.assert_not_called()
                tokenizer.from_file.assert_not_called()
                path.write_bytes(original)

    def test_cls_pooling_normalisation_cpu_inputs_truncation_and_empty_batch(self):
        with (
            patch.object(self.module, 'MODEL_SHA256', self.hashes),
            patch.object(self.module.ort, 'InferenceSession') as session,
        ):

            def fake_run(outputs, feeds):
                self.assertEqual(outputs, ['last_hidden_state'])
                self.assertEqual(set(feeds), {'input_ids', 'attention_mask', 'token_type_ids'})
                for value in feeds.values():
                    self.assertEqual(value.dtype, np.int64)
                    self.assertEqual(value.shape, (2, 512))
                self.assertEqual(feeds['attention_mask'][0].sum(), 3)
                self.assertEqual(feeds['attention_mask'][1].sum(), 512)
                states = np.full((2, 512, 384), 99, dtype=np.float32)
                states[:, 0, :] = 0
                states[:, 0, :2] = [3, 4]
                return [states]

            session.return_value.run.side_effect = fake_run
            embedder = self.module.Embedder(self.directory)
            vectors = embedder.embed_passages(['desk', 'desk ' * 600])
            self.assertEqual(vectors.shape, (2, 384))
            self.assertEqual(vectors.dtype, np.float32)
            np.testing.assert_allclose(vectors[:, :2], [[0.6, 0.8]] * 2)
            np.testing.assert_array_equal(vectors[:, 2:], 0)
            self.assertEqual(session.call_args.kwargs['providers'], ['CPUExecutionProvider'])
            self.assertEqual(session.call_args.kwargs['sess_options'].intra_op_num_threads, 1)
            empty = embedder.embed_passages([])
            self.assertEqual(empty.shape, (0, 384))
            self.assertEqual(empty.dtype, np.float32)
            session.return_value.run.assert_called_once()

    def test_onnx_runtime_telemetry_is_disabled_before_the_session_is_created(self):
        calls = []
        with (
            patch.object(self.module, 'MODEL_SHA256', self.hashes),
            patch.object(self.module.ort, 'disable_telemetry_events', side_effect=lambda: calls.append('off')),
            patch.object(self.module.ort, 'InferenceSession', side_effect=lambda *a, **k: calls.append('session')),
        ):
            self.module.Embedder(self.directory)
        self.assertEqual(calls, ['off', 'session'])

    def test_query_has_exact_prefix_and_service_uses_environment_default(self):
        with (
            patch.object(self.module, 'MODEL_SHA256', self.hashes),
            patch.object(self.module.ort, 'InferenceSession'),
            patch.dict(os.environ, {'PREFLIGHT_EMBEDDING_MODEL_DIR': str(self.directory)}),
        ):
            embedder = self.module.Embedder()
            with patch.object(embedder, 'embed_passages', return_value=np.ones((1, 384), dtype=np.float32)) as call:
                self.assertEqual(embedder.embed_query('desk').shape, (384,))
                call.assert_called_once_with([PREFIX + 'desk'])
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(FileNotFoundError, '/opt/models'):
            self.module.Embedder()


@unittest.skipUnless(MODEL.is_dir(), 'Pinned embedding model is not cached locally')
class RealEmbedderTests(unittest.TestCase):
    def test_determinism_unit_norm_query_prefix_and_semantic_similarity(self):
        module = embedding_module(self)
        embedder = module.Embedder(MODEL)
        texts = [
            'The reviewer needs a handover appointment.',
            'Schedule a review handover meeting.',
            'A purple octopus swims through a coral reef.',
        ]
        first = embedder.embed_passages(texts)
        np.testing.assert_array_equal(first, embedder.embed_passages(texts))
        np.testing.assert_allclose(np.linalg.norm(first, axis=1), 1, atol=1e-6)
        query = embedder.embed_query(texts[0])
        self.assertEqual(query.shape, (384,))
        self.assertEqual(query.dtype, np.float32)
        self.assertAlmostEqual(float(np.linalg.norm(query)), 1, places=6)
        self.assertFalse(np.allclose(query, first[0]))
        self.assertGreater(float(query @ first[1]), float(query @ first[2]))
        self.assertGreater(float(first[0] @ first[1]), float(first[0] @ first[2]))
