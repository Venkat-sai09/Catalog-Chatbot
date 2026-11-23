"""Build a versioned catalog file from the Renesas product-selector export."""
from __future__ import annotations

import argparse
import hashlib
import math
import re
import sqlite3
from pathlib import Path

from openpyxl import load_workbook

from catalog import VARIANT_FIELDS, quote

HYPERLINK = re.compile(r'^=HYPERLINK\("([^"]+)"\s*[,;]\s*"([^"]+)"\)$', re.I)
NUMBER = re.compile(r'[+-]?\d+(?:\.\d+)?')
VOLTAGE_RANGE = re.compile(r'^(\d+(?:\.\d+)?)\s*(?:to|[-–~])\s*(\d+(?:\.\d+)?)$', re.I)
UNKNOWN = {"", "-", "–", "—", "tbd", "n/a", "na"}
SCALAR_FIELDS = {
    "ram_kb", "program_memory_kb", "data_flash_kb", "operating_freq_max_mhz",
    "operating_freq_of_sub_cpu_max", "lead_count", "pitch_mm", "i_o_ports",
    "usb_ports", "can_fd_ch",
}


def key_for(label):
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def clean(value):
    if isinstance(value, str):
        value = value.strip()
        if value.lower() in UNKNOWN:
            return None
    return value


