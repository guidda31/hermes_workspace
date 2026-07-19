"""Naver News Search API adapter feeding the news ``EvidenceProvider``.

Turns Naver's news-search results into the generic records ``make_news_provider``
consumes, so the forward/paper brief can carry recent news per symbol. Key-optional
and graceful: without ``NAVER_CLIENT_ID`` / ``NAVER_CLIENT_SECRET`` the factory
returns ``None`` and the brief simply stays without-news. Uses only stdlib
``urllib``/``json``; the HTTP transport is injected so tests never touch the network.
The client id/secret are secrets: they ride only in request headers and are never
logged nor placed in any raised message.

Naver has no date-range parameter — it returns recent news sorted by date — so the
fetch filters items to ``[begin_date, end_date]`` itself by parsing each ``pubDate``.
The caller supplies the ``{symbol: Korean company name}`` map (later sourced from the
same KRX listing metadata that backs the corp-code map); an unknown symbol yields no
query and an empty result.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import date, datetime
from typing import Callable, Optional

from .brief import EvidenceProvider
from .news_provider import Fetch, make_news_provider

NAVER_NEWS_URL = "https://openapi.naver.com/v1/search/news.json"

# RFC-1123 as emitted by Naver's ``pubDate`` (English C-locale tokens, ``+0900``).
_RFC_1123 = "%a, %d %b %Y %H:%M:%S %z"

_CLIENT_ID_ENV = "NAVER_CLIENT_ID"
_CLIENT_SECRET_ENV = "NAVER_CLIENT_SECRET"

_DISPLAY = "20"

HttpGet = Callable[[str, Mapping[str, str]], Mapping]


def naver_news_fetch(
    *,
    client_id: str,
    client_secret: str,
    name_by_symbol: Mapping[str, str],
    http_get: HttpGet,
) -> Fetch:
    """Build a ``fetch(symbol, begin, end)`` closure over the Naver News API.

    Looks up the Korean name for ``symbol`` (unknown -> empty, no request), queries
    Naver via the injected ``http_get``, strips ``<b>`` tags from titles, keeps items
    whose ``pubDate`` calendar day is in ``[begin, end]``, and returns records shaped
    ``{"title", "pubDate", "link"}`` for ``make_news_provider``.
    """
    if type(client_id) is not str or not client_id:
        raise ValueError("client_id must be a nonempty plain str")
    if type(client_secret) is not str or not client_secret:
        raise ValueError("client_secret must be a nonempty plain str")
    if not isinstance(name_by_symbol, Mapping):
        raise ValueError("name_by_symbol must be a mapping")
    if not callable(http_get):
        raise ValueError("http_get must be callable")

    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}

    def fetch(symbol: str, begin_date: date, end_date: date) -> tuple[dict, ...]:
        if type(symbol) is not str or not symbol:
            raise ValueError("symbol must be a nonempty plain str")
        if type(begin_date) is not date or type(end_date) is not date:
            raise ValueError("begin_date and end_date must be plain dates")
        name = name_by_symbol.get(symbol)
        if type(name) is not str or not name.strip():
            return ()
        payload = http_get(_build_url(name), headers)
        return _records_in_window(payload, begin_date, end_date)

    return fetch


def naver_http_get(
    url: str,
    headers: Mapping[str, str],
    *,
    url_opener=urllib.request.urlopen,
    timeout: float = 10.0,
) -> dict:
    """GET ``url`` with ``headers``, parse the JSON body, and return the object.

    ``HttpGet``-compatible. The secret client id/secret ride only in ``headers``.
    Non-JSON bodies, HTTP errors, and timeouts fail closed with a generic
    ``ValueError`` that never contains a credential.
    """
    if type(url) is not str or not url:
        raise ValueError("url must be a nonempty plain str")
    if not isinstance(headers, Mapping):
        raise ValueError("headers must be a mapping")
    if not callable(url_opener):
        raise ValueError("url_opener must be callable")
    if type(timeout) not in (int, float) or timeout <= 0:
        raise ValueError("timeout must be a positive number")
    request = urllib.request.Request(url, headers={str(k): str(v) for k, v in headers.items()})
    try:
        with url_opener(request, timeout=timeout) as response:
            body = response.read()
    except OSError:
        # HTTPError/URLError/timeout all subclass OSError; suppress the chained
        # context so a header-bearing message can never leak a credential.
        raise ValueError("Naver HTTP request failed") from None
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        raise ValueError("Naver response body was not valid JSON") from None
    if type(payload) is not dict:
        raise ValueError("Naver response must be a JSON object")
    return payload


def naver_news_provider_or_none(
    *,
    symbols,
    name_by_symbol: Mapping[str, str],
    env: Optional[Mapping[str, str]] = None,
    window_days: int = 14,
    http_get: Optional[HttpGet] = None,
) -> Optional[EvidenceProvider]:
    """Build a Naver news provider from the environment, or ``None`` if no key.

    Graceful by design: with either ``NAVER_CLIENT_ID`` or ``NAVER_CLIENT_SECRET``
    absent the forward brief simply stays without-news. The ``{symbol: name}`` map is
    supplied by the caller; it is restricted to ``symbols`` here.
    """
    environment = os.environ if env is None else env
    client_id = environment.get(_CLIENT_ID_ENV)
    client_secret = environment.get(_CLIENT_SECRET_ENV)
    if not client_id or not client_secret:
        return None
    if not isinstance(name_by_symbol, Mapping):
        raise ValueError("name_by_symbol must be a mapping")
    names = {symbol: name_by_symbol[symbol] for symbol in symbols if symbol in name_by_symbol}
    fetch = naver_news_fetch(
        client_id=client_id,
        client_secret=client_secret,
        name_by_symbol=names,
        http_get=http_get if http_get is not None else naver_http_get,
    )
    return make_news_provider(fetch=fetch, window_days=window_days)


def _build_url(name: str) -> str:
    """Build the news-search URL, URL-encoding the Korean ``query`` term."""
    query = urllib.parse.urlencode({"query": name, "display": _DISPLAY, "sort": "date"})
    return f"{NAVER_NEWS_URL}?{query}"


def _records_in_window(payload: object, begin_date: date, end_date: date) -> tuple[dict, ...]:
    """Map in-window Naver items to ``{title, pubDate, link}`` records, fail-closed."""
    if not isinstance(payload, Mapping):
        raise ValueError("Naver response must be a JSON object")
    items = payload.get("items", ())
    if not isinstance(items, (list, tuple)):
        raise ValueError("Naver response 'items' must be a list")
    records: list[dict] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("Naver news items must be objects")
        published = _pub_date(item.get("pubDate"))
        if published is None or not (begin_date <= published <= end_date):
            continue
        records.append({
            "title": _strip_bold(item.get("title")),
            "pubDate": item.get("pubDate"),
            "link": _link(item),
        })
    return tuple(records)


def _pub_date(value: object) -> Optional[date]:
    """Parse Naver's RFC-1123 ``pubDate`` to a calendar day; None if unparseable."""
    if type(value) is not str or not value.strip():
        return None
    try:
        return datetime.strptime(value, _RFC_1123).date()
    except ValueError:
        return None


def _strip_bold(title: object) -> str:
    """Strip Naver's ``<b>``/``</b>`` search-term highlight tags from a title."""
    if type(title) is not str:
        return ""
    return title.replace("<b>", "").replace("</b>", "")


def _link(item: Mapping) -> str:
    """Prefer the article's original link, falling back to the Naver link."""
    for key in ("originallink", "link"):
        value = item.get(key)
        if type(value) is str and value.strip():
            return value
    return ""


if __name__ == "__main__":
    raise SystemExit("naver_news is a library module; import naver_news_provider_or_none")
