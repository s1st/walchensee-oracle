#!/usr/bin/env python3
"""Estimate real-browser traffic to the Walchi Oracle dashboard.

Pulls Cloud Run request logs via `gcloud logging read`, filters out bots,
empty-UA probes, and exploit scanners, normalizes IPv6 addresses to /64
(the granularity at which one customer = one prefix, modulo ISP rotation),
and prints a daily breakdown plus the most-active IPs.

Background notes worth keeping in mind when reading the output:

- m-net (AS8767, prefix 2001:a61::/32) rotates the customer /48 periodically.
  The same person therefore shows up under multiple /64s over a month;
  do not double-count them as separate visitors. The script flags
  consecutive m-net prefixes in the top-N for that reason.
- Cloud Run health probes and most scrapers send no User-Agent. Browsers
  always send a Mozilla/… UA. Anything else is treated as not-human.
- Exploit scanners (looking for /admin.php, /.git/config, /.aws/credentials,
  etc.) make up the bulk of the noise; they are dropped by path regex.

Usage:
  python scripts/dashboard_traffic.py                     # last 30 days, prod
  python scripts/dashboard_traffic.py --days 7
  python scripts/dashboard_traffic.py --top 20
  python scripts/dashboard_traffic.py --days 1 --host walchensee.s1st.de   # one channel
"""
from __future__ import annotations

import argparse
import collections
import subprocess
import sys
from pathlib import Path

try:
    from oracle.traffic import is_mnet_prefix, real_browser_hit
except ModuleNotFoundError:  # plain `python3 scripts/…` outside the venv
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from oracle.traffic import is_mnet_prefix, real_browser_hit

DEFAULT_PROJECT = "walchi-oracle-prod"
DEFAULT_SERVICE = "walchi-oracle-dash"


def fetch_logs(
    project: str, service: str, days: int, limit: int, host: str | None = None
) -> list[str]:
    """Shell out to gcloud to pull GET-request logs. Returns raw TSV lines.

    `host` restricts to one served hostname (substring match on requestUrl) so a
    single service can be split per channel — e.g. a Reddit-only vanity domain
    vs. the Discord/LinkedIn domain. NB: a brand-new domain becomes a CT-log
    scanner magnet within minutes (leakix, GPTBot, datacenter IPs faking browser
    UAs), so its raw host count overstates humans for days — read the top-IP
    list and discount datacenter ranges hammering ~70 hits each."""
    filter_expr = (
        f'resource.type="cloud_run_revision" AND '
        f'resource.labels.service_name="{service}" AND '
        f'httpRequest.requestMethod="GET" AND httpRequest.status<400'
    )
    if host:
        filter_expr += f' AND httpRequest.requestUrl:"{host}"'
    cmd = [
        "gcloud", "logging", "read", filter_expr,
        f"--project={project}",
        f"--freshness={days}d",
        "--format=value(timestamp,httpRequest.remoteIp,httpRequest.requestUrl,httpRequest.userAgent)",
        f"--limit={limit}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"gcloud failed:\n{result.stderr}")
    return [ln for ln in result.stdout.splitlines() if ln.strip()]


def parse(lines: list[str]) -> list[tuple[str, str, str]]:
    """Return (day, ip_key, path) for every real-browser hit."""
    out: list[tuple[str, str, str]] = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        ts, ip, url = parts[0], parts[1], parts[2]
        ua = parts[3] if len(parts) > 3 else ""
        hit = real_browser_hit(ua, url, ip)
        if hit is None:
            continue
        ip_key, path = hit
        out.append((ts[:10], ip_key, path))
    return out


def render(hits: list[tuple[str, str, str]], top_n: int) -> None:
    by_day_ips: dict[str, set[str]] = collections.defaultdict(set)
    by_day_hits: collections.Counter[str] = collections.Counter()
    for d, ip, _ in hits:
        by_day_ips[d].add(ip)
        by_day_hits[d] += 1

    print(f"{'day':12} {'hits':>5} {'uniq IP':>8} {'hits/uniq':>10}")
    for d in sorted(by_day_ips):
        h = by_day_hits[d]
        u = len(by_day_ips[d])
        print(f"{d:12} {h:>5} {u:>8} {h / u:>10.1f}")

    total_hits = len(hits)
    all_ips = {ip for _, ip, _ in hits}
    print()
    print(f"total real-browser hits:           {total_hits}")
    print(f"total unique IP-prefixes (/64):    {len(all_ips)}")
    if all_ips:
        print(f"avg hits per unique visitor:       {total_hits / len(all_ips):.1f}")
    print()

    ip_counts = collections.Counter(ip for _, ip, _ in hits)
    top = ip_counts.most_common(top_n)
    print(f"top-{top_n} most-active IPs:")
    for ip, n in top:
        marker = "  ← m-net (rotates /48 — likely same person if multiple)" if is_mnet_prefix(ip) else ""
        print(f"  {n:>4}  {ip}{marker}")
    if total_hits:
        top_share = sum(n for _, n in top) / total_hits * 100
        print(f"\ntop-{top_n} IPs account for {top_share:.0f}% of all real-browser hits")

    mnet_in_top = [ip for ip, _ in top if is_mnet_prefix(ip)]
    if len(mnet_in_top) > 1:
        mnet_hits = sum(n for ip, n in top if is_mnet_prefix(ip))
        print(
            f"\nnote: {len(mnet_in_top)} of the top-{top_n} are m-net /64s "
            f"({mnet_hits} hits combined). m-net rotates /48 prefixes for "
            f"consumer connections, so these may be one person, not many."
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=30, help="lookback window (default: 30)")
    ap.add_argument("--project", default=DEFAULT_PROJECT)
    ap.add_argument("--service", default=DEFAULT_SERVICE)
    ap.add_argument("--top", type=int, default=10, help="how many top IPs to list (default: 10)")
    ap.add_argument("--limit", type=int, default=20000, help="max log entries to pull (default: 20000)")
    ap.add_argument(
        "--host",
        default=None,
        help="restrict to one served hostname (e.g. walchensee.s1st.de) to isolate a channel",
    )
    args = ap.parse_args()

    scope = f" for host {args.host}" if args.host else ""
    print(f"fetching last {args.days}d of GET logs for {args.service} in {args.project}{scope}…")
    raw = fetch_logs(args.project, args.service, args.days, args.limit, args.host)
    if len(raw) >= args.limit:
        print(f"warning: hit --limit ({args.limit}); raise it or shorten --days for full data")
    print(f"raw GET entries returned: {len(raw)}")
    hits = parse(raw)
    print(f"after filtering bots / exploits / empty-UA: {len(hits)} real-browser hits\n")
    render(hits, top_n=args.top)


if __name__ == "__main__":
    main()
