import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import requests

from swing_v2.kis import KisClient, KisCredentials


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.raise_for_status_called = False

    def raise_for_status(self) -> None:
        self.raise_for_status_called = True

    def json(self) -> dict[str, object]:
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, str]]] = []

    def post(self, url: str, *, json: dict[str, str]) -> FakeResponse:
        self.calls.append((url, json))
        return self.response


class HttpErrorResponse(FakeResponse):
    def raise_for_status(self) -> None:
        raise requests.HTTPError("unauthorized")


class KisClientTests(unittest.TestCase):
    def test_get_access_token_caches_a_successful_token_and_reuses_it(self) -> None:
        response = FakeResponse({"access_token": "test-access-token", "expires_in": 3600})
        session = FakeSession(response)
        client = KisClient(
            credentials=KisCredentials(app_key="test-app-key", app_secret="test-app-secret"),
            session=session,
        )
        now = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "kis_token.json"
            first = client.get_access_token(
                cache_path=cache_path, now=lambda: now,
            )
            second = client.get_access_token(
                cache_path=cache_path, now=lambda: now + timedelta(minutes=1),
            )

            self.assertEqual(first, "test-access-token")
            self.assertEqual(second, "test-access-token")
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(stat.S_IMODE(cache_path.stat().st_mode), 0o600)
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["access_token"], "test-access-token")
            self.assertIn("issued_at", payload)
            self.assertIn("expires_at", payload)

    def test_get_access_token_rejects_an_insecure_cache_before_reissuing(self) -> None:
        response = FakeResponse({"access_token": "fresh-token", "expires_in": 3600})
        session = FakeSession(response)
        client = KisClient(
            credentials=KisCredentials(app_key="test-app-key", app_secret="test-app-secret"),
            session=session,
        )
        now = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "kis_token.json"
            cache_path.write_text(json.dumps({
                "access_token": "unsafe-token", "issued_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=1)).isoformat(),
            }), encoding="utf-8")
            os.chmod(cache_path, 0o644)

            access_token = client.get_access_token(cache_path=cache_path, now=lambda: now)

            self.assertEqual(access_token, "fresh-token")
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(stat.S_IMODE(cache_path.stat().st_mode), 0o600)

    def test_get_access_token_reissues_when_cache_is_inside_early_expiry_skew(self) -> None:
        response = FakeResponse({"access_token": "fresh-token", "expires_in": 3600})
        session = FakeSession(response)
        client = KisClient(
            credentials=KisCredentials(app_key="test-app-key", app_secret="test-app-secret"),
            session=session,
        )
        now = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "kis_token.json"
            cache_path.write_text(json.dumps({
                "access_token": "nearly-expired-token", "issued_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=4)).isoformat(),
            }), encoding="utf-8")
            os.chmod(cache_path, 0o600)

            access_token = client.get_access_token(cache_path=cache_path, now=lambda: now)

            self.assertEqual(access_token, "fresh-token")
            self.assertEqual(len(session.calls), 1)

    def test_get_access_token_reissues_when_cache_json_is_malformed(self) -> None:
        response = FakeResponse({"access_token": "fresh-token", "expires_in": 3600})
        session = FakeSession(response)
        client = KisClient(
            credentials=KisCredentials(app_key="test-app-key", app_secret="test-app-secret"),
            session=session,
        )

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "kis_token.json"
            cache_path.write_text("not JSON", encoding="utf-8")
            os.chmod(cache_path, 0o600)

            access_token = client.get_access_token(cache_path=cache_path)

            self.assertEqual(access_token, "fresh-token")
            self.assertEqual(len(session.calls), 1)

    def test_get_access_token_does_not_cache_a_failed_token_response(self) -> None:
        session = FakeSession(HttpErrorResponse({}))
        client = KisClient(
            credentials=KisCredentials(app_key="test-app-key", app_secret="test-app-secret"),
            session=session,
        )

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "kis_token.json"
            with self.assertRaises(requests.HTTPError):
                client.get_access_token(cache_path=cache_path)

            self.assertFalse(cache_path.exists())

    def test_inquire_balance_gets_expected_headers_and_query_parameters(self) -> None:
        response = MagicMock()
        response.json.return_value = {"output1": []}
        session = MagicMock()
        session.get.return_value = response
        client = KisClient(
            credentials=KisCredentials(app_key="test-app-key", app_secret="test-app-secret"),
            session=session,
        )

        balance = client.inquire_balance("test-access-token", "12345678-01")

        self.assertEqual(balance, {"output1": []})
        session.get.assert_called_once_with(
            "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/trading/inquire-balance",
            headers={
                "authorization": "Bearer test-access-token",
                "appkey": "test-app-key",
                "appsecret": "test-app-secret",
                "tr_id": "TTTC8434R",
                "custtype": "P",
            },
            params={
                "CANO": "12345678",
                "ACNT_PRDT_CD": "01",
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "N",
                "INQR_DVSN": "01",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
            },
        )
        response.raise_for_status.assert_called_once_with()

    def test_inquire_balance_rejects_invalid_account_number(self) -> None:
        session = MagicMock()
        client = KisClient(
            credentials=KisCredentials(app_key="test-app-key", app_secret="test-app-secret"),
            session=session,
        )

        with self.assertRaises(ValueError):
            client.inquire_balance("test-access-token", "12345678_01")

        session.get.assert_not_called()

    def test_inquire_balance_propagates_http_error(self) -> None:
        response = MagicMock()
        response.raise_for_status.side_effect = requests.HTTPError("forbidden")
        session = MagicMock()
        session.get.return_value = response
        client = KisClient(
            credentials=KisCredentials(app_key="test-app-key", app_secret="test-app-secret"),
            session=session,
        )

        with self.assertRaises(requests.HTTPError):
            client.inquire_balance("test-access-token", "12345678-01")

    def test_inquire_balance_rejects_non_object_json_response(self) -> None:
        response = MagicMock()
        response.json.return_value = []
        session = MagicMock()
        session.get.return_value = response
        client = KisClient(
            credentials=KisCredentials(app_key="test-app-key", app_secret="test-app-secret"),
            session=session,
        )

        with self.assertRaises(ValueError):
            client.inquire_balance("test-access-token", "12345678-01")

    def test_issue_access_token_posts_credentials_and_returns_access_token(self) -> None:
        response = FakeResponse({"access_token": "test-access-token"})
        session = FakeSession(response)
        client = KisClient(
            credentials=KisCredentials(app_key="test-app-key", app_secret="test-app-secret"),
            session=session,
        )

        access_token = client.issue_access_token()

        self.assertEqual(access_token, "test-access-token")
        self.assertEqual(
            session.calls,
            [
                (
                    "https://openapi.koreainvestment.com:9443/oauth2/tokenP",
                    {
                        "grant_type": "client_credentials",
                        "appkey": "test-app-key",
                        "appsecret": "test-app-secret",
                    },
                )
            ],
        )
        self.assertTrue(response.raise_for_status_called)

    def test_issue_access_token_propagates_http_error(self) -> None:
        session = FakeSession(HttpErrorResponse({}))
        client = KisClient(
            credentials=KisCredentials(app_key="test-app-key", app_secret="test-app-secret"),
            session=session,
        )

        with self.assertRaises(requests.HTTPError):
            client.issue_access_token()

    def test_issue_access_token_rejects_response_without_access_token(self) -> None:
        session = FakeSession(FakeResponse({"token_type": "Bearer"}))
        client = KisClient(
            credentials=KisCredentials(app_key="test-app-key", app_secret="test-app-secret"),
            session=session,
        )

        with self.assertRaises(ValueError):
            client.issue_access_token()


if __name__ == "__main__":
    unittest.main()
