# KIS OpenAPI domestic daily-price acquisition

## Official API evidence (verified 2026-07-18)

KIS' official sample repository documents the domestic-stock **period price
(day/week/month/year)** API as:

- path: `/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice`
- transaction ID: `FHKST03010100`
- daily request fields: `FID_COND_MRKT_DIV_CODE=J`, `FID_INPUT_ISCD`,
  `FID_INPUT_DATE_1`, `FID_INPUT_DATE_2`, `FID_PERIOD_DIV_CODE=D`, and
  `FID_ORG_ADJ_PRC=0` (adjusted price).

Source (KIS-maintained official sample):
<https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_itemchartprice/inquire_daily_itemchartprice.py>

That sample explicitly says each real/demo call returns **at most 100 records**.
The KIS repository's supplied Postman collection further specifies that to get
more data, request again with `FID_INPUT_DATE_2` set to one day before the
oldest `output2` date. The adapter implements exactly that backward cursor
scheme, normalizes KIS newest-first `output2` records to ascending `DailyBar`s,
and deduplicates overlapping dates.

## Long-history conclusion

The official material establishes the per-request 100-record limit and the
pagination/re-query method. It does **not** state a current, guaranteed global
historical-retention horizon for this domestic daily endpoint. Therefore this
project must not claim that KIS guarantees 3--5 years. The adapter can acquire
that span in approximately 8--13 requests per symbol if KIS continues returning
older 100-record chunks; callers must validate that the returned earliest date
reaches their requested start date before treating the snapshot as complete.

A credentials-safe, read-only smoke request on 2026-07-18 retrieved 21 Samsung
Electronics (`005930`) bars for 2026-06-01 through 2026-06-30. This validates
the endpoint and current credentials for a bounded period; it is not evidence
of a 3--5-year retention guarantee.

## Snapshot and metadata limits

`build_snapshot_from_kis` accepts only an explicit list of assets and writes a
standard immutable local snapshot. Its metadata source must include `KIS`, and
its `trading_value_is_close_times_volume_proxy` flag should be `False`: KIS
maps `acml_tr_pbmn` directly into `DailyBar.trading_value`.

The domestic-stock endpoint does not yield the market-index series used to make
the backtest calendar, so `market_history` is explicit input to the builder.
KIS price data is **not** evidence of point-in-time ETF/stock classification,
listing membership, management designation, or trading-halt history. Continue
to use the KRX metadata snapshot for those controls.
