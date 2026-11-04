"""Turns raw half-hourly readings into everything the dashboard shows.

The prediction model, in plain English:
  * For each fuel we learn  daily kWh = base + slope × "heating degrees" (+ a weekend nudge),
    where heating degrees = how far the day's average temperature was below 15.5°C.
    That's the standard way energy companies normalise for weather.
  * Future days use the real weather forecast for the next ~2 weeks, then the average
    temperature for that date over the last 6 years.
  * The likely range comes from how much your real days scatter around the model,
    plus how much each month's temperature varies year to year.
  * Costs use the prices you're paying right now (for Agile: your recent average p/kWh).
If there's no weather data, it falls back to "same month last year, adjusted for your
recent trend".
"""
import bisect
import math
import re
import statistics
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

from octopus import is_two_rate, product_code, region_of

LON = ZoneInfo("Europe/London")
UTC = timezone.utc
HDD_BASE = 15.5                 # °C – UK standard base for heating degree days
GAS_CO2 = 0.183                 # kg CO2e per kWh of gas (UK govt conversion factor)
ELEC_CO2_DEFAULT = 0.177        # kg CO2e per kWh of grid electricity when no live data
PEAK_START, PEAK_END = 32, 38   # 16:00–19:00 local, in half-hour slots
FUELS = ("elec", "gas", "export")

# Typical UK monthly shape (relative daily use) – only used when there's no weather data.
SEASONAL = {
    "elec": [1.22, 1.12, 1.04, 0.95, 0.88, 0.82, 0.80, 0.82, 0.88, 0.99, 1.12, 1.25],
    "gas": [1.85, 1.65, 1.40, 1.00, 0.62, 0.38, 0.30, 0.30, 0.45, 0.85, 1.40, 1.80],
    "export": [0.25, 0.45, 0.80, 1.20, 1.45, 1.50, 1.50, 1.30, 1.00, 0.65, 0.30, 0.20],
}
# Used only if there's too little cold-weather data to learn your own (kWh per heating degree).
PRIOR_SLOPE = {"gas": 4.0, "elec": 0.15}


# ---------------------------------------------------------------- helpers

def tariff_name(code):
    if not code:
        return None
    p = product_code(code)
    fixed = re.search(r"FIX-(\d+)M", p)
    for key, name in [("INTELLI-FLUX", "Intelligent Octopus Flux"), ("INTELLI", "Intelligent Octopus Go"),
                      ("IOG", "Intelligent Octopus Go"), ("AGILE-OUTGOING", "Agile Outgoing Octopus"),
                      ("AGILE", "Agile Octopus"), ("OUTGOING-SEG", "Outgoing (SEG)"),
                      ("OUTGOING", "Outgoing Octopus"), ("SILVER", "Octopus Tracker"),
                      ("TRACKER", "Octopus Tracker"), ("COSY", "Cosy Octopus"), ("GO", "Octopus Go"),
                      ("FLUX", "Octopus Flux"), ("SNUG", "Snug Octopus"), ("ZERO", "Octopus Zero Bills"),
                      ("PREPAY", "Prepay"), ("VAR", "Flexible Octopus")]:
        if p.startswith(key) or f"-{key}-" in f"-{p}-":
            return name + (f" {fixed.group(1)}M Fixed" if fixed and "Fixed" not in name else "")
    if fixed:
        return f"Octopus {fixed.group(1)}M Fixed"
    return p


def local(ts):
    return datetime.fromtimestamp(ts, LON)


def day_start_ts(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=LON).timestamp())


def hdd(t):
    return max(0.0, HDD_BASE - t)


def month_key(d):
    return f"{d.year:04d}-{d.month:02d}"


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def days_in_month(y, m):
    return (add_months(date(y, m, 1), 1) - date(y, m, 1)).days


def price_on(price, d):
    """(p/kWh, standing p/day) we expect to pay on day d – switches to the roll-over
    tariff once a fixed deal ends."""
    roll = price.get("rollover")
    if roll and price.get("ends") and d.isoformat() >= price["ends"]:
        return roll["unit"] or 0.0, roll["standing"] or 0.0
    return price.get("unit_effective") or 0.0, price.get("standing") or 0.0


def r(x, n=2):
    return None if x is None else round(x, n)


class RateBook:
    """Looks up the price in force at any moment for any tariff."""

    def __init__(self, db):
        self.db = db
        self.cache = {}

    def _series(self, tariff, kind):
        key = (tariff, kind)
        if key not in self.cache:
            rows = self.db.rates(tariff, kind)
            self.cache[key] = ([a for a, _, _ in rows], [b for _, b, _ in rows], [v for _, _, v in rows])
        return self.cache[key]

    def at(self, tariff, kind, ts, extend=False):
        froms, tos, vals = self._series(tariff, kind)
        if not froms:
            return None
        i = bisect.bisect_right(froms, ts) - 1
        if i < 0:
            return vals[0] if extend else None
        for j in range(i, max(-1, i - 4), -1):
            if froms[j] <= ts and (tos[j] is None or ts < tos[j]):
                return vals[j]
        return vals[i] if extend else None

    def unit(self, tariff, ts, extend=False):
        if is_two_rate(tariff):
            secs = ts % 86400            # Economy 7 night is usually 00:30–07:30 GMT
            return self.at(tariff, "night" if 1800 <= secs < 27000 else "day", ts, extend)
        return self.at(tariff, "unit", ts, extend)

    def standing(self, tariff, ts, extend=False):
        return self.at(tariff, "standing", ts, extend)

    def span(self, tariff, kind="unit"):
        froms, tos, _ = self._series(tariff, kind)
        if not froms:
            return None, None
        return froms[0], max((t or 2 ** 40) for t in tos)


class Contracts:
    """Which tariff each fuel was on at any moment."""

    def __init__(self, rows):
        self.by = defaultdict(list)
        for a in rows:
            self.by[a["fuel"]].append(a)

    def current(self, fuel, ts):
        for a in reversed(self.by.get(fuel, [])):
            if a["valid_from"] <= ts and (a["valid_to"] is None or ts < a["valid_to"]):
                return a
        return None

    def tariff(self, fuel, ts):
        items = self.by.get(fuel)
        if not items:
            return None
        for a in reversed(items):
            if a["valid_from"] <= ts and (a["valid_to"] is None or ts < a["valid_to"]):
                return a["tariff"]
        return items[0]["tariff"] if ts < items[0]["valid_from"] else items[-1]["tariff"]


# ---------------------------------------------------------------- weather model

class Climate:
    def __init__(self, weather):
        self.weather = weather                          # 'YYYY-MM-DD' -> (temp, is_forecast)
        by_md = defaultdict(list)
        monthly = defaultdict(list)
        for ds, (t, fc) in weather.items():
            if fc:
                continue
            d = date.fromisoformat(ds)
            by_md[(d.month, d.day)].append(t)
            monthly[(d.year, d.month)].append(t)
        self.normal = {}
        anchor = date(2001, 1, 1)
        for i in range(365):
            d = anchor + timedelta(days=i)
            vals = []
            for k in range(-3, 4):
                dd = d + timedelta(days=k)
                vals += by_md.get((dd.month, dd.day), [])
            if vals:
                self.normal[(d.month, d.day)] = sum(vals) / len(vals)
        self.normal[(2, 29)] = self.normal.get((2, 28))
        month_means = defaultdict(list)
        for (y, m), vals in monthly.items():
            if len(vals) >= days_in_month(y, m) - 1:
                month_means[m].append(sum(vals) / len(vals))
        self.month_sd = {m: (statistics.pstdev(v) if len(v) >= 3 else 1.3) for m, v in month_means.items()}
        self.month_normal = {m: sum(v) / len(v) for m, v in month_means.items()}

    @property
    def available(self):
        return len(self.normal) > 300

    def temp(self, d):
        w = self.weather.get(d.isoformat())
        if w is not None:
            return w[0], ("forecast" if w[1] else "actual")
        n = self.normal.get((d.month, d.day))
        return n, "normal"


