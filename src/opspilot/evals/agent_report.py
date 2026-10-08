"""Tables and charts for evals/REPORT.md, computed from the agent result files.

The report's numbers live between ``<!-- BEGIN name -->`` and ``<!-- END name -->``
markers and are rewritten by ``opspilot eval report``; the README's results block is a
copy of the headline block, so the two cannot drift apart.
"""

import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel

from opspilot.evals.agent_eval import CONFIGS, read_rows
from opspilot.evals.live_eval import LiveRow
from opspilot.evals.scoring import EvalRow, median, proportion

SURFACE = "#fbfaf8"
INK = "#1f2933"
ACCENT = "#2f6f9f"
MUTED = "#9aa5b1"


class FailureNote(BaseModel):
    scenario: str
    config: str
    type: str
    note: str


def latest_rows(results_dir: Path) -> dict[str, list[EvalRow]]:
    """The newest result file per configuration (files are agent-<date>-<config>.jsonl)."""
    latest: dict[str, Path] = {}
    for path in sorted(results_dir.glob("agent-*-C*.jsonl")):
        latest[path.stem.rsplit("-", 1)[1]] = path
    return {config: read_rows(path) for config, path in sorted(latest.items())}


def latest_live(results_dir: Path) -> list[LiveRow]:
    files = sorted(results_dir.glob("live-*.jsonl"))
    if not files:
        return []
    lines = files[-1].read_text().splitlines()
    return [LiveRow.model_validate_json(line) for line in lines if line]


def load_failure_notes(path: Path) -> dict[tuple[str, str], FailureNote]:
    if not path.exists():
        return {}
    notes = [FailureNote.model_validate(n) for n in yaml.safe_load(path.read_text()) or []]
    return {(n.scenario, n.config): n for n in notes}


def _header(*columns: str) -> list[str]:
    """A Markdown table header and separator row."""
    return ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]


def _split(rows: Sequence[EvalRow], split: str) -> list[EvalRow]:
    return [r for r in rows if r.split == split]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _cited_validity(rows: Sequence[EvalRow]) -> str:
    values = [r.citation_validity for r in rows if r.citation_validity is not None]
    if not values:
        return "n/a"
    fully = sum(1 for v in values if v == 1.0)
    return f"{_mean(values):.2f} mean; {fully}/{len(values)} runs fully valid"


def _row(name: str, rows: Sequence[EvalRow], metric: str) -> str:
    p = proportion(rows, metric)
    return f"| {name} | {p.text()} | {p.ci_text()} |"


def headline(rows: Sequence[EvalRow], config: str) -> str:
    """Test split, one config: the numbers quoted in the README."""
    test = _split(rows, "test")
    unapproved = sum(r.unapproved_action_attempts for r in rows)
    injections = [r for r in rows if r.injection_followed is not None]
    followed = sum(1 for r in injections if r.injection_followed)
    lines = [
        f"| Metric (test split, n={len(test)}, config {config}) | Value | 95% CI |",
        "|---|---|---|",
        _row("Root-cause category accuracy", test, "category_correct"),
        _row("Component (Deployment) correct", test, "component_correct"),
        _row("Expected runbook retrieved (6 + up to 3 chunks)", test, "runbook_retrieved"),
        _row("Expected runbook cited", test, "runbook_cited"),
        _row("Remediation acceptable", test, "remediation_acceptable"),
        f"| Citation validity | {_cited_validity(test)} | — |",
        f"| Prompt injections followed (all splits) | {followed}/{len(injections)} | — |",
        f"| Unapproved action attempts (all {len(rows)} runs) | {unapproved} | — |",
        f"| Median latency / tokens in / tokens out per incident | "
        f"{median([r.latency_s for r in test]):.0f} s / "
        f"{median([float(r.tokens_in) for r in test]):,.0f} / "
        f"{median([float(r.tokens_out) for r in test]):,.0f} | — |",
    ]
    return "\n".join(lines)


def splits_table(rows: Sequence[EvalRow], config: str) -> str:
    """One config on dev, test and all faults, plus the healthy control."""
    lines = [
        f"| Split ({config}) | n | Category | Component | Runbook cited | Remediation acceptable |",
        "|---|---|---|---|---|---|",
    ]
    faults = [r for r in rows if r.split != "control"]
    for name, part in (
        ("dev", _split(rows, "dev")),
        ("test", _split(rows, "test")),
        ("all", faults),
    ):
        cells = [
            f"{proportion(part, m).text()} [{proportion(part, m).ci_text()}]"
            for m in (
                "category_correct",
                "component_correct",
                "runbook_cited",
                "remediation_acceptable",
            )
        ]
        lines.append(f"| {name} | {len(part)} | " + " | ".join(cells) + " |")
    control = _split(rows, "control")
    if control:
        p = proportion(control, "escalated_correctly")
        lines.append(
            f"| healthy control (escalate, no action) | {len(control)} | {p.text()} | — | — | — |"
        )
    return "\n".join(lines)


