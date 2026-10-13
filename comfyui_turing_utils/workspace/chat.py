"""Canvas API policy. URL selects the credential owner; model may fall back."""

from urllib.parse import urlsplit

from ..prompt.chat import normalize_chat_endpoint


def chat_settings(value):
    if not isinstance(value, dict) or set(value) - {
        "base_url", "model", "api_key", "api_key_set"
    }:
        raise ValueError("Invalid Canvas Chat API settings")
    result = {}
    for name in ("base_url", "model", "api_key"):
        field = value.get(name, "")
        if not isinstance(field, str):
            raise ValueError("Chat API settings must be strings")
        result[name] = field.strip()
    if result["base_url"]:
        normalize_chat_endpoint(result["base_url"])
    return result


def public_settings(settings):
    result = {**settings, "chat": chat_settings(settings.get("chat", {}))}
    result["chat"]["api_key_set"] = bool(result["chat"].pop("api_key"))
    return result


def api_origin(url):
    if not url.strip():
        return None
    parsed = urlsplit(normalize_chat_endpoint(url))
    return (
        parsed.scheme, parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )


def resolve_chat(local, global_):
    own_url = local.get("base_url", "").strip()
    url = own_url or global_["base_url"]
    if not url:
        raise ValueError("Set Chat API URL on the card or Canvas Project")
    model = local.get("model", "").strip() or global_["model"]
    if not model:
        raise ValueError("Set Chat API Model on the card or Canvas Project")
    return {
        "base_url": url,
        "model": model,
        "api_key": local.get("api_key", "") if own_url else global_["api_key"],
    }
