"""Run with python -m streamlit run app.py."""
from __future__ import annotations

import contextlib
import math
import sqlite3
import os
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, OpenAIError

from catalog import DEFAULT_FIELDS, Filter, Metric, Plan, connect, execute, load_metadata
from planner import (
    billing_error, build_ollama_plan, build_plan, local_constraints, local_followup, local_lookup,
    local_plan, local_ranking, retry_delay,
)

TITLE = "Renesas MCU Catalog Assistant"
DEFAULT_DB = Path(__file__).with_name("catalog.sqlite")
st.set_page_config(page_title=TITLE, layout="wide")
st.title(TITLE)
st.caption("Search, compare, and visualize the Renesas microcontroller and microprocessor catalog.")

db = Path(os.getenv("CATALOG_DB", str(DEFAULT_DB)))
try:
    with contextlib.closing(connect(db)) as connection:
        fields, source = load_metadata(connection)
except (OSError, ValueError) as error:
    st.error(str(error))
    st.stop()

st.sidebar.header("Catalog snapshot")
st.sidebar.write(f"{int(source['parts']):,} base parts · {int(source['variants']):,} orderable variants")
st.sidebar.caption(f"{source['filename']} · {source['sheet']}")
st.sidebar.link_button("Renesas product selector", source["table_url"])
provider = st.sidebar.selectbox("Chat provider", ["Ollama", "OpenAI", "Local commands only"])
api_key = None
model = ""
if provider == "Ollama":
    model = st.sidebar.text_input("Ollama model", value=os.getenv("OLLAMA_MODEL", "qwen2.5:3b")).strip()
    st.sidebar.caption("Runs through Ollama on this computer.")
elif provider == "OpenAI":
    api_key = os.getenv("OPENAI_API_KEY") or st.sidebar.text_input("OpenAI API key", type="password")
    model = st.sidebar.text_input("OpenAI model", value=os.getenv("OPENAI_MODEL", "gpt-4.1-mini")).strip()
if "turns" not in st.session_state:
    st.session_state.turns = []
if "prior" not in st.session_state:
    st.session_state.prior = None


def run(plan):
    previous = st.session_state.prior
    with contextlib.closing(connect(db)) as connection:
        result = execute(connection, fields, source, plan, previous[1] if previous else None)
    st.session_state.prior = (plan, result)
    return result


def label(key):
    return fields[key].label if key in fields else key.title()


def model_error(error):
    if isinstance(error, APITimeoutError):
        return "OpenAI did not respond before the request timed out."
    if isinstance(error, APIConnectionError):
        return "The app could not connect to OpenAI. Check internet access or your proxy."
    if isinstance(error, APIStatusError):
        status = error.status_code
        if status == 401:
            return "OpenAI rejected the API key (401). Check the key in the sidebar."
        if status in {403, 404}:
            return f"OpenAI could not authorize or find the selected model ({status}). Check project access and the model in the sidebar."
        if status == 429:
            billing = billing_error(error)
            code = error.code or error.type or "no error code returned"
            if billing:
                return f"{billing} Check API billing or spending limits (429, {code})."
            delay = retry_delay(error)
            wait = f"Wait at least {max(1, math.ceil(delay))} seconds" if delay is not None else "Wait briefly"
            return f"OpenAI temporarily rate-limited the request (429, {code}). {wait} and try again."
        if status == 400:
            return "OpenAI rejected the model request (400). Check whether the selected model supports structured output."
        return f"OpenAI returned HTTP {status}. Try the request again."
    return "The OpenAI request failed."


def render(plan, result):
    if plan.explanation:
        st.write(plan.explanation)
    unit = "base parts" if plan.level == "base" else "orderable variants"
    scope = {"catalog": "Catalog", "previous": "Previous displayed rows",
             "previous_matches": "All previous matches"}[plan.scope]
    if plan.metric:
        summary = f"{result.total:,} matching {unit} · {len(result.rows):,} aggregate rows shown"
    else:
        summary = f"{len(result.rows):,} shown of {result.total:,} matching {unit}"
    st.caption(f"{scope} · {summary} · {result.source['filename']}")
    if not result.rows:
        st.info("No catalog rows matched the selected conditions.")
        return
    frame = pd.DataFrame(result.rows)
    if plan.chart != "none":
        x = plan.x or plan.group_by
        y = plan.y or ("metric" if plan.metric else None)
        if x in frame and y in frame:
            if plan.chart == "bar":
                chart = alt.Chart(frame).mark_bar().encode(
                    x=alt.X(field=x, type="nominal", title=label(x), sort="-y"),
                    y=alt.Y(field=y, type="quantitative", title=label(y)),
                    tooltip=list(frame.columns)[:8],
                )
            else:
                chart = alt.Chart(frame).mark_circle(size=75).encode(
                    x=alt.X(field=x, type="quantitative", title=label(x)),
                    y=alt.Y(field=y, type="quantitative", title=label(y)),
                    tooltip=list(frame.columns)[:8],
                ).interactive()
            st.altair_chart(chart, width="stretch")
        else:
            st.info("This result does not contain both chart fields.")
    st.dataframe(frame.rename(columns=label), hide_index=True, width="stretch")
    with st.expander("Workbook evidence and query"):
        if result.evidence:
            st.dataframe(pd.DataFrame(result.evidence).dropna(axis=1, how="all"),
                         hide_index=True, width="stretch",
                         column_config={"source_url": st.column_config.LinkColumn("Source")})
        else:
            st.write(f"Computed from matching records in worksheet {result.source['sheet']}.")
        st.json(plan.model_dump(exclude={"explanation"}), expanded=False)
        if plan._ollama_metrics:
            st.write("Ollama timings", plan._ollama_metrics)


