"""Downloads new data from Octopus (plus weather and grid carbon) into the local database.

Runs in a background thread so the page stays responsive. Only new readings are
downloaded after the first sync.
"""
import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests

from octopus import Kraken, Octopus, OctopusError, is_two_rate, region_of

UTC = timezone.utc
HISTORY_YEARS = int(os.environ.get("HISTORY_YEARS", 3))   # how far back the first sync goes
CLIMATE_YEARS = 6                                         # temperature history used for "a normal year"
COMPARE_DAYS = 100                                        # tariff-comparison window (plus a margin)

# Octopus Tracker isn't listed for new customers any more, but its prices are still published.
# Each version, newest last – used for the "did fixing pay off?" price history.
TRACKER_PRODUCTS = ["SILVER-25-04-15", "SILVER-25-09-02", "SILVER-26-04-01"]
AGILE_OUTGOING = "AGILE-OUTGOING-19-05-13"
FIXED_OFFERS = ["Octopus 12M Fixed", "Octopus 18M Fixed"]   # what you could fix at next

# Octopus's API has at times published electricity "inc VAT" prices equal to the ex-VAT price,
# while bills do include VAT – so VAT is added here from the ex-VAT figure. Export payments carry no VAT.
VAT = float(os.environ.get("VAT_RATE", 0.05))
RATES_BASIS = f"exc_vat+{VAT}"          # bump to force a fresh price download


def price_inc_vat(tariff, row):
    if "OUTGOING" in tariff or "EXPORT" in tariff:
        return row["value_exc_vat"]
    return round(row["value_exc_vat"] * (1 + VAT), 6)


# m³ -> kWh: volume correction × typical UK calorific value ÷ 3.6 (the formula on your gas bill)
M3_TO_KWH = 1.02264 * 39.5 / 3.6

# Tariffs offered in the "would another tariff be cheaper?" comparison (matched on display name).
COMPARE_PRODUCTS = [
    ("Flexible Octopus", "Standard variable – follows the price cap"),
    ("Agile Octopus", "Half-hourly prices that follow the wholesale market"),
    ("Octopus Tracker", "Daily prices that follow the wholesale market"),
    ("Octopus 12M Fixed", "Price fixed for 12 months"),
    ("Octopus Go", "Cheap overnight rate – designed for electric cars"),
    ("Cosy Octopus", "Cheap slots through the day – designed for heat pumps"),
]


def _tariff_code(by_payment):
    """Product detail lists tariffs per payment method – usually 'direct_debit_monthly', but
    some (e.g. Flexible Octopus) use 'varying'."""
    for key in ("direct_debit_monthly", "varying", *by_payment.keys()):
        code = (by_payment.get(key) or {}).get("code")
        if code:
            return code
    return None


def iso(ts):
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    if s is None:
        return None
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


