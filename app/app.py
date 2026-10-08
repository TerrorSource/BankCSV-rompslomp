import csv
import io
import json
import os
import re
import shlex
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

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
    """ICS creditcard-export: puntkomma's, dd-mm-jjjj, NL-bedragen met D/C-teken.
    ICS geeft uitgaven positief; Rompslomp verwacht uitgaven negatief, dus
    het teken wordt omgedraaid (incasso's worden daarmee positief)."""
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
            amount = -parse_dutch_amount(raw_amount)
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


def parse_mt940(text):
    """Generieke MT940-parser: :61: (valutadatum, D/C, bedrag) met
    bijbehorende :86:-omschrijving. Bankconventie: credit = positief,
    debet = negatief; R(eversal) draait het teken om."""
    rows = []
    pending = None

    def flush(description=""):
        nonlocal pending
        if pending:
            pending["description"] = description
            rows.append(pending)
            pending = None

    blocks = re.split(r"\r?\n(?=:\d{2}[A-Z]?:)|\r?\n(?=-$)", text.strip())
    for block in blocks:
        if block.startswith(":61:"):
            flush()
            m = re.match(r":61:(\d{6})(\d{4})?(R?[CD])[A-Z]?(\d{1,15},\d{0,2})", block)
            if not m:
                raise ValueError(f"Onleesbare MT940-regel: {block[:60]}")
            raw_date, _, dc, raw_amount = m.groups()
            date = datetime.strptime(raw_date, "%y%m%d").date()
            amount = Decimal(raw_amount.replace(",", "."))
            if dc in ("D", "RC"):
                amount = -amount
            pending = {"date": date.isoformat(), "booking_date": None,
                       "description": "", "amount": None, "_amount": amount}
        elif block.startswith(":86:") and pending:
            desc = " ".join(block[4:].split())
            # gestructureerde SEPA-tags (/NAME/, /REMI/) leesbaar maken;
            # het USTD/STRD-subtype in REMI eerst weghalen
            desc_clean = re.sub(r"/REMI//?(USTD|STRD)//?", "/REMI/", desc)
            parts = []
            for tag in ("NAME", "REMI", "EREF"):
                m = re.search(rf"/{tag}/(.*?)(?=/[A-Z]{{3,4}}/|$)", desc_clean)
                if m and m.group(1).strip():
                    parts.append(m.group(1).strip())
            flush(" — ".join(parts) if parts else desc)
    flush()
    for row in rows:
        row["amount"] = str(row.pop("_amount"))
    return rows


ICS_DATE_KEYS = ("transactionDate", "date", "bookingDate", "processingDate")
ICS_AMOUNT_KEYS = ("billingAmount", "amount", "transactionAmount")


def _find_transaction_list(node):
    """Zoekt (recursief) de eerste lijst met transactie-objecten, zodat zowel
    een kale lijst als een genest antwoord ({"transactions": [...]},
    {"data": {"items": [...]}}) werkt."""
    if isinstance(node, list):
        if node and all(isinstance(x, dict) for x in node) and any(
            any(k in x for k in ICS_DATE_KEYS) for x in node
        ):
            return node
        for x in node:
            found = _find_transaction_list(x)
            if found is not None:
                return found
    elif isinstance(node, dict):
        for v in node.values():
            found = _find_transaction_list(v)
            if found is not None:
                return found
    return None


def _ics_amount(t):
    for key in ICS_AMOUNT_KEYS:
        if key in t and t[key] is not None:
            value = t[key]
            if isinstance(value, dict):  # bijv. {"value": 12.5, "currency": "EUR"}
                value = value.get("value", value.get("amount"))
            return key, value
    return None, None


