"""The private-registry login: the update check, the pulls, the stack lines and ``check``."""

import base64
import email.message
import io
import json
import os
import stat
import subprocess
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from composer import agent_installer, registry, registry_login
from composer.checkup import FAIL, OK, WARN
from composer.launcher import DockerComposeLauncher
from composer.watcher import check_availability

LOGIN = ("deploy-token-1", "s3cret")
BASIC = "Basic " + base64.b64encode(b"deploy-token-1:s3cret").decode("ascii")
IMAGE = "registry.office.test/web/eservices:latest"
MANIFEST = "https://registry.office.test/v2/web/eservices/manifests/latest"
REALM = "https://gitlab.office.test/jwt/auth"


def _error(url, code, headers=None):
    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(url, code, "error", message, io.BytesIO(b""))


class _Response:
    def __init__(self, body=b"", headers=None):
        self._body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._body


class FakeGitLab:
    """A registry behind a GitLab-style token realm that grants pull only to the login."""

    def __init__(self, challenge="bearer", anonymous=200):
        self.challenge = challenge
        self.anonymous = anonymous
        self.calls = []

    def __call__(self, url, headers, *, method="GET", timeout=15):
        self.calls.append((url, dict(headers)))
        auth = headers.get("Authorization", "")
        if url.startswith(REALM):
            if auth and auth != BASIC:
                raise _error(url, 401)
            if not auth and self.anonymous != 200:
                raise _error(url, self.anonymous)
            token = "pull-token" if auth == BASIC else "anonymous-token"
            return _Response(json.dumps({"token": token}).encode("utf-8"))
        if url == MANIFEST:
            if auth in ("Bearer pull-token",) or (self.challenge == "basic" and auth == BASIC):
                return _Response(headers={"Docker-Content-Digest": "sha256:new"})
            if self.challenge == "basic":
                raise _error(url, 401, {"WWW-Authenticate": 'Basic realm="Registry"'})
            raise _error(url, 401, {
                "WWW-Authenticate": f'Bearer realm="{REALM}",service="container_registry",'
                                    'scope="repository:web/eservices:pull"',
            })
        raise AssertionError(f"unexpected request {url}")


class RegistryLoginTransportTests(unittest.TestCase):
    def _digest(self, server, credentials):
        with patch.object(registry, "_open", server):
            return registry.tag_digest(IMAGE, credentials=credentials)

    def test_the_login_buys_a_pull_token_from_the_realm(self):
        server = FakeGitLab()
        self.assertEqual(self._digest(server, LOGIN), ("sha256:new", ""))
        realm_calls = [headers for url, headers in server.calls if url.startswith(REALM)]
        self.assertEqual([headers.get("Authorization") for headers in realm_calls], [BASIC])

    def test_the_login_never_reaches_the_registry_itself_under_a_bearer_challenge(self):
        server = FakeGitLab()
        self._digest(server, LOGIN)
        for url, headers in server.calls:
            if url == MANIFEST:
                self.assertNotEqual(headers.get("Authorization"), BASIC)

    def test_without_a_login_a_private_repository_needs_one(self):
        self.assertEqual(self._digest(FakeGitLab(), None), (None, registry.LOGIN_REQUIRED))

    def test_gitlab_refuses_an_anonymous_token_with_403(self):
        # GitLab's realm answers an anonymous request for a private project with 403.
        self.assertEqual(self._digest(FakeGitLab(anonymous=403), None), (None, registry.LOGIN_REQUIRED))
        self.assertEqual(self._digest(FakeGitLab(anonymous=403), LOGIN), ("sha256:new", ""))

    def test_a_wrong_login_is_refused(self):
        server = FakeGitLab()
        self.assertEqual(self._digest(server, ("deploy-token-1", "wrong")), (None, registry.LOGIN_REFUSED))

    def test_a_basic_registry_gets_the_login_directly(self):
        self.assertEqual(self._digest(FakeGitLab("basic"), LOGIN), ("sha256:new", ""))
        self.assertEqual(self._digest(FakeGitLab("basic"), None), (None, registry.LOGIN_REQUIRED))

    def test_an_unreachable_registry_is_reported(self):
        def offline(url, headers, *, method="GET", timeout=15):
            raise urllib.error.URLError("no route")

        self.assertEqual(self._digest(offline, LOGIN), (None, registry.UNREACHABLE))

    def test_an_untrusted_certificate_is_named(self):
        import ssl

        def private_ca(url, headers, *, method="GET", timeout=15):
            raise urllib.error.URLError(ssl.SSLCertVerificationError("unable to get local issuer certificate"))

        self.assertEqual(self._digest(private_ca, LOGIN), (None, registry.CERTIFICATE))

    def test_a_plaintext_realm_never_receives_the_login(self):
        calls = []

        def server(url, headers, *, method="GET", timeout=15):
            calls.append(url)
            raise _error(url, 401, {"WWW-Authenticate": 'Bearer realm="http://gitlab.office.test/jwt/auth"'})

        self.assertEqual(self._digest(server, LOGIN), (None, registry.FAILED))
        self.assertEqual(calls, [MANIFEST])

    def test_the_storage_hop_gets_no_credentials(self):
        cdn = "https://cdn.office.test/blob"
        blob = "https://registry.office.test/v2/web/eservices/blobs/sha256:c"
        seen = []

        def server(url, headers, *, method="GET", timeout=15):
            seen.append((url, dict(headers)))
            if url.startswith(REALM):
                return _Response(json.dumps({"token": "pull-token"}).encode("utf-8"))
            if url == blob and headers.get("Authorization") != "Bearer pull-token":
                raise _error(url, 401, {"WWW-Authenticate": f'Bearer realm="{REALM}"'})
            if url == blob:
                raise _error(url, 307, {"Location": cdn})
            return _Response(b"{}")

        with patch.object(registry, "_open", server):
            body = registry._fetch_bytes(blob, "application/json", None, 5, follow=True, credentials=LOGIN)
        self.assertEqual(body, b"{}")
        self.assertEqual(seen[-1][0], cdn)
        self.assertNotIn("Authorization", seen[-1][1])


