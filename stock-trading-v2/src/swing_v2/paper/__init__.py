"""Paper trading: simulated execution with NO real money, broker, or network.

This package exercises the full intent -> fill -> position -> cash -> reconciliation
lifecycle against simulated next-open fills, using the same cost model and gap-up / IOC
discipline as the backtest. It is deliberately isolated from ``swing_v2.live`` (which
holds the real, disabled-by-default order path); nothing here submits an order.
"""