class Model:
    """Predicts a day's kWh for one fuel."""

    def __init__(self, fuel, days, climate):
        # days: list of (date, kwh) – complete days only, oldest first
        self.fuel = fuel
        self.kind = "none"
        self.provisional = False
        self.coef = None
        self.sd = 0.0
        self.r2 = None
        self.bias = 0.0
        self.climate = climate
        recent = days[-365:]
        if climate.available:
            rows = [(d, k, climate.weather.get(d.isoformat())) for d, k in recent]
            rows = [(d, k, w[0]) for d, k, w in rows if w is not None]
            if len(rows) >= 21:
                self._fit_weather(rows)
        if self.kind == "none" and len(days) >= 7:
            self._fit_seasonal(days)
        if self.kind != "none":
            last = [(d, k) for d, k in days[-28:]]
            res = [k - self._raw(d) for d, k in last]
            res = [x for x in res if x is not None]
            # recent behaviour change: applied fully to the next few weeks, fading out over ~3 months
            self.bias = (sum(res) / len(res)) if res else 0.0
            self.last_day = days[-1][0]

    def _fit_weather(self, rows):
        X = np.array([[1.0, hdd(t), 1.0 if d.weekday() >= 5 else 0.0] for d, _, t in rows])
        y = np.array([k for _, k, _ in rows])
        spread = X[:, 1].max() - X[:, 1].min()
        expected_sign = -1 if self.fuel == "export" else 1
        if spread >= 6:
            coef, *_ = np.linalg.lstsq(X, y, rcond=None)
            if self.fuel != "export" and coef[1] < 0:
                coef[1] = 0.0
                coef[0], coef[2] = np.linalg.lstsq(X[:, [0, 2]], y, rcond=None)[0]
        else:
            # not enough cold/warm days yet to learn the heating slope – use a typical one
            slope = PRIOR_SLOPE.get(self.fuel, 0.0) * expected_sign if self.fuel != "export" else 0.0
            self.provisional = True
            adj = y - slope * X[:, 1]
            c0, c2 = np.linalg.lstsq(X[:, [0, 2]], adj, rcond=None)[0]
            coef = np.array([c0, slope, c2])
        pred = np.maximum(0, X @ coef)
        resid = y - pred
        self.coef = [float(c) for c in coef]
        self.sd = float(resid.std())
        self.r2 = float(1 - resid.var() / y.var()) if y.var() > 0 else None
        self.kind = "weather"
        self.n = len(rows)

    def _fit_seasonal(self, days):
        by_month = defaultdict(list)
        for d, k in days:
            by_month[(d.year, d.month)].append(k)
        self.month_mean = {key: sum(v) / len(v) for key, v in by_month.items() if len(v) >= 10}
        recent = [k for _, k in days[-30:]]
        self.recent_mean = sum(recent) / len(recent)
        self.recent_month = days[-1][0].month
        # trend: last 60 days vs the same 60 days a year earlier
        last_day = days[-1][0]
        lookup = dict(days)
        now60 = [lookup.get(last_day - timedelta(days=i)) for i in range(60)]
        then60 = [lookup.get(last_day - timedelta(days=365 + i)) for i in range(60)]
        pairs = [(a, b) for a, b in zip(now60, then60) if a is not None and b is not None]
        self.trend = (sum(a for a, _ in pairs) / max(1e-9, sum(b for _, b in pairs))) if len(pairs) > 30 else 1.0
        self.kind = "seasonal"
        resid = [k - self._raw(d) for d, k in days[-365:]]
        self.sd = statistics.pstdev(resid) if len(resid) > 2 else 0.25 * self.recent_mean
        self.n = len(days)

    def _raw(self, d, temp=None):
        if self.kind == "weather":
            if temp is None:
                temp = self.climate.temp(d)[0]
            if temp is None:
                return None
            a, b, c = self.coef
            return max(0.0, a + b * hdd(temp) + (c if d.weekday() >= 5 else 0.0))
        if self.kind == "seasonal":
            last_year = self.month_mean.get((d.year - 1, d.month))
            if last_year is not None:
                return last_year * self.trend
            s = SEASONAL.get(self.fuel, [1] * 12)
            return self.recent_mean * s[d.month - 1] / s[self.recent_month - 1]
        return None

    def predict(self, d, temp=None):
        raw = self._raw(d, temp)
        if raw is None:
            return None
        ahead = (d - self.last_day).days if hasattr(self, "last_day") else 0
        weight = math.exp(-ahead / 45) if ahead > 0 else 0.0
        return max(0.0, raw + self.bias * weight)

    @property
    def slope(self):
        return self.coef[1] if self.kind == "weather" else None

    def describe(self):
        return {"kind": self.kind, "provisional": self.provisional, "r2": r(self.r2),
                "base": r(self.coef[0]) if self.coef else None, "slope": r(self.slope, 3),
                "weekend": r(self.coef[2]) if self.coef else None, "sd": r(self.sd), "days": getattr(self, "n", 0)}


# ---------------------------------------------------------------- the main build

