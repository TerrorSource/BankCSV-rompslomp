import csv
import io
import json
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation

import requests
from flask import Flask, jsonify, render_template, request

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
ROMPSLOMP_BASE = os.environ.get("ROMPSLOMP_BASE_URL", "https://app.rompslomp.nl")

app = Flask(__name__)


# ---------- config ----------

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    # migratie van het oude één-administratie formaat
    if "profiles" not in cfg and cfg.get("company_id"):
        cfg["profiles"] = [{
            "id": 1,
            "name": f"{cfg.get('company_name') or 'Administratie'} — "
                    f"{(cfg.get('account_name') or 'rekening').split(' • ')[-1]}",
            "company_id": cfg.pop("company_id"),
            "company_name": cfg.pop("company_name", None),
            "account_id": cfg.pop("account_id"),
            "account_name": cfg.pop("account_name", None),
        }]
        cfg.pop("invert_amounts", None)
        save_config(cfg)
    return cfg


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)


def get_profile(cfg, profile_id):
    for p in cfg.get("profiles", []):
        if str(p.get("id")) == str(profile_id):
            return p
    return None


# ---------- Rompslomp API ----------

class RompslompError(Exception):
    pass


def api_request(method, path, token, **kwargs):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.request(
            method, f"{ROMPSLOMP_BASE}{path}", headers=headers, timeout=30, **kwargs
        )
    except requests.RequestException as e:
        raise RompslompError(f"Kan Rompslomp niet bereiken: {e}")
    if resp.status_code == 401:
        raise RompslompError("API-token ongeldig of verlopen (401).")
    if resp.status_code == 403:
        raise RompslompError(
            "Geen toegang (403) voor dit bedrijf. Dit gebeurt als het bedrijf op het "
            "gratis Rompslomp-abonnement zit (dan is de API geblokkeerd) of als het "
            "token de rechten 'read:accounts' en 'manage:payments' mist. Kies een "
            "ander bedrijf of pas het token aan in Rompslomp."
        )
    if not resp.ok:
        raise RompslompError(f"Rompslomp API-fout {resp.status_code}: {resp.text[:300]}")
    if resp.status_code == 204:
        return None
    return resp.json()


def fetch_all_payments(token, company_id):
    """Alle bestaande af/bij-schrijvingen ophalen (gepagineerd)."""
    payments = []
    page = 1
    while True:
        data = api_request(
            "GET",
            f"/api/v1/companies/{company_id}/payments",
            token,
            params={"selection": "all", "page": page, "per_page": 100},
        )
        batch = data.get("payments", [])
        payments.extend(batch)
        if len(batch) < 100:
            return payments
        page += 1


# ---------- CSV parsing ----------

def parse_dutch_amount(text):
    """'1.234,56' -> Decimal('1234.56')"""
    cleaned = text.strip().replace(".", "").replace(",", ".")
    return Decimal(cleaned)


def decode_csv(file_bytes):
    try:
        return file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return file_bytes.decode("latin-1")


def parse_ics_csv(reader):
    """ICS creditcard-export: puntkomma's, dd-mm-jjjj, NL-bedragen met D/C-teken."""
    date_col = "Transactiedatum" if "Transactiedatum" in reader.fieldnames else "Boekingsdatum"
    if date_col not in reader.fieldnames or "Bedrag" not in reader.fieldnames:
        raise ValueError("Kolom 'Transactiedatum'/'Boekingsdatum' of 'Bedrag' ontbreekt in het ICS-bestand.")

    rows = []
    for line_no, row in enumerate(reader, start=2):
        raw_date = (row.get(date_col) or "").strip()
        raw_amount = (row.get("Bedrag") or "").strip()
        if not raw_date and not raw_amount:
            continue  # lege regel
        try:
            date = datetime.strptime(raw_date, "%d-%m-%Y").date()
        except ValueError:
            raise ValueError(f"Regel {line_no}: ongeldige datum '{raw_date}' (verwacht dd-mm-jjjj).")
        booking_date = None
        raw_booking = (row.get("Boekingsdatum") or "").strip()
        if raw_booking:
            try:
                booking_date = datetime.strptime(raw_booking, "%d-%m-%Y").date()
            except ValueError:
                pass
        try:
            amount = parse_dutch_amount(raw_amount)
        except (InvalidOperation, AttributeError):
            raise ValueError(f"Regel {line_no}: ongeldig bedrag '{raw_amount}'.")
        rows.append({
            "date": date.isoformat(),
            "booking_date": booking_date.isoformat() if booking_date else None,
            "description": (row.get("Omschrijving") or "").strip(),
            "amount": str(amount),
        })
    return rows


