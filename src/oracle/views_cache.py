"""Pre-computed page-views cache for the dashboard.

`build_payload()` walks the dashboard service's Cloud Run request logs (last
30 days, bot-filtered, IPv6 /64-deduped — same classification as
scripts/dashboard_traffic.py, shared via oracle.traffic) and returns
``{"unique_visitors": int, "total_hits": int}``. Write it from a scheduled job
via ``oracle views-update``; the dashboard then serves the visitor counts with
a single cheap GCS read (``read_cache``) instead of running the multi-second
Cloud Logging walk on a live request.

Mirrors oracle.stats_cache: same RunStore-key convention and the same
build / write / read split. Storage key ``_views_cache`` lands next to
``_stats_cache`` (``runs/_views_cache.json`` on GCS).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from oracle.logger import RunStore, default_store
from oracle.traffic import real_browser_hit

_VIEWS_KEY = "_views_cache"

# Cap the log scan: beyond this the count silently undercounts, which is
# acceptable for a vanity metric (matches scripts/dashboard_traffic.py intent).
_MAX_LOG_ENTRIES = 20_000


def build_payload() -> dict[str, Any]:
    """Count real-browser traffic over the last 30 days from Cloud Run logs.

    On Cloud Run no configuration is needed: the project comes from ADC and
    the service name defaults to the dashboard service. ``LOG_PROJECT`` /
    ``LOG_SERVICE`` are dev overrides in the spirit of ``RUNS_BUCKET``.

    Note this targets the dashboard *service* (``walchi-oracle-dash``) by
    default even when run from the forecast *job* — Cloud Run Jobs don't set
    ``K_SERVICE`` (only services do), so the fallback default applies and the
    walk reads the dashboard's request logs, not the job's.
    """
    from google.cloud import logging as gcp_logging  # lazy: needs the logging client

    client = gcp_logging.Client(project=os.environ.get("LOG_PROJECT") or None)
    service = os.environ.get("LOG_SERVICE") or os.environ.get("K_SERVICE") or "walchi-oracle-dash"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    flt = (
        f'resource.type="cloud_run_revision" AND '
        f'resource.labels.service_name="{service}" AND '
        f'httpRequest.requestMethod="GET" AND httpRequest.status<400 AND '
        f'timestamp>="{cutoff}"'
    )
    ips: set[str] = set()
    hits = 0
    for entry in client.list_entries(filter_=flt, page_size=1000, max_results=_MAX_LOG_ENTRIES):
        req = entry.http_request or {}
        hit = real_browser_hit(
            req.get("userAgent") or "", req.get("requestUrl") or "", req.get("remoteIp") or ""
        )
        if hit is not None:
            hits += 1
            ips.add(hit[0])
    return {"unique_visitors": len(ips), "total_hits": hits}


def write_cache(store: RunStore | None = None) -> dict[str, Any]:
    """Build and persist the page-views payload. Returns the payload."""
    store = store or default_store()
    payload = build_payload()
    store.write(_VIEWS_KEY, payload)
    return payload


def read_cache(store: RunStore | None = None) -> dict[str, Any] | None:
    """Read the pre-computed page-views payload, or None if not yet written."""
    store = store or default_store()
    return store.read(_VIEWS_KEY)
