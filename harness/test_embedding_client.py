import json
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

from memcore import embedding


class MockEmbeddingHandler(BaseHTTPRequestHandler):
    fail_requests = False
    last_headers = None

    def do_POST(self):
        MockEmbeddingHandler.last_headers = dict(self.headers)
        if MockEmbeddingHandler.fail_requests:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": "Internal Server Error"}')
            return

        content_length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(content_length).decode('utf-8'))
        input_data = body.get('input')

        if isinstance(input_data, str):
            input_list = [input_data]
        else:
            input_list = input_data

        data = []
        for idx, text in enumerate(input_list):
            # return deterministic mock embedding
            vec = [float(len(text)), 0.5, 0.25]
            data.append({"object": "embedding", "embedding": vec, "index": idx})

        resp = {
            "object": "list",
            "data": data,
            "model": body.get("model", "mock-model")
        }
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(resp).encode('utf-8'))

    def log_message(self, format, *args):
        # silence log output in test
        pass


class EmbeddingClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), MockEmbeddingHandler)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever)
        cls.server_thread.daemon = True
        cls.server_thread.start()
        cls.endpoint = f'http://127.0.0.1:{cls.port}/v1/embeddings'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        MockEmbeddingHandler.fail_requests = False
        embedding.reset_circuit_breaker()

    def test_get_embedding_success(self):
        vec = embedding.get_embedding("hello world", endpoint=self.endpoint)
        self.assertIsNotNone(vec)
        self.assertEqual(len(vec), 3)
        self.assertAlmostEqual(vec[0], 11.0)
        self.assertAlmostEqual(vec[1], 0.5)

    def test_get_embeddings_batch_success(self):
        texts = ["apple", "banana"]
        vecs = embedding.get_embeddings_batch(texts, endpoint=self.endpoint)
        self.assertIsNotNone(vecs)
        self.assertEqual(len(vecs), 2)
        self.assertAlmostEqual(vecs[0][0], 5.0)
        self.assertAlmostEqual(vecs[1][0], 6.0)

    def test_get_embedding_server_error_returns_none(self):
        MockEmbeddingHandler.fail_requests = True
        vec = embedding.get_embedding("should fail", endpoint=self.endpoint)
        self.assertIsNone(vec)

    def test_get_embedding_offline_server_returns_none_fast(self):
        # Unused port where no server is listening
        dead_endpoint = 'http://127.0.0.1:59999/v1/embeddings'
        start = time.time()
        vec = embedding.get_embedding("offline", endpoint=dead_endpoint, timeout=0.2)
        elapsed = time.time() - start
        self.assertIsNone(vec)
        self.assertLess(elapsed, 1.0)

    def test_circuit_breaker_trips_after_failures(self):
        MockEmbeddingHandler.fail_requests = True
        for _ in range(3):
            embedding.get_embedding("fail", endpoint=self.endpoint, timeout=0.1)
        self.assertTrue(embedding.is_circuit_open())
        # Even if server recovers, circuit breaker blocks until reset or cooldown
        MockEmbeddingHandler.fail_requests = False
        vec = embedding.get_embedding("recovering", endpoint=self.endpoint)
        self.assertIsNone(vec)
        # Reset allows calls again
        embedding.reset_circuit_breaker()
        vec = embedding.get_embedding("recovering", endpoint=self.endpoint)
        self.assertIsNotNone(vec)

    def test_resolve_config_presets(self):
        # 9router preset
        url, model, _ = embedding.resolve_config(provider='9router')
        self.assertEqual(url, 'http://localhost:20128/v1/embeddings')
        self.assertEqual(model, 'text-embedding-3-small')

        # openrouter preset
        url, model, _ = embedding.resolve_config(provider='openrouter')
        self.assertEqual(url, 'https://openrouter.ai/api/v1/embeddings')
        self.assertEqual(model, 'text-embedding-3-small')

        # ollama preset
        url, model, _ = embedding.resolve_config(provider='ollama')
        self.assertEqual(url, 'http://localhost:11434/v1/embeddings')
        self.assertEqual(model, 'nomic-embed-text')

        # disabled preset
        url, _, _ = embedding.resolve_config(provider='none')
        self.assertIsNone(url)

    def test_authorization_header_sent_when_api_key_present(self):
        MockEmbeddingHandler.last_headers = None
        vec = embedding.get_embedding(
            "test auth",
            endpoint=self.endpoint,
            api_key="sk-test-secret-12345",
        )
        self.assertIsNotNone(vec)
        self.assertIsNotNone(MockEmbeddingHandler.last_headers)
        auth = MockEmbeddingHandler.last_headers.get('Authorization')
        self.assertEqual(auth, 'Bearer sk-test-secret-12345')