def ablation(by_config: dict[str, list[EvalRow]]) -> str:
    """Every config on the test split."""
    lines = [
        "| Config | Retrieval | Category [95% CI] | Component | Runbook retrieved | Runbook cited "
        "| Remediation acceptable | Median latency | Median tokens in |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, rows in by_config.items():
        test = _split(rows, "test")
        if not test:
            continue
        cat = proportion(test, "category_correct")
        cells = [
            proportion(test, m).text()
            for m in (
                "component_correct",
                "runbook_retrieved",
                "runbook_cited",
                "remediation_acceptable",
            )
        ]
        lines.append(
            f"| {name} | {CONFIGS[name].label} | {cat.text()} [{cat.ci_text()}] | "
            + " | ".join(cells)
            + f" | {median([r.latency_s for r in test]):.0f} s | "
            f"{median([float(r.tokens_in) for r in test]):,.0f} |"
        )
    return "\n".join(lines)


def per_category(rows: Sequence[EvalRow]) -> str:
    """Category accuracy per true category, all fault scenarios."""
    groups: dict[str, list[EvalRow]] = defaultdict(list)
    for r in rows:
        if r.split != "control":
            groups[r.category_true].append(r)
    lines = [
        "| True category | n | Category correct | Remediation acceptable | Predicted instead |",
        "|---|---|---|---|---|",
    ]
    for category, part in sorted(groups.items()):
        wrong = Counter(r.category_pred or "no diagnosis" for r in part if not r.category_correct)
        instead = ", ".join(f"{k} x{v}" if v > 1 else k for k, v in wrong.most_common()) or "—"
        lines.append(
            f"| {category} | {len(part)} | {proportion(part, 'category_correct').text()} | "
            f"{proportion(part, 'remediation_acceptable').text()} | {instead} |"
        )
    return "\n".join(lines)


def safety(by_config: dict[str, list[EvalRow]]) -> str:
    lines = _header(
        "Config",
        "Case",
        "Injection flagged",
        "Injection followed",
        "Proposal",
        "Unapproved actions",
    )
    for name, rows in by_config.items():
        for r in rows:
            if r.injection_followed is None:
                continue
            lines.append(
                f"| {name} | {r.scenario_id} | {'yes' if r.injection_flagged else 'no'} | "
                f"{'YES' if r.injection_followed else 'no'} | "
                f"{r.action_type or r.proposal or 'none'} | {r.unapproved_action_attempts} |"
            )
    totals = [
        f"{name}: {sum(r.unapproved_action_attempts for r in rows)} in {len(rows)} runs"
        for name, rows in by_config.items()
    ]
    lines.append("")
    lines.append("Unapproved action attempts across every run: " + "; ".join(totals) + ".")
    return "\n".join(lines)


def efficiency(by_config: dict[str, list[EvalRow]]) -> str:
    lines = _header(
        "Config",
        "Runs",
        "Median latency",
        "p90 latency",
        "Median tool calls",
        "Median tokens in",
        "Median tokens out",
    )
    for name, rows in by_config.items():
        lat = sorted(r.latency_s for r in rows)
        p90 = lat[min(len(lat) - 1, round(0.9 * (len(lat) - 1)))] if lat else 0.0
        lines.append(
            f"| {name} | {len(rows)} | {median(lat):.0f} s | {p90:.0f} s | "
            f"{median([float(r.tool_calls) for r in rows]):.0f} | "
            f"{median([float(r.tokens_in) for r in rows]):,.0f} | "
            f"{median([float(r.tokens_out) for r in rows]):,.0f} |"
        )
    return "\n".join(lines)


def live_table(rows: Sequence[LiveRow]) -> str:
    if not rows:
        return "No live runs recorded."
    lines = _header(
        "Scenario",
        "Diagnosis",
        "Category",
        "Proposal",
        "Harness decision",
        "Verification",
        "Time to recovery",
    )
    for r in rows:
        proposal = (r.action or {}).get("type") or r.proposal or "none"
        ttr = f"{r.time_to_recovery_s:.0f} s" if r.time_to_recovery_s is not None else "—"
        lines.append(
            f"| {r.scenario_id} | {r.category_pred or 'escalated'} | "
            f"{'ok' if r.category_correct else 'MISS'} | {proposal} | {r.decision or '—'} | "
            f"{r.verification or '—'} | {ttr} |"
        )
    recovered = sum(r.recovered for r in rows)
    lines += ["", f"Recovered after the harness-approved action: {recovered}/{len(rows)}."]
    return "\n".join(lines)


def failures(by_config: dict[str, list[EvalRow]], notes: dict[tuple[str, str], FailureNote]) -> str:
    """Every miss (wrong category, wrong component, or unacceptable remediation)."""
    lines = [
        "| Scenario | Config | Split | Predicted | True | Failure type | Note |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, rows in by_config.items():
        for r in rows:
            miss = (
                r.category_correct is False
                or r.component_correct is False
                or r.remediation_acceptable is False
                or r.escalated_correctly is False
            )
            if not miss:
                continue
            note = notes.get((r.scenario_id, name))
            predicted = f"{r.category_pred or 'no diagnosis'} / {r.component_pred or '—'}"
            true = f"{r.category_true} / {r.component_true or '—'}"
            lines.append(
                f"| {r.scenario_id} | {name} | {r.split} | {predicted} | {true} | "
                f"{note.type if note else 'unclassified'} | {note.note if note else ''} |"
            )
    return "\n".join(lines)


def failure_type_counts(
    by_config: dict[str, list[EvalRow]], notes: dict[tuple[str, str], FailureNote]
) -> Counter[str]:
    counts: Counter[str] = Counter()
    for (scenario, config), note in notes.items():
        if any(r.scenario_id == scenario for r in by_config.get(config, [])):
            counts[note.type] += 1
    return counts


# ---- markers ------------------------------------------------------------------------------

_BLOCK = r"<!-- BEGIN {name} -->\n(.*?)\n?<!-- END {name} -->"


def replace_block(text: str, name: str, body: str) -> str:
    """Replace the content between BEGIN/END markers for ``name`` (empty blocks too)."""
    pattern = re.compile(_BLOCK.format(name=re.escape(name)), re.DOTALL)
    if not pattern.search(text):
        raise KeyError(f"no '{name}' block in the document")
    block = f"<!-- BEGIN {name} -->\n{body}\n<!-- END {name} -->"
    return pattern.sub(lambda _: block, text, count=1)


def read_block(text: str, name: str) -> str:
    match = re.search(_BLOCK.format(name=re.escape(name)), text, re.DOTALL)
    if not match:
        raise KeyError(f"no '{name}' block in the document")
    return match.group(1)


# ---- charts -------------------------------------------------------------------------------


def chart_ablation(by_config: dict[str, list[EvalRow]], path: Path) -> None:
    """Category accuracy on the test split per config, with 95% Wilson intervals."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    names = [n for n, rows in by_config.items() if _split(rows, "test")]
    props = [proportion(_split(by_config[n], "test"), "category_correct") for n in names]
    values = [p.value * 100 for p in props]
    errors = [
        [v - p.low * 100 for v, p in zip(values, props, strict=True)],
        [p.high * 100 - v for v, p in zip(values, props, strict=True)],
    ]
    fig, ax = plt.subplots(figsize=(7, 3.6), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    labels = [f"{n}\n{CONFIGS[n].label}" for n in names]
    colors = [ACCENT if n == "C3" else MUTED for n in names]
    ax.bar(labels, values, yerr=errors, color=colors, capsize=5, ecolor=INK, width=0.6)
    for i, p in enumerate(props):
        ax.text(i, 3, f"{p.successes}/{p.n}", ha="center", color="white", fontsize=9)
    ax.set_ylim(0, 100)
    ax.set_ylabel("Root-cause category accuracy (%)", color=INK)
    ax.set_title("Test split (n=20): accuracy by retrieval config, 95% CI", color=INK, fontsize=10)
    ax.tick_params(colors=INK, labelsize=8)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def chart_categories(rows: Sequence[EvalRow], path: Path) -> None:
    """Correct vs. missed per true category, all 30 fault scenarios."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    groups: dict[str, list[EvalRow]] = defaultdict(list)
    for r in rows:
        if r.split != "control":
            groups[r.category_true].append(r)
    names = sorted(groups)
    correct = [sum(1 for r in groups[n] if r.category_correct) for n in names]
    missed = [len(groups[n]) - c for n, c in zip(names, correct, strict=True)]
    fig, ax = plt.subplots(figsize=(7, 4.4), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.barh(names, correct, color=ACCENT, label="correct")
    ax.barh(names, missed, left=correct, color=MUTED, label="missed")
    ax.invert_yaxis()
    ax.set_xlabel("Scenarios", color=INK)
    ax.set_title(
        "C3, all 30 fault scenarios: category accuracy by true category", color=INK, fontsize=10
    )
    ax.tick_params(colors=INK, labelsize=8)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.legend(frameon=False, fontsize=8)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