def numeric(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    if isinstance(value, str) and NUMBER.fullmatch(value):
        return float(value)
    return None


def intervals(value):
    # Multiple supplies may all be required. Only infer coverage from a single interval.
    if value is None:
        return []
    text = str(value).strip()
    single = numeric(text)
    if single is not None:
        return [(single, single)]
    match = VOLTAGE_RANGE.fullmatch(text)
    if match:
        lower, upper = map(float, match.groups())
        if lower <= upper:
            return [(lower, upper)]
    return []


def differences(previous, current, columns):
    return [key for key in columns if previous[key] != current[key]]


def ingest(workbook_path: Path, output: Path):
    workbook = load_workbook(workbook_path, read_only=True, data_only=False)
    try:
        sheet = workbook["ag-grid"]
        rows = sheet.values
        next(rows)
        _, table_url, *_ = next(rows)
        _, expected_count, *_ = next(rows)
        labels = list(next(rows))
        keys = [key_for(label) for label in labels]
        if len(set(keys)) != len(keys):
            raise ValueError("Column mappings collide. Review the source header.")
        if not {"part_number", "orderable_part_number"} <= set(keys):
            raise ValueError("Part Number and Orderable Part Number are required.")

        base_keys = [key for key in keys if key not in VARIANT_FIELDS]
        variant_keys = [key for key in keys if key in VARIANT_FIELDS]
        products, variants, voltage_ranges, raw_rows = {}, {}, [], []
        active_base = None
        for row_number, row in enumerate(rows, 5):
            if not any(value is not None for value in row):
                continue
            original = dict(zip(keys, row, strict=True))
            record = {key: clean(value) for key, value in original.items()}
            active_base = record["part_number"] or active_base
            match = HYPERLINK.fullmatch(str(record["orderable_part_number"]))
            if not active_base or not match:
                raise ValueError(f"Missing part identity or hyperlink in worksheet row {row_number}.")
            url, orderable = match.groups()
            if orderable.split("#", 1)[0].casefold() != active_base.casefold():
                raise ValueError(f"Orderable part does not match the base part in row {row_number}.")
            if orderable in variants:
                raise ValueError(f"Duplicate orderable identity in row {row_number}: {orderable}")
            record.update(part_number=active_base, orderable_part_number=orderable)
            base = {key: record[key] for key in base_keys}
            if active_base in products:
                changed = differences(products[active_base], base, base_keys)
                if changed:
                    raise ValueError(f"Row {row_number} disagrees with its base specifications: {changed}")
            else:
                products[active_base] = dict(base, source_row=row_number)
                voltage_ranges.extend((active_base, low, high) for low, high in
                                      intervals(record.get("supply_voltage_v")))
            variants[orderable] = dict({key: record[key] for key in variant_keys},
                                      part_number=active_base, source_row=row_number, source_url=url)
            raw_rows.append((row_number, tuple(original.values())))
    finally:
        workbook.close()

    if len(variants) != int(expected_count):
        raise ValueError(f"Expected {expected_count} variant rows, found {len(variants)}")

    types = {}
    for key in keys:
        dataset = variants if key in VARIANT_FIELDS else products
        values = [record[key] for record in dataset.values() if record[key] is not None]
        scalar = key in SCALAR_FIELDS or (values and all(numeric(value) is not None for value in values))
        types[key] = "number" if scalar else "text"

    def declarations(columns):
        return ", ".join(
            f"{quote(key)} {'REAL' if types[key] == 'number' else 'TEXT'}"
            + (" PRIMARY KEY COLLATE NOCASE" if key in {"part_number", "orderable_part_number"} else "")
            for key in columns
        )

    def normalized(record, columns):
        return [numeric(record[key]) if types[key] == "number" else record[key] for key in columns]

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".building.sqlite")
    if temporary.exists():
        raise ValueError(f"Build file already exists: {temporary}")
    connection = sqlite3.connect(temporary)
    try:
        with connection:
            connection.executescript(f"""
                PRAGMA foreign_keys = ON;
                CREATE TABLE parts ({declarations(base_keys)}, source_row INTEGER NOT NULL);
                CREATE TABLE variants ({declarations(variant_keys)},
                    part_number TEXT NOT NULL COLLATE NOCASE REFERENCES parts(part_number),
                    source_row INTEGER NOT NULL, source_url TEXT NOT NULL);
                CREATE INDEX variants_base ON variants(part_number);
                CREATE TABLE fields (key TEXT PRIMARY KEY, label TEXT, scope TEXT, kind TEXT);
                CREATE TABLE source_metadata (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE voltage_intervals (
                    part_number TEXT NOT NULL REFERENCES parts(part_number), low REAL, high REAL);
                CREATE INDEX voltage_part ON voltage_intervals(part_number);
                CREATE TABLE raw_rows (source_row INTEGER PRIMARY KEY,
                    {", ".join(quote(key) for key in keys)});
            """)
            connection.executemany(
                f"INSERT INTO parts VALUES ({','.join('?' for _ in range(len(base_keys) + 1))})",
                ([*normalized(record, base_keys), record["source_row"]] for record in products.values()),
            )
            connection.executemany(
                f"INSERT INTO variants VALUES ({','.join('?' for _ in range(len(variant_keys) + 3))})",
                ([*normalized(record, variant_keys), record["part_number"], record["source_row"],
                  record["source_url"]] for record in variants.values()),
            )
            connection.executemany("INSERT INTO fields VALUES (?,?,?,?)", (
                (key, label, "variant" if key in VARIANT_FIELDS else "base", types[key])
                for key, label in zip(keys, labels, strict=True)
            ))
            with workbook_path.open("rb") as stream:
                checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            connection.executemany("INSERT INTO source_metadata VALUES (?,?)", {
                "filename": workbook_path.name, "sheet": sheet.title, "table_url": table_url,
                "sha256": checksum, "parts": str(len(products)), "variants": str(len(variants)),
            }.items())
            connection.executemany("INSERT INTO voltage_intervals VALUES (?,?,?)", voltage_ranges)
            connection.executemany(
                f"INSERT INTO raw_rows VALUES ({','.join('?' for _ in range(len(keys) + 1))})",
                ((row_number, *cells) for row_number, cells in raw_rows),
            )
    except Exception:
        connection.close()
        temporary.unlink()
        raise
    else:
        connection.close()
        temporary.replace(output)
    print(f"Built {output} with {len(products):,} base parts and {len(variants):,} variants.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("catalog.sqlite"))
    arguments = parser.parse_args()
    ingest(arguments.workbook, arguments.output)
