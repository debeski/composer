import unittest
from unittest.mock import patch

from composer.constants import (
    SERVICE_FAILED,
    SERVICE_HEALTHY,
    SERVICE_NOT_SEEN,
    SERVICE_STARTING,
    SERVICE_UPDATING,
)
from composer.launcher import DockerComposeLauncher


class ServiceIconTests(unittest.TestCase):
    def setUp(self):
        self.launcher = DockerComposeLauncher()
        self.launcher.services = ["web", "postgres"]

    def test_every_state_has_a_distinct_icon(self):
        icons = {}
        for state in (
            SERVICE_NOT_SEEN,
            SERVICE_UPDATING,
            SERVICE_STARTING,
            SERVICE_HEALTHY,
            SERVICE_FAILED,
        ):
            self.launcher.service_state["web"] = state
            icons[state] = self.launcher.service_icon("web")
        self.assertEqual(len(set(icons.values())), len(icons))
        self.assertNotEqual(icons[SERVICE_UPDATING], icons[SERVICE_HEALTHY])


class MarkServicesUpdatingTests(unittest.TestCase):
    """A service being replaced must not keep reporting the health of the
    container that is going away."""

    def setUp(self):
        self.launcher = DockerComposeLauncher()
        self.launcher.services = ["web", "postgres"]
        self.launcher.service_state = {
            "web": SERVICE_HEALTHY,
            "postgres": SERVICE_HEALTHY,
        }

    def test_unscoped_update_marks_every_service(self):
        self.launcher.mark_services_updating(None)
        self.assertEqual(
            self.launcher.service_state,
            {"web": SERVICE_UPDATING, "postgres": SERVICE_UPDATING},
        )

    def test_scoped_update_leaves_untouched_services_alone(self):
        self.launcher.mark_services_updating("web")
        self.assertEqual(self.launcher.service_state["web"], SERVICE_UPDATING)
        self.assertEqual(self.launcher.service_state["postgres"], SERVICE_HEALTHY)

    def test_excluded_services_are_never_marked(self):
        self.launcher.exclude_services = ["postgres"]
        self.launcher.mark_services_updating(None)
        self.assertEqual(self.launcher.service_state["postgres"], SERVICE_HEALTHY)

    def test_health_monitoring_resolves_the_updating_state(self):
        self.launcher.mark_services_updating(None)
        entries = [
            {"Service": "web", "State": "running", "Health": "starting"},
            {"Service": "postgres", "State": "running", "Health": ""},
        ]
        with patch.object(
            DockerComposeLauncher,
            "get_compose_ps_entries",
            return_value=(True, entries, ""),
        ):
            self.assertTrue(self.launcher.update_service_states())

        self.assertEqual(self.launcher.service_state["web"], SERVICE_STARTING)
        self.assertEqual(self.launcher.service_state["postgres"], SERVICE_HEALTHY)


class UpdateFlowMarksServicesTests(unittest.TestCase):
    def test_pull_and_recreate_mark_their_own_scope(self):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "postgres"]
        launcher.service_state = {"web": SERVICE_HEALTHY, "postgres": SERVICE_HEALTHY}
        launcher.pull_service = "web"
        launcher.up_service = "web"

        launcher.mark_services_updating(launcher.pull_service)
        self.assertEqual(launcher.service_state["web"], SERVICE_UPDATING)
        self.assertEqual(launcher.service_state["postgres"], SERVICE_HEALTHY)

        launcher.pull_service = None
        launcher.up_service = None
        launcher.mark_services_updating(launcher.up_service)
        self.assertEqual(launcher.service_state["postgres"], SERVICE_UPDATING)


if __name__ == "__main__":
    unittest.main()