class LoginScopeTests(unittest.TestCase):
    def test_the_login_belongs_to_web_images_registry(self):
        environ = {"COMPOSER_REGISTRY_USERNAME": LOGIN[0], "COMPOSER_REGISTRY_PASSWORD": LOGIN[1], "WEB_IMAGE": IMAGE}
        self.assertEqual(registry_login.credentials_for(IMAGE, environ), LOGIN)
        self.assertIsNone(registry_login.credentials_for("caddy:2", environ))
        self.assertIsNone(registry_login.credentials_for("ghcr.io/other/app:1", environ))

    def test_composer_registry_host_names_another_registry(self):
        environ = {
            "COMPOSER_REGISTRY_USERNAME": LOGIN[0], "COMPOSER_REGISTRY_PASSWORD": LOGIN[1],
            "COMPOSER_REGISTRY_HOST": "https://GHCR.io/", "WEB_IMAGE": IMAGE,
        }
        self.assertEqual(registry_login.credentials_for("ghcr.io/other/app:1", environ), LOGIN)
        self.assertIsNone(registry_login.credentials_for(IMAGE, environ))

    def test_docker_hub_names_are_one_registry(self):
        environ = {"COMPOSER_REGISTRY_USERNAME": "u", "COMPOSER_REGISTRY_PASSWORD": "p", "COMPOSER_REGISTRY_HOST": "docker.io"}
        self.assertEqual(registry_login.credentials_for("example/app:latest", environ), ("u", "p"))

    def test_half_a_login_is_none(self):
        environ = {"COMPOSER_REGISTRY_USERNAME": "u", "WEB_IMAGE": IMAGE}
        self.assertIsNone(registry_login.credentials(environ))
        self.assertIsNone(registry_login.docker_config(environ))

    def test_without_a_registry_there_is_no_docker_login(self):
        environ = {"COMPOSER_REGISTRY_USERNAME": "u", "COMPOSER_REGISTRY_PASSWORD": "p"}
        self.assertIsNone(registry_login.docker_config(environ))


