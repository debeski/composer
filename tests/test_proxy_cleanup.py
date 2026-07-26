import tempfile
import unittest
from pathlib import Path

from composer.proxy_cleanup import (
    inspect_legacy_proxy_routes,
    remove_pgadmin_proxy_route,
)


CADDYFILE = """example.test {
    route {
        handle_path /media/* {
            root * /app/media
        }

        # pgAdmin under /pgadmin4/.
        handle_path /pgadmin4/* {
            reverse_proxy pgadmin:80 {
                header_up X-Script-Name /pgadmin4
            }
        }

        reverse_proxy web:8000
    }
}
"""

NGINX = """server {
    location /media/ {
        alias /app/media/;
    }

    # ----------------------------
    # pgAdmin Server (/pgadmin/)
    # ----------------------------
    location /pgadmin4/ {
        proxy_pass http://pgadmin:80/;
        proxy_set_header X-Script-Name /pgadmin4;
        proxy_set_header Host $host;
    }

    location / {
        proxy_pass http://web:8000;
    }
}
"""


class ProxyRouteTransformTests(unittest.TestCase):
    def test_caddy_route_and_comments_are_removed(self):
        updated, changed, unsupported = remove_pgadmin_proxy_route(CADDYFILE, "caddy")

        self.assertTrue(changed)
        self.assertFalse(unsupported)
        self.assertNotIn("pgadmin", updated.lower())
        self.assertIn("reverse_proxy web:8000", updated)
        self.assertEqual(updated.count("{"), updated.count("}"))

    def test_nginx_route_and_comments_are_removed(self):
        updated, changed, unsupported = remove_pgadmin_proxy_route(NGINX, "nginx")

        self.assertTrue(changed)
        self.assertFalse(unsupported)
        self.assertNotIn("pgadmin", updated.lower())
        self.assertIn("proxy_pass http://web:8000", updated)
        self.assertEqual(updated.count("{"), updated.count("}"))

    def test_custom_route_is_reported_but_not_rewritten(self):
        custom = CADDYFILE.replace(
            "header_up X-Script-Name /pgadmin4",
            "header_up X-Custom true",
        )
        updated, changed, unsupported = remove_pgadmin_proxy_route(custom, "caddy")

        self.assertFalse(changed)
        self.assertTrue(unsupported)
        self.assertEqual(updated, custom)

    def test_unrelated_static_path_is_not_rewritten(self):
        contents = CADDYFILE + "\n# Keep /srv/static/media/pgadmin unchanged.\n"

        updated, changed, unsupported = remove_pgadmin_proxy_route(
            contents,
            "caddy",
        )

        self.assertTrue(changed)
        self.assertFalse(unsupported)
        self.assertIn("/srv/static/media/pgadmin", updated)

    def test_project_inspection_covers_proxy_and_legacy_nginx_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".proxy").mkdir()
            (root / ".nginx").mkdir()
            (root / ".proxy" / "Caddyfile").write_text(CADDYFILE, encoding="utf-8")
            (root / ".proxy" / "default.conf.template").write_text(
                NGINX,
                encoding="utf-8",
            )
            (root / ".nginx" / "nginx.conf").write_text(NGINX, encoding="utf-8")

            inspection = inspect_legacy_proxy_routes(str(root))

            self.assertEqual(
                inspection["recognized"],
                [
                    ".nginx/nginx.conf",
                    ".proxy/Caddyfile",
                    ".proxy/default.conf.template",
                ],
            )
            self.assertEqual(inspection["unsupported"], [])


if __name__ == "__main__":
    unittest.main()