def parse_ics_json(text):
    """JSON-antwoord van het interne ICS transactie-API (transactionsv3).
    billingAmount: positief = afschrijving, zelfde teken als de ICS CSV-export;
    net als daar wordt het teken omgedraaid zodat uitgaven negatief worden."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Geen geldige JSON: {e}")
    if isinstance(data, list) and not data:
        return []
    transactions = _find_transaction_list(data)
    if transactions is None:
        keys = list(data.keys())[:15] if isinstance(data, dict) else type(data).__name__
        raise ValueError(f"Geen transactielijst gevonden in de JSON. Gevonden velden: {keys}")

    rows = []
    for i, t in enumerate(transactions, start=1):
        raw_date = ""
        for key in ICS_DATE_KEYS:
            if t.get(key):
                raw_date = str(t[key])[:10]
                break
        date = None
        for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
            try:
                date = datetime.strptime(raw_date, fmt).date()
                break
            except ValueError:
                pass
        if not date:
            raise ValueError(f"Transactie {i}: onbekende datum '{raw_date}'. "
                             f"Velden: {sorted(t.keys())[:20]}")
        key, raw_amount = _ics_amount(t)
        if raw_amount is None:
            raise ValueError(f"Transactie {i}: geen bedrag gevonden. Velden: {sorted(t.keys())[:20]}")
        try:
            amount = Decimal(str(raw_amount).replace(",", "."))
        except InvalidOperation:
            raise ValueError(f"Transactie {i}: ongeldig bedrag '{raw_amount}'.")
        # Sommige antwoorden geven een positief bedrag met een apart D/C-veld;
        # een creditering (betaling/terugboeking) is in ICS-conventie negatief.
        dc = str(t.get("debitCredit") or t.get("creditDebitIndicator") or "").upper()
        if dc.startswith("C") and amount > 0:
            amount = -amount
        amount = -amount
        if amount == 0:
            continue
        rows.append({
            "date": date.isoformat(),
            "booking_date": None,
            "description": " ".join(str(t.get("description") or "").split()).upper(),
            "amount": str(amount),
        })
    return rows


def parse_statement(file_bytes):
    """Herkent het formaat (ICS CSV, GoDutch CSV, MT940 of ICS JSON) en
    parseert naar uniforme regels: {date, booking_date, description, amount}."""
    text = decode_csv(file_bytes)
    stripped = text.lstrip()
    lines = text.splitlines()
    header = lines[0] if lines else ""
    if stripped.startswith(("[", "{")):
        return "ICS (API-JSON)", parse_ics_json(stripped)
    if "Transactiedatum" in header or "Boekingsdatum" in header:
        reader = csv.DictReader(io.StringIO(text), delimiter=";")
        return "ICS creditcard", parse_ics_csv(reader)
    if "Debit Amount" in header and "Credit Amount" in header:
        reader = csv.DictReader(io.StringIO(text))
        return "GoDutch", parse_godutch_csv(reader)
    if ":61:" in text:
        return "MT940", parse_mt940(text)
    raise ValueError(
        "Onbekend formaat: dit lijkt geen ICS-, GoDutch-, MT940- of JSON-export. "
        f"Gevonden kopregel: {header[:120]}"
    )


# ---------- ICS transacties ophalen via geplakt DevTools-request ----------

ICS_ALLOWED_HOSTS = ("icscards.nl",)


def parse_curl_command(cmd):
    """Parset een door Chrome gegenereerd 'Copy as cURL'-commando naar
    (url, headers). Alleen requests naar icscards.nl zijn toegestaan en de
    geplakte sessie wordt uitsluitend voor dit ene request gebruikt."""
    cmd = cmd.replace("\\\n", " ").replace("^\n", " ").strip()
    try:
        tokens = shlex.split(cmd)
    except ValueError as e:
        raise ValueError(f"Kon het cURL-commando niet lezen: {e}")
    if not tokens or tokens[0] != "curl":
        raise ValueError("Dit is geen cURL-commando (moet met 'curl' beginnen).")

    url, headers = None, {}
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-H", "--header") and i + 1 < len(tokens):
            key, _, value = tokens[i + 1].partition(":")
            headers[key.strip()] = value.strip()
            i += 2
        elif tok in ("-b", "--cookie") and i + 1 < len(tokens):
            headers["Cookie"] = tokens[i + 1]
            i += 2
        elif tok in ("-X", "--request", "-d", "--data", "--data-raw", "--data-binary", "-o", "--output"):
            i += 2  # waarde overslaan; alleen eenvoudige GET wordt ondersteund
        elif tok.startswith("http://") or tok.startswith("https://"):
            url = tok
            i += 1
        else:
            i += 1
    if not url:
        raise ValueError("Geen URL gevonden in het cURL-commando.")
    host = (urlparse(url).hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in ICS_ALLOWED_HOSTS):
        raise ValueError(f"Alleen requests naar icscards.nl zijn toegestaan (niet '{host}').")
    # encoding aan requests overlaten (Chrome plakt o.a. zstd/br erin)
    headers.pop("accept-encoding", None)
    headers.pop("Accept-Encoding", None)
    return url, headers


def fetch_ics_transactions(curl_cmd):
    url, headers = parse_curl_command(curl_cmd)
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as e:
        raise ValueError(f"Ophalen bij ICS mislukt: {e}")
    if resp.status_code in (401, 403) or "login" in (resp.url or "").lower():
        raise ValueError(
            "ICS-sessie verlopen (de sessie vervalt al na ±1 minuut inactiviteit). "
            "Log opnieuw in, kopieer het request nogmaals en plak het direct."
        )
    if not resp.ok:
        raise ValueError(f"ICS gaf status {resp.status_code}.")
    return parse_ics_json(resp.text)


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
    """Transacties (bestand of geplakt request/JSON) parsen en tegen
    bestaande Rompslomp-betalingen houden."""
    pasted = (request.form.get("pasted") or "").strip()
    if "file" not in request.files and not pasted:
        return jsonify({"error": "Upload een bestand of plak een cURL-commando/JSON."}), 400
    cfg = load_config()
    if not cfg.get("token"):
        return jsonify({"error": "Geen API-token ingesteld: sla eerst de instellingen op."}), 400
    profile = get_profile(cfg, request.form.get("profile_id"))
    if not profile:
        return jsonify({"error": "Geen (geldige) administratie gekozen."}), 400

    try:
        if pasted:
            if pasted.startswith("curl"):
                csv_format, rows = "ICS (opgehaald via API)", fetch_ics_transactions(pasted)
            else:
                csv_format, rows = "ICS (API-JSON)", parse_ics_json(pasted)
        else:
            csv_format, rows = parse_statement(request.files["file"].read())
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
