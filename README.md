# Home energy – personal Octopus Energy dashboard

Your half-hourly smart-meter readings from Octopus, turned into: what this month and this year
are going to cost, when you used lots, what your home draws while you sleep, whether another
tariff would be cheaper, and how the weather and the grid's carbon change things.

Runs on your Mac at **http://localhost:5080**. Everything is stored in one file on your computer
(`data/energy.db`); nothing is uploaded anywhere.

**Built with:** Python + Flask (a small web server), SQLite (a single-file database), numpy for
the prediction maths, and plain HTML/JavaScript with hand-drawn SVG charts. No build tools.

---

## 1. Starting it

```bash
cd ~/src/energy-dashboard && .venv/bin/python app.py
```

Then open <http://localhost:5080>. Leave the Terminal window open while you use it; **Ctrl+C** stops it.

With no API key set it shows **made-up demo data** (a pretend house with solar panels and an
electric car), so you can look around first.

### First-time setup

1. Get your API key and account number: octopus.energy → **Account** → **Personal details** →
   **API access** (direct link: <https://octopus.energy/dashboard/new/accounts/personal-details/api-access>).
   The account number looks like `A-XXXXXXXX`.
2. Run `.venv/bin/python setup_keys.py` – it asks for both (the key is typed hidden), checks them
   with Octopus, shows the tariffs it found and saves them to `.env` (readable only by you, and in
   `.gitignore` so it's never uploaded).
3. Already done for you: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
4. Start the app. The first sync downloads up to 3 years of readings (usually under a minute) and
   the page fills in by itself.

After that it syncs on start-up and every 6 hours while running, or press **Sync now**.
Smart meter readings reach Octopus about a day late, so "today" is always a prediction.

**No smart meter?** If Octopus has no half-hourly readings for your account, the app says so and
shows your balance, bills, transactions, tariffs, Agile prices, weather and grid carbon. Predictions, usage
patterns and anything else worked out from your usage appear by themselves once readings arrive.

---

## 2. What's on each tab

| Tab | What it shows |
|---|---|
| **Overview** | This month's predicted bill (with a likely range and spend-so-far vs last year), this year's predicted total, next 12 months, plain-English "what stands out" notes, monthly bills, the last 30 days |
| **Predictions** | Year-by-year and month-by-month, actual + predicted (hatched) with likely ranges, the next two weeks from the weather forecast, and how the model works. Switch between £ and kWh |
| **Patterns** | A calendar of every day (click any day for the hour-by-hour view), daily use vs what the weather would predict, your average day (weekday/weekend, winter/summer), a week-by-hour heatmap |
| **Big days** | Days that used far more than expected for the weather, your biggest days and half-hours ever, how much you use 4–7pm, and your **always-on** power (and what it costs a year) |
| **Bills & balance** | Your balance (and its history), what to pay each month to stay in credit vs what Octopus suggests, **every Octopus bill re-checked against your meter readings**, and all transactions |
| **Prices & tariffs** | **When your fix ends** – each renewal option priced on your predicted next 12 months, **did fixing pay off?** (vs Flexible and Tracker since you joined), your current rates, Agile prices today/tomorrow with the cheapest 1–4 hour windows, what you actually paid per kWh each month, tariff history, and **"would another tariff be cheaper?"** – your real last 90 days priced on Flexible, Agile, Tracker, 12M Fixed, Go and Cosy |
| **Weather, carbon & solar** | How temperature drives your gas, months warmer/colder than normal, your carbon footprint, the greenest times to use power in the next 48 h, and your solar panels: export earnings and what Agile Outgoing would have paid |
| **Data** | What's been downloaded, gaps, Octopus's own yearly estimate vs your real use |

Every chart has hover details, and the big ones have a **Table** button.

---

## 3. How the predictions work

Each day is predicted as *base use + extra for every degree colder than 15.5°C* (heating degree
days – the standard approach), learned from your last 12 months. Future days use the real
forecast for the next ~2 weeks, then the 6-year average temperature for that date. The
"likely range" covers about 8 in 10 outcomes. Costs use the prices you're paying now, so if the
price cap moves the predictions will too. Full details are on the Predictions tab.

---

## 4. Settings (`.env`)

| Setting | Default | What it does |
|---|---|---|
| `OCTOPUS_API_KEY`, `OCTOPUS_ACCOUNT` | – | Required for your own data |
| `GAS_UNITS` | `auto` | Newer (SMETS2) gas meters report m³, older ones kWh. `auto` works it out by comparing against Octopus's annual estimate; set `m3` or `kWh` if the gas numbers look ~11× off |
| `HISTORY_YEARS` | `3` | How far back the first download goes |
| `AUTO_SYNC_HOURS` | `6` | How often it re-syncs while running |
| `PORT` | `5080` | (Not 5060 – browsers block that port) |
| `DEMO` | – | `1` forces demo data |

---

## 5. Other services used (all free, no accounts)

- **Octopus Energy API** – readings, tariffs, prices. Your balance, direct debit, statements and
  transactions come from Octopus's GraphQL API (same API key, read-only – the app never changes anything).
  Prices are stored with 5% VAT added to the ex-VAT figure, because the API's "inc VAT" electricity
  prices have at times been missing the VAT that bills include.
- **postcodes.io** – turns the first half of your postcode (e.g. `SW1A`) into a rough location.
  Only that outward code is sent, never your full postcode or address.
- **Open-Meteo** – daily temperatures for that area.
- **Carbon Intensity API** (National Grid) – GB grid carbon (used for your footprint and the greenest
  times) plus your region's figure for comparison, last 28 days + 48 h forecast.

## Files

`app.py` web server · `sync.py` downloads · `octopus.py` API client · `analysis.py` all the
maths · `db.py` database · `demo.py` demo data · `static/` the page (`app.js`, `charts.js`, `style.css`).

To start completely fresh: stop the app and delete `data/energy.db`.
