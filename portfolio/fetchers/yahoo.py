"""Fetch US market data (tickers, S&P 500, FX rate) via yfinance."""

from datetime import datetime, timedelta

import pandas as pd
import requests
import yfinance as yf

# BCB series 432 = US Federal Funds rate (% p.a.) — used as money market yield proxy
_BCB_FED_FUNDS_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.432/dados"


def fetch_ticker_monthly(ticker: str, months: int = 13) -> pd.DataFrame:
    """
    Fetch monthly closing prices and returns for a ticker.
    Returns DataFrame with: period (YYYY-MM), {ticker}_close, {ticker}_return_%.
    """
    start = datetime.today() - timedelta(days=months * 32)
    t = yf.Ticker(ticker)
    hist = t.history(start=start.strftime("%Y-%m-%d"), interval="1mo")

    if hist.empty:
        return pd.DataFrame(columns=["period", f"{ticker}_close", f"{ticker}_return_%"])

    close = hist["Close"].copy()
    # Strip timezone before period conversion to silence pandas warning
    if getattr(close.index, "tz", None) is not None:
        close.index = close.index.tz_localize(None)
    df = pd.DataFrame({
        "period": close.index.to_period("M").astype(str),
        f"{ticker}_close": close.values,
        f"{ticker}_return_%": close.pct_change().values * 100,
    })
    return df.dropna(subset=[f"{ticker}_close"]).reset_index(drop=True)


def fetch_current_price(ticker: str) -> float:
    """Return the latest closing price for a ticker."""
    hist = yf.Ticker(ticker).history(period="5d")
    if hist.empty:
        return 0.0
    return float(hist["Close"].iloc[-1])


def fetch_fx_rate(pair: str = "USDBRL=X") -> float:
    """Return the current FX rate for the given pair (default: USD/BRL)."""
    return fetch_current_price(pair)


def fetch_sp500_monthly(months: int = 13) -> pd.DataFrame:
    """Fetch S&P 500 monthly returns. Wrapper around fetch_ticker_monthly."""
    return fetch_ticker_monthly("^GSPC", months)


def _fetch_fed_funds_rate() -> tuple[float, str]:
    """
    Fetch the latest US Federal Funds rate from BCB open data (series 432).
    Returns (rate_pct_annual, source_label). Used as a last-resort proxy for
    money market fund yield when yfinance has no dividend data.
    """
    try:
        today = datetime.today()
        params = {
            "formato": "json",
            "dataInicial": (today.replace(year=today.year - 1)).strftime("%d/%m/%Y"),
            "dataFinal": today.strftime("%d/%m/%Y"),
        }
        r = requests.get(_BCB_FED_FUNDS_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data:
            rate = float(data[-1]["valor"])
            date_str = data[-1]["data"]
            return rate, f"Fed Funds rate {date_str} (BCB series 432, proxy)"
    except Exception as e:
        print(f"  Warning: BCB Fed Funds fallback failed: {e}")
    return 0.0, ""


def fetch_money_market_yield(ticker: str, shares: float) -> dict:
    """
    Fetch yield data for a money market fund with a fixed $1.00 NAV.

    Source priority (never returns 0% if any source succeeds):
      1. yfinance dividend history  → trailing 12-month yield
      2. yfinance info.get('yield') → fund-reported annualized yield
      3. BCB series 432 (US Fed Funds rate) → proxy when no fund data available

    Returns dict keys:
      annualized_yield_pct, monthly_yield_usd, ytd_yield_usd,
      current_value_usd, yield_source (str label for display).
    """
    nav = 1.0
    today = datetime.today()
    result = {
        "ticker": ticker,
        "nav": nav,
        "current_value_usd": shares * nav,
        "annualized_yield_pct": 0.0,
        "monthly_yield_usd": 0.0,
        "ytd_yield_usd": 0.0,
        "yield_source": "unavailable",
    }

    # ── Source 1: yfinance dividend history ───────────────────────────────────
    try:
        t = yf.Ticker(ticker)
        divs = t.dividends

        if divs is not None and not divs.empty:
            if divs.index.tz is not None:
                divs.index = divs.index.tz_convert(None)

            start_12m = pd.Timestamp(today - timedelta(days=365))
            divs_12m = divs[divs.index >= start_12m]
            ann_yield = float(divs_12m.sum()) * 100  # per $1 NAV → %

            if ann_yield > 0:
                last_month_period = pd.Period(today, "M") - 1
                divs_lm = divs[divs.index.to_period("M") == last_month_period]
                monthly_yield_usd = float(divs_lm.sum()) * shares

                divs_ytd = divs[divs.index.year == today.year]
                ytd_yield_usd = float(divs_ytd.sum()) * shares

                result.update({
                    "annualized_yield_pct": ann_yield,
                    "monthly_yield_usd": monthly_yield_usd,
                    "ytd_yield_usd": ytd_yield_usd,
                    "yield_source": "yfinance dividends (trailing 12m)",
                })
                return result

        # ── Source 2: yfinance info yield fields ──────────────────────────────
        info = t.info or {}
        ann_yield_decimal = (
            info.get("yield")
            or info.get("trailingAnnualDividendYield")
            or info.get("dividendYield")
            or 0.0
        )
        if ann_yield_decimal and float(ann_yield_decimal) > 0:
            ann_yield = float(ann_yield_decimal) * 100
            result.update({
                "annualized_yield_pct": ann_yield,
                "monthly_yield_usd": ann_yield / 100 / 12 * (shares * nav),
                "ytd_yield_usd": 0.0,
                "yield_source": "yfinance info.yield",
            })
            return result

    except Exception as e:
        print(f"  Warning: yfinance yield fetch failed for {ticker}: {e}")

    # ── Source 3: BCB Fed Funds rate proxy ────────────────────────────────────
    fed_rate, fed_label = _fetch_fed_funds_rate()
    if fed_rate > 0:
        result.update({
            "annualized_yield_pct": fed_rate,
            "monthly_yield_usd": fed_rate / 100 / 12 * (shares * nav),
            "ytd_yield_usd": 0.0,
            "yield_source": fed_label,
        })
    else:
        result["yield_source"] = "no data available"

    return result
