#!/usr/bin/env python3
"""
BRASA Financial Dashboard — QuickBooks Sync Script
Pulls budget, expenses, and transactions per director from QuickBooks Online
and writes to data/directors.json (+ optionally Google Sheets).
"""

import json
import os
import smtplib
import time
from datetime import datetime, date
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────

QB_CLIENT_ID     = os.getenv("QBO_CLIENT_ID")
QB_CLIENT_SECRET = os.getenv("QBO_CLIENT_SECRET")
QB_REFRESH_TOKEN = os.getenv("QBO_REFRESH_TOKEN")
QB_REALM_ID      = os.getenv("QBO_REALM_ID")

GOOGLE_SHEETS_ID             = os.getenv("GOOGLE_SHEETS_ID")
GOOGLE_SERVICE_ACCOUNT_JSON  = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

ALERT_EMAIL_FROM = os.getenv("ALERT_EMAIL_FROM")
ALERT_EMAIL_TO   = os.getenv("ALERT_EMAIL_TO")
SMTP_HOST        = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT        = int(os.getenv("SMTP_PORT") or 587)
SMTP_USER        = os.getenv("SMTP_USER")
SMTP_PASSWORD    = os.getenv("SMTP_PASSWORD")

QB_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QB_BASE_URL  = f"https://quickbooks.api.intuit.com/v3/company/{QB_REALM_ID}"

DATA_FILE = Path("data/directors.json")
DATA_FILE.parent.mkdir(exist_ok=True)

# ── OAuth helpers ────────────────────────────────────────────────────────────

_access_token: str | None = os.getenv("QBO_ACCESS_TOKEN")  # seed from env
_token_expires_at: float = 0.0


