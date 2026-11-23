# Python Project Study Guide

This project is a Renesas MCU/MPU catalog assistant built with Streamlit.
It combines a local SQLite catalog, a structured query engine, a local natural-language
parser, and optional Ollama or OpenAI planning.

## 1. Project Overview

The program has two main ways to search the catalog:

1. **Query Builder** - Users select fields and filters directly in Streamlit.
2. **Catalog Chat** - Users type natural-language questions. The app first tries local
   parsing, then uses Ollama or OpenAI to create a structured query plan.

The SQLite database is queried only after a plan has been validated. The model does
not generate or execute SQL directly.

## 2. app.py

`app.py` is the main Streamlit application and user-interface entry point.

### Main responsibilities

- Opens the catalog database at startup.
- Loads catalog fields and source metadata.
- Displays the title and catalog snapshot counts.
- Provides provider controls for Ollama, OpenAI, or local commands.
- Creates the Catalog Chat tab.
- Creates the Query Builder tab.
- Converts query plans into Streamlit tables and charts.
- Keeps chat questions and previous query results in `st.session_state`.

### Important functions

- `run(plan)` executes a plan against SQLite and stores the result as the previous result.
- `label(key)` converts a field key into a display label.
- `model_error(error)` converts hosted-model errors into readable messages.
- `render(plan, result)` displays explanations, charts, data tables, source evidence,
  and the query plan.

### Catalog Chat flow

1. Read the user's question.
2. Try `local_lookup`, `local_followup`, `local_ranking`, `local_constraints`, or
   `local_plan`.
3. If no local plan matches, call Ollama or OpenAI.
4. Validate the returned plan using the Pydantic `Plan` model.
5. Execute the plan with `execute()`.
6. Store the question, plan, and result in session state.
7. Render the result in the chat.

### Query Builder flow

1. Select base-part or variant scope.
2. Select display fields.
3. Add filters.
4. Optionally add a metric or chart.
5. Create a `Plan` object.
6. Execute it through `run()`.
7. Render the result.

## 3. catalog.py

`catalog.py` contains the catalog query contract and read-only SQLite execution engine.

### Data contracts

- `CatalogField` describes a field name, label, scope, and type.
- `Filter` describes a filter and supported operation.
- `Metric` describes count, minimum, maximum, or average operations.
- `Plan` describes a complete catalog query.
- `Result` contains displayed rows, source evidence, matching identifiers, and metadata.

### Supported filter operations

- `eq` - Exact match.
- `contains` - Substring match.
- `known` - Field is not empty or null.
- `unknown` - Field is empty or null.
- `gte`, `lte`, `gt`, `lt` - Numeric comparisons.
- `has_voltage` - Check whether a confirmed voltage range covers a requested voltage.

### Important functions

- `connect(path)` opens the SQLite database in read-only mode.
- `load_metadata(connection)` loads field definitions and workbook metadata.
- `_matches(connection, ids)` verifies that requested part numbers exist.
- `execute(connection, fields, source, plan, previous)` converts a validated plan into
  safe SQLite queries.
- `quote(key)` safely quotes database column names.

### Query scopes

- `catalog` searches the complete workbook snapshot.
- `previous` searches the displayed rows from the previous result.
- `previous_matches` searches all rows that matched the previous query, even if only a
  small number were displayed.

### Base and variant levels

- **Base level** represents unique base part numbers such as a microcontroller family
  implementation.
- **Variant level** represents orderable codes such as a specific package or carrier.

Variant fields cannot be requested in a base-level query. The executor raises a clear
error when the plan uses an incompatible field or level.

### SQLite safety

The module validates field names, identifiers, filter counts, output fields, metrics,
and chart requirements before constructing SQL. Parameter values are passed to SQLite
separately instead of being concatenated into SQL text.

## 4. ingest.py

`ingest.py` builds `catalog.sqlite` from the Renesas Excel product-selector export.

It targets the workbook sheet named `ag-grid`.

### Ingestion steps

1. Open the workbook in read-only mode.
2. Read workbook metadata and column labels.
3. Convert labels into safe database keys.
4. Detect the current base part and associated orderable variants.
5. Separate base specifications from variant-specific fields.
6. Normalize values and detect numeric fields.
7. Create a temporary SQLite database.
8. Validate counts and source relationships.
9. Atomically replace the final catalog database.

### Built-in validation

The ingest process stops when:

- The required `part_number` or `orderable_part_number` column is missing.
- Column labels collide after normalization.
- An orderable code has no valid source hyperlink.
- An orderable code does not belong to its base part.
- An orderable code appears more than once.
- The generated variant count differs from the workbook's expected count.
- Rows for one base part disagree on base specifications.
- The temporary database could not be completed.

### Generated tables

- `parts` - Base MCU/MPU specifications.
- `variants` - Orderable variants and availability fields.
- `fields` - Field labels, scopes, and types.
- `source_metadata` - Filename, sheet, URL, checksum, and counts.
- `voltage_intervals` - Parsed single voltage ranges.
- `raw_rows` - Original values for workbook evidence.

