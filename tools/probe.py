#!/usr/bin/env python3
"""One-shot diagnosis for a Huawei Health account, from a terminal.

Prints what the integration would see: which of the four data centres holds the account's
rows, which devices are bound to it, and which metric families actually have numbers. Use
it when the dashboard says "unknown" everywhere and the log is not enough.

The tokens live in Home Assistant's config entry. On the HA host:

    python3 probe.py --refresh-token <refreshToken>

They are in ``.storage/config_entries`` under the ``huawei_health`` entry's ``data``
(``access_token`` / ``refresh_token``), or they can be read off the attributes of the
"Re-login due in days" sensor for the expiry stamps only - the tokens themselves are never
exposed there.

Prefer ``--access-token`` when the entry is fresh: probing rotates the pair, and a rotated
access token is the one Home Assistant has to store. If only the refresh token is used,
let the integration reload the entry afterwards and it re-rotates on its own.

Requires nothing but the standard library; the integration package must be importable, so
run it from the root of a checkout, or from ``custom_components``.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta

try:  # two supported working directories: the checkout root, or custom_components/
    from huawei_health.client import (
        Tokens, discover_data_host, daily_activity, daily_totals, health_series,
        sport_sessions, HuaweiHealthClient, HuaweiError, day_int,
    )
    from huawei_health.const import APP_HOSTS, APP_HOST_LABELS
except ImportError:  # pragma: no cover - depends on where the script is run from
    sys.path.insert(0, "custom_components")
    from huawei_health.client import (
        Tokens, discover_data_host, daily_activity, daily_totals, health_series,
        sport_sessions, HuaweiHealthClient, HuaweiError, day_int,
    )
    from huawei_health.const import APP_HOSTS, APP_HOST_LABELS


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--refresh-token", required=True)
    parser.add_argument("--access-token")
    parser.add_argument("--session-host",
                        help="healthcommon-… host the pair was minted on")
    parser.add_argument("--uid", help="x-huid of the account, if known")
    parser.add_argument("--site-id", help="siteId from the login answer, if known")
    parser.add_argument("--device-code", type=int, default=None,
                        help="restrict the reads to one bound device")
    parser.add_argument("--days", type=int, default=30,
                        help="window to look back over (default 30)")
    parser.add_argument("--skip-discovery", action="store_true",
                        help="only report rows for the host given by --host")
    parser.add_argument("--host", default=APP_HOSTS[0], help="data centre to query")
    parser.add_argument("--verbose", action="store_true", help="log every request outcome")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    tokens = Tokens(access_token=args.access_token, refresh_token=args.refresh_token,
                    uid=args.uid, site_id=args.site_id, session_host=args.session_host,
                    # A token the entry has already rotated looks dead to a fresh client;
                    # let the client rotate it and print the new pair at the end.
                    expires_at=0.0,
                    refresh_expires_at=time.time() + 180 * 24 * 3600)

    if not args.skip_discovery:
        print("\n== which data centre holds this account ==")
        for host in APP_HOSTS:
            print(f"  {host}  {APP_HOST_LABELS.get(host, '')}")
        host, verdicts = discover_data_host(tokens, days=min(args.days, 7),
                                           device_code=args.device_code)
        for line in verdicts.split("; "):
            print(f"  {line}")
        if not host:
            print("  => no cloud returned a single row. The account is not synced to the "
                  "Health cloud, or the band/phone never uploads there.")
        else:
            print(f"  => rows live on {host}")
            args.host = host

    client = HuaweiHealthClient(tokens, host=args.host, device_code=args.device_code)
    end = day_int(date.today())
    start = day_int(date.today() - timedelta(days=max(args.days, 1) - 1))

    print("\n== devices bound to the account ==")
    for line in client.device_codes():
        print(f"  {line}")

    print("\n== getSyncVersions: what the cloud ever received ==")
    for line in client.sync_ledger():
        print(f"  {line}")

    print(f"\n== getSportsStat on {args.host}, {start}..{end} ==")
    try:
        rows = daily_totals(daily_activity(
            client.sports_stat(start, end, args.device_code).get("sportStat") or []))
        if not rows:
            print("  no rows")
        for day, row in sorted(rows.items()):
            print(f"  {day} steps={row['steps']} distance_m={row['distance_m']} "
                  f"kcal={row['kcal']} duration_min={row['duration_min']}")
    except HuaweiError as err:
        print(f"  failed: {err}")

    print("\n== getHealthStat (7 pulse, 9 sleep, 11 stress, 12 intensity) ==")
    for type_id, name in ((7, "heart_rate"), (9, "professional_sleep"),
                          (11, "stress"), (12, "exercise_intensity")):
        try:
            rows = health_series(client.health_stat(start, end, [type_id],
                                                   args.device_code))
        except HuaweiError as err:
            print(f"  type {type_id} {name}: failed: {err}")
            continue
        if not rows:
            print(f"  type {type_id} {name}: no rows - this device family does not fill it")
            continue
        for row in sorted(rows, key=lambda r: r["date"])[-3:]:
            keys = {k: v for k, v in row.items() if k != "date"}
            print(f"  type {type_id} {name}: {row['date']} {keys}")

    print("\n== getSportsDataByTime (workout segments) ==")
    try:
        records = client._by_time_range(
            lambda a, b: client.sports_detail(a, b, (1, 2, 3, 4, 9, 10, 14)), start, end)
        sessions = sport_sessions(records)
        print(f"  {len(records)} segments, {len(sessions)} sessions")
        for session in sessions[-5:]:
            print(f"  {session['start']} type={session['sport_type']} "
                  f"{session['duration_min']}min {session['distance_m']}m "
                  f"{session['kcal']}kcal device={session['device_code']}")
    except HuaweiError as err:
        print(f"  failed: {err}")

    if tokens.dirty:
        print("\n== the probe rotated the pair; store these in the entry ==")
        print(f"  access_token = {tokens.access_token}")
        print(f"  expires_at   = {tokens.expires_at}")
        print(f"  uid          = {tokens.uid}")
        print(f"  site_id      = {tokens.site_id}")
        print("  (the refresh token did not change)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
