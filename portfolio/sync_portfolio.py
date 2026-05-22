"""
Portfolio sync — GitHub Actions entrypoint.

Reads PORTFOLIO_CONFIG env var (JSON string), fetches live data from CVM,
BCB, and Yahoo Finance, then writes data/portfolio.json.

Usage (local):
    PORTFOLIO_CONFIG=$(cat config.json) python portfolio/sync_portfolio.py

Usage (GitHub Actions):
    Reads PORTFOLIO_CONFIG secret automatically.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Allow running from repo root: python portfolio/sync_portfolio.py
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from fetchers.bcb import calc_cdi_monthly, fetch_cdi_daily
from fetchers.cvm import calc_monthly_returns, fetch_fund_data
from fetchers.fixed_income import calc_asset_value
from fetchers.yahoo import (
    fetch_current_price,
    fetch_fx_rate,
    fetch_money_market_yield,
    fetch_sp500_monthly,
    fetch_ticker_monthly,
)


# ── Helpers (inlined from main.py) ───────────────────────────────────────────

def compound_return(series: pd.Series) -> float:
    valid = series.dropna()
    if valid.empty:
        return 0.0
    return float(((1 + valid / 100).prod() - 1) * 100)


def annual_return(monthly_df: pd.DataFrame, col: str = "return_%") -> float:
    if monthly_df.empty or col not in monthly_df.columns:
        return 0.0
    return compound_return(monthly_df.dropna(subset=[col]).tail(12)[col])


def last_monthly_return(monthly_df: pd.DataFrame, col: str = "return_%") -> float:
    if monthly_df.empty or col not in monthly_df.columns:
        return 0.0
    valid = monthly_df.dropna(subset=[col])
    return float(valid.iloc[-1][col]) if not valid.empty else 0.0


# ── Data pipeline ─────────────────────────────────────────────────────────────

def build_portfolio_data(config: dict) -> dict:
    months = config.get("months", 13)
    today = datetime.today()
    start_date = (today.replace(day=1) - timedelta(days=(months - 1) * 32)).replace(day=1)

    # CDI
    fi_starts = [pd.to_datetime(a["start_date"]) for a in config.get("fixed_income", [])]
    cdi_start = min([start_date] + fi_starts) if fi_starts else start_date
    print("Fetching CDI data from BCB...")
    cdi_daily = fetch_cdi_daily(cdi_start, today)
    cdi_monthly = calc_cdi_monthly(cdi_daily)

    # BR funds
    print("Fetching BR fund data from CVM...")
    br_fund_data: dict[str, pd.DataFrame] = {}
    br_fund_monthly: dict[str, pd.DataFrame] = {}
    for fund in config.get("br_funds", []):
        cnpj = fund["cnpj"]
        print(f"  {fund['name']} ({cnpj})")
        data = fetch_fund_data(cnpj, months)
        br_fund_data[cnpj] = data
        br_fund_monthly[cnpj] = calc_monthly_returns(data)

    # Fixed income
    print("Calculating fixed income values...")
    fi_values = []
    for asset in config.get("fixed_income", []):
        val = calc_asset_value(asset, cdi_daily)
        fi_values.append(val)
        print(f"  {val['name']}: R$ {val['current_value']:,.2f} ({val['total_return_%']:+.2f}%)")

    # FX + US tickers
    print("Fetching FX rate and US tickers...")
    fx_rate = fetch_fx_rate()
    print(f"  USDBRL = {fx_rate:.4f}")

    us_ticker_data: dict[str, dict] = {}
    for tc in config.get("us_tickers", []):
        t = tc["ticker"]
        print(f"  {t}...")
        if tc.get("is_money_market"):
            us_ticker_data[t] = {
                "is_money_market": True,
                "current_price_usd": 1.0,
                "monthly": pd.DataFrame(),
                "mm_yield": fetch_money_market_yield(t, tc["shares"]),
            }
        else:
            us_ticker_data[t] = {
                "is_money_market": False,
                "current_price_usd": fetch_current_price(t),
                "monthly": fetch_ticker_monthly(t, months),
            }

    print("Fetching S&P 500 and USDBRL monthly data...")
    sp500_monthly = fetch_sp500_monthly(months)
    usdbrl_monthly = fetch_ticker_monthly("USDBRL=X", months)

    # ── Build asset rows ──────────────────────────────────────────────────────

    asset_rows = []

    for fund in config.get("br_funds", []):
        cnpj = fund["cnpj"]
        daily = br_fund_data.get(cnpj, pd.DataFrame())
        monthly = br_fund_monthly.get(cnpj, pd.DataFrame())
        quota = float(daily["VL_QUOTA"].iloc[-1]) if not daily.empty else 0.0
        value_brl = fund["shares"] * quota
        asset_rows.append({
            "name": fund["name"],
            "type": "BR Fund",
            "value_brl": value_brl,
            "value_usd": value_brl / fx_rate if fx_rate else 0.0,
            "monthly_pct": last_monthly_return(monthly) / 100,
            "annual_pct": annual_return(monthly) / 100,
        })

    for fiv in fi_values:
        asset_rows.append({
            "name": fiv["name"],
            "type": "Fixed Income",
            "value_brl": fiv["current_value"],
            "value_usd": fiv["current_value"] / fx_rate if fx_rate else 0.0,
            "monthly_pct": fiv["monthly_return_%"] / 100,
            "annual_pct": fiv["total_return_%"] / 100,
        })

    for tc in config.get("us_tickers", []):
        t = tc["ticker"]
        td = us_ticker_data.get(t, {})
        value_usd = tc["shares"] * td.get("current_price_usd", 0.0)
        value_brl = value_usd * fx_rate
        if td.get("is_money_market"):
            mm = td.get("mm_yield", {})
            ann_yield = mm.get("annualized_yield_pct", 0.0)
            mon_yield_usd = mm.get("monthly_yield_usd", 0.0)
            asset_rows.append({
                "name": tc.get("name", t),
                "type": "US Money Market",
                "value_brl": value_brl,
                "value_usd": value_usd,
                "monthly_pct": (mon_yield_usd / value_usd) if value_usd else 0.0,
                "annual_pct": ann_yield / 100,
            })
        else:
            ret_col = f"{t}_return_%"
            monthly = td.get("monthly", pd.DataFrame())
            asset_rows.append({
                "name": tc.get("name", t),
                "type": "US Equity",
                "value_brl": value_brl,
                "value_usd": value_usd,
                "monthly_pct": last_monthly_return(monthly, ret_col) / 100,
                "annual_pct": annual_return(monthly, ret_col) / 100,
            })

    total_brl = sum(a["value_brl"] for a in asset_rows)
    total_usd = total_brl / fx_rate if fx_rate else 0.0

    for a in asset_rows:
        a["weight_pct"] = a["value_brl"] / total_brl if total_brl else 0.0

    port_monthly = sum(a["monthly_pct"] * a["weight_pct"] for a in asset_rows) if total_brl else 0.0
    port_annual  = sum(a["annual_pct"]  * a["weight_pct"] for a in asset_rows) if total_brl else 0.0

    cdi_annual = compound_return(cdi_monthly["cdi_%"].tail(12)) / 100 if not cdi_monthly.empty else 0.0

    sp500_col = "^GSPC_return_%"
    fx_col    = "USDBRL=X_return_%"
    sp500_brl_annual = 0.0
    if not sp500_monthly.empty and not usdbrl_monthly.empty:
        merged = pd.merge(
            sp500_monthly[["period", sp500_col]].dropna(),
            usdbrl_monthly[["period", fx_col]].dropna(),
            on="period",
        ).tail(12)
        if not merged.empty:
            sp500_brl_annual = float(
                ((1 + merged[sp500_col] / 100) * (1 + merged[fx_col] / 100)).prod() - 1
            )

    # Allocation by type
    by_type: dict[str, float] = {}
    for a in asset_rows:
        by_type[a["type"]] = by_type.get(a["type"], 0.0) + a["value_brl"]
    allocation_by_type = {k: v / total_brl for k, v in by_type.items()} if total_brl else {}

    return {
        "as_of": today.isoformat(timespec="seconds"),
        "fx_rate": round(fx_rate, 4),
        "summary": {
            "total_brl":          round(total_brl, 2),
            "total_usd":          round(total_usd, 2),
            "monthly_pct":        round(port_monthly * 100, 4),
            "annual_pct":         round(port_annual  * 100, 4),
            "cdi_annual_pct":     round(cdi_annual   * 100, 4),
            "sp500_brl_annual_pct": round(sp500_brl_annual * 100, 4),
            "vs_cdi_pp":          round((port_annual - cdi_annual)          * 100, 4),
            "vs_sp500_brl_pp":    round((port_annual - sp500_brl_annual)    * 100, 4),
        },
        "allocation_by_type": {k: round(v, 6) for k, v in allocation_by_type.items()},
        "assets": [
            {k: (round(v, 6) if isinstance(v, float) else v) for k, v in a.items()}
            for a in asset_rows
        ],
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Load config from env var (GitHub Actions) or local file (dev)
    config_json = os.environ.get("PORTFOLIO_CONFIG")
    if config_json:
        print("Loading config from PORTFOLIO_CONFIG env var...")
        config = json.loads(config_json)
    else:
        config_path = Path(__file__).parent.parent / "portfolio_config.json"
        if not config_path.exists():
            config_path = Path(__file__).parent / ".." / "config.json"
        if not config_path.exists():
            print("ERROR: No config found. Set PORTFOLIO_CONFIG env var or provide portfolio_config.json")
            sys.exit(1)
        print(f"Loading config from {config_path}...")
        with open(config_path) as f:
            config = json.load(f)

    print("\n=== Building portfolio data ===\n")
    try:
        data = build_portfolio_data(config)
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)

    # Write output
    out_path = Path(__file__).parent.parent / "data" / "portfolio.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    s = data["summary"]
    print(f"\n=== Done ===")
    print(f"  Portfolio: R$ {s['total_brl']:,.2f}  ($ {s['total_usd']:,.2f})")
    print(f"  Monthly: {s['monthly_pct']:+.2f}%   Annual: {s['annual_pct']:+.2f}%")
    print(f"  vs CDI: {s['vs_cdi_pp']:+.2f} pp   vs S&P500 BRL: {s['vs_sp500_brl_pp']:+.2f} pp")
    print(f"  Written to: {out_path}")


if __name__ == "__main__":
    main()