### Numeric normalization

Values that are plain numbers or fields in `SCALAR_FIELDS` are stored as SQLite numeric
values. Values containing expressions such as `32, 64` remain source text. This prevents
ambiguous values from being used incorrectly in numeric comparisons.

## 5. planner.py

`planner.py` converts natural-language requests into validated `Plan` objects.

It does not query the catalog directly. It creates a plan, and `catalog.execute()` runs
it.

### Local parsers

- `local_lookup(question, registry, prior)` handles exact base-part or variant lookups
  and two-part comparisons.
- `local_followup(question, registry, prior)` handles explicit follow-ups such as
  changing limits, sorting, or adding a voltage requirement.
- `local_ranking(question, prior)` handles supported top-result and stock follow-ups.
- `local_plan(question, registry, previous)` handles connectivity and RAM-chart
  follow-ups.
- `local_constraints(question, registry)` composes supported RAM, flash, clock,
  CAN-FD, CAN, and stock constraints.

Local parsing is intentionally narrow. It provides fast, predictable behavior for
supported request patterns without a model.

### Ollama integration

`build_ollama_plan()`:

1. Builds a compact field registry and conversation context.
2. Sends the question and context to the local Ollama API.
3. Requests JSON matching the Pydantic `Plan` schema.
4. Validates and normalizes the returned plan.
5. Returns the plan or a readable error.

The Ollama model receives no direct SQL-execution permission.

### OpenAI integration

`build_plan()` sends the question and compact context to OpenAI's structured-response
API. It validates the response as a `Plan` and limits 429 retries.

Billing and quota errors are separated from temporary rate-limit errors.

### Error helpers

- `retry_delay(error)` reads Retry-After information.
- `billing_error(error)` identifies insufficient-credit or spending-limit errors.
- `normalize_plan(registry, plan)` maps common model aliases to exact catalog field
  keys and removes invalid numeric placeholder filters.

### Limitations

The 3B local model may choose the wrong field or invent threshold values. Normalization
handles known aliases and placeholder numbers, but Ollama still depends on model quality.
A larger or better local model, or OpenAI, may produce more reliable plans.

## 6. requirements.txt

This file lists Python dependencies:

- `streamlit` - Web interface.
- `pydantic` - Query-plan validation.
- `pandas` - Tabular result display.
- `altair` - Charts.
- `sqlite3` - Bundled Python SQLite database support.
- `openpyxl` - Excel workbook ingestion.
- `httpx` - Ollama HTTP requests.
- `openai` - Hosted OpenAI structured responses.

SQLite is included in standard CPython installations and does not require a separate
service.

## 7. catalog.sqlite

This is the generated runtime database.

It contains the 5,213 base parts and 11,432 orderable variants represented in the
November 23, 2026 workbook snapshot.

The database is:

- Opened read-only by the application.
- Generated by `ingest.py`.
- Rebuilt if the workbook or ingestion layout changes.
- Protected by foreign keys and indexes in the generated schema.

Stock and pricing values are snapshots and are not live Renesas data.

## 8. Renesas Excel workbook

The workbook is the source evidence for catalog results.

Each base part can have multiple orderable variants. Variant source rows and URLs are
stored by the ingestion process and shown in the workbook evidence panel.

## 9. README.md

`README.md` gives installation, run, Ollama, OpenAI, rebuild, and example-query
instructions.

Typical run command:

```powershell
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Rebuild the database:

```powershell
python ingest.py "C:\path\to\Renesas - Product Selector Microcontrollers & Microprocessors - 2026.11.23.xlsx"
```

## 10. study.md

This file documents how every Python and major project file contributes to the app.

## 11. End-to-End Execution Example

Example question:

```text
Show five parts with the highest maximum clock speed.
```

The execution sequence is:

1. Streamlit receives the question in `app.py`.
2. `app.py` tries the local parser functions in `planner.py`.
3. If no local parser matches, Ollama receives the compact field registry.
4. Ollama returns a JSON object matching `Plan`.
5. Pydantic validates the plan.
6. Aliases are normalized.
7. `catalog.execute()` validates the field keys and constructs SQLite predicates.
8. SQLite returns matching base parts.
9. The result is stored in session state.
10. Streamlit renders the table or chart and workbook evidence.

## 12. Typical Areas for Improvement

- Add automated unit and integration tests.
- Add an Ollama plan fallback or automatic 1.5B model selection.
- Expand regex-based local parsing to more fields.
- Add live stock/pricing integration if an authorized API becomes available.
- Add semantic search for open-ended technical questions.
- Replace or supplement the 3B model with a stronger model for plan generation.
- Add dependency locking or a virtual environment.
- Add tests for invalid aliases and placeholder values.
- Document the generated SQLite schema and field definitions.
