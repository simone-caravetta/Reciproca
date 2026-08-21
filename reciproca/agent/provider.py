"""Build the LLM the agent talks through, per the resolved settings.

The imports are lazy (inside each branch): the venv has all three packages,
but a user who only ever uses one provider should still get a clear error
if a package is missing, not a stack trace from an unrelated import.
"""

import os

PROVIDERS = ("anthropic", "openai", "ollama")


def make_llm(settings):
    """A chat model for the provider named in the settings."""
    provider = settings["provider"]
    if provider not in PROVIDERS:
        raise ValueError(
            f"unknown provider {provider!r} - choose from {', '.join(PROVIDERS)}"
        )
    if provider == "anthropic":
        return _anthropic(settings)
    if provider == "openai":
        return _openai(settings)
    return _ollama(settings)


def _anthropic(settings):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ValueError(
            "the anthropic provider needs ANTHROPIC_API_KEY in the environment - "
            "export it (or set it in a .env), or switch provider in agent_config.json"
        )
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=settings["model"], temperature=settings["temperature"])


def _openai(settings):
    section = settings["openai_compatible"]
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=section["model"],
        base_url=section["base_url"],
        api_key=section["api_key"] or os.environ.get("OPENAI_API_KEY") or "EMPTY",
        temperature=settings["temperature"],
    )


def _ollama(settings):
    section = settings["ollama"]
    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=section["model"],
        base_url=section["base_url"],
        temperature=settings["temperature"],
    )
