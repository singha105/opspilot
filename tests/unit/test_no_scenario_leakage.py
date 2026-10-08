"""The agent must not know the answers: no scenario ids or fault details in its code or prompts."""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from opspilot.faults.scenario import load_scenarios

ROOT = Path(__file__).parents[2]
AGENT_SOURCES = [ROOT / "src" / "opspilot" / "agent", ROOT / "src" / "opspilot" / "prompts"]
# Quantities and generic Kubernetes vocabulary the agent legitimately uses.
_QUANTITY = re.compile(r"^\d+(\.\d+)?(Mi|Gi|m)?$")
GENERIC = {
    "app.kubernetes.io/name",
    "ReadWriteOnce",
    "readiness",
    "liveness",
    "app",
    "cpu",
    "memory",
}


def _values(value: Any, keyed: bool) -> Iterator[str]:
    """String values of an injection; dict keys too where they carry meaning (env names)."""
    if isinstance(value, str):
        yield value
        if " " not in value:  # "ledger-db:5432" also yields "ledger-db"
            yield from value.split(":")
    elif isinstance(value, dict):
        for key, item in value.items():
            if keyed:
                yield str(key)
            yield from _values(item, keyed=True)
    elif isinstance(value, list):
        for item in value:
            yield from _values(item, keyed=True)


def answer_strings() -> set[str]:
    """Scenario ids, plus every distinctive value an injection uses."""
    found: set[str] = set()
    for scenario in load_scenarios(ROOT / "faults" / "scenarios").values():
        found.add(scenario.id)
        for injection in scenario.inject:
            data = injection.model_dump(
                exclude={"type", "deployment", "container"}, exclude_none=True
            )
            found.update(_values(data, keyed=False))
    return {s for s in found if not _QUANTITY.match(s) and s not in GENERIC and len(s) > 2}


def agent_text() -> dict[Path, str]:
    return {
        path: path.read_text()
        for base in AGENT_SOURCES
        for path in base.rglob("*")
        if path.suffix in (".py", ".md")
    }


def test_answer_strings_cover_the_catalog() -> None:
    strings = answer_strings()
    for expected in (
        "oom-payments",
        "MEMORY_BALLAST_MB",
        "/health-wrong",
        "ledger-db",
        "v2-broken",
    ):
        assert any(expected in s for s in strings), expected
    assert "64Mi" not in strings  # quantities are excluded


def leaks(texts: dict[Path, str]) -> list[str]:
    strings = sorted(answer_strings())
    return [
        f"{path.relative_to(ROOT)}: {value!r}"
        for path, text in texts.items()
        for value in strings
        if value in text
    ]


def test_the_guard_catches_a_planted_leak() -> None:
    planted = {ROOT / "src" / "x.py": 'if alert_id == "oom-payments": return "ledger-db"'}
    assert leaks(planted) == ["src/x.py: 'ledger-db'", "src/x.py: 'oom-payments'"]


def test_no_scenario_ids_or_fault_details_in_agent_code_or_prompts() -> None:
    assert leaks(agent_text()) == []
