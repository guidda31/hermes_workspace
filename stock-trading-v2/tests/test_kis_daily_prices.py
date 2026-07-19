"""TDD coverage for the read-only KIS domestic daily-price adapter."""

from datetime import date
from decimal import Decimal
import unittest
from unittest.mock import MagicMock

from swing_v2.backtest_data import SnapshotMetadata, build_snapshot_from_kis
from swing_v2.contracts import DailyBar
from swing_v2.kis import KisClient, KisCredentials, PageRequestBudget


D = Decimal


class KisDailyPriceClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.credentials = KisCredentials(app_key="test-app-key", app_secret="test-app-secret")

    def test_load_daily_bars_sends_official_read_only_request_and_normalizes_descending_output(self) -> None:
        response = MagicMock()
        response.json.return_value = {
            "rt_cd": "0",
            "output2": [
                {"stck_bsop_date": "20240103", "stck_oprc": "71000", "stck_hgpr": "72000", "stck_lwpr": "70000", "stck_clpr": "71500", "acml_vol": "12345", "acml_tr_pbmn": "876543210"},
                {"stck_bsop_date": "20240102", "stck_oprc": "70000", "stck_hgpr": "71000", "stck_lwpr": "69000", "stck_clpr": "70500", "acml_vol": "10000", "acml_tr_pbmn": "700000000"},
            ],
        }
        session = MagicMock()
        session.get.return_value = response
        client = KisClient(credentials=self.credentials, session=session)

        bars = client.load_domestic_daily_bars(
            access_token="test-access-token", symbol="005930", asset_type="STOCK",
            start=date(2024, 1, 2), end=date(2024, 1, 3),
        )

        self.assertEqual([bar.trade_date for bar in bars], [date(2024, 1, 2), date(2024, 1, 3)])
        self.assertEqual(bars[0], DailyBar(date(2024, 1, 2), "005930", "STOCK", D("70000"), D("71000"), D("69000"), D("70500"), 10000, D("700000000"), True))
        session.get.assert_called_once_with(
            "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            headers={"authorization": "Bearer test-access-token", "appkey": "test-app-key", "appsecret": "test-app-secret", "tr_id": "FHKST03010100"},
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "005930", "FID_INPUT_DATE_1": "20240102", "FID_INPUT_DATE_2": "20240103", "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"},
        )
        response.raise_for_status.assert_called_once_with()

    def test_load_daily_bars_pages_backwards_at_the_official_100_record_limit_and_deduplicates(self) -> None:
        newest_page = [
            {"stck_bsop_date": "20240103", "stck_oprc": "11", "stck_hgpr": "11", "stck_lwpr": "11", "stck_clpr": "11", "acml_vol": "1", "acml_tr_pbmn": "11"},
            {"stck_bsop_date": "20240102", "stck_oprc": "10", "stck_hgpr": "10", "stck_lwpr": "10", "stck_clpr": "10", "acml_vol": "1", "acml_tr_pbmn": "10"},
        ]
        newest_page.extend({"stck_bsop_date": "20240102", "stck_oprc": "10", "stck_hgpr": "10", "stck_lwpr": "10", "stck_clpr": "10", "acml_vol": "1", "acml_tr_pbmn": "10"} for _ in range(98))
        older_page = [{"stck_bsop_date": "20240101", "stck_oprc": "9", "stck_hgpr": "9", "stck_lwpr": "9", "stck_clpr": "9", "acml_vol": "1", "acml_tr_pbmn": "9"}]
        responses = []
        for payload in (newest_page, older_page):
            response = MagicMock()
            response.json.return_value = {"rt_cd": "0", "output2": payload}
            responses.append(response)
        session = MagicMock()
        session.get.side_effect = responses
        client = KisClient(credentials=self.credentials, session=session)

        bars = client.load_domestic_daily_bars("test-access-token", "005930", "STOCK", date(2024, 1, 1), date(2024, 1, 3))

        self.assertEqual([bar.trade_date for bar in bars], [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)])
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.get.call_args_list[1].kwargs["params"]["FID_INPUT_DATE_2"], "20240101")

    def test_page_request_budget_applies_delay_to_every_paginated_http_get_and_stops_at_cap(self) -> None:
        page = [{"stck_bsop_date": "20240102", "stck_oprc": "10", "stck_hgpr": "10", "stck_lwpr": "10", "stck_clpr": "10", "acml_vol": "1", "acml_tr_pbmn": "10"}] * 100
        response = MagicMock()
        response.json.return_value = {"rt_cd": "0", "output2": page}
        session = MagicMock()
        session.get.return_value = response
        slept: list[float] = []
        client = KisClient(credentials=self.credentials, session=session)

        with self.assertRaisesRegex(RuntimeError, "cap"):
            client.load_domestic_daily_bars("test-access-token", "005930", "STOCK", date(2024, 1, 1), date(2024, 1, 3), page_request_limiter=PageRequestBudget(max_requests=1, delay_seconds=0.25, sleep=slept.append))

        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(slept, [])

    def test_page_request_budget_delays_between_each_paginated_http_get(self) -> None:
        newest_page = [{"stck_bsop_date": "20240102", "stck_oprc": "10", "stck_hgpr": "10", "stck_lwpr": "10", "stck_clpr": "10", "acml_vol": "1", "acml_tr_pbmn": "10"}] * 100
        older_page = [{"stck_bsop_date": "20240101", "stck_oprc": "10", "stck_hgpr": "10", "stck_lwpr": "10", "stck_clpr": "10", "acml_vol": "1", "acml_tr_pbmn": "10"}]
        session = MagicMock()
        responses = []
        for page in (newest_page, older_page):
            response = MagicMock()
            response.json.return_value = {"rt_cd": "0", "output2": page}
            responses.append(response)
        session.get.side_effect = responses
        slept: list[float] = []
        client = KisClient(credentials=self.credentials, session=session)

        client.load_domestic_daily_bars("test-access-token", "005930", "STOCK", date(2024, 1, 1), date(2024, 1, 3), page_request_limiter=PageRequestBudget(max_requests=2, delay_seconds=0.25, sleep=slept.append))

        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(slept, [0.25])

    def test_load_daily_bars_rejects_invalid_date_bounds_before_network_access(self) -> None:
        session = MagicMock()
        client = KisClient(credentials=self.credentials, session=session)

        with self.assertRaisesRegex(ValueError, "start"):
            client.load_domestic_daily_bars("test-access-token", "005930", "STOCK", date(2024, 1, 3), date(2024, 1, 2))

        session.get.assert_not_called()

    def test_load_daily_bars_rejects_kis_error_payload(self) -> None:
        response = MagicMock()
        response.json.return_value = {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "bad request"}
        session = MagicMock()
        session.get.return_value = response
        client = KisClient(credentials=self.credentials, session=session)

        with self.assertRaisesRegex(ValueError, "EGW00123"):
            client.load_domestic_daily_bars("test-access-token", "005930", "STOCK", date(2024, 1, 2), date(2024, 1, 3))


