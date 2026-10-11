"""Makes up a realistic household's data so the dashboard can be tried without an API key.

The pretend home: gas central heating, 4 kW of solar panels, an electric car charged
twice a week, switched from Flexible Octopus to Agile in March 2025 (and started
charging the car overnight when prices are cheap). Two weeks away every August.

Run directly to rebuild:   python demo.py
"""
import math
import os
import random
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from db import DB
from sync import M3_TO_KWH

LON = ZoneInfo("Europe/London")
UTC = timezone.utc
HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "data", "demo.db")

START = date(2024, 4, 1)
AGILE_FROM = date(2025, 3, 1)
FLEX_E = "E-1R-VAR-22-11-01-C"
FLEX_G = "G-1R-VAR-22-11-01-C"
AGILE = "E-1R-AGILE-24-10-01-C"
EXPORT = "E-1R-OUTGOING-VAR-24-10-26-C"
TRACKER_E = "E-1R-SILVER-25-04-15-C"
TRACKER_G = "G-1R-SILVER-25-04-15-C"
FIXED_E = "E-1R-OE-FIX-12M-26-10-02-C"
FIXED_G = "G-1R-OE-FIX-12M-26-10-02-C"
GO = "E-1R-GO-VAR-22-10-14-C"

# (quarter start, elec p/kWh, elec standing p/day, gas p/kWh, gas standing p/day) – price-cap-ish
FLEX_PRICES = [
    (date(2024, 4, 1), 24.50, 60.1, 6.04, 31.4), (date(2024, 7, 1), 22.36, 60.1, 5.48, 31.6),
    (date(2024, 10, 1), 24.50, 60.9, 6.24, 31.7), (date(2025, 1, 1), 24.86, 61.1, 6.34, 31.6),
    (date(2025, 4, 1), 27.03, 53.8, 6.99, 32.7), (date(2025, 7, 1), 25.73, 51.4, 6.33, 29.8),
    (date(2025, 10, 1), 26.35, 53.7, 6.29, 34.0), (date(2026, 1, 1), 27.69, 54.8, 6.11, 35.1),
    (date(2026, 4, 1), 25.12, 47.6, 5.86, 31.3), (date(2026, 7, 1), 24.03, 41.5, 7.14, 30.0),
    (date(2026, 10, 1), 24.88, 41.4, 7.76, 30.3),
]


def ts_of(d, hour=0, minute=0):
    return int(datetime(d.year, d.month, d.day, hour, minute, tzinfo=LON).timestamp())


def daterange(a, b):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def temps(rng, first, last):
    out = {}
    noise = 0.0
    for d in daterange(first, last):
        doy = d.timetuple().tm_yday
        noise = 0.75 * noise + rng.gauss(0, 1.6)
        out[d] = 10.6 - 6.4 * math.cos(2 * math.pi * (doy - 22) / 365) + noise
    return out


def agile_day(rng, d, temp):
    """48 half-hour Agile prices for one local day."""
    wind = rng.random()                          # 0 calm … 1 very windy
    summer = max(0.0, math.cos(2 * math.pi * (d.timetuple().tm_yday - 172) / 365))
    base = 22 + (8 - temp) * 0.35 - wind * 7
    out = []
    for slot in range(48):
        h = slot / 2
        if h < 5:
            p = base - 4
        elif h < 7:
            p = base
        elif h < 10:
            p = base + 4
        elif h < 16:
            p = base + 1 - summer * 6 * math.sin(math.pi * (h - 10) / 6)
        elif h < 19:
            p = base * 1.15 + 13
        elif h < 22:
            p = base + 4
        else:
            p = base - 1
        p += rng.gauss(0, 1.3)
        if wind > 0.93 and (h < 6 or 11 <= h < 15):
            p = -abs(rng.gauss(2, 3))            # "plunge pricing"
        out.append(round(min(p, 99), 2))
    return out


