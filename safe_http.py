"""Centralized outbound HTTP transport with SSRF and resource controls."""

import asyncio
import ipaddress
import json
import socket
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlsplit

import aiohttp


class SafeHttpError(Exception):
    """Base error for rejected or failed outbound requests."""


class UnsafeDestinationError(SafeHttpError):
    """The URL or one of its resolved addresses is not safe for outbound use."""


class ResponseTooLargeError(SafeHttpError):
    """The response exceeded the configured byte limit."""


class UnexpectedContentTypeError(SafeHttpError):
    """The response content type was not explicitly accepted."""


@dataclass(frozen=True)
class SafeHttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str

    def text(self) -> str:
        charset = "utf-8"
        content_type = self.headers.get("Content-Type", "")
        for part in content_type.split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                charset = value.strip("\"'")
                break
        return self.body.decode(charset, errors="replace")

    def json(self):
        return json.loads(self.body)


def _is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    return ip.is_global


def _validate_url(url: str, allowed_hosts: set[str] | None) -> tuple[str, int]:
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise UnsafeDestinationError("only https URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeDestinationError("URL credentials are not allowed")
    try:
        host = parsed.hostname
        port = parsed.port or 443
    except ValueError as exc:
        raise UnsafeDestinationError("invalid URL port") from exc
    if not host:
        raise UnsafeDestinationError("URL must include a hostname")
    normalized = host.rstrip(".").lower()
    if allowed_hosts is not None and normalized not in allowed_hosts:
        raise UnsafeDestinationError("hostname is not approved for this provider")
    return normalized, port


def _validate_content_type(content_type: str, expected: Iterable[str]) -> None:
    media_type = content_type.split(";", 1)[0].strip().lower()
    accepted = tuple(value.lower() for value in expected)
    if not any(
        media_type == item
        or (item.endswith("/*") and media_type.startswith(item[:-1]))
        for item in accepted
    ):
        raise UnexpectedContentTypeError(
            f"unexpected response content type: {media_type or 'missing'}"
        )


def _append_bounded(body: bytearray, chunk: bytes, limit: int) -> None:
    body.extend(chunk)
    if len(body) > limit:
        raise ResponseTooLargeError("response exceeds byte limit")


async def _resolve_public_addresses(host: str, port: int) -> list[dict]:
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        records = [(literal.version == 6 and socket.AF_INET6 or socket.AF_INET, str(literal))]
    else:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as exc:
            raise SafeHttpError(f"DNS resolution failed for {host}") from exc
        records = [(family, sockaddr[0]) for family, _, _, _, sockaddr in infos]

    unique = []
    seen = set()
    for family, address in records:
        if address in seen:
            continue
        seen.add(address)
        if not _is_public_address(address):
            raise UnsafeDestinationError(
                f"hostname {host} resolved to a non-public address"
            )
        unique.append({
            "hostname": host,
            "host": address,
            "port": port,
            "family": family,
            "proto": socket.IPPROTO_TCP,
            "flags": socket.AI_NUMERICHOST,
        })
    if not unique:
        raise SafeHttpError(f"DNS resolution returned no addresses for {host}")
    return unique


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, records: list[dict]):
        self._records = records

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        return [dict(record) for record in self._records]

    async def close(self):
        return None


class SafeHttpClient:
    def __init__(
        self,
        *,
        total_timeout: float = 15,
        connect_timeout: float = 5,
        max_response_bytes: int = 2 * 1024 * 1024,
        max_redirects: int = 3,
    ):
        self.total_timeout = total_timeout
        self.connect_timeout = connect_timeout
        self.max_response_bytes = max_response_bytes
        self.max_redirects = max_redirects

    async def request(
        self,
        method: str,
        url: str,
        *,
        allowed_hosts: Iterable[str] | None,
        expected_content_types: Iterable[str],
        headers: dict | None = None,
        params=None,
        json_body=None,
        data=None,
        max_response_bytes: int | None = None,
        total_timeout: float | None = None,
    ) -> SafeHttpResponse:
        approved = (
            {host.rstrip(".").lower() for host in allowed_hosts}
            if allowed_hosts is not None
            else None
        )
        expected = tuple(value.lower() for value in expected_content_types)
        limit = max_response_bytes or self.max_response_bytes
        timeout_seconds = total_timeout or self.total_timeout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        current_url = url
        current_method = method.upper()
        current_json = json_body
        current_data = data

        for redirect_count in range(self.max_redirects + 1):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError("outbound request timed out")
            host, port = _validate_url(current_url, approved)
            records = await asyncio.wait_for(
                _resolve_public_addresses(host, port),
                timeout=min(self.connect_timeout, remaining),
            )
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError("outbound request timed out")
            timeout = aiohttp.ClientTimeout(
                total=remaining,
                connect=min(self.connect_timeout, remaining),
            )
            connector = aiohttp.TCPConnector(
                resolver=_PinnedResolver(records),
                use_dns_cache=False,
            )
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                auto_decompress=True,
            ) as session:
                async with session.request(
                    current_method,
                    current_url,
                    headers=headers,
                    params=params,
                    json=current_json,
                    data=current_data,
                    allow_redirects=False,
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        if redirect_count >= self.max_redirects:
                            raise SafeHttpError("too many redirects")
                        location = response.headers.get("Location")
                        if not location:
                            raise SafeHttpError("redirect response omitted Location")
                        current_url = urljoin(str(response.url), location)
                        params = None
                        if response.status == 303 or (
                            response.status in {301, 302}
                            and current_method not in {"GET", "HEAD"}
                        ):
                            current_method = "GET"
                            current_json = None
                            current_data = None
                        continue

                    content_type = response.headers.get("Content-Type", "")
                    _validate_content_type(content_type, expected)
                    declared_length = response.content_length
                    if declared_length is not None and declared_length > limit:
                        raise ResponseTooLargeError("response exceeds byte limit")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        _append_bounded(body, chunk, limit)
                    return SafeHttpResponse(
                        status=response.status,
                        headers=dict(response.headers),
                        body=bytes(body),
                        url=str(response.url),
                    )
        raise SafeHttpError("redirect handling failed")

    async def get_json(self, url: str, **kwargs):
        response = await self.request(
            "GET",
            url,
            expected_content_types=("application/json",),
            **kwargs,
        )
        return response, response.json()

    async def post_json(self, url: str, **kwargs):
        response = await self.request(
            "POST",
            url,
            expected_content_types=("application/json",),
            **kwargs,
        )
        return response, response.json()


safe_http = SafeHttpClient()