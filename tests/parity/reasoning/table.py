#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Generate the REASONING.* parity table from YAML fixtures."""

from __future__ import annotations

import argparse
import datetime
import html as html_lib
import json
import re
import subprocess
import zoneinfo
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests/parity/reasoning/fixtures"
REASONING_CASES_MD = REPO_ROOT / "lib/parsers/REASONING_CASES.md"
SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = REPO_ROOT / "tests/parity"


def _make_jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        trim_blocks=False,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )


def _commit_sha() -> str | None:
    try:
        return (
            subprocess.check_output(
                ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def _case_sort_key(case_id: str) -> tuple[int, int, str]:
    parts = case_id.split(".")
    mode = 0 if parts[1] == "batch" else 1
    top = int(parts[2])
    sub = parts[3] if len(parts) > 3 else ""
    return (mode, top, sub)


def _normalize_text(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str) and v.strip() == "":
        return None
    return v


def _canonical(d: dict[str, Any]) -> str:
    d = {
        **d,
        "normal_text": _normalize_text(d.get("normal_text")),
        "reasoning_text": _normalize_text(d.get("reasoning_text")),
    }
    d.pop("reason", None)
    return json.dumps(d, sort_keys=True, separators=(",", ":"))


def _load() -> tuple[dict[str, dict[str, Any]], list[str], dict[tuple[str, str], Path]]:
    rows: dict[str, dict[str, Any]] = {}
    columns = set()
    refs = {}
    for fp in sorted(FIXTURES.glob("*/REASONING.*.yaml")):
        doc = yaml.safe_load(fp.read_text())
        family = doc["family"]
        row = rows.setdefault(
            family,
            {
                "family": family,
                "model_label": doc.get("model_label", family),
                "cases": {},
            },
        )
        for case_id, case in doc["cases"].items():
            columns.add(case_id)
            row["cases"][case_id] = case
            refs[(family, case_id)] = fp
    return rows, sorted(columns, key=_case_sort_key), refs


def _is_na_stub(case: dict[str, Any]) -> bool:
    return set(case) == {"description", "reason"}


def _cell(case: dict[str, Any] | None) -> tuple[str, str]:
    if case is None:
        return "—", "missing fixture coverage"
    if "expected" not in case:
        if _is_na_stub(case):
            return "n/a", case["reason"]
        return "?", "fixture has no expected block"

    expected = case["expected"]
    dynamo = expected["dynamo"]
    markers = []
    unavailable = 0
    tooltip_parts = [case.get("description", "")]

    for impl, letter in (("vllm", "V"), ("sglang", "S")):
        spec = expected[impl]
        if "unavailable" in spec:
            unavailable += 1
            tooltip_parts.append(f"{impl}: unavailable — {spec['unavailable']}")
            continue
        if "error" in spec:
            markers.append(f"{letter}!")
            tooltip_parts.append(f"{impl}: expected error — {spec['error']}")
            continue
        if _canonical(spec) == _canonical(dynamo):
            tooltip_parts.append(f"{impl}: matches Dynamo")
            continue
        suffix = "" if spec.get("reason") else "?"
        markers.append(f"{letter}{suffix}")
        reason = spec.get("reason", "research-needed")
        tooltip_parts.append(f"{impl}: diverges — {reason}")

    if unavailable == 2:
        return "n/a", "\n".join(p for p in tooltip_parts if p)
    if markers:
        return "".join(markers), "\n".join(p for p in tooltip_parts if p)
    return "=", "\n".join(p for p in tooltip_parts if p)


def _markdown(rows: dict[str, dict[str, Any]], columns: list[str]) -> str:
    out = []
    header = ["Model / parser", *[c.replace("REASONING.", "") for c in columns]]
    out.append("| " + " | ".join(header) + " |")
    out.append("|" + "|".join(["---", *[":-:" for _ in columns]]) + "|")
    for family in sorted(rows):
        row = rows[family]
        cells = [f"{row['model_label']} / `{family}`"]
        for case_id in columns:
            marker, _ = _cell(row["cases"].get(case_id))
            cells.append(marker)
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def _display_case_id(case_id: str) -> str:
    return case_id.replace("REASONING.", "")


def _case_group_label(case_id: str) -> str:
    return "Batch" if _display_case_id(case_id).startswith("batch.") else "Streaming"


def _case_group_key(case_id: str) -> str:
    return _display_case_id(case_id).split(".", 1)[0]


def _case_band_class(case_id: str) -> str:
    return "case-band-0" if _case_group_key(case_id) == "batch" else "case-band-1"


def _case_runs(columns: list[str]) -> list[list[str]]:
    runs = []
    start = 0
    while start < len(columns):
        label = _case_group_label(columns[start])
        end = start + 1
        while end < len(columns) and _case_group_label(columns[end]) == label:
            end += 1
        runs.append(columns[start:end])
        start = end
    return runs


def _column_placeholder_html(key: str, tag: str = "td") -> str:
    key_attr = html_lib.escape(key)
    return (
        f'<{tag} class="col-placeholder col-hidden" '
        f'data-col-placeholder-group="{key_attr}"></{tag}>'
    )


def _column_control_header_html(
    key: str,
    label: str,
    *,
    default_visible: bool,
    css_class: str = "",
    colspan: int | None = None,
) -> str:
    key_attr = html_lib.escape(key)
    visible = "true" if default_visible else "false"
    classes = " ".join(part for part in ("column-control", css_class) if part)
    span_size = colspan if colspan is not None else 1
    if colspan is not None:
        span_attr = f'colspan="{colspan}" data-expanded-colspan="{colspan}"'
    else:
        span_attr = 'rowspan="2"'
    action = "Collapse" if default_visible else "Expand"
    return (
        f'<th class="{html_lib.escape(classes)}" data-col-control-group="{key_attr}" '
        f"{span_attr}>"
        f'<button type="button" class="col-toggle" data-col-toggle="{key_attr}" '
        f'data-col-label="{html_lib.escape(label)}" data-col-span="{span_size}" '
        f'data-default-visible="{visible}" aria-pressed="{visible}" '
        f'aria-label="{action} {html_lib.escape(label)} column">'
        '<span class="col-toggle-symbol" aria-hidden="true"></span>'
        f'<span class="col-toggle-label">{html_lib.escape(label)}</span>'
        "</button></th>"
    )


def _case_group_headers_html(columns: list[str]) -> str:
    headers = [
        _column_control_header_html("model", "Model", default_visible=True),
        _column_control_header_html("parser", "Parser", default_visible=True),
    ]
    for run in _case_runs(columns):
        label = _case_group_label(run[0])
        group_key = _case_group_key(run[0])
        headers.append(
            _column_control_header_html(
                group_key,
                label,
                default_visible=True,
                css_class=f"case-group {_case_band_class(run[0])}",
                colspan=len(run),
            )
        )
    return "".join(headers)


def _parse_case_descriptions() -> dict[str, str]:
    if not REASONING_CASES_MD.exists():
        return {}
    pat = re.compile(
        r"\*\*`REASONING\.(batch|stream)\.([0-9]+(?:\.[a-z])?)`\*\*\s+(.+)"
    )
    out = {}
    lines = REASONING_CASES_MD.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        match = pat.search(lines[i])
        if not match:
            i += 1
            continue
        mode, sub, desc = match.groups()
        body_parts = [desc.strip()]
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if not nxt.strip() or not nxt.startswith(" "):
                break
            if pat.search(nxt):
                break
            body_parts.append(nxt.strip())
            j += 1
        out.setdefault(f"{mode}.{sub}", " ".join(body_parts).rstrip("."))
        i = j
    return out


def _case_header_html(case_id: str, descriptions: dict[str, str]) -> str:
    display = _display_case_id(case_id)
    desc = descriptions.get(display) or ""
    href = "../../../lib/parsers/REASONING_CASES.md"
    return (
        f'<th class="case-sub {_case_band_class(case_id)}" '
        f'data-col-hide-group="{html_lib.escape(_case_group_key(case_id))}">'
        f'<a href="{href}" title="{html_lib.escape(desc)}">{html_lib.escape(display)}</a></th>'
    )


def _case_headers_html(columns: list[str], descriptions: dict[str, str]) -> str:
    headers = []
    for run in _case_runs(columns):
        headers.extend(_case_header_html(case_id, descriptions) for case_id in run)
        headers.append(_column_placeholder_html(_case_group_key(run[0]), tag="th"))
    return "".join(headers)


def _glossary_groups(
    descriptions: dict[str, str], columns: list[str]
) -> list[dict[str, object]]:
    if not descriptions:
        return []
    return [
        {
            "label": _case_group_label(run[0]),
            "rows": [
                (
                    _display_case_id(case_id),
                    descriptions.get(_display_case_id(case_id), ""),
                )
                for case_id in run
            ],
        }
        for run in _case_runs(columns)
    ]


def _format_value(name: str, value: Any) -> str:
    return f"{name}={json.dumps(value, ensure_ascii=False)}"


def _format_output_block_html(block: dict[str, Any] | None) -> str:
    if block is None:
        return html_lib.escape("(no expectation)")
    if "unavailable" in block:
        return html_lib.escape(f"unavailable: {block['unavailable']}")
    if "error" in block:
        return html_lib.escape(f"error matching {block['error']!r}")

    lines = [
        _format_value("reasoning_text", block.get("reasoning_text")),
        _format_value("normal_text", block.get("normal_text")),
    ]
    if block.get("reason"):
        lines.append(_format_value("reason", block["reason"]))
    return html_lib.escape("\n".join(lines))


def _input_text(case: dict[str, Any]) -> str:
    if "chunks" in case:
        return "chunks=" + json.dumps(case["chunks"], ensure_ascii=False, indent=2)
    return _format_value("input_text", case.get("model_text", ""))


def _tooltip_html(case_id: str, family: str, case: dict[str, Any]) -> str:
    parts = [
        f'<div class="ttip-head">{html_lib.escape(case_id)} — {html_lib.escape(family)}</div>'
    ]
    description = case.get("description")
    if description:
        parts.append(f'<pre class="ttip-pre">{html_lib.escape(str(description))}</pre>')
    if "expected" not in case:
        reason = case.get("reason", "fixture has no expected block")
        parts.append('<div class="ttip-section">reason</div>')
        parts.append(f'<pre class="ttip-pre">{html_lib.escape(str(reason))}</pre>')
        return "".join(parts)

    parts.append('<div class="ttip-section">input_text</div>')
    parts.append(f'<pre class="ttip-pre">{html_lib.escape(_input_text(case))}</pre>')
    for impl in ("dynamo", "vllm", "sglang"):
        parts.append(f'<div class="ttip-section">{impl}</div>')
        parts.append(
            f'<pre class="ttip-pre">{_format_output_block_html(case["expected"].get(impl))}</pre>'
        )
    return "".join(parts)


def _missing_tooltip_html(case_id: str, family: str) -> str:
    return (
        f'<div class="ttip-head">{html_lib.escape(case_id)} — '
        f"{html_lib.escape(family)}</div>"
        '<pre class="ttip-pre">missing fixture coverage</pre>'
    )


def _cell_class(marker: str) -> str:
    if marker == "—":
        return "missing"
    if marker == "n/a":
        return "na"
    if "!" in marker:
        return "err"
    if "?" in marker:
        return "research"
    if marker == "=":
        return "ok"
    return "documented"


def _render_cell_html(
    case: dict[str, Any] | None,
    family: str,
    case_id: str,
    refs: dict[tuple[str, str], Path],
) -> str:
    if case is None:
        marker = "—"
        href = ""
        tooltip = _missing_tooltip_html(case_id, family)
    else:
        marker, _ = _cell(case)
        href = str(refs[(family, case_id)].relative_to(SCRIPT_DIR))
        tooltip = _tooltip_html(case_id, family, case)

    classes = f"cell {_cell_class(marker)} {_case_band_class(case_id)}"
    group_key = html_lib.escape(_case_group_key(case_id))
    label = html_lib.escape(marker)
    if href:
        body = f'<a href="{html_lib.escape(href)}">{label}</a>'
    else:
        body = label
    return (
        f'<td class="{classes}" data-col-hide-group="{group_key}">'
        f'{body}<div class="ttip">{tooltip}</div></td>'
    )


def _parser_cell_html(family: str) -> str:
    tooltip = (
        f'<div class="ttip-head">{html_lib.escape(family)}</div>'
        '<pre class="ttip-pre">Reasoning parser family from fixture YAML.</pre>'
    )
    return (
        '<td class="parser" data-col-hide-group="parser">'
        f'<code>{html_lib.escape(family)}</code><div class="ttip">{tooltip}</div></td>'
    )


def _render_row_html(
    family: str,
    row: dict[str, Any],
    columns: list[str],
    refs: dict[tuple[str, str], Path],
) -> str:
    cells = [
        f'<tr><td class="model" data-col-hide-group="model">'
        f'{html_lib.escape(row["model_label"])}</td>',
        _column_placeholder_html("model"),
        _parser_cell_html(family),
        _column_placeholder_html("parser"),
    ]
    for run in _case_runs(columns):
        cells.extend(
            _render_cell_html(row["cases"].get(case_id), family, case_id, refs)
            for case_id in run
        )
        cells.append(_column_placeholder_html(_case_group_key(run[0])))
    cells.append("</tr>")
    return "".join(cells)


def _compute_stats(
    rows: dict[str, dict[str, Any]], columns: list[str]
) -> dict[str, int]:
    stats = {
        "families": len(rows),
        "sub_cases": len(columns),
        "slots": len(rows) * len(columns),
        "real": 0,
        "parity": 0,
        "documented": 0,
        "research": 0,
        "errors": 0,
        "na": 0,
        "missing": 0,
    }
    for family in rows:
        for case_id in columns:
            marker, _ = _cell(rows[family]["cases"].get(case_id))
            if marker == "—":
                stats["missing"] += 1
            elif marker == "n/a":
                stats["na"] += 1
            else:
                stats["real"] += 1
                if marker == "=":
                    stats["parity"] += 1
                elif "!" in marker:
                    stats["errors"] += 1
                elif "?" in marker:
                    stats["research"] += 1
                else:
                    stats["documented"] += 1
    return stats


def _legend_html() -> str:
    return (
        "<strong>Legend:</strong> "
        '<span style="color:#0a7d2c">=</span> full parity '
        "(Dynamo, vLLM, and SGLang produce the same reasoning and normal text) · "
        '<span style="color:#555">V/S</span> divergence '
        "(V = vLLM, S = SGLang; intentional, has <code>reason:</code>) · "
        '<span style="color:#c63">?</span> more research needed '
        "(e.g. V?, S? — diverges with no <code>reason:</code> yet) · "
        '<span style="color:#b00">!</span> expected-error suffix '
        "(e.g. V!, S! — engine crashes by design) · "
        '<span style="color:#aaa">n/a</span> unavailable or not applicable · '
        '<span style="color:#8a6d3b">—</span> missing fixture coverage.'
        "<br><br>"
        "<strong>Tooltip fields:</strong> "
        "<code>input_text</code>=raw model output or stream chunks fed into the reasoning parser · "
        "<code>reasoning_text</code>=hidden reasoning content extracted by the parser · "
        "<code>normal_text</code>=residual text passed onward to response assembly or downstream parsers."
    )


def _html(
    rows: dict[str, dict[str, Any]],
    columns: list[str],
    refs: dict[tuple[str, str], Path],
) -> str:
    generated = datetime.datetime.now(
        zoneinfo.ZoneInfo("America/Los_Angeles")
    ).strftime("%Y-%m-%d %H:%M %Z")
    sha = _commit_sha()
    descriptions = _parse_case_descriptions()
    body_rows = [
        f'<tr class="section"><td data-section-span colspan="{2 + len(columns)}">'
        "Reasoning parsers</td></tr>"
    ]
    for family in sorted(rows):
        body_rows.append(_render_row_html(family, rows[family], columns, refs))

    return (
        _make_jinja_env()
        .get_template("parity_table.html.j2")
        .render(
            title="Dynamo reasoning parser parity table",
            stamp=generated,
            sha=sha,
            short_sha=sha[:12] if sha else "",
            command="python3 tests/parity/generate_parity_table.py reasoning --html",
            output="tests/parity/reasoning/PARITY.html",
            tabs=[],
            panels=[
                {
                    "id": "tab-reasoning",
                    "mode": "reasoning",
                    "active": True,
                    "group_headers": _case_group_headers_html(columns),
                    "sub_headers": _case_headers_html(columns, descriptions),
                    "body_rows": body_rows,
                    "stats": _compute_stats(rows, columns),
                    "glossary_groups": _glossary_groups(descriptions, columns),
                }
            ],
            peer_versions=[],
            legend_html=_legend_html(),
            case_docs_href="../../../lib/parsers/REASONING_CASES.md",
            case_docs_label="lib/parsers/REASONING_CASES.md",
            case_prefix="REASONING.",
        )
    )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", action="store_true", help="emit HTML instead of Markdown")
    args = ap.parse_args(argv)

    rows, columns, refs = _load()
    if args.html:
        print(_html(rows, columns, refs))
    else:
        print(_markdown(rows, columns))


if __name__ == "__main__":
    main()
