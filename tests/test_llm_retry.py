from __future__ import annotations

import asyncio
import unittest

import aiohttp

from app.llm.client import _is_retryable_llm_error


class LlmRetryTests(unittest.TestCase):
    def test_timeout_is_retryable(self) -> None:
        self.assertTrue(_is_retryable_llm_error(asyncio.TimeoutError()))
        self.assertTrue(_is_retryable_llm_error(TimeoutError()))

    def test_http_5xx_and_rate_limit_are_retryable(self) -> None:
        self.assertTrue(
            _is_retryable_llm_error(RuntimeError("OpenAI Compatible 失败 HTTP 503: busy"))
        )
        self.assertTrue(
            _is_retryable_llm_error(RuntimeError("OpenAI Compatible 失败 HTTP 429: rate"))
        )
        self.assertTrue(
            _is_retryable_llm_error(RuntimeError("OpenAI Compatible 失败 HTTP 408: timeout"))
        )

    def test_http_4xx_not_retryable(self) -> None:
        self.assertFalse(
            _is_retryable_llm_error(RuntimeError("OpenAI Compatible 失败 HTTP 400: bad"))
        )
        self.assertFalse(
            _is_retryable_llm_error(RuntimeError("OpenAI Compatible 失败 HTTP 401: auth"))
        )

    def test_network_client_error_is_retryable(self) -> None:
        self.assertTrue(_is_retryable_llm_error(aiohttp.ClientConnectionError()))

    def test_value_error_not_retryable(self) -> None:
        self.assertFalse(_is_retryable_llm_error(ValueError("bad json")))


if __name__ == "__main__":
    unittest.main()