class SyncManager:
    def __init__(self, db, api_key, account_number, gas_units="auto"):
        self.db = db
        self.api_key = api_key
        self.account_number = account_number
        self.gas_units = gas_units
        self.status = {"running": False, "step": None, "error": None, "finished_at": db.get("last_sync")}
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self.status["running"]:
                return False
            self.status.update(running=True, error=None, step="Starting")
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _step(self, text):
        self.status["step"] = text
        print(f"[sync] {text}", flush=True)

    def _run(self):
        try:
            self.sync()
            now = int(time.time())
            self.db.set("last_sync", now)
            self.status["finished_at"] = now
        except OctopusError as e:
            self.status["error"] = str(e)
        except Exception as e:  # keep the server alive whatever happens
            traceback.print_exc()
            self.status["error"] = f"{type(e).__name__}: {e}"
        finally:
            self.db.bump_version()
            self.status.update(running=False, step=None)

    # ------------------------------------------------------------------

    def sync(self):
        api = Octopus(self.api_key)
        if self.db.get("rates_basis") != RATES_BASIS:
            self._step("Re-downloading prices")
            self.db.clear_rates()
            self.db.set("rates_basis", RATES_BASIS)
        self._step("Reading your account")
        meters = self._account(api)
        self._step("Checking your balance and bills")
        try:
            self._money()
        except Exception as e:  # nice-to-have – never fail the sync for it
            print(f"[sync] balance/bills skipped: {e}", flush=True)
        for fuel, info in meters.items():
            self._step(f"Downloading {fuel} readings")
            self._consumption(api, fuel, info)
        self._step("Downloading tariff prices")
        self._tariff_rates(api)
        self._step("Fetching tariffs to compare")
        try:
            self._compare_tariffs(api)
        except Exception as e:  # nice-to-have – never fail the sync for it
            print(f"[sync] tariff comparison skipped: {e}", flush=True)
        self._step("Fetching price history and offers")
        try:
            self._history_and_offers(api)
        except Exception as e:
            print(f"[sync] price history / offers skipped: {e}", flush=True)
        self._step("Fetching weather")
        try:
            self._weather()
        except Exception as e:
            print(f"[sync] weather skipped: {e}", flush=True)
        self._step("Fetching grid carbon intensity")
        try:
            self._carbon()
        except Exception as e:
            print(f"[sync] carbon skipped: {e}", flush=True)

    def _account(self, api):
        acct = api.account(self.account_number)
        props = acct.get("properties") or []
        if not props:
            raise OctopusError("No properties found on this account")
        current = [p for p in props if not p.get("moved_out_at")] or props
        prop = max(current, key=lambda p: p.get("moved_in_at") or "")

        meters, agreements = {}, []
        for point in prop.get("electricity_meter_points", []):
            fuel = "export" if point.get("is_export") else "elec"
            if fuel in meters:
                continue
            meters[fuel] = {"id": point["mpan"], "serials": [m["serial_number"] for m in point.get("meters", [])],
                            "estimate_kwh": point.get("consumption_standard")}
            for a in point.get("agreements", []):
                agreements.append((fuel, a["tariff_code"], parse_ts(a["valid_from"]), parse_ts(a.get("valid_to"))))
        for point in prop.get("gas_meter_points", [])[:1]:
            meters["gas"] = {"id": point["mprn"], "serials": [m["serial_number"] for m in point.get("meters", [])],
                             "estimate_kwh": point.get("consumption_standard")}
            for a in point.get("agreements", []):
                agreements.append(("gas", a["tariff_code"], parse_ts(a["valid_from"]), parse_ts(a.get("valid_to"))))

        self.db.replace_agreements(agreements)
        postcode = (prop.get("postcode") or "").strip().upper()
        self.db.set("account", {
            "number": acct.get("number"),
            "moved_in_at": prop.get("moved_in_at"),
            "outcode": postcode.split(" ")[0] if postcode else None,   # only the first half is ever used
            "meters": {f: {"id_tail": m["id"][-4:], "serials": len(m["serials"]),
                           "estimate_kwh": m["estimate_kwh"]} for f, m in meters.items()},
        })
        return meters

    def _consumption(self, api, fuel, info):
        last = self.db.last_reading_ts(fuel)
        now = int(time.time())
        if last:
            start = last - 2 * 86400       # re-read a little overlap in case of late readings
        else:
            starts = [a["valid_from"] for a in self.db.agreements() if a["fuel"] == fuel]
            start = max(min(starts) if starts else 0, now - HISTORY_YEARS * 365 * 86400)
        factor = self._gas_factor(info) if fuel == "gas" else 1.0
        for serial in info["serials"]:
            try:
                rows = api.consumption(fuel, info["id"], serial, period_from=iso(start))
            except OctopusError:
                continue                    # old/removed meters often 404
            out = [(parse_ts(r["interval_start"]), r["consumption"] * factor, r["consumption"])
                   for r in rows if r.get("consumption") is not None]
            if out:
                self.db.put_readings(fuel, out)
        if fuel == "gas" and self.gas_units == "auto":
            # first sync: now that we have readings, check our m³/kWh guess against Octopus's estimate
            new_factor = self._gas_factor(info, recheck=True)
            if new_factor != factor:
                self.db.regas(new_factor)

    def _gas_factor(self, info, recheck=False):
        """SMETS2 gas meters report m³, SMETS1 report kWh. Work out which."""
        if self.gas_units in ("m3", "kWh"):
            units = self.gas_units
        else:
            units = self.db.get("gas_units")
            if units is None or recheck:
                units = self._guess_gas_units(info) or units or "m3"
        self.db.set("gas_units", units)
        return M3_TO_KWH if units == "m3" else 1.0

    def _guess_gas_units(self, info):
        estimate = info.get("estimate_kwh")
        with self.db.connect() as c:
            row = c.execute("SELECT SUM(raw), COUNT(*) FROM readings WHERE fuel='gas' "
                            "AND ts > strftime('%s','now') - 365*86400").fetchone()
        total, n = row[0], row[1]
        if not total or n < 48 * 60:
            return None
        yearly_raw = total * (365 * 48 / n)
        if estimate:
            # m³ figures are ~11× smaller than kWh. Pick whichever is closer to Octopus's annual estimate.
            ratio = estimate / yearly_raw
            return "m3" if ratio > 3.3 else "kWh"
        return "m3" if yearly_raw < 3000 else "kWh"

    def _tariff_rates(self, api):
        now = int(time.time())
        horizon = now + 2 * 86400              # catches tomorrow's Agile prices
        tariffs = {}
        for a in self.db.agreements():
            lo, hi = tariffs.get(a["tariff"], (a["valid_from"], a["valid_to"] or horizon))
            tariffs[a["tariff"]] = (min(lo, a["valid_from"]), max(hi, a["valid_to"] or horizon))
        for tariff, (lo, hi) in tariffs.items():
            lo = max(lo, now - (HISTORY_YEARS + 1) * 365 * 86400)
            self._fetch_rates(api, tariff, lo, min(hi, horizon))

    def _fetch_rates(self, api, tariff, lo, hi):
        kinds = [("day-unit-rates", "day"), ("night-unit-rates", "night")] if is_two_rate(tariff) \
            else [("standard-unit-rates", "unit")]
        kinds.append(("standing-charges", "standing"))
        for endpoint, kind in kinds:
            first, last = self.db.first_rate_ts(tariff, kind), self.db.last_rate_ts(tariff, kind)
            spans = []
            if first is None:
                spans.append((lo, hi))
            else:
                if lo < first - 86400:                 # need older prices than we have
                    spans.append((lo, first))
                if max(lo, last) < hi:
                    spans.append((max(lo, last), hi))
            for a, b in spans:
                if a >= b:                             # e.g. an agreement that starts and ends at once
                    continue
                rows = api.rates(tariff, endpoint, iso(a), iso(b))
                self.db.put_rates(tariff, kind, [(parse_ts(r["valid_from"]), parse_ts(r["valid_to"]), price_inc_vat(tariff, r))
                                                 for r in rows if r.get("valid_from") and r.get("value_exc_vat") is not None])

    def _compare_tariffs(self, api):
        elec = [a for a in self.db.agreements() if a["fuel"] == "elec"]
        if not elec:
            return
        region = region_of(elec[-1]["tariff"])
        has_gas = any(a["fuel"] == "gas" for a in self.db.agreements())
        products = [p for p in api.products() if p.get("direction") == "IMPORT" and not p.get("is_prepay")]
        now = int(time.time())
        start = now - COMPARE_DAYS * 86400
        out = []
        for name, blurb in COMPARE_PRODUCTS:
            matches = [p for p in products if p["display_name"] == name]
            if not matches:
                continue
            product = max(matches, key=lambda p: p.get("available_from") or "")
            detail = api.product(product["code"])
            e = (detail.get("single_register_electricity_tariffs") or {}).get(f"_{region}", {})
            g = (detail.get("single_register_gas_tariffs") or {}).get(f"_{region}", {})
            e_code = _tariff_code(e)
            g_code = _tariff_code(g) if has_gas else None
            if not e_code:
                continue
            for code in filter(None, [e_code, g_code]):
                self._fetch_rates(api, code, start, now + 2 * 86400)
            out.append({"name": name, "blurb": blurb, "product": product["code"],
                        "variable": bool(product.get("is_variable")),
                        "elec": e_code, "gas": g_code})
        # Agile prices for your region are always handy for "when's cheapest?"
        self.db.set("region", region)
        self.db.set("compare_tariffs", out)

    def _money(self):
        k = Kraken(self.api_key, self.account_number)
        bal = k.overview()
        bal["at"] = int(time.time())
        self.db.set("balance", bal)
        self.db.set("transactions", k.transactions())
        self.db.set("statements", k.statements())

    def _first_reading(self):
        with self.db.connect() as c:
            row = c.execute("SELECT MIN(ts) FROM readings").fetchone()
        return row[0]

    def _history_and_offers(self, api):
        elec = [a for a in self.db.agreements() if a["fuel"] == "elec"]
        if not elec:
            return
        region = region_of(elec[-1]["tariff"])
        has_gas = any(a["fuel"] == "gas" for a in self.db.agreements())
        now = int(time.time())
        start = self._first_reading() or now - 365 * 86400
        horizon = now + 2 * 86400

        def codes(product):
            detail = api.product(product)
            e = _tariff_code((detail.get("single_register_electricity_tariffs") or {}).get(f"_{region}", {}))
            g = _tariff_code((detail.get("single_register_gas_tariffs") or {}).get(f"_{region}", {})) if has_gas else None
            return detail, e, g

        # Flexible and Tracker over your whole history: "did fixing pay off?"
        history = {}
        flex = next((c for c in self.db.get("compare_tariffs") or [] if c["name"] == "Flexible Octopus"), None)
        if flex:
            for code in filter(None, [flex["elec"], flex.get("gas")]):
                self._fetch_rates(api, code, start, horizon)
            history["Flexible Octopus"] = [{"elec": flex["elec"], "gas": flex.get("gas"), "from": None, "to": None}]
        tracker = []
        for product in TRACKER_PRODUCTS:
            try:
                detail, e, g = codes(product)
            except OctopusError:
                continue
            a = parse_ts(detail.get("available_from")) or start
            tracker.append({"elec": e, "gas": g, "from": a, "to": None})
        # each Tracker version covers from its launch until the next one launched
        tracker.sort(key=lambda t: t["from"])
        for i, t in enumerate(tracker):
            t["to"] = tracker[i + 1]["from"] if i + 1 < len(tracker) else None
        tracker = [t for t in tracker if (t["to"] or horizon) > start]
        for t in tracker:
            for code in filter(None, [t["elec"], t["gas"]]):
                self._fetch_rates(api, code, max(start, t["from"]), min(horizon, t["to"] or horizon))
        if tracker:
            history["Octopus Tracker"] = tracker
        self.db.set("history_tariffs", history)

        # Agile Outgoing over your export history: "would a different export tariff pay more?"
        if any(a["fuel"] == "export" for a in self.db.agreements()):
            _, e, _ = codes(AGILE_OUTGOING)
            if e:
                self._fetch_rates(api, e, start, horizon)
                self.db.set("export_alternatives", [{"name": "Agile Outgoing Octopus", "tariff": e}])

        # Fixed deals you could switch to when your current one ends
        products = api.products()
        offers = []
        for name in FIXED_OFFERS:
            matches = [p for p in products if p["display_name"] == name and p.get("direction") == "IMPORT"]
            if not matches:
                continue
            product = max(matches, key=lambda p: p.get("available_from") or "")
            detail, e, g = codes(product["code"])
            if not e:
                continue
            for code in filter(None, [e, g]):
                self._fetch_rates(api, code, now - 3 * 86400, horizon)
            ex = ((detail.get("single_register_electricity_tariffs") or {}).get(f"_{region}") or {})
            ex = next(iter(ex.values()), {}) if ex else {}
            offers.append({"name": name, "product": product["code"], "elec": e, "gas": g,
                           "term": detail.get("term"), "exit_fee": ex.get("exit_fees_inc_vat"),
                           "exit_fee_type": ex.get("exit_fees_type")})
        self.db.set("fixed_offers", offers)

    def _location(self):
        loc = self.db.get("location")
        if loc:
            return loc
        outcode = (self.db.get("account") or {}).get("outcode")
        if not outcode:
            return None
        # Only the outward half of the postcode (e.g. "SW1A") is sent – it's area-level, not your address.
        r = requests.get(f"https://api.postcodes.io/outcodes/{outcode}", timeout=20)
        r.raise_for_status()
        res = r.json()["result"]
        loc = {"lat": round(res["latitude"], 2), "lon": round(res["longitude"], 2), "outcode": outcode}
        self.db.set("location", loc)
        return loc

    def _weather(self):
        loc = self._location()
        if not loc:
            return
        today = datetime.now(UTC).date()
        existing = {d for d, (t, fc) in self.db.weather().items() if not fc}
        first = today - timedelta(days=CLIMATE_YEARS * 365)
        archive_end = today - timedelta(days=6)
        missing_from = first
        if existing:
            missing_from = datetime.strptime(max(existing), "%Y-%m-%d").date() + timedelta(days=1)
        params = {"latitude": loc["lat"], "longitude": loc["lon"], "daily": "temperature_2m_mean",
                  "timezone": "Europe/London"}
        rows = []
        if missing_from <= archive_end:
            r = requests.get("https://archive-api.open-meteo.com/v1/archive", timeout=60, params=dict(
                params, start_date=missing_from.isoformat(), end_date=archive_end.isoformat()))
            r.raise_for_status()
            d = r.json()["daily"]
            rows += [(day, t, 0) for day, t in zip(d["time"], d["temperature_2m_mean"]) if t is not None]
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=60,
                         params=dict(params, past_days=7, forecast_days=16))
        r.raise_for_status()
        d = r.json()["daily"]
        rows += [(day, t, 1 if day >= today.isoformat() else 0)
                 for day, t in zip(d["time"], d["temperature_2m_mean"]) if t is not None]
        self.db.put_weather(rows)

    def _carbon(self):
        outcode = (self.db.get("location") or {}).get("outcode")
        if not outcode:
            return
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        base = "https://api.carbonintensity.org.uk/regional/intensity"
        rows = []
        # the API only allows short ranges per request – fetch the last 28 days a week at a time
        for back in (28, 21, 14, 7):
            a, b = now - timedelta(days=back), now - timedelta(days=back - 7)
            url = f"{base}/{a:%Y-%m-%dT%H:%MZ}/{b:%Y-%m-%dT%H:%MZ}/postcode/{outcode}"
            r = requests.get(url, timeout=30, headers={"Accept": "application/json"})
            r.raise_for_status()
            for item in r.json()["data"]["data"]:
                rows.append((parse_ts(item["from"].replace("Z", ":00Z")), item["intensity"]["forecast"], 0))
        r = requests.get(f"{base}/{now:%Y-%m-%dT%H:%MZ}/fw48h/postcode/{outcode}", timeout=30)
        r.raise_for_status()
        for item in r.json()["data"]["data"]:
            rows.append((parse_ts(item["from"].replace("Z", ":00Z")), item["intensity"]["forecast"], 1))
        self.db.put_carbon(rows)

        # Great Britain as a whole – the fairer figure for a footprint, as regions trade power constantly
        nat = "https://api.carbonintensity.org.uk/intensity"
        rows = []
        for back in (28, 21, 14, 7):
            a, b = now - timedelta(days=back), now - timedelta(days=back - 7)
            r = requests.get(f"{nat}/{a:%Y-%m-%dT%H:%MZ}/{b:%Y-%m-%dT%H:%MZ}", timeout=30)
            r.raise_for_status()
            for item in r.json()["data"]:
                v = item["intensity"].get("actual") or item["intensity"].get("forecast")
                if v is not None:
                    rows.append((parse_ts(item["from"].replace("Z", ":00Z")), v, 0))
        r = requests.get(f"{nat}/{now:%Y-%m-%dT%H:%MZ}/fw48h", timeout=30)
        r.raise_for_status()
        for item in r.json()["data"]:
            if item["intensity"].get("forecast") is not None:
                rows.append((parse_ts(item["from"].replace("Z", ":00Z")), item["intensity"]["forecast"], 1))
        self.db.put_carbon(rows, table="carbon_national")