def parse_godutch_csv(reader):
    """GoDutch-bankexport: komma's, ISO-datums, aparte Debit/Credit-kolommen.
    Bedrag = Credit - Debit (af = negatief, bij = positief), zoals de
    bestaande boekingen in Rompslomp."""
    rows = []
    for line_no, row in enumerate(reader, start=2):
        raw_date = (row.get("Date") or "").strip()
        if not raw_date:
            continue
        try:
            date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(f"Regel {line_no}: ongeldige datum '{raw_date}' (verwacht jjjj-mm-dd).")
        try:
            debit = Decimal((row.get("Debit Amount") or "").strip() or "0")
            credit = Decimal((row.get("Credit Amount") or "").strip() or "0")
        except InvalidOperation:
            raise ValueError(f"Regel {line_no}: ongeldig bedrag "
                             f"'{row.get('Debit Amount')}'/'{row.get('Credit Amount')}'.")
        amount = credit - debit
        if amount == 0:
            continue
        description = " — ".join(
            part for part in ((row.get("Counterparty") or "").strip(),
                              (row.get("Description") or "").strip())
            if part
        )
        rows.append({
            "date": date.isoformat(),
            "booking_date": None,
            "description": description,
            "amount": str(amount),
        })
    return rows


def parse_bank_csv(file_bytes):
    """Herkent het exportformaat aan de kopregel en parseert naar
    uniforme regels: {date, booking_date, description, amount}."""
    text = decode_csv(file_bytes)
    header = text.splitlines()[0] if text.splitlines() else ""
    if "Transactiedatum" in header or "Boekingsdatum" in header:
        reader = csv.DictReader(io.StringIO(text), delimiter=";")
        return "ICS creditcard", parse_ics_csv(reader)
    if "Debit Amount" in header and "Credit Amount" in header:
        reader = csv.DictReader(io.StringIO(text))
        return "GoDutch", parse_godutch_csv(reader)
    raise ValueError(
        "Onbekend CSV-formaat: dit lijkt geen ICS- of GoDutch-export. "
        f"Gevonden kopregel: {header[:120]}"
    )


# ---------- duplicaatdetectie ----------

DUPLICATE_TOLERANCE_DAYS = 3


def build_existing_pool(existing, account_id):
    """Index op absoluut bedrag -> lijst datums, alleen voor de gekozen rekening."""
    pool = {}
    count = 0
    for p in existing:
        if p.get("account_id") != account_id:
            continue
        try:
            amount = abs(Decimal(p.get("amount") or "0"))
            date = datetime.strptime((p.get("paid_at") or "")[:10], "%Y-%m-%d").date()
        except (InvalidOperation, ValueError):
            continue
        pool.setdefault(amount, []).append(date)
        count += 1
    return pool, count


def match_and_consume(row, pool):
    """Duplicaat als hetzelfde absolute bedrag bestaat met een datum binnen de
    tolerantie. Elke bestaande boeking dekt maximaal één CSV-regel af, zodat
    twee identieke transacties op dezelfde dag niet allebei tegen dezelfde
    boeking wegvallen. Datums zitten er soms een dag(je) naast en het teken
    verschilt per invoermethode, vandaar de marge en het absolute bedrag."""
    amount = abs(Decimal(row["amount"]))
    dates = pool.get(amount)
    if not dates:
        return False
    row_dates = [datetime.strptime(row["date"], "%Y-%m-%d").date()]
    if row.get("booking_date"):
        row_dates.append(datetime.strptime(row["booking_date"], "%Y-%m-%d").date())
    best = min(dates, key=lambda d: min(abs((d - rd).days) for rd in row_dates))
    if min(abs((best - rd).days) for rd in row_dates) <= DUPLICATE_TOLERANCE_DAYS:
        dates.remove(best)
        return True
    return False


# ---------- routes ----------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    cfg = load_config()
    return jsonify({
        "has_token": bool(cfg.get("token")),
        "profiles": cfg.get("profiles", []),
    })


@app.route("/api/config", methods=["POST"])
def set_config():
    data = request.get_json(force=True)
    cfg = load_config()
    token = (data.get("token") or "").strip()
    if token:
        cfg["token"] = token
    if "profiles" in data:
        profiles = []
        for i, p in enumerate(data["profiles"], start=1):
            if not p.get("company_id") or not p.get("account_id"):
                return jsonify({"error": f"Administratie {i} is onvolledig: kies bedrijf en rekening."}), 400
            profiles.append({
                "id": i,
                "name": (p.get("name") or "").strip() or f"Administratie {i}",
                "company_id": int(p["company_id"]),
                "company_name": p.get("company_name"),
                "account_id": int(p["account_id"]),
                "account_name": p.get("account_name"),
            })
        cfg["profiles"] = profiles
    save_config(cfg)
    return jsonify({"ok": True})


