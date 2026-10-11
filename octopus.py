"""Talks to the Octopus Energy API (https://developer.octopus.energy/).

Your API key is used as the username with a blank password. Only the account and
consumption endpoints need it – prices and products are public.
"""
import requests

BASE = "https://api.octopus.energy/v1"
TIMEOUT = 60


class OctopusError(Exception):
    pass


class Octopus:
    def __init__(self, api_key=None):
        self.session = requests.Session()
        if api_key:
            self.session.auth = (api_key, "")

    def _get(self, url, params=None):
        resp = self.session.get(url, params=params, timeout=TIMEOUT)
        if resp.status_code == 401:
            raise OctopusError("Octopus rejected the API key – check OCTOPUS_API_KEY in .env")
        if resp.status_code == 404:
            raise OctopusError(f"Not found: {url}")
        resp.raise_for_status()
        return resp.json()

    def _paged(self, url, params):
        """Follow the 'next' links until every page has been read."""
        out = []
        data = self._get(url, params)
        out.extend(data.get("results", []))
        while data.get("next"):
            data = self._get(data["next"])
            out.extend(data.get("results", []))
        return out

    # ---- account (needs API key) ----

    def account(self, number):
        return self._get(f"{BASE}/accounts/{number}/")

    def consumption(self, fuel, point_id, serial, period_from=None, period_to=None):
        kind = "electricity-meter-points" if fuel != "gas" else "gas-meter-points"
        params = {"page_size": 25000, "order_by": "period"}
        if period_from:
            params["period_from"] = period_from
        if period_to:
            params["period_to"] = period_to
        return self._paged(f"{BASE}/{kind}/{point_id}/meters/{serial}/consumption/", params)

    # ---- prices (public) ----

    def rates(self, tariff_code, kind, period_from=None, period_to=None):
        """kind: standard-unit-rates | day-unit-rates | night-unit-rates | standing-charges"""
        product = product_code(tariff_code)
        fuel = "gas-tariffs" if tariff_code.startswith("G-") else "electricity-tariffs"
        params = {"page_size": 1500}
        if period_from:
            params["period_from"] = period_from
        if period_to:
            params["period_to"] = period_to
        rows = self._paged(f"{BASE}/products/{product}/{fuel}/{tariff_code}/{kind}/", params)
        return [r for r in rows if r.get("payment_method") in (None, "DIRECT_DEBIT")]

    def products(self):
        return self._paged(f"{BASE}/products/", {"brand": "OCTOPUS_ENERGY", "is_business": "false",
                                                  "page_size": 100})

    def product(self, code):
        return self._get(f"{BASE}/products/{code}/")


def product_code(tariff_code):
    """E-1R-AGILE-24-10-01-C  ->  AGILE-24-10-01"""
    return "-".join(tariff_code.split("-")[2:-1])


def region_of(tariff_code):
    return tariff_code.split("-")[-1]


def is_two_rate(tariff_code):
    return tariff_code.startswith("E-2R-")


# ---- account money matters (GraphQL API – the REST API doesn't have these). All read-only. ----

GRAPHQL = f"{BASE}/graphql/"


class Kraken:
    """Octopus's GraphQL API. Signs in with your API key."""

    def __init__(self, api_key, account_number):
        self.account_number = account_number
        r = requests.post(GRAPHQL, timeout=TIMEOUT, json={
            "query": "mutation($key: String!) { obtainKrakenToken(input: {APIKey: $key}) { token } }",
            "variables": {"key": api_key}}).json()
        self.token = ((r.get("data") or {}).get("obtainKrakenToken") or {}).get("token")
        if not self.token:
            raise OctopusError(f"Couldn't sign in to the GraphQL API: {r.get('errors')}")

    def query(self, q, **variables):
        variables["n"] = self.account_number
        r = requests.post(GRAPHQL, timeout=TIMEOUT, headers={"Authorization": self.token},
                          json={"query": q, "variables": variables}).json()
        if r.get("errors") and not r.get("data"):
            raise OctopusError(f"GraphQL error: {r['errors'][0].get('message')}")
        return r.get("data") or {}

    def overview(self):
        """Balance, direct debit, and Octopus's own view of whether your payments are enough."""
        acct = self.query("""query($n: String!) { account(accountNumber: $n) {
            balance overdueBalance projectedBalance canRenewTariff shouldReviewPayments
            paymentAdequacy { suggestedDirectDebitAmount minimumDirectDebitAmount }
            paymentSchedules(first: 5) { edges { node { paymentAmount paymentDay validFrom validTo scheduleType paymentFrequency } } }
        } }""")["account"]
        schedules = [e["node"] for e in ((acct.get("paymentSchedules") or {}).get("edges") or [])]
        active = next((s for s in schedules if not s.get("validTo") and s.get("scheduleType") == "DIRECT_DEBIT"), None)
        pa = acct.get("paymentAdequacy") or {}
        pounds = lambda x: None if x is None else x / 100
        return {
            "balance": acct["balance"] / 100, "overdue": (acct.get("overdueBalance") or 0) / 100,
            "projected_balance": pounds(acct.get("projectedBalance")),
            "suggested_dd": pounds(pa.get("suggestedDirectDebitAmount")),
            "minimum_dd": pounds(pa.get("minimumDirectDebitAmount")),
            "can_renew": acct.get("canRenewTariff"), "should_review_payments": acct.get("shouldReviewPayments"),
            "direct_debit": {"amount": active["paymentAmount"] / 100, "day": active.get("paymentDay"),
                             "frequency": active.get("paymentFrequency"), "since": active.get("validFrom")}
            if active else None,
        }

    def transactions(self, limit=500):
        """Every charge, payment and credit, newest first. Amounts in pounds; balance positive = credit."""
        out, after = [], None
        while len(out) < limit:
            data = self.query("""query($n: String!, $after: String) { account(accountNumber: $n) {
                transactions(first: 100, after: $after) {
                    pageInfo { hasNextPage endCursor }
                    edges { node { __typename id postedDate amount title balanceCarriedForward isReversed billingDocumentIdentifier } } }
            } }""", after=after)["account"]["transactions"]
            for e in data["edges"]:
                n = e["node"]
                out.append({"id": n["id"], "type": n["__typename"], "date": n["postedDate"], "amount": n["amount"] / 100,
                            "title": n["title"], "balance": (n.get("balanceCarriedForward") or 0) / 100,
                            "reversed": n.get("isReversed"), "statement": n.get("billingDocumentIdentifier")})
            if not data["pageInfo"]["hasNextPage"]:
                break
            after = data["pageInfo"]["endCursor"]
        return out

    def statements(self):
        data = self.query("""query($n: String!) { account(accountNumber: $n) {
            bills(first: 100) { edges { node { ... on StatementType {
                id issuedDate fromDate toDate openingBalance closingBalance
                totalCharges { grossTotal } totalCredits { grossTotal } } } } }
        } }""")["account"]["bills"]
        out = []
        for e in data["edges"]:
            n = e["node"]
            if not n.get("id"):
                continue
            out.append({"id": n["id"], "issued": n["issuedDate"], "from": n["fromDate"], "to": n["toDate"],
                        "opening": (n.get("openingBalance") or 0) / 100, "closing": (n.get("closingBalance") or 0) / 100,
                        "charges": ((n.get("totalCharges") or {}).get("grossTotal") or 0) / 100,
                        "credits": ((n.get("totalCredits") or {}).get("grossTotal") or 0) / 100})
        return out


def account_balance(api_key, account_number):
    """Kept for the balance tile – same as Kraken(...).overview()."""
    return Kraken(api_key, account_number).overview()
