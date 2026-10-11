"""The local database – a single SQLite file in data/.

Everything downloaded from Octopus (and the weather / carbon services) is kept here so
the dashboard opens instantly and only new readings need downloading on each sync.
"""
import json
import os
import sqlite3
import threading

HERE = os.path.dirname(os.path.abspath(__file__))

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (      -- one row per half hour per fuel
    fuel TEXT NOT NULL,                    -- elec | gas | export
    ts   INTEGER NOT NULL,                 -- start of the half hour, unix seconds (UTC)
    kwh  REAL NOT NULL,                    -- always kWh (gas converted from m³ if needed)
    raw  REAL NOT NULL,                    -- what the meter reported
    PRIMARY KEY (fuel, ts)
);
CREATE TABLE IF NOT EXISTS agreements (    -- which tariff you were on, when
    fuel TEXT NOT NULL,
    tariff TEXT NOT NULL,
    valid_from INTEGER NOT NULL,
    valid_to INTEGER,
    PRIMARY KEY (fuel, tariff, valid_from)
);
CREATE TABLE IF NOT EXISTS rates (         -- prices, in pence inc VAT
    tariff TEXT NOT NULL,
    kind TEXT NOT NULL,                    -- unit | day | night | standing
    valid_from INTEGER NOT NULL,
    valid_to INTEGER,
    value REAL NOT NULL,
    PRIMARY KEY (tariff, kind, valid_from)
);
CREATE TABLE IF NOT EXISTS weather (       -- daily mean temperature, °C
    date TEXT PRIMARY KEY,
    temp REAL NOT NULL,
    forecast INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS carbon (        -- grid carbon intensity, gCO2/kWh, per half hour
    ts INTEGER PRIMARY KEY,
    intensity REAL NOT NULL,
    forecast INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS carbon_national (   -- Great Britain as a whole, same units
    ts INTEGER PRIMARY KEY,
    intensity REAL NOT NULL,
    forecast INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class DB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        with self.connect() as c:
            c.executescript(SCHEMA)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- meta ----

    def get(self, key, default=None):
        with self.connect() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        with self._lock, self.connect() as c:
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, json.dumps(value)))

    # ---- bulk writes ----

    def put_readings(self, fuel, rows):
        """rows: iterable of (ts, kwh, raw)"""
        with self._lock, self.connect() as c:
            c.executemany("INSERT OR REPLACE INTO readings(fuel, ts, kwh, raw) VALUES (?, ?, ?, ?)",
                          [(fuel, ts, kwh, raw) for ts, kwh, raw in rows])

    def put_rates(self, tariff, kind, rows):
        """rows: iterable of (valid_from, valid_to, value)"""
        with self._lock, self.connect() as c:
            c.executemany("INSERT OR REPLACE INTO rates(tariff, kind, valid_from, valid_to, value) "
                          "VALUES (?, ?, ?, ?, ?)", [(tariff, kind, a, b, v) for a, b, v in rows])

    def replace_agreements(self, rows):
        """rows: iterable of (fuel, tariff, valid_from, valid_to)"""
        with self._lock, self.connect() as c:
            c.execute("DELETE FROM agreements")
            c.executemany("INSERT OR REPLACE INTO agreements VALUES (?, ?, ?, ?)", list(rows))

    def put_weather(self, rows):
        """rows: iterable of (date, temp, forecast)"""
        with self._lock, self.connect() as c:
            c.executemany("INSERT OR REPLACE INTO weather(date, temp, forecast) VALUES (?, ?, ?)", list(rows))

    def put_carbon(self, rows, table="carbon"):
        with self._lock, self.connect() as c:
            c.executemany(f"INSERT OR REPLACE INTO {table}(ts, intensity, forecast) VALUES (?, ?, ?)", list(rows))

    def regas(self, factor):
        """Re-work gas kWh from the raw meter values (used if the gas units guess changes)."""
        with self._lock, self.connect() as c:
            c.execute("UPDATE readings SET kwh = raw * ? WHERE fuel='gas'", (factor,))

    # ---- reads ----

    def readings(self, fuel):
        with self.connect() as c:
            return [(r[0], r[1]) for r in c.execute(
                "SELECT ts, kwh FROM readings WHERE fuel=? ORDER BY ts", (fuel,))]

    def last_reading_ts(self, fuel):
        with self.connect() as c:
            row = c.execute("SELECT MAX(ts) FROM readings WHERE fuel=?", (fuel,)).fetchone()
        return row[0]

    def fuels(self):
        with self.connect() as c:
            return [r[0] for r in c.execute("SELECT DISTINCT fuel FROM readings")]

    def agreements(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM agreements ORDER BY fuel, valid_from")]

    def rates(self, tariff, kind):
        with self.connect() as c:
            return [(r[0], r[1], r[2]) for r in c.execute(
                "SELECT valid_from, valid_to, value FROM rates WHERE tariff=? AND kind=? ORDER BY valid_from",
                (tariff, kind))]

    def last_rate_ts(self, tariff, kind):
        with self.connect() as c:
            row = c.execute("SELECT MAX(valid_from) FROM rates WHERE tariff=? AND kind=?",
                            (tariff, kind)).fetchone()
        return row[0]

    def clear_rates(self):
        with self._lock, self.connect() as c:
            c.execute("DELETE FROM rates")

    def first_rate_ts(self, tariff, kind):
        with self.connect() as c:
            row = c.execute("SELECT MIN(valid_from) FROM rates WHERE tariff=? AND kind=?",
                            (tariff, kind)).fetchone()
        return row[0]

    def weather(self):
        with self.connect() as c:
            return {r[0]: (r[1], bool(r[2])) for r in c.execute("SELECT date, temp, forecast FROM weather")}

    def carbon(self, table="carbon"):
        with self.connect() as c:
            return [(r[0], r[1], bool(r[2])) for r in c.execute(
                f"SELECT ts, intensity, forecast FROM {table} ORDER BY ts")]

    def data_version(self):
        """Changes whenever a sync writes anything – used to cache the analysis."""
        return self.get("data_version", 0)

    def bump_version(self):
        self.set("data_version", self.data_version() + 1)