def current_token(payload=None):
    token = ((payload or {}).get("token") or "").strip()
    if not token:
        token = load_config().get("token", "")
    if not token:
        raise RompslompError("Geen API-token ingesteld. Vul eerst je Rompslomp API-token in.")
    return token


@app.route("/api/companies", methods=["POST"])
def companies():
    data = request.get_json(force=True) or {}
    try:
        token = current_token(data)
        result = api_request("GET", "/api/v1/companies", token, params={"per_page": 100})
    except RompslompError as e:
        return jsonify({"error": str(e)}), 400
    companies_out = []
    for c in result.get("companies", []):
        ac = c.get("access_control") or {}
        scopes = ac.get("allowed_scopes") or []
        missing = [s for s in ("read:accounts", "manage:payments") if scopes and s not in scopes]
        companies_out.append({
            "id": c["id"],
            "name": c.get("name") or c.get("owner_name") or str(c["id"]),
            "api_accessible": ac.get("api_accessible", True),
            "missing_scopes": missing,
        })
    return jsonify({"companies": companies_out})


@app.route("/api/accounts", methods=["POST"])
def accounts():
    data = request.get_json(force=True) or {}
    company_id = data.get("company_id")
    if not company_id:
        return jsonify({"error": "Geen bedrijf gekozen."}), 400
    try:
        token = current_token(data)
        result = api_request(
            "GET",
            f"/api/v1/companies/{company_id}/accounts",
            token,
            params={"selection": "payment", "per_page": 100},
        )
    except RompslompError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({
        "accounts": [
            {"id": a["id"], "name": a.get("path_name") or a.get("name") or str(a["id"])}
            for a in result.get("accounts", [])
        ]
    })


@app.route("/api/preview", methods=["POST"])
def preview():
    """CSV parsen en tegen bestaande Rompslomp-betalingen houden."""
    if "file" not in request.files:
        return jsonify({"error": "Geen bestand geüpload."}), 400
    cfg = load_config()
    if not cfg.get("token"):
        return jsonify({"error": "Geen API-token ingesteld: sla eerst de instellingen op."}), 400
    profile = get_profile(cfg, request.form.get("profile_id"))
    if not profile:
        return jsonify({"error": "Geen (geldige) administratie gekozen."}), 400

    try:
        csv_format, rows = parse_bank_csv(request.files["file"].read())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        existing = fetch_all_payments(cfg["token"], profile["company_id"])
    except RompslompError as e:
        return jsonify({"error": str(e)}), 400

    pool, existing_count = build_existing_pool(existing, profile["account_id"])
    out = []
    for row in rows:
        out.append({
            "date": row["date"],
            "description": row["description"],
            "amount": row["amount"],
            "duplicate": match_and_consume(row, pool),
        })
    return jsonify({
        "format": csv_format,
        "rows": out,
        "existing_count": existing_count,
        "new_count": sum(1 for r in out if not r["duplicate"]),
    })


@app.route("/api/import", methods=["POST"])
def do_import():
    data = request.get_json(force=True) or {}
    rows = data.get("rows", [])
    if not rows:
        return jsonify({"error": "Geen regels geselecteerd."}), 400
    cfg = load_config()
    if not cfg.get("token"):
        return jsonify({"error": "Geen API-token ingesteld."}), 400
    profile = get_profile(cfg, data.get("profile_id"))
    if not profile:
        return jsonify({"error": "Geen (geldige) administratie gekozen."}), 400

    results = []
    for row in rows:
        payload = {
            "payment": {
                "amount": str(row["amount"]),
                "description": row["description"],
                "account_id": profile["account_id"],
                "paid_at": row["date"],
            }
        }
        try:
            created = api_request(
                "POST",
                f"/api/v1/companies/{profile['company_id']}/payments",
                cfg["token"],
                json=payload,
            )
            results.append({"row": row, "ok": True, "id": created["payment"]["id"]})
        except RompslompError as e:
            results.append({"row": row, "ok": False, "error": str(e)})

    return jsonify({
        "results": results,
        "imported": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
    })


if __name__ == "__main__":
    from waitress import serve
    port = int(os.environ.get("PORT", "8000"))
    print(f"CSV → Rompslomp importer draait op poort {port}")
    serve(app, host="0.0.0.0", port=port)
