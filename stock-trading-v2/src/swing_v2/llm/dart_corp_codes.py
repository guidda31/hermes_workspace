"""Loader for DART's official ``CORPCODE.xml`` corporate-code dump.

DART's ``opendart.fss.or.kr/api/corpCode.xml`` endpoint returns a ZIP whose single
``CORPCODE.xml`` entry has a ``<result>`` root of many ``<list>`` rows, each with an
8-digit ``<corp_code>``, a ``<corp_name>``, a 6-digit ``<stock_code>`` for LISTED
companies (blank/space for unlisted), and a ``<modify_date>``. This module turns the
listed rows into the ``{stock_code: corp_code}`` mapping consumed by
``make_dart_disclosure_provider``. It reads no ``.env`` and does no network I/O: the
caller injects raw ZIP or XML bytes. Unlisted rows are the vast majority and are
silently skipped; every other anomaly fails closed with ``ValueError``.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree

# KRX short codes are 6 uppercase-alphanumeric chars — usually digits, but some real
# listed securities carry a letter (e.g. "0068Y0"). Matches krx_xlsx_normalizer.
_KRX_SHORT_CODE = re.compile(r"[0-9A-Z]{6}")


def parse_corp_code_xml(xml_bytes: bytes) -> dict[str, str]:
    """Parse ``CORPCODE.xml`` bytes into ``{stock_code: corp_code}`` for listed rows."""
    if type(xml_bytes) is not bytes:
        raise ValueError("xml_bytes must be plain bytes")
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        raise ValueError("CORPCODE.xml is malformed") from exc
    if root.tag != "result":
        raise ValueError(f"CORPCODE.xml root must be <result>, got <{root.tag}>")
    mapping: dict[str, str] = {}
    for row in root.findall("list"):
        stock_code = _text(row, "stock_code")
        if not stock_code:
            continue  # Unlisted company: no stock_code, skip (the vast majority).
        if not _KRX_SHORT_CODE.fullmatch(stock_code):
            raise ValueError(f"listed stock_code must be 6 alphanumeric chars, got {stock_code!r}")
        corp_code = _text(row, "corp_code")
        if len(corp_code) != 8 or not corp_code.isdigit():
            raise ValueError(f"corp_code must be 8 digits, got {corp_code!r}")
        existing = mapping.get(stock_code)
        if existing is not None and existing != corp_code:
            raise ValueError(
                f"conflicting corp_code for stock_code {stock_code}: "
                f"{existing} vs {corp_code}"
            )
        mapping[stock_code] = corp_code
    return mapping


def load_corp_codes_from_zip(zip_bytes: bytes) -> dict[str, str]:
    """Unzip a DART corpCode ZIP, read its single ``.xml`` entry, and parse it."""
    if type(zip_bytes) is not bytes:
        raise ValueError("zip_bytes must be plain bytes")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            xml_names = [name for name in archive.namelist() if name.lower().endswith(".xml")]
            if not xml_names:
                raise ValueError("corpCode ZIP has no .xml entry")
            if len(xml_names) != 1:
                raise ValueError(f"corpCode ZIP must have exactly one .xml entry, got {len(xml_names)}")
            xml_bytes = archive.read(xml_names[0])
    except zipfile.BadZipFile as exc:
        raise ValueError("corpCode ZIP is corrupt") from exc
    return parse_corp_code_xml(xml_bytes)


def _text(row: ElementTree.Element, field: str) -> str:
    """Return the stripped text of ``row``'s ``field`` child, or empty if absent."""
    child = row.find(field)
    if child is None or child.text is None:
        return ""
    return child.text.strip()
