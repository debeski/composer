import json
import tempfile
import unittest
from pathlib import Path

from composer import relay
from composer.relay import RelayResponder, build_catalog, parse_operation, perform, project_fields
from tests.test_relay import FakeConnection, FakeResponse, public_resolver

FIXTURES = Path(__file__).parent / "fixtures"


class BuiltinOperationTests(unittest.TestCase):
    def spec(self):
        return json.loads((FIXTURES / "relay_weather_ops.json").read_text())["operations"]

    def test_builtins_are_exactly_the_specification_shared_with_djangolux(self):
        self.assertEqual(relay.BUILTIN_OPERATIONS, self.spec())

    def test_builtins_parse_and_run_without_a_lock_or_a_project_directory(self):
        catalog = build_catalog(None)
        self.assertEqual(sorted(catalog.operations), ["weather.current", "weather.geocode"])
        self.assertTrue(all(op.builtin and op.host == "api.openweathermap.org" for op in catalog.operations.values()))
        self.assertEqual(catalog.unapproved, [])

    def test_a_project_cannot_shadow_a_builtin(self):
        directory = Path(tempfile.mkdtemp())
        shadow = {**self.spec()[1], "url": "https://evil.example.net/data/2.5/weather"}
        (directory / "operations.json").write_text(json.dumps({"schema_version": 1, "operations": [shadow]}))
        (directory / "operations.lock").write_text(json.dumps({"schema_version": 1, "operations": {
            "weather.current": relay.operation_digest(shadow)}}))
        catalog = build_catalog(directory)
        self.assertEqual(catalog.operations["weather.current"].host, "api.openweathermap.org")
        self.assertIn("built-in", " ".join(p["reason"] for p in catalog.problems))

    def call(self, name, params, body):
        FakeConnection.instances.clear()
        FakeConnection.response = FakeResponse(body=json.dumps(body).encode(), content_type="application/json; charset=utf-8")
        operation = build_catalog(None).operations[name]
        return perform(operation, params, "OWM-KEY", resolver=public_resolver, connection_factory=FakeConnection)

    def test_geocode_returns_only_the_listed_columns(self):
        rows = [{"name": "Tripoli", "state": "Tripoli District", "country": "LY", "lat": 32.9, "lon": 13.2, "local_names": {"ar": "طرابلس"}},
                {"name": "Tripoli", "country": "LB", "lat": 34.4, "lon": 35.8}]
        out = self.call("weather.geocode", {"q": "Tripoli", "limit": 5}, rows)
        self.assertEqual(out["data"], {
            "[].name": ["Tripoli", "Tripoli"], "[].state": ["Tripoli District", None],
            "[].country": ["LY", "LB"], "[].lat": [32.9, 34.4], "[].lon": [13.2, 35.8]})
        _, target, _ = FakeConnection.instances[0].sent
        self.assertTrue(target.startswith("/geo/1.0/direct?"))
        self.assertIn("appid=OWM-KEY", target)
        self.assertIn("q=Tripoli", target)

    def test_current_reading_and_its_parameter_rules(self):
        reading = {"main": {"temp": 21.5, "feels_like": 20.9, "humidity": 40}, "dt": 1790000000,
                   "weather": [{"id": 800, "icon": "01d", "description": "clear sky"}], "wind": {"speed": 3}}
        out = self.call("weather.current", {"lat": 32.9, "lon": 13.2, "units": "metric", "lang": "ar"}, reading)
        self.assertEqual(out["data"], {"main.temp": 21.5, "main.feels_like": 20.9, "dt": 1790000000,
                                       "weather.0.id": 800, "weather.0.icon": "01d", "weather.0.description": "clear sky"})
        for bad in ({"lat": 91, "lon": 0}, {"lat": 0, "lon": 181}, {"lat": 0, "lon": 0, "units": "kelvinish"},
                    {"lat": 0, "lon": 0, "lang": "english"}, {"lat": 0, "lon": 0, "extra": 1}):
            with self.subTest(bad=bad), self.assertRaises(relay.RelayFailure) as caught:
                self.call("weather.current", bad, reading)
            self.assertEqual(caught.exception.code, "invalid")

    def test_the_agent_advertises_builtins_without_any_declaration(self):
        state, agent = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        responder = RelayResponder(state, agent, None, composer_version="test")
        responder.answer()
        caps = json.loads((state / "relay" / "capabilities.json").read_text())
        self.assertEqual(sorted(caps["operations"]), ["weather.current", "weather.geocode"])
        self.assertTrue(caps["operations"]["weather.geocode"]["builtin"] and caps["operations"]["weather.geocode"]["auth"])

    def test_projection_matches_the_cases_djangolux_also_checks(self):
        for case in json.loads((FIXTURES / "relay_projection_cases.json").read_text())["cases"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(project_fields(case["document"], tuple(case["fields"])), case["expected"])


if __name__ == "__main__":
    unittest.main()
