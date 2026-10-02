"""WooClient: auth config, retry/backoff, Retry-After, timeouts, error mapping. No network, no real store."""
import json
import logging
import unittest

import requests

from woo_client import (MAX_ATTEMPTS, AuthError, Config, ConfigError, InvalidInputError,
                        MalformedResponseError, NotFoundError, PageOutOfRangeError,
                        RateLimitError, UpstreamError, WooClient, load_config)

KEY, SECRET = "ck_test_KEY_123", "cs_test_SECRET_456"
CONFIG = Config("https://shop.example.com", KEY, SECRET)


def make_response(status=200, body=None, headers=None, text=None):
    r = requests.Response()
    r.status_code = status
    r._content = (text if text is not None else json.dumps(body if body is not None else [])).encode()
    r.headers.update(headers or {})
    return r


class FakeSession:
    """Plays back a script of responses/exceptions and records every call."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]   # last item repeats
        if isinstance(item, Exception):
            raise item
        return item


def client_for(*script):
    session, sleeps = FakeSession(*script), []
    return WooClient(CONFIG, session=session, sleep=sleeps.append), session, sleeps


class RetryTests(unittest.TestCase):
    def test_429_is_retried_with_exponential_backoff(self):
        client, session, sleeps = client_for(make_response(429), make_response(429), make_response(200, [{"id": 1}]))
        self.assertEqual(client.get("orders").data, [{"id": 1}])
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_retry_after_header_is_respected(self):
        client, _, sleeps = client_for(make_response(429, headers={"Retry-After": "7"}), make_response(200, []))
        client.get("orders")
        self.assertEqual(sleeps, [7.0])

    def test_very_long_retry_after_fails_fast_instead_of_blocking_the_agent(self):
        client, session, sleeps = client_for(make_response(429, headers={"Retry-After": "600"}))
        with self.assertRaisesRegex(RateLimitError, "600s"):
            client.get("orders")
        self.assertEqual((len(session.calls), sleeps), (1, []))

    def test_retries_are_bounded_no_infinite_loop(self):
        client, session, sleeps = client_for(make_response(429))   # 429 forever
        with self.assertRaises(RateLimitError):
            client.get("orders")
        self.assertEqual(len(session.calls), MAX_ATTEMPTS)
        self.assertEqual(len(sleeps), MAX_ATTEMPTS - 1)

    def test_read_timeout_is_retried_then_surfaces_clearly(self):
        client, session, _ = client_for(requests.exceptions.ReadTimeout("boom"))
        with self.assertRaisesRegex(UpstreamError, "did not respond in time"):
            client.get("orders")
        self.assertEqual(len(session.calls), MAX_ATTEMPTS)

    def test_timeout_then_success(self):
        client, _, sleeps = client_for(requests.exceptions.ConnectTimeout("x"), make_response(200, []))
        self.assertEqual(client.get("orders").data, [])
        self.assertEqual(sleeps, [1.0])

    def test_connection_error_is_retried_then_surfaces_clearly(self):
        client, session, _ = client_for(requests.exceptions.ConnectionError("dns failure"))
        with self.assertRaisesRegex(UpstreamError, "Could not connect"):
            client.get("orders")
        self.assertEqual(len(session.calls), MAX_ATTEMPTS)

    def test_503_is_retried(self):
        client, session, _ = client_for(make_response(503), make_response(200, []))
        client.get("orders")
        self.assertEqual(len(session.calls), 2)

    def test_non_retryable_errors_are_not_retried(self):
        cases = {400: InvalidInputError, 401: AuthError, 403: AuthError, 404: NotFoundError, 500: UpstreamError}
        for status, exc in cases.items():
            with self.subTest(status=status):
                client, session, sleeps = client_for(make_response(status, {"code": "x"}))
                with self.assertRaises(exc):
                    client.get("orders/1")
                self.assertEqual((len(session.calls), sleeps), (1, []))

    def test_redirects_are_not_followed_so_credentials_are_not_forwarded(self):
        client, session, _ = client_for(make_response(301, headers={"Location": "https://evil.example"}))
        with self.assertRaisesRegex(UpstreamError, "redirected"):
            client.get("orders")
        self.assertIs(session.calls[0][1]["allow_redirects"], False)


class ErrorHandlingTests(unittest.TestCase):
    def test_errors_do_not_leak_secrets_or_raw_bodies(self):
        body = {"code": "internal_error", "message": "SECRET_BODY_DETAIL <stack trace>"}
        with self.assertLogs("woocommerce-connector", level=logging.ERROR) as logs:
            client, _, _ = client_for(make_response(500, body))
            with self.assertRaises(UpstreamError) as ctx:
                client.get("orders")
        msg = str(ctx.exception)
        for forbidden in (KEY, SECRET, "SECRET_BODY_DETAIL", "stack trace"):
            self.assertNotIn(forbidden, msg)
        self.assertTrue(any("HTTP 500" in line for line in logs.output))   # technical detail IS logged internally
        self.assertTrue(all(KEY not in line and SECRET not in line for line in logs.output))

    def test_page_beyond_last_page_is_a_clear_error(self):
        client, _, _ = client_for(make_response(400, {"code": "rest_post_invalid_page_number"}))
        with self.assertRaisesRegex(PageOutOfRangeError, "beyond the last page"):
            client.get("orders", {"page": 99})

    def test_html_instead_of_json_is_reported_as_malformed(self):
        client, _, _ = client_for(make_response(200, text="<html>Maintenance</html>"))
        with self.assertRaisesRegex(MalformedResponseError, "not valid JSON"):
            client.get("orders")

    def test_pagination_headers_and_basic_auth_are_sent(self):
        client, session, _ = client_for(make_response(200, [], headers={"X-WP-Total": "42", "X-WP-TotalPages": "5"}))
        resp = client.get("orders", {"page": 2})
        self.assertEqual((resp.total, resp.total_pages), (42, 5))
        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://shop.example.com/wp-json/wc/v3/orders")
        self.assertEqual(kwargs["auth"], (KEY, SECRET))
        self.assertEqual(kwargs["timeout"], (5.0, 15.0))     # explicit timeout is always set


class ConfigTests(unittest.TestCase):
    URL, K, S = "WOOCOMMERCE_STORE_URL", "WOOCOMMERCE_CONSUMER_KEY", "WOOCOMMERCE_CONSUMER_SECRET"

    def test_no_credentials_means_demo_mode(self):
        self.assertIsNone(load_config({}))

    def test_partial_credentials_are_an_error_not_silent_demo_mode(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config({self.URL: "https://shop.example.com", self.K: "ck_x"})
        self.assertIn(self.S, str(ctx.exception))
        self.assertNotIn("ck_x", str(ctx.exception))

    def test_plain_http_is_rejected_except_localhost(self):
        env = {self.K: "ck", self.S: "cs"}
        with self.assertRaisesRegex(ConfigError, "https"):
            load_config({**env, self.URL: "http://shop.example.com"})
        self.assertEqual(load_config({**env, self.URL: "http://localhost:8080/"}).store_url, "http://localhost:8080")

    def test_config_repr_hides_secrets(self):
        cfg = load_config({self.URL: "https://s.example.com/", self.K: KEY, self.S: SECRET})
        self.assertNotIn(KEY, repr(cfg))
        self.assertNotIn(SECRET, repr(cfg))
        self.assertEqual(cfg.store_url, "https://s.example.com")


if __name__ == "__main__":
    unittest.main()
