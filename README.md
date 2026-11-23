# Renesas MCU Catalog Assistant

A local hackathon app for querying the Renesas microcontroller and microprocessor catalog, comparing parts, and visualizing results. The query builder works without an API key. Chat accepts free-form questions when a model key is configured.

The bundled catalog contains **5,213 base parts and 11,432 orderable variants** from the November 23, 2026 Renesas export. The default provider is **Ollama with `qwen2.5:3b`**, and OpenAI is optional. Use **Local commands only** to demo supported chat requests and Query Builder without a model service.

## Run locally

In PowerShell, from this directory:

```powershell
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

The bundled catalog.sqlite is generated from the November 23, 2026 workbook. To rebuild it:

```powershell
python ingest.py 'C:\path\to\Renesas - Product Selector Microcontrollers & Microprocessors - 2026.11.23.xlsx'
```

### Catalog Chat with Ollama

1. Install and start [Ollama for Windows](https://ollama.com/download/windows).
2. In a new PowerShell window, download the default [Qwen2.5 3B model](https://ollama.com/library/qwen2.5:3b):

```powershell
ollama pull qwen2.5:3b
```

3. In the app sidebar, select **Ollama** and leave the model set to `qwen2.5:3b`. To use another installed model, run `ollama list` and enter its exact tag. You can also set `OLLAMA_MODEL` before starting Streamlit.

The app calls Ollama at `http://127.0.0.1:11434` and validates its structured query plan before running SQLite. With the local model downloaded, this path needs no OpenAI key or credits. Model speed and query-plan quality depend on the model and machine.

The 3B model is the default. CPU inference can take longer on complex queries. Run `ollama ps` while the model is loaded and check the Processor column to see where it is running. The app stops waiting after 180 seconds of inactivity from Ollama and leaves successfully loaded models warm for ten minutes. Standard catalog rankings and open-ended requests are sent to Ollama for query planning.

For hosted chat, select **OpenAI** and enter an API key in the app sidebar or supply OPENAI_API_KEY through the environment. Set OPENAI_MODEL to a model available to your account that supports structured output. The default is gpt-4.1-mini. Only the selected provider receives the question and query context. SQL execution and table generation stay local.

## Try it

1. Ask “Show five parts with the highest maximum clock speed.”
2. Ask “What CAN, UART, USB, and Ethernet connectivity do those parts have?”
3. Ask “Chart RAM for the same parts.”
4. Ask “Compare R9A07G076M30GBA#AC0 and R9A07G076M30GBA#BC0 by carrier type and stock.”
5. Ask “Chart the number of base parts by series across the catalog.”

Chat handles exact part-number lookups, basic two-part comparisons, supported rankings, and “only in-stock” follow-ups locally. It can also rank parts by clock speed and show connectivity or chart RAM when the model is unavailable. For open-ended queries, the model receives a compact field registry and recent query context. Temporary 429 responses get at most one short retry. Billing and quota errors are shown with their code and are sent back immediately. Choose the OpenAI model in the sidebar if your project uses a different one.

Use **Query builder** to set exact part numbers, select fields, add filters, switch between base and variant scope, or configure charts. Successful builder queries also set conversation context. Catalog values represent the workbook export; availability and pricing are snapshots.

## Implementation notes

- Chat runs supported combinations of RAM, program memory, CAN/CAN-FD, clock speed, and in-stock filters locally when it can parse the complete request. Try “Find five in-stock parts with at least 64 KB RAM and two CAN-FD channels, ranked by clock speed.”
- After a detail result, “Make it 10” expands the display from the previous matching set. “Sort those by RAM” reorders the displayed parts, while “Sort all matches by RAM ascending” uses the full prior matching set. “Add a 3.3 V requirement” filters the prior matching set using confirmed supply-voltage coverage. These follow-ups run locally.
- Use either base part numbers or full orderable codes for both sides of a comparison. When the two levels are mixed, the app asks for consistent identifiers.
- The workbook evidence panel displays load, prompt processing, and generation timings when Ollama returns them. Continuing from all previous matches uses the stored part identities for that result.

- The first implementation uses SQLite and Altair via Streamlit because DuckDB and Plotly could not be installed in the available environment. Query plans, the field registry, and chart specifications have separate boundaries so those choices can be revisited.
- Query operations support equality, substring, numeric thresholds on scalar fields, presence, and conservative single-interval voltage checks. Ambiguous scalar values such as RAM listed as “32, 64” are excluded from numeric matches and retained in the original row. Complex multi-supply values and tiered prices are retained as source text.
- The initial chat translates questions into queries and renders results with provenance. Semantic retrieval, free-form grounded synthesis, currency and price-tier comparisons, and production deployment are later work. Context lasts for the active session.
- The ingestion adapter targets the Renesas Product Selector export layout. It stages a replacement database and stops if source counts, variant identities, or shared specifications disagree.

See [requirements](../requirements.md), [architecture](../architecture.md), and [technology stack](../tech-stack.md).
