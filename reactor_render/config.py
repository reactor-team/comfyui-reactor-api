"""Plugin configuration: where the Reactor API key and session limits come from."""

import configparser
import os

# The plugin root holds config.ini, one level up from this package.
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.ini")


def saved_api_key() -> str | None:
    """REACTOR_API_KEY under [API] in the plugin's config.ini, laid out as in config.ini.example."""
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)
    return config.get("API", "REACTOR_API_KEY", fallback="").strip() or None


def connect_args() -> dict:
    """The Reactor client's authentication, from REACTOR_API_KEY or the plugin's config.ini.

    A missing key is a node configuration error (ValueError), which ComfyUI reports on the
    node that needed it; it is not a crash, so nothing below may treat it as one.
    """
    # REACTOR_LOCAL=1 targets a model served by `reactor run` on this machine, which needs no key.
    if os.environ.get("REACTOR_LOCAL") == "1":
        connect = {"local": True}
        if os.environ.get("REACTOR_API_URL"):
            connect["api_url"] = os.environ["REACTOR_API_URL"]
        return connect
    if key := os.environ.get("REACTOR_API_KEY") or saved_api_key():
        return {"api_key": key}
    raise ValueError(
        "No Reactor API key configured: set REACTOR_API_KEY in the environment ComfyUI starts from, "
        f"or add it under [API] in {CONFIG_PATH} (see config.ini.example)."
    )


def max_sessions() -> int:
    """MAX_CONCURRENT under [Sessions] in the plugin's config.ini: how many Reactor sessions this server runs at once."""
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)
    return max(1, config.getint("Sessions", "MAX_CONCURRENT", fallback=1))
