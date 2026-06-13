# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# src/providers/llm_provider.py
#
# What this file does:
#   Single swap file for LLM provider configuration.
#   If you want to change the primary LLM, change this file only.
#   Nothing else in the codebase needs to change.
#
#   This file exposes:
#     get_provider(name)  → returns a callable provider function
#     list_providers()    → returns all configured provider names
#     test_all()          → tests all configured providers
#
#   Provider cascade order (from config.yaml):
#     1. Groq    — Llama 3.3 70B  (primary,   30 RPM free)
#     2. DeepSeek — R1             (secondary, free tier)
#     3. Gemini  — 2.0 Flash       (tertiary,  15 RPM free)
#     4. Qwen    — 2.5 72B         (fallback,  60 RPM via OpenRouter)
#
#   To swap to a different primary provider:
#     1. Edit config.yaml: llm.primary.provider = "gemini"
#     2. That is it. No Python changes needed.
#
#   To add a new provider (e.g. Ollama for local inference):
#     1. Add a function _call_ollama() below
#     2. Add "ollama" to the PROVIDER_MAP dictionary
#     3. Update config.yaml: llm.primary.provider = "ollama"
#
# How other files use it:
#   from src.providers.llm_provider import get_provider
#   provider_fn = get_provider("groq")
#   result = provider_fn(prompt)
# =============================================================================

import os
import re
import json
from typing import Callable, Optional

from src.config import config


# -----------------------------------------------------------------------------
# PROVIDER IMPLEMENTATIONS
# Each function takes a prompt string and returns a raw JSON string.
# Raises an exception on failure — caller handles retry/fallback.
# -----------------------------------------------------------------------------

def _call_groq(prompt: str) -> str:
    """
    Calls Groq API with Llama 3.3 70B.
    Free tier: 30 RPM, 6,000 TPM.
    Best for: structured JSON output, fast inference.
    """
    from groq import Groq

    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY not set")

    client = Groq(api_key=api_key)
    response = client.chat.completions.create(
        model=       config.llm.primary.model,
        messages=    [{"role": "user", "content": prompt}],
        temperature= config.llm.temperature,
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content


def _call_deepseek(prompt: str) -> str:
    """
    Calls DeepSeek R1 via OpenAI-compatible API.
    Free tier available. Strong reasoning capability.

    Special handling:
        DeepSeek R1 outputs <think>...</think> reasoning tokens
        before the final JSON answer. These must be stripped before
        JSON parsing — otherwise json.loads() will fail.
    """
    from openai import OpenAI

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise EnvironmentError("DEEPSEEK_API_KEY not set")

    client = OpenAI(
        api_key=  api_key,
        base_url= "https://api.deepseek.com",
    )
    response = client.chat.completions.create(
        model=       config.llm.secondary.model,
        messages=    [{"role": "user", "content": prompt}],
        temperature= config.llm.temperature,
    )
    raw = response.choices[0].message.content

    # Strip DeepSeek R1 chain-of-thought reasoning tokens
    # Pattern: content between <think> and </think> tags
    json_match = re.search(r"</think>\s*(\{.*\})", raw, re.DOTALL)
    if json_match:
        return json_match.group(1)

    # Fallback: extract any JSON object from the response
    json_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if json_match:
        return json_match.group(0)

    # Return raw if no JSON found — caller will handle parse error
    return raw


def _call_gemini(prompt: str) -> str:
    """
    Calls Gemini 2.0 Flash via google-genai SDK.
    Free tier: 15 RPM, 1M TPM.
    Best for: large context windows, multimodal tasks.
    """
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise EnvironmentError("GEMINI_API_KEY not set")

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=    config.llm.tertiary.model,
        contents= prompt,
        config=   types.GenerateContentConfig(
            response_mime_type= "application/json",
            temperature=        config.llm.temperature,
        ),
    )
    return response.text


def _call_openrouter_qwen(prompt: str) -> str:
    """
    Calls Qwen 2.5 72B via OpenRouter free tier.
    Free tier: 60 RPM (highest of all providers).
    Best for: APAC entity name recognition, Asian language context.
    Used as final fallback before deterministic regex.
    """
    from openai import OpenAI

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise EnvironmentError("OPENROUTER_API_KEY not set")

    client = OpenAI(
        api_key=  api_key,
        base_url= "https://openrouter.ai/api/v1",
    )
    response = client.chat.completions.create(
        model=       config.llm.quaternary.model,
        messages=    [{"role": "user", "content": prompt}],
        temperature= config.llm.temperature,
    )
    return response.choices[0].message.content


