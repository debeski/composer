import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from composer.registry import _bearer_token, _open


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


if __name__ == "__main__":
    unittest.main()