def build():
    rng = random.Random(42)
    if os.path.exists(PATH):
        os.remove(PATH)
    db = DB(PATH)
    today = datetime.now(LON).date()
    yesterday = today - timedelta(days=1)

    # ---- weather: 6 years of history + 16-day "forecast" ----
    t = temps(rng, today - timedelta(days=6 * 365), today + timedelta(days=15))
    db.put_weather([(d.isoformat(), round(v, 1), 1 if d >= today else 0) for d, v in t.items()])

    # ---- prices ----
    def flex_rows(idx):
        rows = []
        for i, q in enumerate(FLEX_PRICES):
            a = ts_of(q[0])
            b = ts_of(FLEX_PRICES[i + 1][0]) if i + 1 < len(FLEX_PRICES) else None
            rows.append((a, b, q[idx]))
        return rows

    db.put_rates(FLEX_E, "unit", flex_rows(1))
    db.put_rates(FLEX_E, "standing", flex_rows(2))
    db.put_rates(FLEX_G, "unit", flex_rows(3))
    db.put_rates(FLEX_G, "standing", flex_rows(4))
    db.put_rates(EXPORT, "unit", [(ts_of(START), None, 15.0)])
    db.put_rates(EXPORT, "standing", [])

    agile = {}
    agile_rows = []
    for d in daterange(START, today + timedelta(days=1)):
        prices = agile_day(rng, d, t[d])
        if d == today + timedelta(days=1) and datetime.now(LON).hour < 16:
            prices = []                              # tomorrow's prices appear ~4pm
        for slot, p in enumerate(prices):
            a = ts_of(d) + slot * 1800
            agile[a] = p
            agile_rows.append((a, a + 1800, p))
    db.put_rates(AGILE, "unit", agile_rows)
    db.put_rates(AGILE, "standing", [(ts_of(START), ts_of(date(2025, 4, 1)), 48.0),
                                     (ts_of(date(2025, 4, 1)), None, 46.4)])

    # tariffs offered in the comparison
    cmp_from = today - timedelta(days=101)
    tracker_e, tracker_g = [], []
    for d in daterange(cmp_from, today + timedelta(days=1)):
        day = [agile.get(ts_of(d) + s * 1800, 20) for s in range(48)]
        avg = sum(day) / 48
        tracker_e.append((ts_of(d), ts_of(d + timedelta(days=1)), round(avg * 1.08 + 3, 2)))
        tracker_g.append((ts_of(d), ts_of(d + timedelta(days=1)), round(5.2 + (12 - t[d]) * 0.06 + rng.gauss(0, 0.2), 2)))
    db.put_rates(TRACKER_E, "unit", tracker_e)
    db.put_rates(TRACKER_E, "standing", [(ts_of(cmp_from), None, 46.4)])
    db.put_rates(TRACKER_G, "unit", tracker_g)
    db.put_rates(TRACKER_G, "standing", [(ts_of(cmp_from), None, 30.1)])
    launch = date(2026, 10, 2)
    db.put_rates(FIXED_E, "unit", [(ts_of(launch), None, 23.95)])
    db.put_rates(FIXED_E, "standing", [(ts_of(launch), None, 44.9)])
    db.put_rates(FIXED_G, "unit", [(ts_of(launch), None, 6.82)])
    db.put_rates(FIXED_G, "standing", [(ts_of(launch), None, 32.5)])
    go_rows = []
    for d in daterange(cmp_from, today + timedelta(days=1)):
        a = int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())
        go_rows += [(a, a + 1800, 27.4), (a + 1800, a + 5 * 3600 + 1800, 8.5), (a + 5 * 3600 + 1800, a + 86400, 27.4)]
    db.put_rates(GO, "unit", go_rows)
    db.put_rates(GO, "standing", [(ts_of(cmp_from), None, 46.4)])

    db.replace_agreements([
        ("elec", FLEX_E, ts_of(START), ts_of(AGILE_FROM)),
        ("elec", AGILE, ts_of(AGILE_FROM), None),
        ("gas", FLEX_G, ts_of(START), None),
        ("export", EXPORT, ts_of(START), None),
    ])

    # ---- the household ----
    elec, gas, export = [], [], []
    away = set()
    for y in (2024, 2025, 2026):
        a = date(y, 8, 10 + rng.randint(0, 6))
        away |= {a + timedelta(days=i) for i in range(12)}
    ev_days = set()
    for d in daterange(START, yesterday):
        if rng.random() < 2 / 7 and d not in away:
            ev_days.add(d)
    for d in daterange(START, yesterday):
        temp = t[d]
        doy = d.timetuple().tm_yday
        weekend = d.weekday() >= 5
        home = d not in away
        dark = 0.5 + 0.5 * math.cos(2 * math.pi * (doy - 355) / 365)   # 1 in midwinter
        sunny = max(0.05, rng.betavariate(2, 2) * (0.55 + 0.45 * (1 - dark)))
        daylen = 12 + 4.3 * math.cos(2 * math.pi * (doy - 172) / 365)
        heat_need = max(0.0, 15.5 - temp) * rng.uniform(3.4, 4.4) if home else max(0.0, 12 - temp) * 0.8
        washes = [rng.randint(18, 30)] if home and rng.random() < 0.4 else []
        cook = rng.randint(34, 37)
        ev_slot = None
        if d in ev_days:
            if d >= AGILE_FROM:
                prices = [agile.get(ts_of(d) + s * 1800 + 86400, 15) for s in range(0, 12)]
                ev_slot = 48 + min(range(len(prices) - 5), key=lambda j: sum(prices[j:j + 6]))
            else:
                ev_slot = rng.randint(36, 40)
        if d == date(d.year, 12, 25):
            heat_need *= 1.15
        stuck_on = d == today - timedelta(days=12)        # heating left on all day by mistake
        for slot in range(48):
            h = slot / 2
            load = 0.085 + rng.uniform(0, 0.02)                         # fridge, router, standby
            if home:
                if 6.5 <= h < 8.5 and not weekend:
                    load += 0.22
                if 8 <= h < 10.5 and weekend:
                    load += 0.25
                if 9 <= h < 17 and weekend:
                    load += 0.12
                if 17 <= h < 22.5:
                    load += 0.28 + 0.18 * dark
                if slot in (cook, cook + 1):
                    load += rng.uniform(0.6, 1.1) * (1.4 if d == date(d.year, 12, 25) else 1)
                if slot in [w for x in washes for w in (x, x + 1, x + 2)]:
                    load += 0.35
                if rng.random() < 0.04:
                    load += 0.1                                         # kettle
            if ev_slot is not None and ev_slot <= slot < ev_slot + 6:
                load += 3.5
            # previous evening's EV charge that spills past midnight
            prev = d - timedelta(days=1)
            if prev in ev_days and prev >= AGILE_FROM:
                prices = [agile.get(ts_of(prev) + s * 1800 + 86400, 15) for s in range(0, 12)]
                s0 = min(range(len(prices) - 5), key=lambda j: sum(prices[j:j + 6]))
                if s0 <= slot < s0 + 6:
                    load += 3.5
            sun = 0.0
            rise, sset = 12.6 - daylen / 2, 12.6 + daylen / 2
            if rise < h + 0.25 < sset:
                sun = 4.0 * 0.5 * 0.78 * sunny * math.sin(math.pi * (h + 0.25 - rise) / daylen) ** 1.5
            imp = max(0.0, load - sun)
            exp_ = max(0.0, sun - load)
            # gas: hot water + heating in the morning and evening
            g = 0.0
            if home and (6.5 <= h < 7.5 or 18 <= h < 19):
                g += rng.uniform(0.6, 0.9)
            if heat_need > 0 and (6 <= h < 9 or 16 <= h < 22.5):
                g += heat_need / 19 * rng.uniform(0.8, 1.2)
            if stuck_on and 9 <= h < 16:
                g += 1.6
            ts = ts_of(d) + slot * 1800
            elec.append((ts, round(imp, 3), round(imp, 3)))
            export.append((ts, round(exp_, 3), round(exp_, 3)))
            gm3 = round(g / M3_TO_KWH, 3)
            gas.append((ts, gm3 * M3_TO_KWH, gm3))
    # a few missing readings, like the real thing
    gone = {ts_of(date(2025, 11, 3)) + i * 1800 for i in range(48)}
    db.put_readings("elec", [x for x in elec if x[0] not in gone])
    db.put_readings("gas", gas)
    db.put_readings("export", export)

    # ---- grid carbon (last 28 days + 48h forecast) ----
    now = int(time.time())
    first = (now // 1800) * 1800 - 28 * 86400
    rows = []
    wave = 0.0
    for i in range(28 * 48 + 96):
        ts = first + i * 1800
        h = (local(ts).hour + local(ts).minute / 60)
        wave = 0.97 * wave + rng.gauss(0, 6)
        g = 150 + wave + 35 * math.exp(-((h - 18) ** 2) / 6) - 40 * math.exp(-((h - 13) ** 2) / 8)
        rows.append((ts, max(15, round(g)), 1 if ts >= now - 1800 else 0))
    db.put_carbon(rows)

    db.set("account", {"number": "A-DEMO0000", "moved_in_at": "2024-04-01T00:00:00+01:00", "outcode": "DEMO",
                       "meters": {"elec": {"id_tail": "0001", "serials": 1, "estimate_kwh": 4100},
                                  "gas": {"id_tail": "0002", "serials": 1, "estimate_kwh": 11500},
                                  "export": {"id_tail": "0003", "serials": 1, "estimate_kwh": None}}})
    db.set("location", {"lat": 51.5, "lon": -0.1, "outcode": "DEMO"})
    db.set("region", "C")
    db.set("gas_units", "m3")
    db.set("compare_tariffs", [
        {"name": "Flexible Octopus", "blurb": "Standard variable – follows the price cap", "product": "VAR-22-11-01",
         "variable": True, "elec": FLEX_E, "gas": FLEX_G},
        {"name": "Agile Octopus", "blurb": "Half-hourly prices that follow the wholesale market",
         "product": "AGILE-24-10-01", "variable": True, "elec": AGILE, "gas": None},
        {"name": "Octopus Tracker", "blurb": "Daily prices that follow the wholesale market",
         "product": "SILVER-25-04-15", "variable": True, "elec": TRACKER_E, "gas": TRACKER_G},
        {"name": "Octopus 12M Fixed", "blurb": "Price fixed for 12 months", "product": "OE-FIX-12M-26-10-02",
         "variable": False, "elec": FIXED_E, "gas": FIXED_G},
        {"name": "Octopus Go", "blurb": "Cheap overnight rate – designed for electric cars",
         "product": "GO-VAR-22-10-14", "variable": True, "elec": GO, "gas": None},
    ])
    db.set("last_sync", now)
    db.bump_version()
    return db


def local(ts):
    return datetime.fromtimestamp(ts, LON)


if __name__ == "__main__":
    t0 = time.time()
    build()
    print(f"Demo data written to {PATH} in {time.time() - t0:.1f}s")
