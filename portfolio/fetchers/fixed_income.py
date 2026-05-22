"""Calculate fixed income asset values and history based on CDI rates or pre-fixado rates."""

from datetime import datetime

import pandas as pd

from fetchers.bcb import calc_accumulated_cdi


def _parse_asset(asset: dict) -> tuple:
    """Extract normalized fields supporting both old and new config key names."""
    invested = asset.get("invested_amount") or asset.get("invested", 0.0)
    rate_type = asset.get("rate_type", "cdi")
    # CDI: support both 'rate_pct_cdi' (old) and 'rate_pct' (new)
    rate_pct_cdi = asset.get("rate_pct_cdi") or asset.get("rate_pct", 100.0)
    rate_annual_pct = asset.get("rate_annual_pct", 0.0)
    return invested, rate_type, rate_pct_cdi, rate_annual_pct


def calc_asset_value(asset: dict, cdi_daily: pd.DataFrame) -> dict:
    """
    Calculate current value and returns of a fixed income asset.

    Supports two rate types via config field 'rate_type':
      - "cdi"  : CDI-linked. Uses 'rate_pct' or 'rate_pct_cdi'.
      - "pre"  : Pre-fixado annual rate. Uses 'rate_annual_pct'.

    If today is past the asset's 'maturity' date, compounding stops at maturity
    and the returned dict includes matured=True.

    Returns dict with: name, rate_type, rate_label, invested_amount,
                       current_value, total_return_%, monthly_return_%, matured.
    """
    start_date = pd.to_datetime(asset["start_date"])
    invested, rate_type, rate_pct_cdi, rate_annual_pct = _parse_asset(asset)

    maturity_date = pd.to_datetime(asset["maturity"]) if asset.get("maturity") else None
    today = datetime.today()
    is_matured = maturity_date is not None and today.date() > maturity_date.date()

    cdi_from_start = cdi_daily[cdi_daily["data"] >= start_date].copy()

    # Stop compounding at maturity if the asset has already matured
    if is_matured and maturity_date is not None:
        cdi_from_start = cdi_from_start[cdi_from_start["data"] <= maturity_date]

    empty_result = {
        "name": asset["name"],
        "rate_type": rate_type,
        "rate_label": _rate_label(rate_type, rate_pct_cdi, rate_annual_pct),
        "invested_amount": invested,
        "current_value": invested,
        "total_return_%": 0.0,
        "monthly_return_%": 0.0,
        "matured": is_matured,
    }

    if cdi_from_start.empty:
        return empty_result

    if rate_type == "pre":
        total_factor, monthly_return = _calc_pre(cdi_from_start, rate_annual_pct)
    else:
        accumulated = calc_accumulated_cdi(cdi_from_start, rate_pct_cdi=rate_pct_cdi)
        total_factor = float(accumulated["accumulated_factor"].iloc[-1])
        monthly_return = _last_month_return_cdi(cdi_from_start, rate_pct_cdi)

    current_value = invested * total_factor
    total_return = (total_factor - 1) * 100

    return {
        "name": asset["name"],
        "rate_type": rate_type,
        "rate_label": _rate_label(rate_type, rate_pct_cdi, rate_annual_pct),
        "invested_amount": invested,
        "current_value": round(current_value, 2),
        "total_return_%": round(total_return, 4),
        "monthly_return_%": round(monthly_return, 4),
        "matured": is_matured,
    }


def calc_monthly_history(asset: dict, cdi_daily: pd.DataFrame) -> pd.DataFrame:
    """
    Build month-by-month accumulated value for a fixed income asset.
    Compounding stops at maturity date if the asset has matured.
    Returns DataFrame with: period (YYYY-MM), value (BRL).
    """
    start_date = pd.to_datetime(asset["start_date"])
    invested, rate_type, rate_pct_cdi, rate_annual_pct = _parse_asset(asset)

    maturity_date = pd.to_datetime(asset["maturity"]) if asset.get("maturity") else None
    today = datetime.today()

    cdi_from_start = cdi_daily[cdi_daily["data"] >= start_date].copy()

    # Cap at maturity if already matured
    if maturity_date is not None and today.date() > maturity_date.date():
        cdi_from_start = cdi_from_start[cdi_from_start["data"] <= maturity_date]

    if cdi_from_start.empty:
        return pd.DataFrame(columns=["period", "value"])

    if rate_type == "pre":
        df = cdi_from_start.copy()
        df["period"] = df["data"].dt.to_period("M").astype(str)
        month_days = df.groupby("period").size().reset_index(name="n_days")
        month_days["cum_days"] = month_days["n_days"].cumsum()
        month_days["accumulated_factor"] = (1 + rate_annual_pct / 100) ** (month_days["cum_days"] / 252)
        month_days["value"] = invested * month_days["accumulated_factor"]
        return month_days[["period", "value"]]
    else:
        accumulated = calc_accumulated_cdi(cdi_from_start, rate_pct_cdi=rate_pct_cdi)
        accumulated["period"] = accumulated["data"].dt.to_period("M").astype(str)
        monthly = accumulated.groupby("period")["accumulated_factor"].last().reset_index()
        monthly["value"] = invested * monthly["accumulated_factor"]
        return monthly[["period", "value"]]


# ── Private helpers ───────────────────────────────────────────────────────────

def _rate_label(rate_type: str, rate_pct_cdi: float, rate_annual_pct: float) -> str:
    if rate_type == "pre":
        return f"{rate_annual_pct:.2f}% a.a."
    return f"{rate_pct_cdi:.2f}% CDI"


def _calc_pre(cdi_from_start: pd.DataFrame, rate_annual_pct: float) -> tuple[float, float]:
    """Return (total_factor, last_complete_month_return_%) for a pre-fixado asset."""
    n_total = len(cdi_from_start)
    total_factor = (1 + rate_annual_pct / 100) ** (n_total / 252)

    current_period = pd.Period(datetime.today(), "M")
    df = cdi_from_start.copy()
    df["period"] = df["data"].dt.to_period("M")
    last_full = df[df["period"] < current_period]

    if not last_full.empty:
        last_period = last_full["period"].max()
        n_month = int((last_full["period"] == last_period).sum())
        monthly_factor = (1 + rate_annual_pct / 100) ** (n_month / 252)
        monthly_return = (monthly_factor - 1) * 100
    else:
        monthly_return = 0.0

    return total_factor, monthly_return


def _last_month_return_cdi(cdi_from_start: pd.DataFrame, rate_pct_cdi: float) -> float:
    """Return last complete month's return for a CDI-linked asset."""
    df = cdi_from_start.copy()
    df["period"] = df["data"].dt.to_period("M")
    current_period = pd.Period(datetime.today(), "M")
    last_full = df[df["period"] < current_period]

    if not last_full.empty:
        last_period = last_full["period"].max()
        last_month = last_full[last_full["period"] == last_period]
        monthly_factor = ((1 + last_month["valor"] / 100) ** (rate_pct_cdi / 100)).prod()
        return (monthly_factor - 1) * 100
    return 0.0
