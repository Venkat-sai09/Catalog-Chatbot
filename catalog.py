"""Read-only catalog queries and the shared plan contract."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

VARIANT_FIELDS = {
    "orderable_part_number", "status", "samples", "longevity", "stock",
    "budgetary_price_usd", "carrier_type",
}
DEFAULT_FIELDS = [
    "part_number", "series_name", "main_cpu", "operating_freq_max_mhz",
    "program_memory_kb", "ram_kb", "package",
]


@dataclass(frozen=True)
class CatalogField:
    key: str
    label: str
    scope: str
    kind: str


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Filter(Contract):
    field: str
    op: Literal["eq", "in", "contains", "gte", "lte", "gt", "lt", "known", "unknown", "has_voltage"]
    value: str | None


class Metric(Contract):
    op: Literal["count", "min", "max", "avg"]
    field: str | None


class Plan(Contract):
    _ollama_metrics: dict = PrivateAttr(default_factory=dict)
    action: Literal["query", "clarify"]
    scope: Literal["catalog", "previous", "previous_matches"]
    level: Literal["base", "variant"]
    identifiers: list[str]
    fields: list[str]
    filters: list[Filter]
    group_by: str | None
    metric: Metric | None
    sort_by: str | None
    descending: bool
    limit: int = Field(ge=1, le=100)
    chart: Literal["none", "bar", "scatter"]
    x: str | None
    y: str | None
    explanation: str


@dataclass
class Result:
    rows: list[dict]
    evidence: list[dict]
    total: int
    base_ids: list[str]
    orderable_ids: list[str]
    source: dict
    matching_base_ids: list[str]
    matching_orderable_ids: list[str]
    level: str


def quote(key: str) -> str:
    return '"' + key.replace('"', '""') + '"'


def connect(path: Path):
    if not path.is_file():
        raise ValueError(f"Catalog file is missing: {path}")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.set_progress_handler(lambda: 1, 20_000_000)
    return connection


def load_metadata(connection):
    fields = {
        row["key"]: CatalogField(**dict(row))
        for row in connection.execute("SELECT key, label, scope, kind FROM fields")
    }
    fields["device_type"] = CatalogField("device_type", "Device Type (classified series)", "base", "text")
    fields["in_stock_variants"] = CatalogField("in_stock_variants", "In-stock Variants (snapshot)", "base", "number")
    source = dict(connection.execute("SELECT key, value FROM source_metadata"))
    return fields, source


def _matches(connection, ids):
    if not ids:
        return
    for identifier in ids:
        found = connection.execute(
            """SELECT 1 FROM variants
               WHERE orderable_part_number = ? COLLATE NOCASE OR part_number = ? COLLATE NOCASE
               LIMIT 1""",
            (identifier, identifier),
        ).fetchone()
        if not found:
            raise ValueError(f"Part number not found: {identifier}")


def execute(connection, fields, source, plan: Plan, previous: Result | None = None):
    if plan.action == "clarify":
        raise ValueError(plan.explanation)
    if len(plan.identifiers) > 100 or len(plan.filters) > 20:
        raise ValueError("Use at most 100 identifiers and 20 filters per query.")
    if len({("#" in identifier) for identifier in plan.identifiers}) > 1:
        raise ValueError("The comparison mixes base parts and orderable codes. Use two base part numbers or specify the full orderable code for each.")
    _matches(connection, plan.identifiers)
    level = plan.level
    clauses, params = [], []

    def field(key):
        if key not in fields:
            raise ValueError(f"Unknown catalog field: {key}")
        return fields[key]

    def column(key):
        entry = field(key)
        if key == "device_type":
            return """(CASE
                WHEN p.series_name GLOB 'RA[0-9]*' OR p.series_name GLOB 'RX[0-9]*'
                    OR p.series_name GLOB 'RL78/*' OR p.series_name GLOB 'RH850/*' THEN 'MCU'
                WHEN p.series_name GLOB 'RZ/*' THEN 'MPU'
                END)"""
        if key == "in_stock_variants":
            return """(SELECT COUNT(*) FROM variants AS availability
                WHERE availability.part_number = p.part_number
                AND availability.stock = 'In Stock' COLLATE NOCASE)"""
        if level == "base" and entry.scope == "variant":
            raise ValueError(f"{entry.label} requires the orderable variant level.")
        return ("v." if entry.scope == "variant" else "p.") + quote(key)

    def restrict_identifiers(values, key):
        if not values:
            clauses.append("0 = 1")
            return
        clauses.append(f"{key} IN (SELECT value FROM json_each(?))")
        params.append(json.dumps(values))

    if plan.scope in {"previous", "previous_matches"}:
        if previous is None:
            raise ValueError("Run a catalog query before referring to its results.")
        if plan.scope == "previous_matches":
            if level == previous.level == "variant":
                restrict_identifiers(previous.matching_orderable_ids, "v.orderable_part_number")
            else:
                restrict_identifiers(previous.matching_base_ids, "p.part_number")
        elif not previous.base_ids:
            raise ValueError("The prior result is an aggregate. Choose all previous matches to explore it.")
        elif level == "variant" and previous.orderable_ids:
            restrict_identifiers(previous.orderable_ids, "v.orderable_part_number")
        else:
            restrict_identifiers(previous.base_ids, "p.part_number")

    if plan.identifiers:
        identifiers = [value.upper() for value in plan.identifiers]
        if level == "base":
            clauses.append(
                "(UPPER(p.part_number) IN (SELECT value FROM json_each(?)) OR EXISTS "
                "(SELECT 1 FROM variants AS ids WHERE ids.part_number = p.part_number "
                "AND UPPER(ids.orderable_part_number) IN (SELECT value FROM json_each(?))))"
            )
        else:
            clauses.append(
                "(UPPER(p.part_number) IN (SELECT value FROM json_each(?)) "
                "OR UPPER(v.orderable_part_number) IN (SELECT value FROM json_each(?)))"
            )
        params.extend([json.dumps(identifiers)] * 2)

    for condition in plan.filters:
        info = field(condition.field)
        col = column(info.key)
        op = condition.op
        value = condition.value
        if op in {"known", "unknown"}:
            clauses.append(f"{col} IS {'NOT ' if op == 'known' else ''}NULL")
            continue
        if value is None or not value.strip():
            raise ValueError(f"Enter a value for {info.label}.")
        if op == "has_voltage":
            if info.key != "supply_voltage_v":
                raise ValueError("Voltage coverage applies to Supply Voltage (V).")
            try:
                volts = float(value)
            except ValueError as exc:
                raise ValueError("Enter voltage as a number in volts.") from exc
            clauses.append("""EXISTS (
                SELECT 1 FROM voltage_intervals AS voltage
                WHERE voltage.part_number = p.part_number
                AND voltage.low <= ? AND voltage.high >= ?
            )""")
            params.extend([volts, volts])
        elif op == "contains":
            clauses.append(f"INSTR(LOWER(CAST({col} AS TEXT)), LOWER(?)) > 0")
            params.append(value)
        elif op == "in":
            values = [item.strip() for item in value.split("|") if item.strip()]
            if not values:
                raise ValueError(f"Enter at least one value for {info.label}.")
            clauses.append(
                f"{col} COLLATE NOCASE IN (SELECT value FROM json_each(?))"
            )
            params.append(json.dumps(values))
        elif info.kind == "number":
            try:
                number = float(value)
            except ValueError as exc:
                raise ValueError(f"{info.label} requires a numeric value.") from exc
            operator = {"eq": "=", "gte": ">=", "lte": "<=", "gt": ">", "lt": "<"}[op]
            clauses.append(f"{col} {operator} ?")
            params.append(number)
        elif op == "eq":
            clauses.append(f"{col} = ? COLLATE NOCASE")
            params.append(value)
        else:
            raise ValueError(f"Numeric thresholds require a numeric field. Check {info.label}.")

    relation = "parts AS p"
    if level == "variant":
        relation += " JOIN variants AS v ON v.part_number = p.part_number"
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    match = f" FROM {relation}{where}"
    identity_fields = "p.part_number, v.orderable_part_number" if level == "variant" else "p.part_number"
    matching = connection.execute(f"SELECT {identity_fields}" + match, params).fetchall()
    counted = len(matching)
    matching_base_ids = list(dict.fromkeys(row[0] for row in matching))
    matching_orderable_ids = [row[1] for row in matching] if level == "variant" else []
    evidence = []
    base_ids, orderable_ids = [], []
    if plan.metric:
        metric = plan.metric
        if metric.op == "count":
            measure = "COUNT(*)" if metric.field is None else f"COUNT({column(metric.field)})"
        else:
            if not metric.field or field(metric.field).kind != "number":
                raise ValueError("This metric requires an existing numeric field.")
            measure = f"{metric.op.upper()}({column(metric.field)})"
        group = f"{column(plan.group_by)} AS {quote(plan.group_by)}, " if plan.group_by else ""
        grouping = f" GROUP BY {column(plan.group_by)}" if plan.group_by else ""
        order = " ORDER BY metric " + ("DESC" if plan.descending else "ASC")
        if plan.group_by:
            order += ", " + quote(plan.group_by)
        result_rows = connection.execute(
            f"SELECT {group}{measure} AS metric" + match + grouping + order + " LIMIT ?",
            [*params, plan.limit],
        ).fetchall()
    else:
        if plan.group_by:
            raise ValueError("Grouping requires a count, minimum, maximum, or average.")
        selected = ["part_number"]
        if level == "variant":
            selected.append("orderable_part_number")
        selected.extend(plan.fields or [key for key in DEFAULT_FIELDS if key in fields])
        selected = list(dict.fromkeys(selected))
        if len(selected) > 35:
            raise ValueError("Select at most 35 display fields.")
        projections = ", ".join(f"{column(key)} AS {quote(key)}" for key in selected)
        row_column = "v.source_row" if level == "variant" else "p.source_row"
        ordering = column(plan.sort_by) if plan.sort_by else "p.part_number"
        order = f" ORDER BY {ordering} {'DESC' if plan.descending else 'ASC'} NULLS LAST, {row_column}"
        order_params = []
        if plan.scope == "previous" and plan.sort_by is None:
            if level == "variant" and previous.orderable_ids:
                order_key, order_ids = "v.orderable_part_number", previous.orderable_ids
            else:
                order_key, order_ids = "p.part_number", previous.base_ids
            order = (
                " ORDER BY (SELECT CAST(key AS INTEGER) FROM json_each(?) "
                f"WHERE value = {order_key}) NULLS LAST, {row_column}"
            )
            order_params.append(json.dumps(order_ids))
        link_column = ", v.source_url AS _source_url" if level == "variant" else ""
        result_rows = connection.execute(
            f"SELECT {projections}, {row_column} AS _source_row{link_column}" + match + order + " LIMIT ?",
            [*params, *order_params, plan.limit],
        ).fetchall()
        evidence = [
            {"part_number": row["part_number"], "orderable_part_number": row["orderable_part_number"]
             if level == "variant" else None, "source_row": row["_source_row"],
             "source_url": row["_source_url"] if level == "variant" else None}
            for row in result_rows
        ]
        base_ids = list(dict.fromkeys(row["part_number"] for row in result_rows))
        if level == "variant":
            orderable_ids = [row["orderable_part_number"] for row in result_rows]

    rows = [{key: value for key, value in dict(row).items() if not key.startswith("_source_")}
            for row in result_rows]
    return Result(rows, evidence, counted, base_ids, orderable_ids, source,
                  matching_base_ids, matching_orderable_ids, level)