def _call_ollama(prompt: str) -> str:
    """
    Calls a locally running Ollama instance.
    NOT configured by default — add to config.yaml to enable.
    Useful for: air-gapped environments, maximum data privacy.

    To enable:
        1. Install Ollama: ollama.ai
        2. Pull a model: ollama pull llama3.3
        3. Update config.yaml: llm.primary.provider = "ollama"
        4. Update config.yaml: llm.primary.model = "llama3.3"
    """
    import urllib.request

    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    model      = config.llm.primary.model

    payload = json.dumps({
        "model":  model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{ollama_url}/api/generate",
        data=    payload,
        method=  "POST",
        headers= {"Content-Type": "application/json"},
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
        return result.get("response", "")


# -----------------------------------------------------------------------------
# PROVIDER MAP
# Maps provider name strings to their implementation functions.
# To add a new provider: add it here and implement a _call_X() function above.
# -----------------------------------------------------------------------------

PROVIDER_MAP: dict[str, Callable[[str], str]] = {
    "groq":        _call_groq,
    "deepseek":    _call_deepseek,
    "gemini":      _call_gemini,
    "openrouter":  _call_openrouter_qwen,
    "ollama":      _call_ollama,
}


# -----------------------------------------------------------------------------
# PUBLIC API
# These are the functions imported by other modules
# -----------------------------------------------------------------------------

def get_provider(name: str) -> Callable[[str], str]:
    """
    Returns the provider function for a given provider name.

    Args:
        name: Provider name from config.yaml
              e.g. "groq", "deepseek", "gemini", "openrouter"

    Returns:
        A callable that takes a prompt string and returns JSON string.

    Raises:
        ValueError if provider name is not in PROVIDER_MAP.
    """
    name_lower = name.strip().lower()
    if name_lower not in PROVIDER_MAP:
        raise ValueError(
            f"Unknown LLM provider: '{name}'. "
            f"Available providers: {list(PROVIDER_MAP.keys())}. "
            f"To add a new provider, update src/providers/llm_provider.py "
            f"and config.yaml."
        )
    return PROVIDER_MAP[name_lower]


def get_cascade() -> list[tuple[str, Callable]]:
    """
    Returns the full provider cascade in configured order.
    Used by llm_matcher.py to try providers in sequence.

    Returns:
        List of (provider_name, provider_function) tuples
        in the order defined by config.yaml.
    """
    cascade = []

    providers_in_order = [
        (config.llm.primary.provider,    config.llm.primary.model),
        (config.llm.secondary.provider,  config.llm.secondary.model),
        (config.llm.tertiary.provider,   config.llm.tertiary.model),
        (config.llm.quaternary.provider, config.llm.quaternary.model),
    ]

    for provider_name, model in providers_in_order:
        if provider_name in PROVIDER_MAP:
            cascade.append((provider_name, PROVIDER_MAP[provider_name]))
        else:
            print(
                f"[LLM_PROVIDER] Unknown provider '{provider_name}' "
                f"in config.yaml — skipping."
            )

    return cascade


def list_providers() -> list[str]:
    """Returns all available provider names."""
    return list(PROVIDER_MAP.keys())


# -----------------------------------------------------------------------------
# PROVIDER TESTER
# Tests each provider with a simple prompt
# Called by preflight_check.py
# -----------------------------------------------------------------------------

def test_provider(name: str) -> tuple[bool, str]:
    """
    Tests a single provider with a minimal prompt.

    Returns:
        (success: bool, detail: str)
    """
    try:
        provider_fn = get_provider(name)
        test_prompt = (
            'Return this exact JSON: {"status": "ok", "provider": "'
            + name + '"}'
        )
        result = provider_fn(test_prompt)

        # Try to parse as JSON
        parsed = json.loads(result)
        if parsed.get("status") == "ok":
            return True, f"Response: {result[:50]}"
        else:
            return True, f"Responded but unexpected format: {result[:50]}"

    except EnvironmentError as e:
        return False, f"API key not set: {e}"
    except json.JSONDecodeError:
        return True, f"Responded but non-JSON format (may be OK): {result[:50]}"
    except Exception as e:
        return False, str(e)[:100]


def test_all() -> dict[str, tuple[bool, str]]:
    """
    Tests all providers and returns results.
    Called by preflight_check.py.

    Returns:
        Dict of {provider_name: (success, detail)}
    """
    results = {}
    for name in PROVIDER_MAP:
        print(f"[LLM_PROVIDER] Testing {name}...")
        success, detail = test_provider(name)
        results[name] = (success, detail)
        status = "PASS" if success else "FAIL"
        print(f"  [{status}] {name}: {detail}")
    return results


# -----------------------------------------------------------------------------
# STANDALONE TEST
# Run directly to test all configured LLM providers:
#   python src/providers/llm_provider.py
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

    print("\n" + "=" * 60)
    print("LLM PROVIDER TEST")
    print("=" * 60)
    print(f"Configured cascade:")
    for name, model in [
        (config.llm.primary.provider,    config.llm.primary.model),
        (config.llm.secondary.provider,  config.llm.secondary.model),
        (config.llm.tertiary.provider,   config.llm.tertiary.model),
        (config.llm.quaternary.provider, config.llm.quaternary.model),
    ]:
        print(f"  {name}: {model}")

    print("\nTesting all providers...")
    results = test_all()

    passed = sum(1 for s, _ in results.values() if s)
    failed = sum(1 for s, _ in results.values() if not s)

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)

    if failed > 0:
        print("\nFailed providers will be skipped in the cascade.")
        print("Pipeline will use remaining available providers.")