"""Socrata / SODA extraction, shared by all three Montgomery County datasets.

The pagination design is the part Part 1a says is checked specifically, so the
reasoning is written down here rather than only in DECISIONS.md.

Keyset, not offset
------------------
`$offset` without a deterministic `$order` silently skips and repeats rows when
the table is written concurrently. Adding `$order` makes it deterministic but
still O(n) server-side and still shifts under inserts. Walking a cursor —
`:id > <last seen>` ordered by `:id` — is immune to both by construction.

`$order=:id` is NOT lexicographic; the string is an encoding of an internal row
identifier. Verified 2026-09-04 that a cursor walk returns exactly the same rows
in the same order as an offset walk, so the comparison is internally consistent.
Do not sort, validate or reconstruct these client-side.

Bounded upper watermark
-----------------------
Every request also carries `:updated_at <= T`, where T is fixed at run start.
Without it there is a real gap: a row inserted mid-run with an `:id` below the
current cursor matches the floor filter but can never be reached, and advancing
the watermark past it would lose it permanently. Freezing the ceiling at T means
anything arriving during the run is simply next run's work.

Watermark on :updated_at, not crash_date_time
---------------------------------------------
`crash_date_time` is when the crash happened; `:updated_at` is when the row
appeared. Max crash date trails max `:updated_at` by ~2 days, which is exactly
the late-arrival window a business-date watermark would silently drop.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

import requests

from . import http

log = logging.getLogger(__name__)

# Socrata's own default page size is 1000. 20k is a saner operating point: no
# server-side cap was found at 50k or even 100k (checked 2026-09-04), but
# 100k-row responses are chunked and slow, and a smaller page loses less work
# when a run dies mid-flight.
DEFAULT_PAGE_SIZE = 20_000

ID_FIELD = ":id"
UPDATED_AT_FIELD = ":updated_at"


@dataclass(frozen=True)
class Page:
    records: list[dict]
    response: http.Response
    params: dict


def format_ts(dt: datetime) -> str:
    """Socrata fixed_timestamp literal: millisecond precision, Z-suffixed."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def resource_url(base: str, dataset_id: str) -> str:
    return f"{base.rstrip('/')}/{dataset_id}.json"


def fetch_schema(
    base: str, dataset_id: str, *, session: requests.Session | None = None
) -> tuple[list[str], list[str]]:
    """Read the authoritative field list from the X-SODA2-* response headers.

    Not from the rows: Socrata omits null fields from each JSON object entirely,
    so a row-derived schema is whatever happened to be populated in the sample.
    """
    resp = http.get(resource_url(base, dataset_id), params={"$limit": 1}, session=session)
    fields = [f for f in resp.headers.get("X-SODA2-Fields", "").strip("[]").replace('"', "").split(",") if f]
    types = [t for t in resp.headers.get("X-SODA2-Types", "").strip("[]").replace('"', "").split(",") if t]
    return [f.strip() for f in fields], [t.strip() for t in types]


def count_rows(
    base: str,
    dataset_id: str,
    *,
    where: str | None = None,
    session: requests.Session | None = None,
) -> int:
    params: dict = {"$select": "count(1) AS n"}
    if where:
        params["$where"] = where
    resp = http.get(resource_url(base, dataset_id), params=params, session=session)
    rows = json.loads(resp.body)
    return int(rows[0]["n"]) if rows else 0


def build_where(floor: str, ceiling: str, cursor: str | None) -> str:
    clauses = [
        f"{UPDATED_AT_FIELD} > '{floor}'",
        f"{UPDATED_AT_FIELD} <= '{ceiling}'",
    ]
    if cursor:
        clauses.append(f"{ID_FIELD} > '{cursor}'")
    return " AND ".join(clauses)


def paginate(
    base: str,
    dataset_id: str,
    *,
    floor: str,
    ceiling: str,
    page_size: int = DEFAULT_PAGE_SIZE,
    session: requests.Session | None = None,
) -> Iterator[Page]:
    """Yield pages of records, walking a keyset cursor over `:id`."""
    url = resource_url(base, dataset_id)
    cursor: str | None = None
    page_no = 0

    while True:
        params = {
            "$where": build_where(floor, ceiling, cursor),
            "$order": ID_FIELD,
            "$limit": page_size,
            # Surfaces :id / :created_at / :updated_at alongside the payload.
            "$$exclude_system_fields": "false",
        }
        resp = http.get(url, params=params, session=session)
        records = json.loads(resp.body)
        page_no += 1

        if not records:
            log.debug("%s: page %d empty, done", dataset_id, page_no)
            return

        yield Page(records=records, response=resp, params=params)

        if len(records) < page_size:
            log.debug("%s: page %d short (%d < %d), done", dataset_id, page_no, len(records), page_size)
            return

        last = records[-1].get(ID_FIELD)
        if not last:
            raise RuntimeError(
                f"{dataset_id}: page {page_no} has no {ID_FIELD}; cannot advance cursor safely. "
                "Refusing to fall back to $offset — that is the failure mode this avoids."
            )
        cursor = last