class DockerConfigTests(unittest.TestCase):
    def test_the_docker_config_holds_only_this_login_privately(self):
        environ = {"COMPOSER_REGISTRY_USERNAME": LOGIN[0], "COMPOSER_REGISTRY_PASSWORD": LOGIN[1], "WEB_IMAGE": IMAGE}
        path = registry_login.docker_config(environ)
        config = Path(path) / "config.json"
        self.assertEqual(json.loads(config.read_text()), {
            "auths": {"registry.office.test": {"auth": base64.b64encode(b"deploy-token-1:s3cret").decode("ascii")}},
        })
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertEqual(registry_login.docker_config(environ), path, "one directory per login and process")

    def test_docker_hub_uses_dockers_own_key(self):
        environ = {"COMPOSER_REGISTRY_USERNAME": "hub-user", "COMPOSER_REGISTRY_PASSWORD": "p2", "WEB_IMAGE": "example/app:1"}
        config = json.loads((Path(registry_login.docker_config(environ)) / "config.json").read_text())
        self.assertEqual(list(config["auths"]), ["https://index.docker.io/v1/"])

    def test_every_compose_command_gets_the_login(self):
        environ = {"COMPOSER_REGISTRY_USERNAME": LOGIN[0], "COMPOSER_REGISTRY_PASSWORD": LOGIN[1], "WEB_IMAGE": IMAGE}
        with patch.dict(os.environ, environ, clear=False):
            env = DockerComposeLauncher().build_compose_env()
        self.assertTrue((Path(env["DOCKER_CONFIG"]) / "config.json").is_file())

    def test_without_a_login_compose_keeps_the_callers_docker_config(self):
        with patch.dict(os.environ, {"DOCKER_CONFIG": "/home/op/.docker"}, clear=True):
            env = DockerComposeLauncher().build_compose_env()
        self.assertEqual(env["DOCKER_CONFIG"], "/home/op/.docker")


class WatcherLoginTests(unittest.TestCase):
    @patch("composer.watcher._local_repo_digest", return_value="sha256:old")
    def test_the_watcher_sends_the_login_to_its_registry_only(self, _local):
        seen = {}

        def digest(image, *, token=None, credentials=None, timeout=15):
            seen[image] = credentials
            return None, registry.LOGIN_REQUIRED

        environ = {"COMPOSER_REGISTRY_USERNAME": LOGIN[0], "COMPOSER_REGISTRY_PASSWORD": LOGIN[1], "WEB_IMAGE": IMAGE}
        with patch.dict(os.environ, environ, clear=True), patch("composer.watcher.tag_digest", digest):
            available, images = check_availability([IMAGE, "caddy:2"])
        self.assertFalse(available)
        self.assertEqual(seen, {IMAGE: LOGIN, "caddy:2": None})
        self.assertEqual(images[0]["reason"], registry.LOGIN_REQUIRED)

    @patch("composer.watcher._local_repo_digest", return_value="sha256:old")
    @patch("composer.watcher.remote_image_labels", return_value={})
    @patch("composer.watcher.tag_digest", return_value=("sha256:new", ""))
    def test_a_readable_tag_has_no_reason(self, _digest, _labels, _local):
        with patch.dict(os.environ, {}, clear=True):
            _available, images = check_availability([IMAGE])
        self.assertNotIn("reason", images[0])


RESIDENT = '''services:
  web:
    image: example/app
  # DjangoLux Composer agent start
  composer-executor:
    image: debeski/composer:latest
    environment:
      WEB_IMAGE: "${WEB_IMAGE:-example/app:latest}"
      COMPOSER_EXECUTOR_SOCKET: "/run/composer-exec/composer-exec.sock"

  composer-agent:
    image: debeski/composer:latest
    environment:
      DOCKER_HOST: "tcp://docker-socket-proxy:2375"
      WEB_IMAGE: "${WEB_IMAGE:-example/app:latest}"
      COMPOSER_REGISTRY_HOST: "registry.office.test"
  # DjangoLux Composer agent end

volumes:
  dlux_runtime:
'''


class StackLinesTests(unittest.TestCase):
    def test_both_roles_receive_the_login_lines(self):
        updated = agent_installer._ensure_registry_login(RESIDENT)
        executor = agent_installer._block_bodies(updated)["composer-executor"]
        for key in registry_login.KEYS:
            self.assertIn(f'      {key}: "${{{key}:-}}"\n', executor)
        agent = agent_installer._block_bodies(updated)["composer-agent"]
        self.assertIn('      COMPOSER_REGISTRY_HOST: "registry.office.test"\n', agent, "an operator value stays")
        self.assertEqual(agent.count("COMPOSER_REGISTRY_HOST"), 1)
        self.assertIn('      COMPOSER_REGISTRY_PASSWORD: "${COMPOSER_REGISTRY_PASSWORD:-}"\n', agent)
        self.assertEqual(agent_installer._ensure_registry_login(updated), updated)

    def test_the_generated_block_passes_the_login(self):
        block = agent_installer._hardened_stack("demo", {"web"}, {
            "web_image": "${WEB_IMAGE:-example/app:latest}",
            "version_label": "org.demo.dlux_baked_version",
            "proxy_networks": [], "agent_networks": [],
        })
        self.assertEqual(agent_installer._ensure_registry_login(block), block)
        for key in registry_login.KEYS:
            self.assertEqual(block.count(f'{key}: "${{{key}:-}}"'), 2)