def get_access_token() -> str:
    global _access_token, _token_expires_at

    if _access_token and time.time() < _token_expires_at - 60:
        return _access_token

    resp = requests.post(
        QB_TOKEN_URL,
        data={
            "grant_type":    "refresh_token",
            "refresh_token": QB_REFRESH_TOKEN,
        },
        auth=(QB_CLIENT_ID, QB_CLIENT_SECRET),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    if not resp.ok:
        # Refresh token may be expired — fall back to env access token if present
        if _access_token:
            print(f"[WARN] Token refresh failed ({resp.status_code}), using existing access token")
            return _access_token
        resp.raise_for_status()
    data = resp.json()
    _access_token      = data["access_token"]
    _token_expires_at  = time.time() + data.get("expires_in", 3600)
    # Persist fresh token to .env so next run reuses it
    _update_env_token(_access_token, data.get("refresh_token", QB_REFRESH_TOKEN))
    return _access_token


def _update_env_token(access_token: str, refresh_token: str) -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    import re
    text = env_path.read_text()
    for key, value in [("QBO_ACCESS_TOKEN", access_token), ("QBO_REFRESH_TOKEN", refresh_token)]:
        pattern = rf"^{re.escape(key)}\s*=.*$"
        replacement = f"{key}={value}"
        if re.search(pattern, text, flags=re.MULTILINE):
            text = re.sub(pattern, replacement, text, flags=re.MULTILINE)
        else:
            text = text.rstrip("\n") + f"\n{replacement}\n"
    env_path.write_text(text)


def qb_get(path: str, params: dict | None = None, attempt: int = 1) -> dict:
    """GET request to QB API with automatic retry (3x, exponential backoff)."""
    headers = {
        "Authorization": f"Bearer {get_access_token()}",
        "Accept":        "application/json",
    }
    url = f"{QB_BASE_URL}{path}"
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 401 and attempt <= 3:
            global _access_token
            _access_token = None
            time.sleep(2 ** attempt)
            return qb_get(path, params, attempt + 1)
        if not resp.ok:
            # Surface QB error body before raising
            try:
                err_body = resp.json()
                fault = err_body.get("Fault", err_body.get("fault", {}))
                print(f"[DEBUG] QB error body: {fault}")
            except Exception:
                print(f"[DEBUG] QB raw response: {resp.text[:400]}")
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        if attempt <= 3:
            wait = 2 ** attempt
            print(f"[WARN] QB request failed (attempt {attempt}/3), retrying in {wait}s — {exc}")
            time.sleep(wait)
            return qb_get(path, params, attempt + 1)
        raise


# ── QB data fetchers ─────────────────────────────────────────────────────────

def fetch_classes() -> tuple[dict[str, str], dict[str, str]]:
    """
    Returns (id_to_name, name_to_id) for all QB Classes.
    id_to_name:  {class_id: class_name}
    name_to_id:  {class_name: class_id}
    """
    data = qb_get("/query", {"query": "SELECT * FROM Class WHERE Active = true MAXRESULTS 1000"})
    id_to_name: dict[str, str] = {}
    name_to_id: dict[str, str] = {}
    for c in data.get("QueryResponse", {}).get("Class", []):
        if c.get("SubClass"):  # skip sub-classes — they appear as duplicates
            continue
        id_to_name[c["Id"]] = c["Name"]
        name_to_id[c["Name"]] = c["Id"]
    return id_to_name, name_to_id


def fetch_budgets(year: int) -> dict[str, dict]:
    """
    Returns {class_name: {"_total": X, "AccountName": Y, ...}}
    Each BudgetDetail line may repeat (monthly entries) — we sum them.
    """
    data = qb_get("/query", {"query": "SELECT * FROM Budget MAXRESULTS 100"})
    budgets: dict[str, dict] = {}
    year_tags = {str(year), f"FY{str(year)[2:]}"}
    for b in data.get("QueryResponse", {}).get("Budget", []):
        name = b.get("Name", "")
        if not any(tag in name for tag in year_tags):
            continue
        for line in b.get("BudgetDetail", []):
            class_name   = line.get("ClassRef",   {}).get("name", "")
            account_name = line.get("AccountRef", {}).get("name", "")
            amount       = float(line.get("Amount", 0))
            if not class_name:
                continue
            if class_name not in budgets:
                budgets[class_name] = {"_total": 0.0}
            budgets[class_name]["_total"] += amount
            if account_name:
                # QB budget stores "Parent:Sub-account" — keep only the leaf name
                # so it matches what the P&L reports as the category name.
                account_key = account_name.split(":")[-1].strip()
                budgets[class_name][account_key] = budgets[class_name].get(account_key, 0.0) + amount
    return budgets


def fetch_pnl(start_date: str, end_date: str, class_id: str) -> dict:
    """
    Returns P&L report data filtered by class ID.
    start_date / end_date: "YYYY-MM-DD"
    """
    params = {
        "start_date":        start_date,
        "end_date":          end_date,
        "class":             class_id,
        "accounting_method": "Accrual",
    }
    return qb_get("/reports/ProfitAndLoss", params)


def fetch_invoices(start_date: str, end_date: str) -> list[dict]:
    """
    Fetches all QB invoices for the year.
    Relações Corporativas owns ALL revenue — QB uses DepartmentRef (Iniciativa)
    on invoices, not ClassRef, so we return every invoice without class filtering.
    """
    page_size = 200
    start_pos = 1
    all_invoices: list = []

    while True:
        params = {
            "query": (
                f"SELECT * FROM Invoice WHERE TxnDate >= '{start_date}' "
                f"AND TxnDate <= '{end_date}' "
                f"STARTPOSITION {start_pos} MAXRESULTS {page_size}"
            ),
            "minorversion": "75",
        }
        data = qb_get("/query", params)
        page = data.get("QueryResponse", {}).get("Invoice", [])
        if not page:
            break
        all_invoices.extend(page)
        if len(page) < page_size:
            break
        start_pos += page_size

    result = []
    today = date.today()
    for inv in sorted(all_invoices, key=lambda x: x.get("TxnDate", ""), reverse=True):
        balance      = float(inv.get("Balance", 0))
        total        = float(inv.get("TotalAmt", 0))
        due_date_str = inv.get("DueDate", "")

        if balance == 0:
            status = "Pago"
        elif due_date_str and date.fromisoformat(due_date_str) < today:
            status = "Atrasado"
        else:
            status = "Pendente"

        # Iniciativa comes from DepartmentRef (QB "Location") — not ClassRef
        dept = inv.get("DepartmentRef") or {}
        dept_name = dept.get("name", "")
        if dept_name.lower() in ("nenhum", "none", ""):
            dept_name = "—"

        # First meaningful line item description
        descricao = ""
        for line in inv.get("Line", []):
            desc = line.get("Description", "")
            if desc and desc.strip():
                descricao = desc.strip()
                break

        result.append({
            "id":         inv.get("Id", ""),
            "numero":     inv.get("DocNumber", ""),
            "parceiro":   inv.get("CustomerRef", {}).get("name", "—"),
            "iniciativa": dept_name,
            "descricao":  descricao,
            "data":       inv.get("TxnDate", ""),
            "vencimento": due_date_str,
            "total":      round(total, 2),
            "emAberto":   round(balance, 2),
            "status":     status,
        })

    # Sort descending by data (TxnDate)
    result.sort(key=lambda x: x["data"], reverse=True)
    return result


def fetch_transactions(start_date: str, end_date: str, class_id: str) -> list[dict]:
    """Returns the last 10 transactions for a given class (by ID)."""
    params = {
        "start_date": start_date,
        "end_date":   end_date,
        "class":      class_id,
        "sort_by":    "tx_date",
        "sort_order": "descend",
    }
    data = qb_get("/reports/TransactionList", params)
    rows = []
    report_rows = (
        data.get("Rows", {}).get("Row", [])
    )
    # Columns: Date(0), Type(1), Num(2), Posting(3), Name(4),
    #          Location(5), Memo(6), Account(7), Split(8), Amount(9)
    expense_types = {
        "expense", "bill", "bill payment (check)", "bill payment (credit card)",
        "check", "credit card credit", "journal entry", "purchase order",
    }
    for row in report_rows:
        if row.get("type") != "Data":
            continue
        cols = row.get("ColData", [])
        if len(cols) < 10:
            continue
        tipo  = cols[1].get("value", "").lower()
        conta = cols[7].get("value", "")
        # Skip income-side entries (invoices, payments, A/R)
        if tipo not in expense_types:
            continue
        if "receivable" in conta.lower() or "a/r" in conta.lower():
            continue
        rows.append({
            "data":       cols[0].get("value", ""),
            "tipo":       cols[1].get("value", ""),
            "fornecedor": cols[4].get("value", "") or cols[6].get("value", ""),
            "conta":      conta,
            "categoria":  cols[8].get("value", ""),
            "descricao":  cols[6].get("value", ""),
            "valor":      abs(_parse_float(cols[9].get("value", "0"))),
        })
    return rows[:50]


# ── P&L parser ───────────────────────────────────────────────────────────────

def _parse_float(v: str) -> float:
    try:
        return float(v.replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0.0


SKIP_ACCOUNTS = {"reconciliation discrepancies", "nenhum", "other business expenses"}


def _collect_expense_rows(rows: list, categorias: list) -> float:
    """
    Recursively walk P&L row tree, collecting leaf expense entries.
    Returns the sum of all collected amounts.
    """
    total = 0.0
    for sub in rows:
        if sub.get("type") == "Section":
            sub_rows = sub.get("Rows", {}).get("Row", [])
            if sub_rows:
                # Has children — recurse to get leaf entries
                total += _collect_expense_rows(sub_rows, categorias)
            else:
                # Leaf section (no sub-rows) — take its Summary total
                summary_cols = sub.get("Summary", {}).get("ColData", [])
                nome  = summary_cols[0].get("value", "").replace("Total ", "") if summary_cols else ""
                valor = _parse_float(summary_cols[1].get("value", "0")) if len(summary_cols) > 1 else 0.0
                if nome and valor and nome.lower() not in SKIP_ACCOUNTS:
                    categorias.append({"nome": nome, "gasto": round(valor, 2)})
                    total += valor
        else:
            cols  = sub.get("ColData", [])
            nome  = cols[0].get("value", "") if cols else ""
            valor = _parse_float(cols[1].get("value", "0")) if len(cols) > 1 else 0.0
            if nome and valor and nome.lower() not in SKIP_ACCOUNTS:
                categorias.append({"nome": nome, "gasto": round(valor, 2)})
                total += valor
    return total


def parse_pnl(report: dict) -> dict:
    """
    Extracts total expenses and a breakdown by category from a P&L report.

    QB P&L can have two expense sections: "Expenses" and "Other Expenses".
    We recurse into sub-sections to collect leaf-level categories, excluding
    internal QB accounts (Reconciliation Discrepancies, Other Business Expenses, etc.).

    Returns {"total_gasto": float, "categorias": [{nome, gasto}]}
    """
    categorias: list = []
    section_totals: list[float] = []

    rows = report.get("Rows", {}).get("Row", [])
    for section in rows:
        header_name = section.get("Header", {}).get("ColData", [{}])[0].get("value", "")
        if "expense" not in header_name.lower() and "despesa" not in header_name.lower():
            continue

        section_cat_total = _collect_expense_rows(
            section.get("Rows", {}).get("Row", []), categorias
        )
        if section_cat_total:
            section_totals.append(section_cat_total)

    total_gasto = sum(section_totals)
    return {"total_gasto": round(total_gasto, 2), "categorias": categorias}


# ── Director mapping ──────────────────────────────────────────────────────────

def build_director_map(
    id_to_name: dict[str, str],
    name_to_id: dict[str, str],
) -> list[dict]:
    """
    Returns a list of {class_id, class_name, slug} dicts.
    Customize manual_map to assign friendly URL slugs to your QB class names.
    """
    # QB class name → director URL slug (used in ?diretor=slug)
    manual_map: dict[str, str] = {
        # "Nome da Classe no QB": "slug-do-diretor",
        # "Summit Americas": "summit-americas",
        # "Marketing": "marketing",
        # "Operações": "operacoes",
    }

    result = []
    for class_id, class_name in id_to_name.items():
        slug = manual_map.get(
            class_name,
            class_name.lower().replace(" ", "-").replace("/", "-"),
        )
        result.append({"class_id": class_id, "class_name": class_name, "slug": slug})
    return result


# ── Google Sheets export (optional) ──────────────────────────────────────────

def export_to_sheets(directors_data: list[dict]) -> None:
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=scopes)
        gc    = gspread.authorize(creds)
        sh    = gc.open_by_key(GOOGLE_SHEETS_ID)

        ws = sh.worksheet("dados") if "dados" in [w.title for w in sh.worksheets()] else sh.add_worksheet("dados", 1000, 20)
        ws.clear()

        header = ["slug", "nome", "orcamento", "total_gasto", "disponivel", "percentual", "atualizado_em"]
        rows   = [header]
        for d in directors_data:
            rows.append([
                d["slug"],
                d["nome"],
                d["orcamento"],
                d["total_gasto"],
                d["disponivel"],
                d["percentual"],
                d["atualizado_em"],
            ])
        ws.update("A1", rows)
        print(f"[OK] Google Sheets atualizado ({len(directors_data)} diretores)")
    except Exception as exc:
        print(f"[WARN] Falha ao exportar para Google Sheets: {exc}")


# ── Email alert ───────────────────────────────────────────────────────────────

def send_alert_email(subject: str, body: str) -> None:
    if not all([ALERT_EMAIL_FROM, ALERT_EMAIL_TO, SMTP_USER, SMTP_PASSWORD]):
        return
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = ALERT_EMAIL_FROM
        msg["To"]      = ALERT_EMAIL_TO

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        print(f"[OK] Email de alerta enviado para {ALERT_EMAIL_TO}")
    except Exception as exc:
        print(f"[WARN] Falha ao enviar email: {exc}")


# ── Main sync ─────────────────────────────────────────────────────────────────

def sync() -> None:
    now       = datetime.now()
    year      = now.year
    start_date = f"{year}-01-01"
    end_date   = now.strftime("%Y-%m-%d")

    print(f"[{now.isoformat()}] Iniciando sincronização QB → directors.json")

    try:
        id_to_name, name_to_id = fetch_classes()
        director_map = build_director_map(id_to_name, name_to_id)
        budgets      = fetch_budgets(year)

        directors_data = []
        for entry in director_map:
            class_id   = entry["class_id"]
            class_name = entry["class_name"]
            slug       = entry["slug"]
            print(f"  → Processando: {class_name} (id={class_id}, slug={slug})")

            pnl_report   = fetch_pnl(start_date, end_date, class_id)
            pnl          = parse_pnl(pnl_report)
            transactions = fetch_transactions(start_date, end_date, class_id)

            class_budgets = budgets.get(class_name, {})
            orcamento     = class_budgets.get("_total", 0.0)
            total_gasto   = pnl["total_gasto"]
            disponivel    = max(orcamento - total_gasto, 0.0)
            percentual    = round((total_gasto / orcamento * 100) if orcamento > 0 else 0, 1)

            # Attach per-account budget to each category.
            # Try exact match first; fall back to just the leaf part after " – "
            # because QB budget strips the parent prefix while P&L keeps the full name.
            categorias = []
            for cat in pnl["categorias"]:
                cat_orcamento = class_budgets.get(cat["nome"], 0.0)
                if not cat_orcamento:
                    leaf = cat["nome"].split(" – ")[-1].strip()
                    cat_orcamento = class_budgets.get(leaf, 0.0)
                categorias.append({**cat, "orcamento": round(cat_orcamento, 2)})

            if class_name == "Relações Corporativas":
                faturas = fetch_invoices(start_date, end_date)
                receita_total    = round(sum(f["total"]    for f in faturas), 2)
                a_receber        = round(sum(f["emAberto"] for f in faturas), 2)
                parcerias_ativas = len({f["parceiro"] for f in faturas})
            else:
                faturas          = []
                receita_total    = 0.0
                a_receber        = 0.0
                parcerias_ativas = 0

            directors_data.append({
                "slug":             slug,
                "nome":             class_name,
                "orcamento":        round(orcamento, 2),
                "total_gasto":      total_gasto,
                "disponivel":       round(disponivel, 2),
                "percentual":       percentual,
                "categorias":       categorias,
                "lancamentos":      transactions,
                "faturas":          faturas,
                "receita_total":    receita_total,
                "a_receber":        a_receber,
                "parcerias_ativas": parcerias_ativas,
                "atualizado_em":    now.isoformat(),
            })

        output = {
            "gerado_em":  now.isoformat(),
            "periodo":    {"inicio": start_date, "fim": end_date},
            "diretores":  directors_data,
        }

        DATA_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] {DATA_FILE} atualizado com {len(directors_data)} diretores")

        export_to_sheets(directors_data)

    except Exception as exc:
        msg = f"Erro na sincronização BRASA Dashboard: {exc}"
        print(f"[ERROR] {msg}")
        send_alert_email("[BRASA Dashboard] Falha na sincronização", msg)
        raise


if __name__ == "__main__":
    sync()