chat, explore = st.tabs(["Catalog chat", "Query builder"])

with chat:
    if provider == "OpenAI" and not api_key:
        st.info("Add an OpenAI API key or select Ollama for open-ended chat.")
    for turn in st.session_state.turns:
        with st.chat_message("user"):
            st.write(turn["question"])
        with st.chat_message("assistant"):
            if "error" in turn:
                st.write(turn["error"])
            else:
                if "notice" in turn:
                    st.caption(turn["notice"])
                render(turn["plan"], turn["result"])

    question = st.chat_input("Ask about the Renesas catalog")
    if question:
        turn = {"question": question}
        prior = st.session_state.prior
        previous = prior[1] if prior else None
        plan = (
            local_lookup(question, fields, prior)
            or local_followup(question, fields, prior)
            or local_plan(question, fields, previous)
        )
        failure = {
            "Ollama": "Choose an installed Ollama model in the sidebar.",
            "OpenAI": "Open-ended chat requires an OpenAI API key.",
            "Local commands only": "This request needs a model provider.",
        }[provider]
        try:
            with st.spinner("Interpreting your question and querying the catalog"):
                history = [item["question"] for item in st.session_state.turns]
                if plan is None and provider == "Ollama" and model:
                    try:
                        plan = build_ollama_plan(model, question, fields, prior[0] if prior else None,
                                                 previous, history)
                    except ValueError as error:
                        failure = str(error)
                elif plan is None and provider == "OpenAI" and api_key and model:
                    try:
                        with OpenAI(api_key=api_key, timeout=35.0, max_retries=0) as client:
                            plan = build_plan(client, model, question, fields,
                                              prior[0] if prior else None, previous,
                                              history)
                    except OpenAIError as error:
                        failure = model_error(error)
                elif plan is None and api_key:
                    failure = "Choose an OpenAI model in the sidebar."
                if plan is None:
                    raise ValueError(f"{failure} Use Query builder for this request.")
                turn.update(plan=plan, result=run(plan))
        except ValueError as error:
            turn["error"] = str(error)
        except sqlite3.Error:
            turn["error"] = "SQLite could not complete this query. Try a fresh catalog search or fewer filters."
        st.session_state.turns.append(turn)
        st.rerun()

with explore:
    st.subheader("Build a catalog query")
    st.caption("Look up parts, apply filters, compare fields, or group records without a model API key.")
    level = st.radio("Counting level", ["base", "variant"], horizontal=True,
                     format_func=lambda item: "Base parts" if item == "base" else "Orderable variants")
    available = [key for key, info in fields.items() if level == "variant" or info.scope == "base"]
    numeric = [key for key in available if fields[key].kind == "number"]
    scope = st.selectbox("Data scope", ["catalog", "previous", "previous_matches"],
                         format_func=lambda item: {"catalog": "Full catalog", "previous": "Previous displayed parts",
                                                   "previous_matches": "All previous matches"}[item])
    ids = st.text_input("Exact part numbers (comma separated)")
    selected = st.multiselect("Display fields", available, default=[key for key in DEFAULT_FIELDS if key in available],
                              format_func=label, key=f"fields_{level}")
    conditions = []
    for index in range(st.number_input("Number of filters", 0, 10, 0)):
        left, middle, right = st.columns(3)
        with left:
            key = st.selectbox("Field", available, format_func=label, key=f"filter_field_{level}_{index}")
        operations = ["eq", "contains", "known", "unknown"]
        if key in numeric:
            operations += ["gte", "lte", "gt", "lt"]
        if key == "supply_voltage_v":
            operations += ["has_voltage"]
        with middle:
            op = st.selectbox("Condition", operations, key=f"filter_op_{level}_{index}")
        with right:
            value = st.text_input("Value", disabled=op in {"known", "unknown"},
                                  key=f"filter_value_{level}_{index}")
        conditions.append(Filter(field=key, op=op, value=value if op not in {"known", "unknown"} else None))

    left, middle, right = st.columns(3)
    with left:
        metric = st.selectbox("Summarize", [None, "count", "min", "max", "avg"])
        measure = st.selectbox("Measure", numeric, format_func=label) if metric in {"min", "max", "avg"} else None
        group = st.selectbox("Group by", [None, *available], format_func=lambda key: label(key) if key else "None") if metric else None
    with middle:
        sort = st.selectbox("Sort by", [None, *available],
                            format_func=lambda key: label(key) if key else "Part number", disabled=metric is not None)
        descending = st.checkbox("Descending", value=True)
        limit = st.slider("Maximum rows shown", 1, 100, 15)
    with right:
        modes = ["none", "bar"] if metric else ["none", "bar", "scatter"]
        chart = st.selectbox("Chart", modes)
        x, y = None, None
        if chart != "none":
            if metric:
                x, y = group, "metric"
            else:
                x_options = numeric if chart == "scatter" else available
                x = st.selectbox("X axis", x_options, format_func=label)
                y = st.selectbox("Y axis", numeric, format_func=label)
    if st.button("Run query", type="primary"):
        try:
            query = Plan(
                action="query", scope=scope, level=level,
                identifiers=[item.strip() for item in ids.split(",") if item.strip()],
                fields=list(dict.fromkeys(selected + ([x, y] if chart != "none" and not metric else []))),
                filters=conditions, group_by=group, metric=Metric(op=metric, field=measure) if metric else None,
                sort_by=sort, descending=descending, limit=limit, chart=chart, x=x, y=y, explanation="",
            )
            st.session_state.builder_result = (query, run(query))
        except ValueError as error:
            st.error(str(error))
        except sqlite3.Error:
            st.error("SQLite could not complete this query. Try a fresh catalog search or fewer filters.")
    if "builder_result" in st.session_state:
        render(*st.session_state.builder_result)
