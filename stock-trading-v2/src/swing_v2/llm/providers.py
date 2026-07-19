"""Wire real DART / news evidence providers from injected keys + transports.

Assembling a provider makes no request; only invoking the returned closure does (via
the injected or default HTTP transport). The ``*_from_env`` wrappers read the API key
from the environment — raising if absent — so a Hermes routine can enable real
disclosures once ``OPENDART_API_KEY`` is provisioned, exactly like KIS credentials.
The key is never logged. A concrete news source (e.g. Naver News) plus its key must be
chosen before news can be enabled; ``news_provider`` accepts any injected ``fetch``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
from typing import Callable, Optional

from .brief import EvidenceProvider
from .dart_corp_codes import load_corp_codes_from_zip
from .dart_disclosures import make_dart_disclosure_provider
from .dart_transport import dart_http_get, fetch_corp_code_zip
from .naver_news import naver_news_provider_or_none
from .news_provider import make_news_provider

_DART_ENV_KEYS = ("OPENDART_API_KEY", "DART_API_KEY")


def dart_provider(
    *,
    api_key: str,
    corp_code_by_symbol: Mapping[str, str],
    http_get: Optional[Callable[[str, Mapping[str, str]], Mapping]] = None,
    window_days: int = 60,
) -> EvidenceProvider:
    """Build a DART disclosure provider over an injected (or default) HTTP transport."""
    return make_dart_disclosure_provider(
        http_get=http_get if http_get is not None else dart_http_get,
        api_key=api_key,
        corp_code_by_symbol=corp_code_by_symbol,
        window_days=window_days,
    )


def fetch_corp_codes(*, api_key: str, url_opener=None) -> dict[str, str]:
    """Fetch DART's corpCode.xml zip and return the {symbol: corp_code} map."""
    zip_bytes = (
        fetch_corp_code_zip(api_key)
        if url_opener is None
        else fetch_corp_code_zip(api_key, url_opener=url_opener)
    )
    return load_corp_codes_from_zip(zip_bytes)


def _require_dart_key(env: Mapping[str, str]) -> str:
    for name in _DART_ENV_KEYS:
        value = env.get(name)
        if value:
            return value
    raise ValueError(f"no DART API key in the environment (expected one of {list(_DART_ENV_KEYS)})")


def dart_provider_from_env(
    *,
    corp_code_by_symbol: Optional[Mapping[str, str]] = None,
    env: Optional[Mapping[str, str]] = None,
    window_days: int = 60,
) -> EvidenceProvider:
    """Build a DART provider using the API key from the environment.

    If ``corp_code_by_symbol`` is not supplied it is fetched from DART (a network
    call); pass it explicitly to build the provider without any request.
    """
    environment = os.environ if env is None else env
    api_key = _require_dart_key(environment)
    codes = corp_code_by_symbol if corp_code_by_symbol is not None else fetch_corp_codes(api_key=api_key)
    return dart_provider(api_key=api_key, corp_code_by_symbol=codes, window_days=window_days)


def news_provider(*, fetch, window_days: int = 14) -> EvidenceProvider:
    """Wrap an injected news ``fetch`` (source-agnostic) into an EvidenceProvider."""
    return make_news_provider(fetch=fetch, window_days=window_days)


def news_provider_or_none(
    *,
    symbols: Sequence[str],
    name_cache_path=None,
    name_by_symbol: Optional[Mapping[str, str]] = None,
    env: Optional[Mapping[str, str]] = None,
    window_days: int = 14,
    http_get: Optional[Callable[[str, Mapping[str, str]], Mapping]] = None,
) -> Optional[EvidenceProvider]:
    """Build a Naver-News provider from the environment, or None if unavailable.

    Graceful: with no symbol->Korean-name map (param or cache file) or no NAVER keys,
    returns None so the brief simply carries no news and nothing breaks.
    """
    names = dict(name_by_symbol) if name_by_symbol is not None else None
    if names is None and name_cache_path is not None:
        path = Path(name_cache_path)
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                names = {str(k): str(v) for k, v in loaded.items()}
    if not names:
        return None
    return naver_news_provider_or_none(
        symbols=symbols, name_by_symbol=names, env=env, window_days=window_days, http_get=http_get,
    )


def _optional_dart_key(env: Mapping[str, str]) -> Optional[str]:
    for name in _DART_ENV_KEYS:
        value = env.get(name)
        if value:
            return value
    return None


def _corp_codes_for(
    symbols: Sequence[str], *, api_key: str, cache_path, url_opener
) -> dict[str, str]:
    """Load the {symbol: corp_code} map from a local cache, else fetch and cache it."""
    codes: Optional[dict] = None
    path = Path(cache_path) if cache_path is not None else None
    if path is not None and path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            codes = {str(k): str(v) for k, v in loaded.items()}
    if codes is None:
        codes = fetch_corp_codes(api_key=api_key, url_opener=url_opener)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(codes, ensure_ascii=False), encoding="utf-8")
    return {symbol: codes[symbol] for symbol in symbols if symbol in codes}


def dart_disclosure_provider_or_none(
    *,
    symbols: Sequence[str],
    env: Optional[Mapping[str, str]] = None,
    cache_path=None,
    window_days: int = 60,
    http_get: Optional[Callable[[str, Mapping[str, str]], Mapping]] = None,
    url_opener=None,
) -> Optional[EvidenceProvider]:
    """Build a DART disclosure provider from the environment, or None if no key.

    Graceful by design: with no OPENDART key the forward brief simply stays price-only.
    Corp codes are cached at ``cache_path`` so they are not re-fetched every run.
    """
    environment = os.environ if env is None else env
    api_key = _optional_dart_key(environment)
    if api_key is None:
        return None
    corp_codes = _corp_codes_for(symbols, api_key=api_key, cache_path=cache_path, url_opener=url_opener)
    if not corp_codes:
        return None
    return dart_provider(
        api_key=api_key, corp_code_by_symbol=corp_codes, http_get=http_get, window_days=window_days,
    )
