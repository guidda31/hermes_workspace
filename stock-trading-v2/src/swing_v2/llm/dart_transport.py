"""Injectable stdlib HTTP transport for DART (opendart.fss.or.kr).

Provides the concrete ``HttpGet``-compatible callable that ``make_dart_disclosure_provider``
consumes as ``http_get=``, plus a raw corpCode-ZIP fetcher for ``load_corp_codes_from_zip``.
It uses only stdlib ``urllib``/``json`` and takes an injected ``url_opener`` so tests never
touch the network. The DART ``crtfc_key`` is a secret: it rides only in the query string and
is never logged nor placed in any raised message — every transport failure fails closed with
a generic ``ValueError`` whose chained context is suppressed so the key cannot leak.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Mapping

DART_CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"


def dart_http_get(
    url: str,
    params: Mapping[str, str],
    *,
    url_opener=urllib.request.urlopen,
    timeout: float = 10.0,
) -> dict:
    """GET ``url`` with ``params``, parse the JSON body, and return the object.

    ``HttpGet``-compatible. Non-JSON bodies, HTTP errors, and timeouts fail
    closed with a generic ``ValueError`` that never contains ``crtfc_key``.
    """
    if type(url) is not str or not url:
        raise ValueError("url must be a nonempty plain str")
    if not isinstance(params, Mapping):
        raise ValueError("params must be a mapping")
    body = _read(_build_url(url, params), url_opener, timeout)
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        raise ValueError("DART response body was not valid JSON") from None
    if type(payload) is not dict:
        raise ValueError("DART response must be a JSON object")
    return payload


def fetch_corp_code_zip(
    api_key: str,
    *,
    url_opener=urllib.request.urlopen,
    timeout: float = 10.0,
) -> bytes:
    """GET the DART corpCode endpoint and return the raw ZIP bytes.

    The bytes feed ``load_corp_codes_from_zip``. ``api_key`` is a secret and
    never appears in any raised message.
    """
    if type(api_key) is not str or not api_key:
        raise ValueError("api_key must be a nonempty plain str")
    body = _read(_build_url(DART_CORP_CODE_URL, {"crtfc_key": api_key}), url_opener, timeout)
    if type(body) is not bytes:
        raise ValueError("corpCode response must be raw bytes")
    return body


def _build_url(url: str, params: Mapping[str, str]) -> str:
    """Append ``params`` as a URL-encoded query string to ``url``."""
    query = urllib.parse.urlencode({str(k): str(v) for k, v in params.items()})
    return f"{url}?{query}" if query else url


def _read(full_url: str, url_opener, timeout: float) -> bytes:
    """Open ``full_url`` and return its raw body, failing closed without the key."""
    if not callable(url_opener):
        raise ValueError("url_opener must be callable")
    if type(timeout) not in (int, float) or timeout <= 0:
        raise ValueError("timeout must be a positive number")
    try:
        with url_opener(full_url, timeout=timeout) as response:
            return response.read()
    except OSError:
        # HTTPError/URLError/timeout all subclass OSError; suppress context so
        # the URL (which carries crtfc_key) can never leak through the chain.
        raise ValueError("DART HTTP request failed") from None
