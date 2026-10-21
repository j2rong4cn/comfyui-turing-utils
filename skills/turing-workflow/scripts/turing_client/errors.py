"""Small JSON error contracts shared by the HTTP service and portable client."""

import json

import aiohttp


class ValidationError(ValueError):
    def __init__(self, message, *, node_errors=None, issues=None):
        super().__init__(message)
        self.details = {
            "code": "workflow_invalid",
            "phase": "validation",
            "message": message,
        }
        if node_errors is not None:
            self.details["node_errors"] = node_errors
        if issues is not None:
            self.details["issues"] = issues


class ApiError(RuntimeError):
    def __init__(self, status, payload):
        self.status = status
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {"error": payload}
        self.details = (
            payload.get("error_details") if isinstance(payload, dict) else None
        )
        if not isinstance(self.details, dict):
            message = (
                payload.get("error", str(payload))
                if isinstance(payload, dict)
                else str(payload)
            )
            self.details = {
                "code": "http_error",
                "phase": "request",
                "message": str(message),
            }
        self.details = {**self.details, "http_status": status}
        super().__init__(self.details["message"])


class ServerRestarted(RuntimeError):
    pass


class JobFailed(RuntimeError):
    def __init__(self, state):
        self.state = state
        self.details = state.get("error_details") or {
            "code": "job_failed",
            "phase": "executing",
            "message": state.get("error") or state["status"],
        }
        super().__init__(self.details["message"])


def describe(error, phase):
    details = getattr(error, "details", None)
    if isinstance(details, dict):
        return {**details}
    code = (
        "server_restarted"
        if isinstance(error, ServerRestarted)
        else "timeout"
        if isinstance(error, TimeoutError)
        else "connection_error"
        if isinstance(error, aiohttp.ClientError)
        else "local_io_error"
        if isinstance(error, OSError)
        else "invalid_input"
        if isinstance(error, (ValueError, KeyError, TypeError))
        else "client_error"
    )
    return {"code": code, "phase": phase, "message": str(error) or type(error).__name__}
