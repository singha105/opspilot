"""Each scenario injects, shows its expected symptom, and resets cleanly.

Needs: make cluster-up demo-build demo-deploy
"""

import pytest

from opspilot.config import get_settings
from opspilot.faults.injector import Injector
from opspilot.faults.scenario import load_scenarios
from opspilot.kube import admin_client

pytestmark = pytest.mark.integration

SETTINGS = get_settings()
SCENARIOS = load_scenarios(SETTINGS.scenarios_dir)


@pytest.fixture(scope="module")
def injector() -> Injector:
    return Injector(
        admin_client(SETTINGS.admin_context),
        context=SETTINGS.admin_context,
        base_dir=SETTINGS.demo_base_dir,
    )


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_inject_symptom_reset(injector: Injector, scenario_id: str) -> None:
    scenario = SCENARIOS[scenario_id]
    assert injector.all_ready(scenario.namespace), "namespace must start healthy"
    try:
        injector.inject(scenario)
        symptom_s = injector.wait_for_symptom(scenario)
    finally:
        reset_s = injector.reset(scenario)
    print(f"\n{scenario_id}: symptom {symptom_s:.1f}s, reset {reset_s:.1f}s")
    assert injector.all_ready(scenario.namespace)
    assert not injector.symptom_present(scenario)
