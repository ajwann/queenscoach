"""CSV reading for GTFS text files.

GTFS quotes any field containing a comma (``"Mt. Holly Road, North"``), so
splitting on commas is wrong. :mod:`csv` already implements RFC 4180 quoting,
escaped ``""``, and CRLF line endings; this module adds the GTFS-specific
handling on top: a UTF-8 BOM, surrounding whitespace, and empty fields that
should read as absent rather than as an empty string.
"""

from __future__ import annotations

import csv
import io

GtfsRow = dict[str, str | None]


def parse_csv(text: str) -> list[GtfsRow]:
    """Parse CSV text into row dicts keyed by header name.

    Empty fields become ``None``. Rows with fewer fields than the header leave
    the missing columns as ``None``; extra fields are discarded.
    """
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff"), newline=""))
    try:
        header = [name.strip() for name in next(reader)]
    except StopIteration:
        return []

    rows: list[GtfsRow] = []
    for values in reader:
        if not values:
            continue
        row: GtfsRow = {}
        for column, key in enumerate(header):
            if not key:
                continue
            value = values[column].strip() if column < len(values) else ""
            row[key] = value or None
        rows.append(row)
    return rows