def build(db, demo=False):
    t0 = time.time()
    now = int(time.time())
    today = local(now).date()
    rb = RateBook(db)
    ct = Contracts(db.agreements())
    climate = Climate(db.weather())
    fuels = [f for f in FUELS if f in db.fuels()]

    # --- half-hourly rows with price ---
    hh = {}
    daily = {}
    for fuel in fuels:
        rows = []
        days = defaultdict(lambda: {"kwh": 0.0, "cost": 0.0, "n": 0, "priced": 0})
        for ts, kwh in db.readings(fuel):
            dt = local(ts)
            tariff = ct.tariff(fuel, ts)
            price = rb.unit(tariff, ts) if tariff else None
            if price is None and tariff:
                price = rb.unit(tariff, ts, extend=True)
            cost = kwh * price if price is not None else 0.0
            ds = dt.date()
            rows.append((ts, kwh, cost, ds, dt.hour * 2 + dt.minute // 30, price))
            day = days[ds]
            day["kwh"] += kwh
            day["cost"] += cost
            day["n"] += 1
        for ds, day in days.items():
            tariff = ct.tariff(fuel, day_start_ts(ds) + 43200)
            sc = (rb.standing(tariff, day_start_ts(ds) + 43200, extend=True) or 0.0) if tariff and fuel != "export" else 0.0
            day["standing"] = sc
            day["complete"] = day["n"] >= 46
        hh[fuel] = rows
        daily[fuel] = dict(sorted(days.items()))

    complete = {f: [(d, v["kwh"]) for d, v in daily[f].items() if v["complete"]] for f in fuels}
    models = {f: Model(f, complete[f], climate) for f in fuels if complete[f]}

    # --- what you're paying now ---
    prices_now = {}
    for fuel in fuels or FUELS:         # no readings at all: still show the tariffs on the account
        tariff = ct.tariff(fuel, now)
        if not tariff:
            continue
        recent = [x for x in hh.get(fuel, []) if x[0] >= now - 30 * 86400]
        weighted = None
        if recent:
            num = den = 0.0
            for ts, kwh, *_ in recent:
                p = rb.unit(tariff, ts, extend=True)
                if p is not None:
                    num += kwh * p
                    den += kwh
            weighted = num / den if den else None
        unit_now = rb.unit(tariff, now, extend=True)
        prices_now[fuel] = {
            "tariff": tariff, "name": tariff_name(tariff),
            "unit_now": r(unit_now),
            "unit_effective": r(weighted if weighted is not None else unit_now) if fuels else None,
            "standing": r(rb.standing(tariff, now, extend=True) or 0.0) if fuel != "export" else 0.0,
            "two_rate": is_two_rate(tariff),
            "fixed": "FIX" in product_code(tariff),
        }
        # Fixed deals end: Octopus moves you to Flexible unless you pick something else.
        agreement = ct.current(fuel, now)
        if agreement and agreement["valid_to"] and fuel != "export":
            p = prices_now[fuel]
            p["ends"] = local(agreement["valid_to"]).date().isoformat()
            flex = next((c for c in db.get("compare_tariffs") or [] if c["name"] == "Flexible Octopus"), None)
            code = flex and flex.get(fuel)
            if code and code != tariff:
                p["rollover"] = {"name": "Flexible Octopus", "tariff": code,
                                 "unit": r(rb.unit(code, now, extend=True)),
                                 "standing": r(rb.standing(code, now, extend=True) or 0.0)}

    out = {
        "generated_at": now, "demo": demo, "today": today.isoformat(), "fuels": fuels,
        "no_readings": not fuels,
        "account": _account_info(db, ct, fuels),
        "prices_now": prices_now,
        "models": {f: m.describe() for f, m in models.items()},
    }
    monthly, yearly, next12 = _forecast(fuels, daily, models, climate, prices_now, today)
    out["monthly"] = monthly
    out["yearly"] = yearly
    out["next12"] = next12
    out["daily"] = _daily_list(fuels, daily, climate, models)
    out["kpis"] = _kpis(fuels, daily, monthly, yearly, next12, today)
    out["patterns"] = _patterns(fuels, hh, daily, now)
    out["peaks"] = _peaks(fuels, hh, daily, models, climate, today)
    out["baseload"] = _baseload(hh, daily, prices_now) if "elec" in fuels else None
    out["weather"] = _weather_section(fuels, daily, models, climate, prices_now, today)
    out["agile"] = _agile(db, rb, ct, hh, now, today)
    out["carbon"] = _carbon(db, hh, daily, now, today)
    out["compare"] = _compare(db, rb, ct, hh, daily, prices_now, now)
    out["tariff_history"] = [{"fuel": a["fuel"], "tariff": a["tariff"], "name": tariff_name(a["tariff"]),
                              "from": a["valid_from"], "to": a["valid_to"]} for a in db.agreements()]
    out["quality"] = _quality(fuels, daily, today)
    out["bills"] = _bill_check(db, daily, fuels)
    out["payments"] = _payment_plan(db.get("balance"), monthly, fuels, today, daily, out["bills"])
    out["transactions"] = _transactions(db)
    out["renewal"] = _renewal(db, rb, prices_now, models, climate, today, now)
    out["price_history"] = _price_history(db, rb, hh, daily, fuels)
    out["solar"] = _solar(db, rb, hh) if "export" in fuels else None
    out["insights"] = _insights(out, daily, models, prices_now, today)
    out["build_seconds"] = round(time.time() - t0, 2)
    return out


def _account_info(db, ct, fuels):
    acct = db.get("account") or {}
    return {"meters": acct.get("meters", {}), "outcode": (db.get("location") or {}).get("outcode"),
            "region": db.get("region"), "gas_units": db.get("gas_units"),
            "last_sync": db.get("last_sync"), "moved_in_at": acct.get("moved_in_at")}


def _forecast(fuels, daily, models, climate, prices_now, today):
    """Monthly actuals + predictions, from your first month of data to the end of next year."""
    first = min((min(daily[f]) for f in fuels if daily[f]), default=today)
    start = date(first.year, first.month, 1)
    end = date(today.year + 1, 12, 31)
    months = []
    m = start
    while m <= end:
        months.append(m)
        m = add_months(m, 1)

    last_complete = {}
    for f in fuels:
        comp = [d for d, v in daily[f].items() if v["complete"]]
        last_complete[f] = max(comp) if comp else None

    rows = []
    var_by_key = {}
    for m in months:
        n_days = days_in_month(m.year, m.month)
        row = {"month": month_key(m), "days": n_days}
        temps = []
        for f in fuels:
            fr = {"kwh": 0.0, "cost": 0.0, "unit_cost": 0.0, "days": 0, "fkwh": 0.0, "fcost": 0.0, "fdays": 0, "var": 0.0}
            model = models.get(f)
            price = prices_now.get(f, {})
            unit = price.get("unit_effective") or 0.0
            sc = price.get("standing") or 0.0
            temp_unknown_days = 0
            for i in range(n_days):
                d = m + timedelta(days=i)
                v = daily[f].get(d)
                lc = last_complete[f]
                if lc is not None and d <= lc:
                    if v and v["complete"]:
                        fr["kwh"] += v["kwh"]
                        fr["cost"] += v["cost"] + v["standing"]
                        fr["unit_cost"] += v["cost"]
                        fr["days"] += 1
                    continue
                if lc is None or model is None:
                    continue
                t, src = climate.temp(d) if climate.available else (None, "none")
                if f == fuels[0] and t is not None:
                    temps.append(t)
                p = model.predict(d, t)
                if p is None:
                    continue
                fr["fkwh"] += p
                u, st = price_on(price, d)
                fr["fcost"] += p * u + st
                fr["fdays"] += 1
                if src == "normal":
                    temp_unknown_days += 1
            if model is not None and fr["fdays"]:
                v_noise = (model.sd ** 2) * fr["fdays"] * 3          # ×3: bad days tend to come in runs
                slope = abs(model.slope or 0.0)
                v_temp = (slope * climate.month_sd.get(m.month, 1.3) * temp_unknown_days) ** 2
                fr["var"] = v_noise + v_temp
            for d in [m + timedelta(days=i) for i in range(n_days)]:
                w = climate.weather.get(d.isoformat())
                if w and not w[1] and f == fuels[0]:
                    temps.append(w[0])
            sd = math.sqrt(fr["var"])
            total = fr["kwh"] + fr["fkwh"]
            row[f] = {
                "kwh": r(fr["kwh"], 1), "cost": r(fr["cost"] / 100), "days": fr["days"],
                "fkwh": r(fr["fkwh"], 1), "fcost": r(fr["fcost"] / 100), "fdays": fr["fdays"],
                "total_kwh": r(total, 1), "total_cost": r((fr["cost"] + fr["fcost"]) / 100),
                "low_kwh": r(max(fr["kwh"], total - 1.28 * sd), 1), "high_kwh": r(total + 1.28 * sd, 1),
                "low_cost": r((fr["cost"] + max(0, fr["fcost"] - 1.28 * sd * unit)) / 100),
                "high_cost": r((fr["cost"] + fr["fcost"] + 1.28 * sd * unit) / 100),
                "ppk": r(fr["unit_cost"] / fr["kwh"], 1) if fr["kwh"] > 0 and f != "export" else None,
            }
            var_by_key[(row["month"], f)] = (fr["var"], unit)
        row["temp"] = r(sum(temps) / len(temps), 1) if temps else None
        row["normal_temp"] = r(climate.month_normal.get(m.month), 1)
        row["status"] = ("past" if add_months(m, 1) <= date(today.year, today.month, 1)
                         else "current" if m.year == today.year and m.month == today.month else "future")
        rows.append(row)

    # trim leading months with almost no data and keep it tidy
    yearly = {}
    for row in rows:
        y = row["month"][:4]
        yr = yearly.setdefault(y, {"year": int(y)})
        for f in fuels:
            fy = yr.setdefault(f, {"kwh": 0.0, "cost": 0.0, "days": 0, "fkwh": 0.0, "fcost": 0.0,
                                   "fdays": 0, "var": 0.0, "unit": 0.0})
            x = row[f]
            fy["kwh"] += x["kwh"]
            fy["cost"] += x["cost"]
            fy["days"] += x["days"]
            fy["fkwh"] += x["fkwh"]
            fy["fcost"] += x["fcost"]
            fy["fdays"] += x["fdays"]
            var, unit = var_by_key[(row["month"], f)]
            fy["var"] += var
            fy["unit"] = unit
    years = []
    for y, yr in sorted(yearly.items()):
        n_days = 366 if (int(y) % 4 == 0 and (int(y) % 100 != 0 or int(y) % 400 == 0)) else 365
        item = {"year": yr["year"], "days": n_days}
        for f in fuels:
            fy = yr[f]
            sd = math.sqrt(fy["var"])
            total = fy["kwh"] + fy["fkwh"]
            item[f] = {"kwh": r(fy["kwh"], 0), "cost": r(fy["cost"]), "days": fy["days"],
                       "fkwh": r(fy["fkwh"], 0), "fcost": r(fy["fcost"]), "fdays": fy["fdays"],
                       "total_kwh": r(total, 0), "total_cost": r(fy["cost"] + fy["fcost"]),
                       "low_cost": r(fy["cost"] + max(0, fy["fcost"] - 1.28 * sd * fy["unit"] / 100)),
                       "high_cost": r(fy["cost"] + fy["fcost"] + 1.28 * sd * fy["unit"] / 100),
                       "low_kwh": r(total - 1.28 * sd, 0), "high_kwh": r(total + 1.28 * sd, 0)}
        years.append(item)

    # next 12 months, from tomorrow
    nxt = {}
    horizon_end = add_months(date(today.year, today.month, 1), 12)
    for f in fuels:
        model = models.get(f)
        price = prices_now.get(f, {})
        unit = price.get("unit_effective") or 0.0
        sc = price.get("standing") or 0.0
        kwh = cost = var = 0.0
        if model:
            d = today
            while d < today + timedelta(days=365):
                t = climate.temp(d)[0] if climate.available else None
                p = model.predict(d, t) or 0.0
                kwh += p
                u, st = price_on(price, d)
                cost += p * u + st
                d += timedelta(days=1)
            var = model.sd ** 2 * 365 * 3 + sum(
                (abs(model.slope or 0) * climate.month_sd.get(mm, 1.3) * 30) ** 2 for mm in range(1, 13))
        sd = math.sqrt(var)
        nxt[f] = {"kwh": r(kwh, 0), "cost": r(cost / 100), "low_cost": r((cost - 1.28 * sd * unit) / 100),
                  "high_cost": r((cost + 1.28 * sd * unit) / 100)}
    del horizon_end
    return rows, years, nxt


def _daily_list(fuels, daily, climate, models):
    dates = sorted(set().union(*[daily[f].keys() for f in fuels])) if fuels else []
    out = []
    for d in dates:
        w = climate.weather.get(d.isoformat())
        item = {"d": d.isoformat(), "t": r(w[0], 1) if w else None}
        for f in fuels:
            v = daily[f].get(d)
            if v:
                item[f] = r(v["kwh"], 2)
                item[f + "_cost"] = r((v["cost"] + v["standing"]) / 100)
                if not v["complete"]:
                    item.setdefault("partial", []).append(f)
                m = models.get(f)
                if m and m.kind == "weather" and w:
                    item[f + "_exp"] = r(m.predict(d, w[0]), 2)
        out.append(item)
    return out


def _sum_days(daily, fuel, a, b):
    """kWh, cost(£) for complete-or-not days a..b inclusive."""
    kwh = cost = 0.0
    n = 0
    for d, v in daily.get(fuel, {}).items():
        if a <= d <= b:
            kwh += v["kwh"]
            cost += v["cost"] + v["standing"]
            n += 1
    return kwh, cost / 100, n


def _kpis(fuels, daily, monthly, yearly, next12, today):
    bills = [f for f in fuels if f != "export"]
    cur = next((m for m in monthly if m["status"] == "current"), None)
    this_key = month_key(today)
    ly_key = f"{today.year - 1:04d}-{today.month:02d}"
    ly = next((m for m in monthly if m["month"] == ly_key), None)
    prev_key = month_key(add_months(date(today.year, today.month, 1), -1))
    prev = next((m for m in monthly if m["month"] == prev_key), None)
    year = next((y for y in yearly if y["year"] == today.year), None)
    last_year = next((y for y in yearly if y["year"] == today.year - 1), None)

    def tot(row, key):
        return round(sum((row[f][key] or 0) for f in bills), 2) if row else None

    k = {
        "month": this_key,
        "month_so_far": tot(cur, "cost"), "month_days": cur[bills[0]]["days"] if cur and bills else 0,
        "month_projected": tot(cur, "total_cost"), "month_low": tot(cur, "low_cost"), "month_high": tot(cur, "high_cost"),
        "month_last_year": tot(ly, "cost") if ly and all(ly[f]["days"] >= ly["days"] - 2 for f in bills) else None,
        "last_month": tot(prev, "cost"), "last_month_key": prev_key,
        "year": today.year, "year_so_far": tot(year, "cost"), "year_projected": tot(year, "total_cost"),
        "year_low": tot(year, "low_cost"), "year_high": tot(year, "high_cost"),
        "last_year": tot(last_year, "cost") if last_year and all(last_year[f]["days"] >= 360 for f in bills) else None,
        "next12": round(sum(next12[f]["cost"] or 0 for f in bills), 2) if bills else None,
        "next12_low": round(sum(next12[f]["low_cost"] or 0 for f in bills), 2) if bills else None,
        "next12_high": round(sum(next12[f]["high_cost"] or 0 for f in bills), 2) if bills else None,
        "per_fuel": {},
    }
    end = max((max(daily[f]) for f in fuels if daily[f]), default=today)
    for f in fuels:
        k30, c30, n30 = _sum_days(daily, f, end - timedelta(days=29), end)
        k365, c365, n365 = _sum_days(daily, f, end - timedelta(days=364), end)
        pk, pc, pn = _sum_days(daily, f, end - timedelta(days=364 + 365), end - timedelta(days=365))
        k["per_fuel"][f] = {
            "month_kwh": cur[f]["kwh"] if cur else None, "month_projected_kwh": cur[f]["total_kwh"] if cur else None,
            "avg_daily_kwh_30": r(k30 / n30, 2) if n30 else None, "avg_daily_cost_30": r(c30 / n30) if n30 else None,
            "kwh_365": r(k365, 0), "cost_365": r(c365), "days_365": n365,
            "kwh_prev_365": r(pk, 0) if pn >= 330 else None, "year_projected_kwh": year[f]["total_kwh"] if year else None,
            "next12_kwh": next12[f]["kwh"], "next12_cost": next12[f]["cost"],
        }
    return k


def _patterns(fuels, hh, daily, now):
    out = {}
    cutoff = now - 90 * 86400
    for f in fuels:
        wk = [[0.0, 0] for _ in range(48)]
        we = [[0.0, 0] for _ in range(48)]
        grid = [[[0.0, 0] for _ in range(24)] for _ in range(7)]
        seasons = {"winter": [[0.0, 0] for _ in range(48)], "summer": [[0.0, 0] for _ in range(48)]}
        for ts, kwh, cost, d, slot, _ in hh[f]:
            mon = d.month
            s = "winter" if mon in (12, 1, 2) else "summer" if mon in (6, 7, 8) else None
            if s:
                seasons[s][slot][0] += kwh
                seasons[s][slot][1] += 1
            if ts < cutoff:
                continue
            bucket = we if d.weekday() >= 5 else wk
            bucket[slot][0] += kwh
            bucket[slot][1] += 1
            cell = grid[d.weekday()][slot // 2]
            cell[0] += kwh
            cell[1] += 0.5      # two half-hours make one hour
        avg = lambda b: [r(s / n, 3) if n else None for s, n in b]
        by_wd = [[0.0, 0] for _ in range(7)]
        recent_days = [(d, v) for d, v in daily[f].items() if v["complete"]][-365:]
        for d, v in recent_days:
            by_wd[d.weekday()][0] += v["kwh"]
            by_wd[d.weekday()][1] += 1
        out[f] = {
            "weekday": avg(wk), "weekend": avg(we),
            "winter": avg(seasons["winter"]), "summer": avg(seasons["summer"]),
            "week_hour": [[r(s / n, 3) if n else None for s, n in row] for row in grid],
            "by_weekday": [r(s / n, 2) if n else None for s, n in by_wd],
        }
    return out


def _peaks(fuels, hh, daily, models, climate, today):
    out = {"top_days": {}, "top_halfhours": [], "unusual": [], "peak_share": None}
    for f in fuels:
        comp = [(d, v) for d, v in daily[f].items() if v["complete"]]
        top = sorted(comp, key=lambda x: -x[1]["kwh"])[:10]
        out["top_days"][f] = [{"d": d.isoformat(), "kwh": r(v["kwh"], 1), "cost": r((v["cost"] + v["standing"]) / 100),
                               "t": r(climate.weather.get(d.isoformat(), (None,))[0], 1)} for d, v in top]
        m = models.get(f)
        if m and f != "export" and m.sd > 0:
            for d, v in comp:
                if d < today - timedelta(days=90):
                    continue
                w = climate.weather.get(d.isoformat())
                exp = m.predict(d, w[0] if w else None)
                if exp is None:
                    continue
                z = (v["kwh"] - exp) / m.sd
                if z > 2.0 and v["kwh"] - exp > 1:
                    out["unusual"].append({"d": d.isoformat(), "fuel": f, "kwh": r(v["kwh"], 1),
                                           "expected": r(exp, 1), "extra": r(v["kwh"] - exp, 1),
                                           "z": r(z, 1), "t": r(w[0], 1) if w else None})
    out["unusual"].sort(key=lambda x: x["d"], reverse=True)
    out["unusual"] = out["unusual"][:12]
    if "elec" in fuels:
        top = sorted(hh["elec"], key=lambda x: -x[1])[:10]
        out["top_halfhours"] = [{"ts": ts, "d": d.isoformat(), "time": local(ts).strftime("%H:%M"),
                                 "kwh": r(kwh, 2), "kw": r(kwh * 2, 1), "price": r(p, 1),
                                 "cost": r(c / 100)} for ts, kwh, c, d, slot, p in top]
        cutoff = hh["elec"][-1][0] - 30 * 86400 if hh["elec"] else 0
        tot_k = tot_c = pk = pc = 0.0
        for ts, kwh, c, d, slot, p in hh["elec"]:
            if ts < cutoff:
                continue
            tot_k += kwh
            tot_c += c
            if PEAK_START <= slot < PEAK_END:
                pk += kwh
                pc += c
        if tot_k:
            out["peak_share"] = {"kwh_share": r(pk / tot_k * 100, 1), "cost_share": r(pc / tot_c * 100, 1) if tot_c else None,
                                 "hours_share": r(6 / 48 * 100, 1)}
    return out


def _baseload(hh, daily, prices_now):
    """'Always on' power: the quietest night-time half-hours (midnight–6am), in watts.
    Night-only so solar panels don't hide it."""
    per_day = defaultdict(list)
    for ts, kwh, c, d, slot, p in hh["elec"]:
        if slot < 12:
            per_day[d].append(kwh)
    series = []
    for d, vals in sorted(per_day.items()):
        if len(vals) < 10:
            continue
        vals.sort()
        series.append((d, vals[1] * 2000))
    if not series:
        return None
    by_month = defaultdict(list)
    for d, w in series:
        by_month[month_key(d)].append(w)
    recent = [w for _, w in series[-30:]]
    watts = statistics.median(recent)
    unit = (prices_now.get("elec") or {}).get("unit_effective") or 0
    kwh_year = watts / 1000 * 8760
    recent_days = [v["kwh"] for d, v in list(daily["elec"].items())[-365:] if v["complete"]]
    avg_day = sum(recent_days) / len(recent_days) if recent_days else 0     # per-day, so short histories compare fairly
    return {"watts": round(watts), "kwh_year": round(kwh_year), "cost_year": r(kwh_year * unit / 100, 0),
            "share": r(watts * 24 / 1000 / avg_day * 100, 0) if avg_day else None,
            "monthly": [{"month": k, "watts": round(statistics.median(v))} for k, v in sorted(by_month.items())],
            "daily": [{"d": d.isoformat(), "watts": round(w)} for d, w in series[-365:]]}


def _weather_section(fuels, daily, models, climate, prices_now, today):
    if not climate.available:
        return None
    out = {"fuels": {}, "next_days": []}
    for f in fuels:
        m = models.get(f)
        pts = []
        for d, v in list(daily[f].items())[-365:]:
            w = climate.weather.get(d.isoformat())
            if w and v["complete"] and not w[1]:
                pts.append([r(w[0], 1), r(v["kwh"], 2), d.isoformat()])
        line = []
        if m and m.kind == "weather":
            for t in range(-4, 27):
                line.append([t, r(m._raw(date(2001, 1, 3), t), 2)])   # a weekday
        unit = (prices_now.get(f) or {}).get("unit_effective") or 0
        slope = m.slope if m and m.kind == "weather" else None
        out["fuels"][f] = {"points": pts, "line": line, "slope": r(slope, 3),
                           "slope_cost": r(slope * unit / 100, 3) if slope is not None else None,
                           "provisional": m.provisional if m else None, "r2": r(m.r2) if m else None}
    for i in range(0, 14):
        d = today + timedelta(days=i)
        w = climate.weather.get(d.isoformat())
        if not w:
            continue
        item = {"d": d.isoformat(), "t": r(w[0], 1), "normal": r(climate.normal.get((d.month, d.day)), 1)}
        for f in fuels:
            m = models.get(f)
            if m:
                p = m.predict(d, w[0])
                item[f] = r(p, 1)
                u, st = price_on(prices_now.get(f) or {}, d)
                item[f + "_cost"] = r((p * u + st) / 100)
        out["next_days"].append(item)
    return out


def _agile_code(db, ct, now):
    current = ct.tariff("elec", now)
    if current and "AGILE" in current:
        return current, True
    for c in db.get("compare_tariffs", []) or []:
        if c["name"] == "Agile Octopus":
            return c["elec"], False
    return None, False


def _agile(db, rb, ct, hh, now, today):
    code, on_agile = _agile_code(db, ct, now)
    if not code:
        return None
    start = day_start_ts(today)
    froms, tos, vals = rb._series(code, "unit")
    i = bisect.bisect_left(froms, start)
    slots = [{"ts": froms[j], "p": r(vals[j])} for j in range(i, len(froms))]
    if not slots:
        return None
    upcoming = [s for s in slots if s["ts"] + 1800 > now]

    def best(hours):
        n = hours * 2
        best_avg, best_ts = None, None
        for j in range(0, len(upcoming) - n + 1):
            window = upcoming[j:j + n]
            if any(window[k + 1]["ts"] - window[k]["ts"] != 1800 for k in range(n - 1)):
                continue
            avg = sum(s["p"] for s in window) / n
            if best_avg is None or avg < best_avg:
                best_avg, best_ts = avg, window[0]["ts"]
        return {"hours": hours, "ts": best_ts, "avg": r(best_avg)} if best_ts else None

    out = {"tariff": code, "on_agile": on_agile, "slots": slots,
           "cheapest": [b for b in (best(h) for h in (1, 2, 3, 4)) if b],
           "negative": [s for s in upcoming if s["p"] < 0],
           "today_avg": r(statistics.mean([s["p"] for s in slots if s["ts"] < start + 86400])) if slots else None}
    if on_agile and hh.get("elec"):
        recent = [x for x in hh["elec"] if x[0] >= now - 30 * 86400 and x[5] is not None]
        if recent:
            paid = sum(x[2] for x in recent) / max(1e-9, sum(x[1] for x in recent))
            simple = statistics.mean(x[5] for x in recent)
            out["your_avg"] = r(paid)
            out["market_avg"] = r(simple)
    return out


def _carbon(db, hh, daily, now, today):
    regional = db.carbon()
    rows = db.carbon("carbon_national") or regional
    reg = [v for ts, v, fc in regional if not fc and ts >= now - 28 * 86400]
    intensity = {ts: v for ts, v, fc in rows}
    out = {"forecast": [], "daily": [], "greenest": None}
    if "elec" in hh:
        recent = [x for x in hh["elec"] if x[0] >= now - 28 * 86400]
        matched = [(kwh, intensity.get(ts)) for ts, kwh, *_ in recent]
        matched = [(k, i) for k, i in matched if i is not None]
        if matched:
            your = sum(k * i for k, i in matched) / max(1e-9, sum(k for k, _ in matched))
            grid = statistics.mean(i for _, i in matched)
            out["your_intensity"] = r(your, 0)
            out["grid_intensity"] = r(grid, 0)
        by_day = defaultdict(lambda: [0.0, 0.0])
        for ts, kwh, c, d, slot, p in recent:
            i = intensity.get(ts)
            if i is not None:
                by_day[d][0] += kwh * i / 1000
        if "gas" in daily:
            for d, v in daily["gas"].items():
                if d in by_day:
                    by_day[d][1] = v["kwh"] * GAS_CO2
        out["daily"] = [{"d": d.isoformat(), "elec": r(a, 2), "gas": r(b, 2)} for d, (a, b) in sorted(by_day.items())]
    factor = (out.get("your_intensity") or ELEC_CO2_DEFAULT * 1000) / 1000
    year = {}
    for f, k in (("elec", factor), ("gas", GAS_CO2)):
        if f in daily:
            days = list(daily[f].values())[-365:]
            year[f] = r(sum(v["kwh"] for v in days) * k, 0)
    out["year_kg"] = year
    out["year_basis_days"] = min((min(365, len(daily[f])) for f in year), default=0)
    fc = [{"ts": ts, "g": v} for ts, v, f in rows if f and ts + 1800 > now]
    out["forecast"] = fc
    if len(fc) >= 6:
        best = min(range(len(fc) - 5), key=lambda j: sum(x["g"] for x in fc[j:j + 6]))
        out["greenest"] = {"ts": fc[best]["ts"], "avg": r(sum(x["g"] for x in fc[best:best + 6]) / 6, 0)}
    out["available"] = bool(rows)
    out["basis"] = "national" if db.carbon("carbon_national") else "regional"
    out["regional_avg"] = r(statistics.mean(reg), 0) if reg else None
    return out


def _compare(db, rb, ct, hh, daily, prices_now, now):
    cands = db.get("compare_tariffs") or []
    if not cands or "elec" not in hh or not hh["elec"]:
        return None
    end_day = max(d for d, v in daily["elec"].items() if v["complete"])
    start_day = end_day - timedelta(days=89)
    a, b = day_start_ts(start_day), day_start_ts(end_day + timedelta(days=1))
    sel = {f: [x for x in hh.get(f, []) if a <= x[0] < b] for f in ("elec", "gas")}
    days = (end_day - start_day).days + 1

    def actual(f):
        if not sel[f]:
            return 0.0
        unit = sum(x[2] for x in sel[f])
        standing = sum(v["standing"] for d, v in daily[f].items() if start_day <= d <= end_day)
        return (unit + standing) / 100

    act = {f: actual(f) for f in ("elec", "gas")}
    rows = []
    for c in cands:
        item = {"name": c["name"], "blurb": c["blurb"], "variable": c["variable"], "product": c["product"],
                "current": "elec" in prices_now and c["product"] == product_code(prices_now["elec"]["tariff"]),
                "estimated_history": False}
        total = 0.0
        for f, code in (("elec", c["elec"]), ("gas", c.get("gas"))):
            if not sel[f]:
                continue
            if not code:
                total += act[f]
                item[f] = None
                continue
            lo, _ = rb.span(code)
            if lo is None:
                total += act[f]
                item[f] = None
                continue
            if lo > a:
                item["estimated_history"] = True
            cost = sum(kwh * (rb.unit(code, ts, extend=True) or 0) for ts, kwh, *_ in sel[f])
            sc = sum(rb.standing(code, day_start_ts(start_day + timedelta(days=i)) + 43200, extend=True) or 0
                     for i in range(days))
            item[f] = r((cost + sc) / 100)
            total += (cost + sc) / 100
        item["total"] = r(total)
        rows.append(item)
    yours = sum(act.values())
    for row in rows:
        row["diff"] = r(row["total"] - yours)
        row["diff_year"] = r((row["total"] - yours) * 365 / days, 0)
    rows.sort(key=lambda x: x["total"])
    return {"from": start_day.isoformat(), "to": end_day.isoformat(), "days": days,
            "yours": r(yours), "yours_elec": r(act["elec"]), "yours_gas": r(act["gas"]), "rows": rows}


def _payment_plan(bal, monthly, fuels, today, daily=None, bills_check=None):
    """What monthly direct debit keeps the balance above £0 for the next 12 months?

    Starts from your balance at the last sync, takes away each month's predicted bills
    (the whole of this month, in case none of it is billed yet), adds solar export earnings,
    and adds one payment per month on your direct-debit day.
    """
    if not bal:
        return None
    dd = bal.get("direct_debit") or {}
    octopus = {"suggested_dd": bal.get("suggested_dd"), "minimum_dd": bal.get("minimum_dd"),
               "projected_balance": bal.get("projected_balance"),
               "should_review": bal.get("should_review_payments")}
    if not fuels:                       # nothing to predict bills from, so no payment plan
        return {"balance": bal["balance"], "overdue": bal.get("overdue", 0), "at": bal.get("at"),
                "direct_debit": dd or None, "octopus": octopus}
    pay_day = dd.get("day") or 1
    start = date(today.year, today.month, 1)
    months = [m for m in monthly if m["month"] >= month_key(start)][:12]
    bills = [f for f in fuels if f != "export"]

    # What's already been used but not yet billed? Octopus's last charge date tells us.
    last_billed = (bills_check or {}).get("last_billed") or {}
    unbilled = None
    if daily and last_billed:
        unbilled = 0.0
        for f in fuels:
            lb = last_billed.get(f)
            if not lb:
                continue
            lb = date.fromisoformat(lb)
            sign = -1 if f == "export" else 1
            unbilled += sign * sum((v["cost"] + v["standing"]) / 100 for d, v in daily[f].items() if d > lb and v["complete"])

    def costs(cautious):
        out = []
        for i, m in enumerate(months):
            if i == 0 and unbilled is not None:
                # this month = everything used since the last bill + the predicted rest of the month
                c = unbilled + sum((m[f]["fcost"] * (m[f]["high_cost"] / m[f]["total_cost"] if cautious and m[f]["total_cost"] else 1)) or 0 for f in bills)
                if "export" in fuels:
                    c -= m["export"]["fcost"] or 0
            else:
                c = sum((m[f]["high_cost"] if cautious else m[f]["total_cost"]) or 0 for f in bills)
                if "export" in fuels:
                    c -= (m["export"]["low_cost"] if cautious else m["export"]["total_cost"]) or 0
            out.append(c)
        return out

    # payment in this month only if its debit day hasn't passed yet
    payments = [0 if (i == 0 and today.day >= pay_day) else 1 for i in range(len(months))]
    paid_by = [sum(payments[:i + 1]) for i in range(len(months))]

    def needed(cs, end_at=None):
        need, cum = 0.0, 0.0
        for i, c in enumerate(cs):
            cum += c
            if paid_by[i]:
                need = max(need, (cum - bal["balance"]) / paid_by[i])
        if end_at is not None:   # finish the 12 months with this balance
            need = max(need, (cum - bal["balance"] + end_at) / paid_by[-1])
        return max(0.0, math.ceil(need))

    def path(cs, p):
        b, out = bal["balance"], []
        for i, c in enumerate(cs):
            b += p * payments[i] - c
            out.append(round(b, 2))
        return out

    typical, cautious = costs(False), costs(True)
    plan = {
        "balance": bal["balance"], "overdue": bal.get("overdue", 0), "at": bal.get("at"),
        "direct_debit": dd or None,
        "months": [m["month"] for m in months],
        "cost": [round(c, 2) for c in typical], "cost_cautious": [round(c, 2) for c in cautious],
        "payments": payments,
        "min_typical": needed(typical), "min_cautious": needed(cautious),
        "keep_balance": needed(typical, end_at=bal["balance"]),
        "total_12": round(sum(typical), 0),
        "unbilled": round(unbilled, 2) if unbilled is not None else None,
        "last_billed": max(last_billed.values()) if last_billed else None,
        "octopus": octopus,
    }
    plan["path_min"] = path(typical, plan["min_typical"])
    plan["path_cautious"] = path(cautious, plan["min_cautious"])
    if dd.get("amount") is not None:
        cur = path(typical, dd["amount"])
        plan["path_current"] = cur
        plan["path_current_cautious"] = path(cautious, dd["amount"])
        neg = next((i for i, b in enumerate(cur) if b < 0), None)
        plan["current_goes_negative"] = plan["months"][neg] if neg is not None else None
        plan["current_lowest"] = min(cur) if cur else None
        plan["current_lowest_month"] = plan["months"][cur.index(min(cur))] if cur else None
        plan["current_end"] = cur[-1] if cur else None
    return plan


def _transactions(db):
    tx = db.get("transactions")
    if not tx:
        return None
    return [{k: t[k] for k in ("date", "type", "title", "amount", "balance", "reversed")} for t in tx]


def _cost_on(rb, tariff, hh_rows, days):
    """Unit + standing cost (£) of the given half-hours on a tariff, using its prices at the time."""
    unit = sum(kwh * (rb.unit(tariff, ts, extend=True) or 0) for ts, kwh, *_ in hh_rows)
    standing = sum(rb.standing(tariff, day_start_ts(d) + 43200, extend=True) or 0 for d in days)
    return (unit + standing) / 100


def _renewal(db, rb, prices_now, models, climate, today, now):
    """Your fix ends soon: what would the next 12 months cost on each option?"""
    ends = min((p["ends"] for f, p in prices_now.items() if f in ("elec", "gas") and p.get("ends")), default=None)
    offers = db.get("fixed_offers") or []
    if not ends:
        return None
    start = date.fromisoformat(ends)
    days = [start + timedelta(days=i) for i in range(365)]
    kwh = {}
    for f in ("elec", "gas"):
        m = models.get(f)
        if m and f in prices_now:
            kwh[f] = sum((m.predict(d, climate.temp(d)[0] if climate.available else None) or 0) for d in days)
    if not kwh:                         # no usage to price the options on
        return None

    def price(code, f):
        if not code:
            return None
        u, sc = rb.unit(code, now, extend=True), rb.standing(code, now, extend=True)
        if u is None:
            return None
        return {"unit": r(u), "standing": r(sc or 0), "cost": r((kwh[f] * u + 365 * (sc or 0)) / 100)}

    options = []
    for o in offers:
        row = {"name": o["name"], "kind": "fixed", "term": o.get("term"), "exit_fee": o.get("exit_fee"),
               "exit_fee_type": o.get("exit_fee_type")}
        for f in kwh:
            row[f] = price(o.get(f), f)
        options.append(row)
    flex = next((c for c in db.get("compare_tariffs") or [] if c["name"] == "Flexible Octopus"), None)
    if flex:
        options.append({"name": "Flexible Octopus", "kind": "rollover", **{f: price(flex.get(f), f) for f in kwh}})
    tracker = (db.get("history_tariffs") or {}).get("Octopus Tracker")
    if tracker:
        t = tracker[-1]
        row = {"name": "Octopus Tracker", "kind": "tracker"}
        for f in kwh:
            code = t.get(f)
            if not code:
                row[f] = None
                continue
            froms, _, vals = rb._series(code, "unit")
            recent = [v for a, v in zip(froms, vals) if a > now - 90 * 86400]
            if not recent:
                row[f] = None
                continue
            u = sum(recent) / len(recent)
            sc = rb.standing(code, now, extend=True) or 0
            row[f] = {"unit": r(u), "standing": r(sc), "cost": r((kwh[f] * u + 365 * sc) / 100), "average": True}
        options.append(row)
    current = {"name": "Your current fix (if renewed at the same price)", "kind": "current"}
    for f in kwh:
        p = prices_now[f]
        current[f] = {"unit": p["unit_now"], "standing": p["standing"],
                      "cost": r((kwh[f] * (p["unit_now"] or 0) + 365 * (p["standing"] or 0)) / 100)}
    options.append(current)
    for o in options:
        parts = [o.get(f) for f in kwh]
        o["total"] = r(sum(x["cost"] for x in parts if x)) if all(parts) else None
    base = next((o["total"] for o in options if o["kind"] == "rollover"), None)
    for o in options:
        o["vs_rollover"] = r(o["total"] - base) if base is not None and o["total"] is not None else None
    options.sort(key=lambda o: (o["total"] is None, o["total"] or 0))
    bal = db.get("balance") or {}
    return {"ends": ends, "days_left": (start - today).days, "can_renew": bal.get("can_renew"),
            "kwh": {f: r(v, 0) for f, v in kwh.items()}, "options": options}


def _price_history(db, rb, hh, daily, fuels):
    """Did fixing pay off? Your real usage priced on Flexible and Tracker at the prices of the time."""
    hist = db.get("history_tariffs") or {}
    if not hist:
        return None

    def tracker_code(entries, f, ts):
        for e in entries:
            if (e["from"] is None or e["from"] <= ts) and (e["to"] is None or ts < e["to"]):
                return e.get(f)
        return None

    out = {"fuels": {}, "since": None}
    for f in ("elec", "gas"):
        if f not in fuels or not hh.get(f):
            continue
        rows = hh[f]
        months = defaultdict(lambda: {"kwh": 0.0, "yours": 0.0, "days": set()})
        alt = {name: defaultdict(float) for name in hist}
        for ts, kwh, cost, d, slot, p in rows:
            mk = month_key(d)
            mo = months[mk]
            mo["kwh"] += kwh
            mo["yours"] += cost
            mo["days"].add(d)
            for name, entries in hist.items():
                code = entries[0].get(f) if name != "Octopus Tracker" else tracker_code(entries, f, ts)
                if code:
                    alt[name][mk] += kwh * (rb.unit(code, ts, extend=name != "Octopus Tracker") or 0)
        series = []
        totals = {"yours": 0.0, **{n: 0.0 for n in hist}}
        for mk in sorted(months):
            mo = months[mk]
            item = {"month": mk, "kwh": r(mo["kwh"], 1)}
            standing_yours = sum(daily[f][d]["standing"] for d in mo["days"]) / 100
            item["yours"] = r(mo["yours"] / 100 + standing_yours)
            item["yours_ppk"] = r(mo["yours"] / mo["kwh"], 2) if mo["kwh"] else None
            totals["yours"] += item["yours"]
            for name, entries in hist.items():
                sc = 0.0
                for d in mo["days"]:
                    ts = day_start_ts(d) + 43200
                    code = entries[0].get(f) if name != "Octopus Tracker" else tracker_code(entries, f, ts)
                    sc += (rb.standing(code, ts, extend=True) or 0) if code else 0
                cost = alt[name][mk] / 100 + sc / 100
                item[name] = r(cost)
                item[name + "_ppk"] = r(alt[name][mk] / mo["kwh"], 2) if mo["kwh"] else None
                totals[name] += cost
            series.append(item)
        out["fuels"][f] = {"months": series, "totals": {k: r(v) for k, v in totals.items()}}
        out["since"] = min(out["since"] or series[0]["month"], series[0]["month"])
    out["names"] = list(hist)
    return out


def _solar(db, rb, hh):
    """What your solar export earns, and what other export tariffs would have paid."""
    exp = hh.get("export") or []
    if not exp:
        return None
    alts = db.get("export_alternatives") or []
    months = defaultdict(lambda: defaultdict(float))
    for ts, kwh, cost, d, slot, p in exp:
        mk = month_key(d)
        mo = months[mk]
        mo["export_kwh"] += kwh
        mo["earned"] += cost / 100
        for a in alts:
            mo[a["name"]] += kwh * (rb.unit(a["tariff"], ts) or 0) / 100
    series = [{"month": mk, **{k: r(v, 2) for k, v in mo.items()}} for mk, mo in sorted(months.items())]
    export_kwh = sum(m["export_kwh"] for m in series)
    earned = sum(m["earned"] for m in series)
    return {
        "months": series, "export_kwh": r(export_kwh, 0), "earned": r(earned),
        "alternatives": [{"name": a["name"], "earned": r(sum(m.get(a["name"], 0) for m in series))} for a in alts],
        "since": series[0]["month"] if series else None,
    }


CHARGE_FUEL = {"Gas": "gas", "Electricity": "elec"}


def _charges(transactions):
    """Octopus's charges per fuel, oldest first. Export credits show up as negative electricity charges."""
    out = defaultdict(list)
    for t in transactions or []:
        if t["type"] != "Charge" or t.get("reversed") or t["title"] not in CHARGE_FUEL:
            continue
        fuel = CHARGE_FUEL[t["title"]]
        if fuel == "elec" and t["amount"] < 0:
            fuel = "export"
        out[fuel].append(t)
    for f in out:
        out[f].sort(key=lambda t: t["date"])
    return out


def _bill_check(db, daily, fuels, shift=0):
    """Re-works each Octopus charge from your half-hourly readings and compares.

    A charge posted on date D covers the days after the previous charge for that fuel, up to D."""
    tx = db.get("transactions")
    if not tx:
        return None
    charges = _charges(tx)
    statements = {s["id"]: s for s in db.get("statements") or []}
    rows = {}
    last_billed = {}
    for f, items in charges.items():
        if f not in daily or not daily[f]:
            continue
        first_day = min(daily[f])
        prev = None
        for t in items:
            end = date.fromisoformat(t["date"]) + timedelta(days=shift)
            start = (prev + timedelta(days=1)) if prev else first_day
            prev = end
            ours, days, missing = 0.0, 0, 0
            d = start
            while d <= end:
                v = daily[f].get(d)
                if v:
                    ours += (v["cost"] + v["standing"]) / 100
                    days += 1
                    missing += 0 if v["complete"] else 1
                elif d >= first_day:
                    missing += 1
                d += timedelta(days=1)
            octo = abs(t["amount"])
            key = t.get("statement") or t["date"]
            st = statements.get(t.get("statement")) or {}
            row = rows.setdefault(key, {"statement": key, "issued": st.get("issued") or t["date"],
                                        "from": start.isoformat(), "to": end.isoformat(), "fuels": {}})
            row["from"] = min(row["from"], start.isoformat())
            row["to"] = max(row["to"], end.isoformat())
            fr = row["fuels"].setdefault(f, {"octopus": 0.0, "ours": 0.0, "days": 0, "missing": 0})
            fr["octopus"] += octo
            fr["ours"] += ours
            fr["days"] += days
            fr["missing"] += missing
            last_billed[f] = max(last_billed.get(f, ""), end.isoformat())
    out = []
    for row in sorted(rows.values(), key=lambda r: r["to"], reverse=True):
        sign = lambda f: -1 if f == "export" else 1
        row["octopus_net"] = round(sum(sign(f) * v["octopus"] for f, v in row["fuels"].items()), 2)
        row["ours_net"] = round(sum(sign(f) * v["ours"] for f, v in row["fuels"].items()), 2)
        row["diff"] = round(row["octopus_net"] - row["ours_net"], 2)
        for v in row["fuels"].values():
            v["octopus"], v["ours"] = round(v["octopus"], 2), round(v["ours"], 2)
            v["diff"] = round(v["octopus"] - v["ours"], 2)
        row["missing"] = sum(v["missing"] for v in row["fuels"].values())
        big = abs(row["diff"]) > max(1.0, 0.08 * abs(row["octopus_net"]))
        row["flag"] = "check" if big and not row["missing"] else "gaps" if row["missing"] else "ok"
        out.append(row)
    matched = [r for r in out if not r["missing"]]
    return {"rows": out, "last_billed": last_billed,
            "octopus_total": round(sum(r["octopus_net"] for r in matched), 2),
            "ours_total": round(sum(r["ours_net"] for r in matched), 2)}


def _quality(fuels, daily, today):
    out = {}
    for f in fuels:
        days = daily[f]
        if not days:
            continue
        first, last = min(days), max(days)
        span = (last - first).days + 1
        recent_start = max(first, today - timedelta(days=365))
        missing = sum(1 for i in range((last - recent_start).days + 1)
                      if not (days.get(recent_start + timedelta(days=i)) or {}).get("complete"))
        out[f] = {"first": first.isoformat(), "last": last.isoformat(), "days": len(days),
                  "span": span, "missing_365": missing}
    return out


def _fmt_money(x):
    return f"£{x:,.0f}" if abs(x) >= 100 else f"£{x:,.2f}"


def _insights(out, daily, models, prices_now, today):
    tips = []
    for f in ("elec", "gas"):
        p = prices_now.get(f) or {}
        if not p.get("ends"):
            continue
        ends = date.fromisoformat(p["ends"])
        days_left = (ends - today).days
        when = ends.strftime("%-d %B %Y")
        names_ = {"elec": "electricity", "gas": "gas"}
        text = f"Your {p['name']} {names_[f]} deal ends on {when} ({days_left} days)."
        roll = p.get("rollover")
        if roll and roll.get("unit") is not None:
            kwh_year = (out["kpis"]["per_fuel"].get(f) or {}).get("next12_kwh") or 0
            diff = (kwh_year * (roll["unit"] - (p.get("unit_effective") or 0)) + 365 * (roll["standing"] - (p.get("standing") or 0))) / 100
            text += (f" If you roll onto {roll['name']} at today's prices ({roll['unit']:.1f}p/kWh) "
                     f"it would cost about {_fmt_money(abs(diff))} a year {'more' if diff > 0 else 'less'} – predictions already allow for this.")
        tips.append({"tone": "warn" if days_left <= 60 else "info", "text": text})
    names = {"elec": "electricity", "gas": "gas", "export": "solar export"}
    monthly = out["monthly"]
    past = [m for m in monthly if m["status"] == "past"]
    if past:
        last = past[-1]
        ly = next((m for m in monthly if m["month"] == f"{int(last['month'][:4]) - 1}{last['month'][4:]}"), None)
        mname = datetime.strptime(last["month"], "%Y-%m").strftime("%B")
        for f in ("elec", "gas"):
            if ly and f in last and ly.get(f) and ly[f]["days"] >= ly["days"] - 2 and last[f]["days"] >= last["days"] - 2 and ly[f]["kwh"]:
                pct = (last[f]["kwh"] - ly[f]["kwh"]) / ly[f]["kwh"] * 100
                if abs(pct) >= 3:
                    word = "less" if pct < 0 else "more"
                    tone = "good" if pct < 0 else "warn"
                    tips.append({"tone": tone, "text": f"You used {abs(pct):.0f}% {word} {names[f]} in {mname} than in {mname} last year "
                                                       f"({last[f]['kwh']:,.0f} vs {ly[f]['kwh']:,.0f} kWh)."})
    k = out["kpis"]
    if k.get("year_projected") and k.get("last_year"):
        diff = k["year_projected"] - k["last_year"]
        tips.append({"tone": "good" if diff < 0 else "warn",
                     "text": f"{k['year']} is on track to cost {_fmt_money(k['year_projected'])} – "
                             f"{_fmt_money(abs(diff))} {'less' if diff < 0 else 'more'} than {k['year'] - 1}."})
    bl = out.get("baseload")
    if bl and bl.get("cost_year"):
        tips.append({"tone": "info" if bl["watts"] < 150 else "warn",
                     "text": f"Your home never drops below about {bl['watts']} W – fridges, routers, standby. "
                             f"That ‘always on’ load costs roughly {_fmt_money(bl['cost_year'])} a year"
                             + (f" ({bl['share']:.0f}% of your electricity)." if bl.get("share") else ".")})
    w = out.get("weather")
    if w and w["fuels"].get("gas", {}).get("slope_cost"):
        g = w["fuels"]["gas"]
        tips.append({"tone": "info", "text": f"Each degree colder adds about {g['slope']:.1f} kWh of gas a day "
                                             f"(≈ {g['slope_cost'] * 100:.0f}p). A winter month 2°C colder than normal "
                                             f"costs around {_fmt_money(g['slope_cost'] * 2 * 30)} more."})
    ps = out["peaks"].get("peak_share")
    if ps and ps["kwh_share"] and ps["kwh_share"] > 20:
        tips.append({"tone": "warn" if out.get("agile") and out["agile"]["on_agile"] else "info",
                     "text": f"{ps['kwh_share']:.0f}% of your electricity is used 4–7pm (that's only 12.5% of the day)"
                             + (f", which is {ps['cost_share']:.0f}% of the cost – the priciest time on Agile."
                                if out.get("agile") and out["agile"]["on_agile"] and ps.get("cost_share") else ".")})
    ag = out.get("agile")
    if ag and ag.get("your_avg") is not None:
        diff = ag["market_avg"] - ag["your_avg"]
        tips.append({"tone": "good" if diff > 0 else "warn",
                     "text": f"On Agile you've paid {ag['your_avg']:.1f}p/kWh on average over 30 days, vs "
                             f"{ag['market_avg']:.1f}p if you'd used power evenly – "
                             + ("nice load-shifting." if diff > 0 else "try moving big loads to cheaper slots.")})
    pay = out.get("payments")
    if pay and pay.get("months") and pay.get("direct_debit"):
        dd = pay["direct_debit"]["amount"]
        if pay.get("current_goes_negative"):
            when = datetime.strptime(pay["current_goes_negative"], "%Y-%m").strftime("%B %Y")
            tips.insert(0, {"tone": "warn", "text": f"On your £{dd:.0f} direct debit your balance is predicted to go below £0 in {when}. "
                                                    f"Paying £{pay['min_typical']:.0f} a month would keep it above £0 all year (£{pay['min_cautious']:.0f} for a cold winter)."})
        else:
            low_when = datetime.strptime(pay["current_lowest_month"], "%Y-%m").strftime("%B")
            tips.insert(0, {"tone": "good", "text": f"Your £{dd:.0f} direct debit should keep you in credit all year – lowest point about "
                                                    f"{_fmt_money(pay['current_lowest'])} in {low_when}."})
    cmp_ = out.get("compare")
    if cmp_ and cmp_["rows"]:
        best = cmp_["rows"][0]
        if best["diff"] < -5 and not best["current"]:
            tips.append({"tone": "info", "text": f"{best['name']} would have cost {_fmt_money(-best['diff'])} less over the last "
                                                 f"{cmp_['days']} days (about {_fmt_money(-best['diff_year'])} a year) – see the tariff comparison."})
    unusual = out["peaks"].get("unusual") or []
    if unusual:
        u = unusual[0]
        when = datetime.strptime(u["d"], "%Y-%m-%d").strftime("%a %-d %b")
        tips.append({"tone": "warn", "text": f"{when}: {u['kwh']} kWh of {names[u['fuel']]} – about {u['extra']} kWh more "
                                             f"than normal for that weather. Anything unusual that day?"})
    pat = out["patterns"].get("elec")
    if pat and all(x is not None for x in pat["by_weekday"]):
        wd = sum(pat["by_weekday"][:5]) / 5
        we = sum(pat["by_weekday"][5:]) / 2
        if wd and abs(we - wd) / wd > 0.08:
            tips.append({"tone": "info", "text": f"You use {abs(we - wd) / wd * 100:.0f}% {'more' if we > wd else 'less'} electricity "
                                                 f"on weekend days ({we:.1f} kWh) than weekdays ({wd:.1f} kWh)."})
    top = out["peaks"]["top_days"].get("elec") or []
    if top:
        t = top[0]
        when = datetime.strptime(t["d"], "%Y-%m-%d").strftime("%-d %B %Y")
        tips.append({"tone": "info", "text": f"Your biggest electricity day was {when}: {t['kwh']} kWh (£{t['cost']:.2f})."})
    return tips


# ---------------------------------------------------------------- one day in detail

def day_detail(db, ds):
    d = date.fromisoformat(ds)
    a = day_start_ts(d)
    b = day_start_ts(d + timedelta(days=1))
    rb = RateBook(db)
    ct = Contracts(db.agreements())
    carbon = {ts: v for ts, v, _ in (db.carbon("carbon_national") or db.carbon())}
    w = db.weather().get(ds)
    out = {"d": ds, "t": w[0] if w else None, "slots": []}
    per = {}
    for f in db.fuels():
        with db.connect() as c:
            per[f] = {row[0]: row[1] for row in c.execute(
                "SELECT ts, kwh FROM readings WHERE fuel=? AND ts>=? AND ts<?", (f, a, b))}
    ts = a
    while ts < b:
        item = {"ts": ts, "time": local(ts).strftime("%H:%M"), "carbon": carbon.get(ts)}
        for f, vals in per.items():
            kwh = vals.get(ts)
            tariff = ct.tariff(f, ts)
            p = rb.unit(tariff, ts, extend=True) if tariff else None
            item[f] = r(kwh, 3)
            item[f + "_p"] = r(p)
            item[f + "_cost"] = r(kwh * p / 100, 3) if kwh is not None and p is not None else None
        out["slots"].append(item)
        ts += 1800
    totals = {}
    for f in per:
        k = sum(s[f] or 0 for s in out["slots"])
        c = sum(s[f + "_cost"] or 0 for s in out["slots"])
        tariff = ct.tariff(f, a + 43200)
        sc = (rb.standing(tariff, a + 43200, extend=True) or 0) / 100 if tariff and f != "export" else 0
        totals[f] = {"kwh": r(k, 2), "cost": r(c + sc), "standing": r(sc)}
    out["totals"] = totals
    return out
