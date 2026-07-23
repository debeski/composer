import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


class ControlPlaneError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class ControlPlaneClient:
    def __init__(self, base_url: str, *, allow_http_localhost: bool = False):
        self.base_url = str(base_url or "").strip().rstrip("/")
        parsed = urllib.parse.urlparse(self.base_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (
            allow_http_localhost and parsed.scheme == "http" and local
        ):
            raise ValueError("COMPOSER_CONTROL_URL must use HTTPS (HTTP is localhost-only).")
        if not parsed.netloc:
            raise ValueError("COMPOSER_CONTROL_URL is invalid.")

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Dict[str, Any]] = None,
        credentials: Optional[Dict[str, str]] = None,
        timeout: float = 30,
    ) -> Optional[Dict[str, Any]]:
        headers = {"Accept": "application/json", "User-Agent": "composer-agent/1"}
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if credentials:
            headers["Authorization"] = f"Bearer {credentials['secret']}"
            headers["X-Composer-Agent-ID"] = credentials["agent_id"]
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status == 204:
                    return None
                raw = response.read(1024 * 1024)
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(4096).decode("utf-8", "replace")
            except Exception:
                detail = ""
            raise ControlPlaneError(detail or str(exc), status=exc.code) from exc
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise ControlPlaneError(str(exc)) from exc
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError) as exc:
            raise ControlPlaneError("Control plane returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise ControlPlaneError("Control plane returned a non-object response.")
        return payload

    def enroll(self, token: str, capabilities: Dict[str, Any]) -> Dict[str, str]:
        response = self._request(
            "POST",
            "/api/agent/v1/enroll/",
            body={"schema_version": 1, "enrollment_token": token, **capabilities},
            timeout=20,
        ) or {}
        agent_id = str(response.get("agent_id") or "").strip()
        secret = str(response.get("agent_secret") or "").strip()
        if not agent_id or not secret:
            raise ControlPlaneError("Enrollment response omitted agent credentials.")
        return {"agent_id": agent_id, "secret": secret}

    def next_command(self, credentials: Dict[str, str], wait_seconds: int = 25):
        response = self._request(
            "GET",
            f"/api/agent/v1/commands/next/?wait={max(0, min(25, int(wait_seconds)))}",
            credentials=credentials,
            timeout=max(10, wait_seconds + 10),
        )
        if not response:
            return None
        return response.get("command") if isinstance(response.get("command"), dict) else response

    def post_event(self, credentials: Dict[str, str], operation_id: str, event: Dict[str, Any]):
        self._request(
            "POST",
            f"/api/agent/v1/operations/{operation_id}/events/",
            body=event,
            credentials=credentials,
            timeout=20,
        )

    def post_snapshot(self, credentials: Dict[str, str], snapshot: Dict[str, Any]):
        self._request(
            "PUT",
            "/api/agent/v1/snapshot/",
            body=snapshot,
            credentials=credentials,
            timeout=20,
        )

    def post_local_operation(self, credentials: Dict[str, str], operation: Dict[str, Any]):
        self._request(
            "POST",
            "/api/agent/v1/local-operations/",
            body=operation,
            credentials=credentials,
            timeout=20,
        )

    def put_capabilities(self, credentials: Dict[str, str], capabilities: Dict[str, Any]):
        self._request(
            "PUT",
            "/api/agent/v1/capabilities/",
            body=capabilities,
            credentials=credentials,
            timeout=20,
        )

    def begin_rotation(self, credentials: Dict[str, str]) -> Dict[str, str]:
        response = self._request(
            "POST",
            "/api/agent/v1/credentials/rotate/",
            body={"schema_version": 1},
            credentials=credentials,
            timeout=20,
        ) or {}
        secret = str(response.get("agent_secret") or "").strip()
        rotation_id = str(response.get("rotation_id") or "").strip()
        if not secret or not rotation_id:
            raise ControlPlaneError("Credential rotation response was incomplete.")
        return {"agent_id": credentials["agent_id"], "secret": secret, "rotation_id": rotation_id}

    def confirm_rotation(self, credentials: Dict[str, str], rotation_id: str):
        self._request(
            "POST",
            "/api/agent/v1/credentials/rotate/confirm/",
            body={"schema_version": 1, "rotation_id": rotation_id},
            credentials=credentials,
            timeout=20,
        )
