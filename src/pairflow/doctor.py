"""Pairflow Doctor — a read-only health check for a flows.json file.

The doctor is a standalone CLI (entry point: ``pairflow-doctor``) that parses
a flows file, reports its shape, validates all function-node JS bodies, and
flags structural anomalies (dangling link references, duplicate ids, wires
to missing nodes). It is intended for two audiences:

  * Pairflow users who want to sanity-check their own flows file before
    pointing the MCP server at it.
  * Pairflow contributors who want a quick smoke test of the read and
    validate paths against any flows.json, theirs or otherwise.

Doctor never modifies the input file unless explicitly run with --smoke,
which performs a destructive write-path exercise on a temporary copy.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import flows
from .validate import validate_function


@dataclass
class CheckReport:
    flows_file: Path
    file_size: int
    total_nodes: int
    tab_count: int
    tabs: list[dict[str, Any]] = field(default_factory=list)
    nodes_by_type: Counter = field(default_factory=Counter)
    function_total: int = 0
    function_valid: int = 0
    function_errors: list[tuple[str, str]] = field(default_factory=list)  # (id, error)
    dangling_link_refs: list[tuple[str, str, str]] = field(default_factory=list)  # (linker_id, type, missing_target)
    dangling_wire_refs: list[tuple[str, str]] = field(default_factory=list)  # (src_id, missing_dst)
    duplicate_ids: list[str] = field(default_factory=list)


def check(flows_file: Path) -> CheckReport:
    text = flows_file.read_text()
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"{flows_file} is not a JSON array")

    by_id: dict[str, dict] = {}
    duplicates: list[str] = []
    for n in data:
        nid = n.get("id")
        if nid in by_id:
            duplicates.append(nid)
        else:
            by_id[nid] = n

    tabs = [n for n in data if n.get("type") == "tab"]
    report = CheckReport(
        flows_file=flows_file,
        file_size=len(text.encode("utf-8")),
        total_nodes=len(data),
        tab_count=len(tabs),
        tabs=[{"id": t["id"], "label": t.get("label", "")} for t in tabs],
        duplicate_ids=duplicates,
    )

    for n in data:
        t = n.get("type", "?")
        if t == "tab":
            continue
        report.nodes_by_type[t] += 1

        # Function-node body validation
        if t == "function" and isinstance(n.get("func"), str):
            report.function_total += 1
            result = validate_function(n["func"])
            if result.ok:
                report.function_valid += 1
            else:
                report.function_errors.append((n["id"], result.error or "?"))

        # Dangling wires
        for grp in n.get("wires", []) or []:
            for dst in grp or []:
                if dst not in by_id:
                    report.dangling_wire_refs.append((n["id"], dst))

        # Dangling link-in / link-out references
        if t in ("link in", "link out"):
            for tgt in n.get("links", []) or []:
                if tgt not in by_id:
                    report.dangling_link_refs.append((n["id"], t, tgt))

    return report


def render(report: CheckReport) -> str:
    lines: list[str] = []
    lines.append(f"Pairflow Doctor — {report.flows_file}")
    lines.append("")
    lines.append(f"  File size:                  {report.file_size:>10,} bytes")
    lines.append(f"  Top-level array:            ok ({report.total_nodes} nodes)")
    lines.append(f"  Tabs:                       {report.tab_count}")
    lines.append(f"  Nodes (excluding tabs):     {sum(report.nodes_by_type.values())}")
    lines.append("")
    if report.nodes_by_type:
        lines.append("  Node types (top 10):")
        for t, c in report.nodes_by_type.most_common(10):
            lines.append(f"    {t:<32s} {c:>5d}")
        if len(report.nodes_by_type) > 10:
            lines.append(f"    ... ({len(report.nodes_by_type) - 10} more)")
        lines.append("")

    if report.function_total:
        status = "ok" if report.function_valid == report.function_total else "FAIL"
        lines.append(
            f"  Function-node bodies:       {report.function_total} checked, "
            f"{report.function_valid} ok, {report.function_total - report.function_valid} invalid  [{status}]"
        )
        for nid, err in report.function_errors[:10]:
            lines.append(f"    - {nid}: {err.splitlines()[0]}")
        if len(report.function_errors) > 10:
            lines.append(f"    ... ({len(report.function_errors) - 10} more)")
        lines.append("")

    lines.append(f"  Duplicate ids:              {len(report.duplicate_ids)}"
                 + ("  [FAIL]" if report.duplicate_ids else "  ok"))
    lines.append(f"  Wires to missing nodes:     {len(report.dangling_wire_refs)}"
                 + ("  [warn]" if report.dangling_wire_refs else "  ok"))
    lines.append(f"  Dangling link references:   {len(report.dangling_link_refs)}"
                 + ("  [warn]" if report.dangling_link_refs else "  ok"))

    if report.dangling_link_refs[:5]:
        lines.append("")
        for src, kind, tgt in report.dangling_link_refs[:5]:
            lines.append(f"    - {kind} {src} → missing {tgt}")
        if len(report.dangling_link_refs) > 5:
            lines.append(f"    ... ({len(report.dangling_link_refs) - 5} more)")

    return "\n".join(lines)


def smoke_exercise(flows_file: Path) -> str:
    """Run the write path against a temporary copy of `flows_file`.

    Adds a node, validates a function body, performs an update, wires/unwires,
    and deletes — printing a one-line status for each. Leaves the original
    file untouched.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="pairflow-doctor-"))
    work = tmpdir / "flows.json"
    shutil.copy2(flows_file, work)
    lines = [f"Smoke exercise on copy: {work}", ""]
    try:
        tabs = flows.list_tabs(work)
        if not tabs:
            return "\n".join(lines + ["  (no tabs in flow — cannot smoke)"])
        tab_id = tabs[0]["id"]

        node = flows.add_node(work, tab_id=tab_id, node_type="debug",
                              props={"name": "pairflow-doctor-smoke"}, x=2000, y=2000)
        lines.append(f"  nr_add_node:     created {node['id']}")

        result = validate_function("return msg;")
        lines.append(f"  validate (ok):   {result.ok}")

        result = validate_function("if (true) {\nreturn")
        lines.append(f"  validate (bad):  {result.ok} (expected False)")

        flows.update_node(work, node["id"], {"name": "doctor-renamed"})
        lines.append("  nr_update_node:  ok")

        # Pick any non-link peer: the smoke node is a `debug`, and `nr_wire`
        # rejects mixing link nodes with non-link nodes by design.
        other = next(
            (n for n in flows.list_nodes(work, tab_id=tab_id)["nodes"]
             if n["id"] != node["id"] and n["type"] not in ("link in", "link out")),
            None,
        )
        if other is not None:
            flows.wire(work, src_id=node["id"], src_port=0, dst_id=other["id"])
            flows.unwire(work, src_id=node["id"], src_port=0, dst_id=other["id"])
            lines.append("  nr_wire/unwire:  ok")

        flows.delete_node(work, node["id"])
        lines.append("  nr_delete_node:  ok")

        backups = sorted(work.parent.glob(f"{work.name}.*.bak"))
        lines.append(f"  backups created: {len(backups)}")
    finally:
        try:
            shutil.rmtree(tmpdir)
        except OSError:
            pass

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pairflow-doctor",
        description="Read-only health check (and optional write-smoke) for a flows.json file.",
    )
    parser.add_argument("--flows", required=True, metavar="PATH",
                        help="Path to the flows.json file to check.")
    parser.add_argument("--smoke", action="store_true",
                        help="In addition to the read-only check, run the write tools "
                             "against a temporary copy of the file (the original is "
                             "never modified).")
    args = parser.parse_args(argv)

    flows_file = Path(args.flows).expanduser()
    if not flows_file.is_file():
        print(f"pairflow-doctor: {flows_file} not found", file=sys.stderr)
        return 2

    try:
        report = check(flows_file)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"pairflow-doctor: {exc}", file=sys.stderr)
        return 1

    print(render(report))

    exit_code = 0
    if report.duplicate_ids or report.function_errors:
        exit_code = 1

    if args.smoke:
        print()
        print(smoke_exercise(flows_file))

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
