import logging
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

import main


class TMDBResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_key = main.TMDB_API_KEY
        self.original_client = main._tmdb_client
        self.responses = []
        self.requests = []

        def handler(request):
            self.requests.append(request)
            response = self.responses.pop(0) if self.responses else httpx.Response(200, json={"results": []})
            if isinstance(response, BaseException):
                raise response
            return response

        main.TMDB_API_KEY = "test-key-never-log"
        main.TMDB_CACHE.clear()
        main._tmdb_semaphore = None
        main._tmdb_semaphore_loop = None
        main._tmdb_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.sleep = patch("main.asyncio.sleep", new_callable=AsyncMock)
        self.sleep_mock = self.sleep.start()
        self.random = patch("main.random.uniform", return_value=0.0)
        self.random.start()

    async def asyncTearDown(self):
        self.sleep.stop()
        self.random.stop()
        if main._tmdb_client is not None and main._tmdb_client is not self.original_client:
            await main._tmdb_client.aclose()
        main._tmdb_client = self.original_client
        main.TMDB_API_KEY = self.original_key
        main.TMDB_CACHE.clear()
        main._tmdb_semaphore = None
        main._tmdb_semaphore_loop = None

    async def test_success_response_is_cached(self):
        self.responses = [httpx.Response(200, json={"results": [{"id": 1}]})]
        first = await main.tmdb_get("/search/movie", {"query": "alien"})
        second = await main.tmdb_get("/search/movie", {"query": "alien"})
        self.assertEqual(first, second)
        self.assertEqual(len(self.requests), 1)

    async def test_missing_key_is_a_controlled_configuration_error(self):
        main.TMDB_API_KEY = ""
        with self.assertRaises(HTTPException) as raised:
            await main.tmdb_get("/search/movie", {"query": "alien"})
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(self.requests, [])

    async def test_invalid_key_is_not_retried_or_logged(self):
        self.responses = [httpx.Response(401, json={"status_message": "Invalid API key"})]
        with self.assertLogs("main", level=logging.WARNING) as captured:
            with self.assertRaises(HTTPException) as raised:
                await main.tmdb_get("/movie/7")
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("test-key-never-log", "\n".join(captured.output))
        self.assertIn("Invalid API key", "\n".join(captured.output))

    async def test_timeout_is_retried_then_reported_as_gateway_timeout(self):
        self.responses = [httpx.ReadTimeout("read timed out") for _ in range(main.TMDB_MAX_ATTEMPTS)]
        with self.assertRaises(HTTPException) as raised:
            await main.tmdb_get("/movie/7")
        self.assertEqual(raised.exception.status_code, 504)
        self.assertEqual(len(self.requests), main.TMDB_MAX_ATTEMPTS)

    async def test_connection_failure_is_retried(self):
        self.responses = [httpx.ConnectError("connection failed") for _ in range(main.TMDB_MAX_ATTEMPTS)]
        with self.assertRaises(HTTPException) as raised:
            await main.tmdb_get("/movie/7")
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(len(self.requests), main.TMDB_MAX_ATTEMPTS)

    async def test_rate_limit_honors_retry_after_and_recovers(self):
        self.responses = [
            httpx.Response(429, headers={"Retry-After": "0"}, json={"status_message": "Slow down"}),
            httpx.Response(200, json={"results": []}),
        ]
        result = await main.tmdb_get("/search/movie", {"query": "alien"})
        self.assertEqual(result, {"results": []})
        self.assertEqual(len(self.requests), 2)
        self.sleep_mock.assert_awaited_once_with(0.0)

    async def test_http_request_timeout_is_retried(self):
        self.responses = [
            httpx.Response(408, json={"status_message": "Request timeout"}),
            httpx.Response(200, json={"results": []}),
        ]
        result = await main.tmdb_get("/search/movie", {"query": "alien"})
        self.assertEqual(result, {"results": []})
        self.assertEqual(len(self.requests), 2)

    async def test_server_error_retries_and_a_later_request_uses_recovered_cache(self):
        self.responses = [
            httpx.Response(503, text="temporary proxy failure"),
            httpx.Response(200, json={"results": [{"id": 42}]}),
        ]
        path = "/trending/movie/day"
        recovered = await main.tmdb_get(path)
        cached = await main.tmdb_get(path)
        self.assertEqual(recovered, {"results": [{"id": 42}]})
        self.assertEqual(cached, recovered)
        self.assertEqual(len(self.requests), 2)

    async def test_malformed_json_is_reported_as_upstream_failure(self):
        self.responses = [httpx.Response(200, content=b"not-json")]
        with self.assertRaises(HTTPException) as raised:
            await main.tmdb_get("/search/movie", {"query": "alien"})
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(len(self.requests), 1)

    async def test_transient_failure_uses_stale_successful_cache(self):
        path = "/search/movie"
        params = {"query": "alien"}
        key = main._cache_key(path, params)
        now = time.monotonic()
        main.TMDB_CACHE[key] = (now - 1, now + 300, {"results": [{"id": 9}]})
        self.responses = [
            httpx.Response(502, text="upstream unavailable")
            for _ in range(main.TMDB_MAX_ATTEMPTS)
        ]
        result = await main.tmdb_get(path, params)
        self.assertEqual(result, {"results": [{"id": 9}]})
        self.assertEqual(len(self.requests), main.TMDB_MAX_ATTEMPTS)

    async def test_invalid_key_does_not_use_stale_cache(self):
        path = "/search/movie"
        params = {"query": "alien"}
        key = main._cache_key(path, params)
        now = time.monotonic()
        main.TMDB_CACHE[key] = (now - 1, now + 300, {"results": [{"id": 9}]})
        self.responses = [httpx.Response(401, json={"status_message": "Invalid API key"})]
        with self.assertRaises(HTTPException) as raised:
            await main.tmdb_get(path, params)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(len(self.requests), 1)

    async def test_genre_failure_preserves_details_and_tfidf_recommendations(self):
        details = main.MovieDetails(
            tmdb_id=7,
            title="Local movie",
            genres=[{"id": 18, "name": "Drama"}],
        )
        local_recommendation = main.TFIDFRecommendation(title="Local match", score=0.8)
        with (
            patch("main.get_movie_details", new=AsyncMock(return_value=details)),
            patch("main.tfidf_recommend_titles", return_value=[("Local match", 0.8)]),
            patch("main.poster_for_title", new=AsyncMock(return_value=None)),
            patch("main.tmdb_get", new=AsyncMock(side_effect=HTTPException(502, "TMDB unavailable"))),
        ):
            bundle = await main.movie_recommendations(7, 1, 1)

        self.assertEqual(bundle.movie_details, details)
        self.assertEqual(bundle.tfidf_recommendations, [local_recommendation])
        self.assertEqual(bundle.genre_recommendations, [])
        self.assertEqual(
            set(bundle.model_dump()),
            {"query", "movie_details", "tfidf_recommendations", "genre_recommendations"},
        )

    def test_existing_recommendation_endpoint_schema_is_preserved(self):
        schema = main.app.openapi()
        paths = schema["paths"]
        self.assertIn("/api/movie/{tmdb_id}/recommendations", paths)
        response_schema = paths["/api/movie/{tmdb_id}/recommendations"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(response_schema["$ref"], "#/components/schemas/RecommendationBundle")
