"""Bounded asynchronous HTTP fetching with explicit redirect scope."""

import asyncio
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx


@dataclass
class FetchResult:
    requested_url: str
    url: str
    status: int | None = None
    content_type: str = ""
    body: bytes = b""
    error: str | None = None
    detail: str = ""


def origin(url: str) -> tuple[str, str | None, int]:
    p = urlsplit(url)
    return p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80)


class Fetcher:
    """A semaphore limits concurrent requests across all hosts in one run."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        concurrency: int,
        max_bytes: int,
        request_timeout: float,
        max_redirects: int = 5,
    ) -> None:
        self.client = client
        self.semaphore = asyncio.Semaphore(concurrency)
        self.max_bytes = max_bytes
        self.request_timeout = request_timeout
        self.max_redirects = max_redirects

    async def get(self, url: str, allowed_origin: str) -> FetchResult:
        async with self.semaphore:
            try:
                # Wall-clock cap also catches servers that trickle bytes forever.
                async with asyncio.timeout(self.request_timeout):
                    return await self._get(url, allowed_origin)
            except (httpx.HTTPError, httpx.InvalidURL, TimeoutError, ValueError) as exc:
                return FetchResult(url, url, error="network_error", detail=str(exc) or "Timed out")

    async def _get(self, url: str, allowed_origin: str) -> FetchResult:
        current = url
        for hop in range(self.max_redirects + 1):
            async with self.client.stream("GET", current) as response:
                result = FetchResult(
                    url,
                    str(response.url),
                    response.status_code,
                    response.headers.get("content-type", ""),
                )
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        result.error = "redirect_error"
                        result.detail = "Redirect has no Location header"
                        return result
                    next_url = urljoin(current, location)
                    parsed = urlsplit(next_url)
                    if (
                        origin(next_url) != origin(allowed_origin)
                        or parsed.username is not None
                        or parsed.password is not None
                    ):
                        result.error = "blocked_redirect"
                        result.detail = f"Redirect leaves target origin: {next_url}"
                        return result
                    if hop == self.max_redirects:
                        result.error = "redirect_error"
                        result.detail = "Too many redirects"
                        return result
                    current = next_url
                    continue
                chunks = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(chunks) + len(chunk) > self.max_bytes:
                        result.error = "too_large"
                        result.detail = f"Body exceeds {self.max_bytes} decoded bytes"
                        return result
                    chunks.extend(chunk)
                result.body = bytes(chunks)
                return result
        raise AssertionError("Unreachable redirect state")
