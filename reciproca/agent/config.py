"""Agent settings: agent_config.json next to the app, overridden by env.

Precedence: environment > the JSON file > defaults. The env vars are
RECIPROCA_AGENT_PROVIDER, RECIPROCA_AGENT_MODEL, RECIPROCA_AGENT_BASE_URL
and RECIPROCA_AGENT_TEMPERATURE; the provider's own key keeps its standard
name (ANTHROPIC_API_KEY / OPENAI_API_KEY / OLLAMA_BASE_URL).

The file lives next to bot_config.json, in the same directory the CLI and
the GUI already share.
"""

import json
import os
import sys

from reciproca.config import data_path

AGENT_CONFIG_FILE = data_path("agent_config.json")

DEFAULTS = {
    "provider": "anthropic",
    "model": "claude-sonnet-5",
    "temperature": 0.2,
    # Any OpenAI-compatible endpoint: unsloth / vllm / LM Studio local
    # servers all speak this protocol. api_key is whatever that server
    # wants; local ones usually accept a placeholder.
    "openai_compatible": {
        "base_url": "http://localhost:8000/v1",
        "model": "llama3.1-8b-instruct",
        "api_key": "EMPTY",
    },
    "ollama": {
        "base_url": "http://localhost:11434",
        "model": "llama3.1",
    },
}

_ENV_KEYS = (
    ("provider", "RECIPROCA_AGENT_PROVIDER"),
    ("model", "RECIPROCA_AGENT_MODEL"),
)


def load_settings():
    """The resolved settings: env wins over the file, the file over defaults.

    The dicts are copied, never shared with DEFAULTS, so callers can mutate
    the result freely.
    """
    settings = json.loads(json.dumps(DEFAULTS))
    try:
        with open(AGENT_CONFIG_FILE, encoding="utf-8") as f:
            loaded = json.load(f)
        for key, value in loaded.items():
            if isinstance(value, dict) and isinstance(settings.get(key), dict):
                settings[key].update(value)
            else:
                settings[key] = value
    except FileNotFoundError:
        pass  # first run: the defaults are the whole story
    except (OSError, json.JSONDecodeError) as e:
        print(f"⚠️  Could not read {AGENT_CONFIG_FILE}: {e}", file=sys.stderr)

    for key, env_name in _ENV_KEYS:
        if os.environ.get(env_name):
            settings[key] = os.environ[env_name]
    if os.environ.get("RECIPROCA_AGENT_MODEL"):
        # The env model wins everywhere, not only on the top-level key: a
        # --model override must be honoured whatever provider is active.
        for section in ("openai_compatible", "ollama"):
            settings[section]["model"] = os.environ["RECIPROCA_AGENT_MODEL"]
    if os.environ.get("RECIPROCA_AGENT_BASE_URL"):
        for section in ("openai_compatible", "ollama"):
            settings[section]["base_url"] = os.environ["RECIPROCA_AGENT_BASE_URL"]
    if os.environ.get("RECIPROCA_AGENT_TEMPERATURE"):
        settings["temperature"] = float(os.environ["RECIPROCA_AGENT_TEMPERATURE"])
    return settings
