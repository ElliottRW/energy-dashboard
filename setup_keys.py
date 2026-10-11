"""Saves your Octopus account number and API key into .env – run it yourself in Terminal:

    cd ~/src/energy-dashboard && .venv/bin/python setup_keys.py

The key is typed hidden (nothing shows as you type or paste), checked against Octopus,
and written only to .env on this Mac (readable by you alone).
"""
import getpass
import os
import re
import stat
import sys

from octopus import Octopus, OctopusError
from analysis import tariff_name

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(HERE, ".env")


def main():
    print("Find both at: octopus.energy → Account → Personal details → API access\n")
    account = input("Account number (looks like A-1A2B3C4D): ").strip().upper()
    if not re.fullmatch(r"A-[0-9A-F]{8}", account):
        print("That doesn't look like an Octopus account number (A- followed by 8 letters/digits).")
        sys.exit(1)
    key = getpass.getpass("API key (hidden – paste it and press Enter): ").strip()
    if not key.startswith("sk_"):
        print("Octopus API keys start with 'sk_' – please check and try again.")
        sys.exit(1)

    print("\nChecking with Octopus…")
    try:
        acct = Octopus(key).account(account)
    except OctopusError as e:
        print(f"Octopus said no: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Couldn't reach Octopus: {e}")
        sys.exit(1)

    props = [p for p in acct.get("properties", []) if not p.get("moved_out_at")] or acct.get("properties", [])
    print("✓ Key works. Tariffs found:")
    for p in props[:1]:
        for kind, label in (("electricity_meter_points", "Electricity"), ("gas_meter_points", "Gas")):
            for point in p.get(kind, []):
                ags = sorted(point.get("agreements", []), key=lambda a: a["valid_from"])
                if not ags:
                    continue
                a = ags[-1]
                name = label + (" export" if point.get("is_export") else "")
                ends = f" until {a['valid_to'][:10]}" if a.get("valid_to") else ""
                print(f"   {name}: {tariff_name(a['tariff_code'])}{ends}")

    # keep any other settings already in .env
    lines = []
    if os.path.exists(ENV):
        with open(ENV) as f:
            lines = [ln for ln in f.read().splitlines()
                     if not ln.startswith(("OCTOPUS_API_KEY=", "OCTOPUS_ACCOUNT=", "DEMO="))]
    lines = [f"OCTOPUS_ACCOUNT={account}", f"OCTOPUS_API_KEY={key}"] + lines
    with open(ENV, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(ENV, stat.S_IRUSR | stat.S_IWUSR)
    print(f"\n✓ Saved to .env. Restart the app to load your own data.")


if __name__ == "__main__":
    main()
