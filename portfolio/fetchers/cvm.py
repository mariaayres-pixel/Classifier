"""Fetch and process CVM daily fund data. Supports any fund CNPJ."""

import io
import zipfile
from datetime import datetime, timedelta

import pandas as pd
import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; investment-tracker/1.0)"}
BASE_URL = "https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS"
CAD_URL = "https://dados.cvm.gov.br/dados/FI/CAD/DADOS/cad_fi.csv"

_month_cache: dict[str, pd.DataFrame] = {}
_cad_cache: pd.DataFrame | None = None


def _fetch_raw_month(year: int, month: int) -> pd.DataFrame:
    """Download a full monthly zip from CVM (cached in memory)."""
    key = f"{year}{month:02d}"
    if key in _month_cache:
        return _month_cache[key]

    url = f"{BASE_URL}/inf_diario_fi_{year}{month:02d}.zip"
    response = requests.get(url, timeout=60, headers=HEADERS)
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        with z.open(z.namelist()[0]) as f:
            df = pd.read_csv(f, sep=";", dtype=str, encoding="latin1")

    _month_cache[key] = df
    return df


def _fetch_cad_csv() -> pd.DataFrame:
    """Download and cache the CVM fund registration CSV (~10 MB, one-time per session)."""
    global _cad_cache
    if _cad_cache is not None:
        return _cad_cache
    print("  Downloading CVM registration database (one-time, ~10 MB)...")
    response = requests.get(CAD_URL, timeout=120, headers=HEADERS)
    response.raise_for_status()
    df = pd.read_csv(io.BytesIO(response.content), sep=";", dtype=str, encoding="latin1")
    _cad_cache = df
    return df


def verify_cnpj(cnpj: str) -> dict:
    """
    Verify a CNPJ against the CVM fund registration database.

    Tries exact match first, then digits-only fallback (CVM sometimes stores
    CNPJs without punctuation). Returns dict with: cnpj, name, status, found.
    """
    df = _fetch_cad_csv()

    # Accept any column whose name contains "CNPJ" — CVM has changed naming across versions
    cnpj_col = next((c for c in df.columns if "CNPJ" in c.upper()), None)
    name_col = next((c for c in df.columns if "DENOM" in c.upper()), None)
    status_col = "SIT" if "SIT" in df.columns else None

    if cnpj_col is None:
        sample = list(df.columns[:8])
        return {"cnpj": cnpj, "name": f"? (no CNPJ column — cols: {sample})", "status": "UNKNOWN", "found": False}

    # 1) Exact match (e.g. "36.499.594/0001-74")
    match = df[df[cnpj_col] == cnpj]

    # 2) Digits-only fallback (e.g. "36499594000174")
    if match.empty:
        digits = "".join(filter(str.isdigit, cnpj))
        mask = df[cnpj_col].str.replace(r"\D", "", regex=True) == digits
        match = df[mask]

    if match.empty:
        return {"cnpj": cnpj, "name": "NOT FOUND IN CVM DATABASE", "status": "UNKNOWN", "found": False}

    row = match.iloc[-1]
    return {
        "cnpj": cnpj,
        "name": str(row[name_col]).strip() if name_col else "N/A",
        "status": str(row[status_col]).strip() if status_col else "N/A",
        "found": True,
    }


def fetch_fund_data(cnpj: str, months: int = 13) -> pd.DataFrame:
    """
    Fetch last N months of daily CVM data for a given fund CNPJ.
    Returns DataFrame with DT_COMPTC, VL_QUOTA, VL_PATRIM_LIQ, NR_COTST.
    """
    today = datetime.today()
    frames = []

    for i in range(months):
        target = (today.replace(day=1) - timedelta(days=i * 28)).replace(day=1)
        try:
            print(f"  Fetching CVM {target.year}-{target.month:02d}...")
            raw = _fetch_raw_month(target.year, target.month)
            filtered = raw[raw["CNPJ_FUNDO_CLASSE"] == cnpj].copy()
            if not filtered.empty:
                frames.append(filtered)
        except requests.HTTPError as e:
            print(f"    Skipped {target.year}-{target.month:02d}: {e}")

    if not frames:
        return pd.DataFrame()

    data = pd.concat(frames).drop_duplicates()
    data["DT_COMPTC"] = pd.to_datetime(data["DT_COMPTC"])
    data["VL_QUOTA"] = pd.to_numeric(data["VL_QUOTA"].str.replace(",", "."), errors="coerce")
    data["VL_PATRIM_LIQ"] = pd.to_numeric(data["VL_PATRIM_LIQ"].str.replace(",", "."), errors="coerce")
    data["NR_COTST"] = pd.to_numeric(data["NR_COTST"], errors="coerce")
    return data.sort_values("DT_COMPTC").reset_index(drop=True)


def calc_monthly_returns(data: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate monthly returns from daily quota data.
    Returns DataFrame with: period (str YYYY-MM), VL_QUOTA, return_%.
    """
    if data.empty:
        return pd.DataFrame(columns=["period", "VL_QUOTA", "return_%"])

    grouped = data.groupby(data["DT_COMPTC"].dt.to_period("M"))
    monthly = grouped["VL_QUOTA"].last().reset_index()
    monthly.columns = ["period", "VL_QUOTA"]
    monthly["return_%"] = monthly["VL_QUOTA"].pct_change() * 100
    monthly["period"] = monthly["period"].astype(str)
    return monthly