class KisSnapshotBuilderTests(unittest.TestCase):
    def test_snapshot_builder_binds_kis_bars_to_explicit_assets_and_no_lookahead_calendar(self) -> None:
        class Adapter:
            def load_domestic_daily_bars(self, access_token: str, symbol: str, asset_type: str, start: date, end: date) -> tuple[DailyBar, ...]:
                self.call = (access_token, symbol, asset_type, start, end)
                return (
                    DailyBar(date(2024, 1, 2), symbol, asset_type, D("10"), D("10"), D("10"), D("10"), 1, D("10"), True),
                    DailyBar(date(2024, 1, 4), symbol, asset_type, D("12"), D("12"), D("12"), D("12"), 1, D("12"), True),
                )
        adapter = Adapter()
        market_history = (
            DailyBar(date(2024, 1, 2), "KOSPI", "INDEX", D("2500"), D("2500"), D("2500"), D("2500"), 0, D("0"), True),
            DailyBar(date(2024, 1, 3), "KOSPI", "INDEX", D("2510"), D("2510"), D("2510"), D("2510"), 0, D("0"), True),
            DailyBar(date(2024, 1, 4), "KOSPI", "INDEX", D("2520"), D("2520"), D("2520"), D("2520"), 0, D("0"), True),
        )

        snapshot = build_snapshot_from_kis(
            adapter=adapter, access_token="test-token", symbols=("005930",), asset_types={"005930": "STOCK"},
            market_symbol="KOSPI", market_history=market_history, start=date(2024, 1, 2), end=date(2024, 1, 4),
            metadata=SnapshotMetadata("KIS OpenAPI domestic daily price (adjusted)", "2024-02-01T00:00:00+00:00", "2024-01-04", False),
        )

        self.assertEqual(adapter.call, ("test-token", "005930", "STOCK", date(2024, 1, 2), date(2024, 1, 4)))
        self.assertEqual(snapshot.metadata.source, "KIS OpenAPI domestic daily price (adjusted)")
        self.assertEqual(snapshot.trade_calendar, (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)))
        self.assertEqual(snapshot.histories["005930"][-1].trade_date, date(2024, 1, 4))


if __name__ == "__main__":
    unittest.main()
