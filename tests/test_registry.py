import email.message
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from composer import registry
from composer.registry import _bearer_token, _fetch_bytes, _open


class RegistryTransportTests(unittest.TestCase):
    def test_redirect_is_rejected_without_forwarding_credentials(self):
        received_headers = []

        class TargetHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                received_headers.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()

            def log_message(self, format, *args):
                return

        with ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler) as target:
            target_thread = threading.Thread(target=target.serve_forever, daemon=True)
            target_thread.start()
            target_url = f"http://127.0.0.1:{target.server_port}/capture"

            class RedirectHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(302)
                    self.send_header("Location", target_url)
                    self.end_headers()

                def log_message(self, format, *args):
                    return

            with ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler) as redirect:
                redirect_thread = threading.Thread(
                    target=redirect.serve_forever,
                    daemon=True,
                )
                redirect_thread.start()
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    _open(
                        f"http://127.0.0.1:{redirect.server_port}/manifest",
                        {"Authorization": "Bearer registry-secret"},
                    )
                self.assertEqual(raised.exception.code, 302)
                self.assertEqual(received_headers, [])
                redirect.shutdown()
                redirect_thread.join()
            target.shutdown()
            target_thread.join()


    def test_bearer_challenge_rejects_non_https_realm(self):
        challenge = 'Bearer realm="http://tokens.example.test",service="registry"'
        self.assertIsNone(_bearer_token(challenge, 1))

class BlobRedirectTests(unittest.TestCase):
    """A blob read follows the registry's storage hop; nothing else does.

    Docker Hub answers a blob GET with a 307 to a pre-signed CDN URL, so the
    image config — and the version label in it — is unreadable without this one
    hop. It is still a hop to https only, and still without our credentials.
    """

    @staticmethod
    def _redirect(location, code=307):
        headers = email.message.Message()
        headers["Location"] = location
        return urllib.error.HTTPError("https://registry.test/v2/x/blobs/sha256:a", code, "redirect", headers, None)

    def _fetch(self, error, **kwargs):
        seen = []

        class Response:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *_): return False
            def read(self_inner): return b"{}"

        def fake_open(url, headers, *, method="GET", timeout=15):
            if not seen:
                seen.append((url, headers))
                raise error
            seen.append((url, headers))
            return Response()

        with patch.object(registry, "_open", fake_open):
            body = _fetch_bytes("https://registry.test/v2/x/blobs/sha256:a", "application/json",
                                "registry-secret", 5, **kwargs)
        return body, seen

    def test_a_blob_follows_the_storage_redirect(self):
        body, seen = self._fetch(self._redirect("https://cdn.test/blob"), follow=True)
        self.assertEqual(body, b"{}")
        self.assertEqual(seen[1][0], "https://cdn.test/blob")

    def test_the_registry_token_is_not_handed_to_the_storage_host(self):
        _body, seen = self._fetch(self._redirect("https://cdn.test/blob"), follow=True)
        self.assertNotIn("Authorization", seen[1][1])

    def test_a_plaintext_redirect_is_refused(self):
        body, seen = self._fetch(self._redirect("http://cdn.test/blob"), follow=True)
        self.assertIsNone(body)
        self.assertEqual(len(seen), 1, "nothing may be fetched over http")

    def test_a_manifest_read_does_not_follow_it(self):
        body, seen = self._fetch(self._redirect("https://cdn.test/blob"))
        self.assertIsNone(body)
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
