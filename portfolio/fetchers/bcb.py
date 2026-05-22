"""Fetch CDI rates from the Brazilian Central Bank (BCB) open data API."""

from datetime import datetime

import pandas as pd
import requests

# BCB series 12 = CDI daily rate (% per day)
BCB_CDI_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.12/dados"


def fetch_cdi_daily(start_date: datetime, end_date: datetime) -> pd.DataFrame:
    """
    Fetch daily CDI rates from BCB between start_date and end_date.
    Returns DataFrame with columns: data (date), valor (daily rate %).
    Example: valor = 0.0406 means 0.0406% per day.
    """
    params = {
        "formato": "json",
        "dataInicial": start_date.strftime("%d/%m/%Y"),
        "dataFinal": end_date.strftime("%d/%m/%Y"),
    }
    response = requests.get(BCB_CDI_URL, params=params, timeout=30)
    response.raise_for_status()

    df = pd.DataFrame(response.json())
    df["data"] = pd.to_datetime(df["data"], format="%d/%m/%Y")
    df["valor"] = pd.to_numeric(df["valor"], errors="coerce")
    return df.sort_values("data").reset_index(drop=True)


def calc_cdi_monthly(cdi_daily: pd.DataFrame) -> pd.DataFrame:
    """
    Compound daily CDI rates into monthly returns.
    Returns DataFrame with: period (str YYYY-MM), cdi_% (monthly return %).
    """
    df = cdi_daily.copy()
    df["factor"] = 1 + df["valor"] / 100
    df["period"] = df["data"].dt.to_period("M")

    monthly = df.groupby("period")["factor"].prod().reset_index()
    monthly["cdi_%"] = (monthly["factor"] - 1) * 100
    monthly["period"] = monthly["period"].astype(str)
    return monthly[["period", "cdi_%"]]


def calc_accumulated_cdi(cdi_daily: pd.DataFrame, rate_pct_cdi: float = 100.0) -> pd.DataFrame:
    """
    Calculate the daily accumulated factor for a CDI-linked asset.

    For an asset at X% of CDI, the daily factor is:
        (1 + cdi_daily_rate/100) ^ (X/100)

    Returns DataFrame with: data (date), accumulated_factor (cumulative product).
    """
    df = cdi_daily.copy()
    df["daily_factor"] = (1 + df["valor"] / 100) ** (rate_pct_cdi / 100)
    df["accumulated_factor"] = df["daily_factor"].cumprod()
    return df[["data", "accumulated_factor", "daily_factor"]]
