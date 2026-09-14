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
from collections.abc import Iterable, Iterator

GtfsRow = dict[str, str | None]


def parse_csv(text: str) -> list[GtfsRow]:
    """Parse CSV text into row dicts keyed by header name.

    Empty fields become ``None``. Rows with fewer fields than the header leave
    the missing columns as ``None``; extra fields are discarded.
    """
    return list(iter_csv(io.StringIO(text, newline="")))


def iter_csv(lines: Iterable[str], columns: frozenset[str] | None = None) -> Iterator[GtfsRow]:
    """Yield rows one at a time, for tables too large to hold as dicts.

    ``lines`` must be opened with ``newline=""`` so quoted line breaks survive.
    When ``columns`` is given, each row carries only those keys, which keeps a
    large table's per-row cost to the fields actually read.
    """
    reader = csv.reader(lines)
    try:
        header = [name.strip() for name in next(reader)]
    except StopIteration:
        return
    if header:
        header[0] = header[0].lstrip("\ufeff")
    wanted = [
        (column, key)
        for column, key in enumerate(header)
        if key and (columns is None or key in columns)
    ]

    for values in reader:
        if not values:
            continue
        row: GtfsRow = {}
        for column, key in wanted:
            value = values[column].strip() if column < len(values) else ""
            row[key] = value or None
        yield row