def _config(agent_env, executor_env=None):
    return {"services": {
        "composer-agent": {
            "command": ["agent", "run", "--check-image", IMAGE],
            "environment": agent_env,
        },
        "composer-executor": {"environment": executor_env if executor_env is not None else agent_env},
    }}


PASSED = {"WEB_IMAGE": IMAGE, "COMPOSER_REGISTRY_USERNAME": LOGIN[0],
          "COMPOSER_REGISTRY_PASSWORD": LOGIN[1], "COMPOSER_REGISTRY_HOST": ""}


class RegistryLoginCheckTests(unittest.TestCase):
    def _check(self, config, digest=("sha256:new", ""), secrets=None, resident=False, local_build=False):
        launcher = DockerComposeLauncher()
        launcher.resident_check = resident
        calls = []

        def tag_digest(image, *, token=None, credentials=None, timeout=15):
            calls.append(credentials)
            return digest

        with patch.object(launcher, "compose_config_json", return_value=config), \
                patch.object(launcher, "_compose_env_with_secrets", return_value=secrets or {}), \
                patch.object(launcher, "_is_local_build", return_value=local_build), \
                patch.object(registry, "tag_digest", tag_digest):
            return launcher._check_registry_login(), calls

    def test_a_local_build_is_ok_without_asking_the_registry(self):
        result, calls = self._check(_config({"WEB_IMAGE": IMAGE}), digest=(None, registry.LOGIN_REQUIRED), local_build=True)
        self.assertEqual(result["level"], OK)
        self.assertIn("built on this machine", result["message"])
        self.assertEqual(calls, [])

    def test_local_build_reads_repo_digests(self):
        def run(stdout, code=0):
            return patch("subprocess.run", return_value=subprocess.CompletedProcess([], code, stdout=stdout, stderr=""))
        with run("[]\n"):
            self.assertTrue(DockerComposeLauncher._is_local_build(IMAGE))
        with run('["registry.example.test/web/app@sha256:abc"]\n'):
            self.assertFalse(DockerComposeLauncher._is_local_build(IMAGE))
        with run("", code=1):
            self.assertFalse(DockerComposeLauncher._is_local_build(IMAGE))
        with patch("subprocess.run", side_effect=OSError("no docker")):
            self.assertFalse(DockerComposeLauncher._is_local_build(IMAGE))

    def test_a_working_login_is_ok(self):
        result, calls = self._check(_config(PASSED))
        self.assertEqual(result["level"], OK)
        self.assertIn("with the registry login", result["message"])
        self.assertEqual(calls, [LOGIN])

    def test_a_private_registry_without_a_login_warns(self):
        result, _ = self._check(_config({"WEB_IMAGE": IMAGE}), digest=(None, registry.LOGIN_REQUIRED))
        self.assertEqual(result["level"], WARN)
        self.assertIn("COMPOSER_REGISTRY_USERNAME", result["fix"])

    def test_a_refused_login_fails(self):
        result, _ = self._check(_config(PASSED), digest=(None, registry.LOGIN_REFUSED))
        self.assertEqual(result["level"], FAIL)

    def test_a_login_the_stack_does_not_pass_warns_before_asking(self):
        result, calls = self._check(
            _config({"WEB_IMAGE": IMAGE}),
            secrets={"COMPOSER_REGISTRY_USERNAME": "u", "COMPOSER_REGISTRY_PASSWORD": "p"},
        )
        self.assertEqual(result["level"], WARN)
        self.assertIn("check --fix", result["fix"])
        self.assertEqual(calls, [])

    def test_an_unreachable_registry_does_not_warn(self):
        result, _ = self._check(_config(PASSED), digest=(None, registry.UNREACHABLE))
        self.assertEqual(result["level"], OK)

    def test_an_untrusted_certificate_warns(self):
        result, _ = self._check(_config(PASSED), digest=(None, registry.CERTIFICATE))
        self.assertEqual(result["level"], WARN)
        self.assertIn("certs.d", result["fix"])

    def test_the_resident_check_never_asks_the_registry(self):
        result, calls = self._check(_config(PASSED), resident=True)
        self.assertEqual(result["level"], OK)
        self.assertEqual(calls, [])

    def test_no_agent_no_check(self):
        result, _ = self._check({"services": {"web": {}}})
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
