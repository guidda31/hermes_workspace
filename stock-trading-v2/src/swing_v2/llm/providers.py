"""Wire real DART / news evidence providers from injected keys + transports.

Assembling a provider makes no request; only invoking the returned closure does (via
the injected or default HTTP transport). The ``*_from_env`` wrappers read the API key
from the environment — raising if absent — so a Hermes routine can enable real
disclosures once ``OPENDART_API_KEY`` is provisioned, exactly like KIS credentials.
The key is never logged. A concrete news source (e.g. Naver News) plus its key must be
chosen before news can be enabled; ``news_provider`` accepts any injected ``fetch``.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from typing import Callable, Optional

from .brief import EvidenceProvider
from .dart_corp_codes import load_corp_codes_from_zip
from .dart_disclosures import make_dart_disclosure_provider
from .dart_transport import dart_http_get, fetch_corp_code_zip
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
