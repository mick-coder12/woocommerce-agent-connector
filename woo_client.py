"""Read-only HTTP client for the WooCommerce REST API.

Responsibilities: configuration from environment variables, API-key (HTTP Basic)
authentication, timeouts, retry with exponential backoff, and translating HTTP /
network failures into short, agent-safe error messages.

Nothing in this module logs or returns credentials or raw response bodies.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger("woocommerce-connector")
logger.addHandler(logging.NullHandler())   # library-style: the host (server.py) configures output

# --- Retry / timeout policy -------------------------------------------------
MAX_ATTEMPTS = 4               # 1 initial try + 3 retries; the loop is bounded
BASE_DELAY_S = 1.0             # backoff schedule: 1s, 2s, 4s
MAX_DELAY_S = 30.0             # never sleep longer than this, even if Retry-After says so
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 15.0
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
USER_AGENT = "WooCommerce-Agent-Connector/1.0"


# --- Errors -----------------------------------------------------------------
class ConnectorError(Exception):
    """Base class. The message is always safe to show to the agent."""


class ConfigError(ConnectorError):
    pass


class InvalidInputError(ConnectorError):
    pass


class AuthError(ConnectorError):
    pass


class NotFoundError(ConnectorError):
    pass


class PageOutOfRangeError(ConnectorError):
    pass


class RateLimitError(ConnectorError):
    pass


class UpstreamError(ConnectorError):
    pass


class MalformedResponseError(ConnectorError):
    pass


# --- Configuration ----------------------------------------------------------
ENV_URL = "WOOCOMMERCE_STORE_URL"
ENV_KEY = "WOOCOMMERCE_CONSUMER_KEY"
ENV_SECRET = "WOOCOMMERCE_CONSUMER_SECRET"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class Config:
    store_url: str
    consumer_key: str = field(repr=False)      # repr=False: never printed by accident
    consumer_secret: str = field(repr=False)


def load_config(env: Optional[Dict[str, str]] = None) -> Optional[Config]:
    """Load credentials from environment variables only.

    Returns None when *no* credential variable is set (-> demo mode).
    Raises ConfigError when the configuration is partial or invalid, so a typo
    can never silently turn into "serving fake data as if it were real".
    """
    env = os.environ if env is None else env
    values = {name: (env.get(name) or "").strip() for name in (ENV_URL, ENV_KEY, ENV_SECRET)}
    if not any(values.values()):
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError(
            "Incomplete WooCommerce configuration; missing: " + ", ".join(missing)
            + ". Set all three variables, or none of them to run in demo mode."
        )
    parsed = urlparse(values[ENV_URL])
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError(f"{ENV_URL} must be a full URL such as https://your-store.com")
    if parsed.username or parsed.password:
        raise ConfigError(f"{ENV_URL} must not contain embedded credentials.")
    if parsed.scheme == "http" and parsed.hostname not in _LOCAL_HOSTS:
        raise ConfigError(
            f"{ENV_URL} must use https:// (API keys are sent with HTTP Basic auth and must not "
            "travel over plain HTTP). http:// is only allowed for localhost."
        )
    return Config(values[ENV_URL].rstrip("/"), values[ENV_KEY], values[ENV_SECRET])


# --- Client -----------------------------------------------------------------
@dataclass
class ApiResponse:
    data: Any
    total: Optional[int] = None         # X-WP-Total
    total_pages: Optional[int] = None   # X-WP-TotalPages


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Retry-After is either delta-seconds or an HTTP date. Returns seconds or None."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def _header_int(headers: Any, name: str) -> Optional[int]:
    try:
        return int(headers.get(name))
    except (TypeError, ValueError):
        return None


class WooClient:
    """GET-only client. There is deliberately no method that can write."""

    def __init__(self, config: Config, session: Optional[requests.Session] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self._config = config
        self._session = session or requests.Session()
        self._sleep = sleep

    def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> ApiResponse:
        url = f"{self._config.store_url}/wp-json/wc/v3/{endpoint}"
        last_error: ConnectorError = UpstreamError("Request to WooCommerce failed.")

        for attempt in range(1, MAX_ATTEMPTS + 1):
            retry_after: Optional[float] = None
            try:
                response = self._session.get(
                    url,
                    auth=(self._config.consumer_key, self._config.consumer_secret),
                    params=params,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                    timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                    allow_redirects=False,  # never forward credentials to a redirect target
                )
            except requests.exceptions.Timeout:   # checked before ConnectionError: ConnectTimeout is both
                logger.warning("Timeout calling %s (attempt %d/%d)", endpoint, attempt, MAX_ATTEMPTS)
                last_error = UpstreamError(
                    f"The WooCommerce store did not respond in time (after {attempt} attempt(s)). Try again shortly.")
            except requests.exceptions.ConnectionError as exc:
                logger.warning("Connection error calling %s (attempt %d/%d): %s",
                               endpoint, attempt, MAX_ATTEMPTS, type(exc).__name__)
                last_error = UpstreamError(
                    "Could not connect to the WooCommerce store. Check WOOCOMMERCE_STORE_URL and that the store is online.")
            except requests.exceptions.RequestException as exc:   # invalid URL, redirect loop, ... not retryable
                logger.error("Non-retryable request error for %s: %s", endpoint, type(exc).__name__)
                raise UpstreamError(f"The request to WooCommerce failed ({type(exc).__name__}).") from None
            else:
                if response.status_code not in RETRYABLE_STATUS:
                    return self._finish(response, endpoint)       # success or non-retryable error
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                logger.warning("HTTP %d from %s (attempt %d/%d)", response.status_code, endpoint, attempt, MAX_ATTEMPTS)
                if response.status_code == 429:
                    last_error = RateLimitError(
                        f"WooCommerce rate limit exceeded; gave up after {attempt} attempt(s). Try again later.")
                else:
                    last_error = UpstreamError(
                        f"WooCommerce is temporarily unavailable (HTTP {response.status_code}); "
                        f"gave up after {attempt} attempt(s).")

            if attempt == MAX_ATTEMPTS:
                break
            if retry_after is not None and retry_after > MAX_DELAY_S:
                # Server wants a long pause: don't block the agent, tell it when to come back.
                raise RateLimitError(
                    f"WooCommerce asked clients to wait {int(retry_after)}s before retrying. Try again later.")
            delay = retry_after if retry_after is not None else min(BASE_DELAY_S * 2 ** (attempt - 1), MAX_DELAY_S)
            self._sleep(delay)

        raise last_error

    @staticmethod
    def _finish(response: requests.Response, endpoint: str) -> ApiResponse:
        status = response.status_code
        if 200 <= status < 300:
            try:
                data = response.json()
            except ValueError:
                logger.error("Non-JSON 2xx response from %s", endpoint)
                raise MalformedResponseError(
                    "WooCommerce returned a response that was not valid JSON. "
                    "A security plugin, maintenance page or wrong store URL is a likely cause.") from None
            return ApiResponse(data, _header_int(response.headers, "X-WP-Total"),
                               _header_int(response.headers, "X-WP-TotalPages"))

        code = ""
        try:
            body = response.json()
            if isinstance(body, dict) and isinstance(body.get("code"), str):
                code = body["code"][:80]
        except ValueError:
            pass
        # Log technical detail internally (truncated); the agent only gets the safe message below.
        logger.error("HTTP %d from %s code=%r body=%r", status, endpoint, code, (response.text or "")[:200])

        if 300 <= status < 400:
            raise UpstreamError("WooCommerce redirected the request. Update WOOCOMMERCE_STORE_URL to the final https:// URL.")
        if status == 401:
            raise AuthError("WooCommerce rejected the API credentials (HTTP 401). Check the consumer key and secret.")
        if status == 403:
            raise AuthError("The API key is not permitted to read this resource (HTTP 403). Ensure it has Read permission.")
        if status == 404:
            if code == "rest_no_route":
                raise UpstreamError("WooCommerce REST API endpoint not found (HTTP 404). Check WOOCOMMERCE_STORE_URL "
                                    "and that the REST API / pretty permalinks are enabled.")
            raise NotFoundError("The requested resource was not found.")
        if status == 400 and code == "rest_post_invalid_page_number":
            raise PageOutOfRangeError("The requested page is beyond the last page of results.")
        if status == 400:
            raise InvalidInputError(f"WooCommerce rejected the request parameters (HTTP 400{', ' + code if code else ''}).")
        raise UpstreamError(f"WooCommerce returned an unexpected error (HTTP {status}).")
