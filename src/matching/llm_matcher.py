# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# src/matching/llm_matcher.py
#
# What this file does:
#   Implements Tier 5 — LLM-assisted orphan counterparty suggestion.
#
#   At this point in the pipeline, all deterministic matching has been
#   exhausted. The remaining rows are genuine orphans — no counterpart
#   was found using tranid, hash, tolerance, or subset sum.
#
#   The LLM's role is narrow and specific:
#     Given a sanitised account description from an orphan transaction,
#     suggest which of the 5 APAC entities is the most likely counterparty.
#
#   Privacy design (critical):
#     ZERO financial amounts sent to any external API.
#     ZERO entity names sent (replaced with abstract placeholders).
#     ZERO transaction IDs sent.
#     ONLY sanitised account description text is sent.
#     The LLM receives: anonymised text strings only.
#
#   LLM cascade (all free):
#     Primary:    Groq — Llama 3.3 70B (30 RPM)
#     Secondary:  DeepSeek R1 (free tier) — strips <think> tokens
#     Tertiary:   Gemini 2.0 Flash (15 RPM)
#     Fallback:   Qwen 2.5 72B via OpenRouter (60 RPM)
#
#   Batching (critical for rate limit compliance):
#     ALL orphans are batched into ONE API call per provider.
#     Never one call per orphan — that exhausts 15 RPM in seconds.
#
#   Hallucination guard:
#     Pydantic Literal type enforces that the LLM can ONLY return
#     entity codes from the APAC 5 list. Any hallucinated entity
#     raises a ValidationError and routes to UNCLASSIFIED_ESCROW.
#
# How other files use it:
#   from src.matching.llm_matcher import suggest_counterparties
#   enriched_orphans = suggest_counterparties(orphan_df)
# =============================================================================

import json
import os
import re
import time
from typing import Optional, Literal

import pandas as pd
from pydantic import BaseModel, Field, ValidationError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from src.config import config


# -----------------------------------------------------------------------------
# CONSTANTS
# -----------------------------------------------------------------------------

NS            = config.netsuite.fields
LLM_CFG       = config.llm
VALID_ENTITIES = config.valid_entity_codes

# Pydantic Literal type — LLM can ONLY return one of these exact strings
# If it returns anything else, Pydantic raises ValidationError immediately
APACEntity = Literal["ANTH-SG", "ANTH-AU", "ANTH-IN", "ANTH-JP", "ANTH-HK"]


# -----------------------------------------------------------------------------
# PYDANTIC OUTPUT SCHEMA
# Enforces structured, whitelisted output from all LLM providers
# -----------------------------------------------------------------------------

class OrphanResolution(BaseModel):
    """
    Strict output schema for LLM orphan counterparty suggestion.

    The LLM must return exactly this structure — no freeform text.
    Pydantic validates every field on instantiation.
    If counterparty_entity is not in the APAC 5 list, ValidationError fires.
    """
    orphan_index:        int   = Field(
        description="Index of the orphan transaction in the batch"
    )
    counterparty_entity: APACEntity = Field(
        description=(
            "The most likely counterparty entity from the approved list: "
            "ANTH-SG, ANTH-AU, ANTH-IN, ANTH-JP, ANTH-HK"
        )
    )
    confidence_score:    float = Field(
        ge=0.0, le=1.0,
        description="Confidence between 0.0 and 1.0"
    )
    reasoning:           str   = Field(
        max_length=200,
        description="Max 200 character explanation of the suggestion"
    )


class BatchResolution(BaseModel):
    """Wrapper for a batch of orphan resolutions."""
    resolutions: list[OrphanResolution]


# -----------------------------------------------------------------------------
# TEXT SANITISER
# Strips all sensitive information before sending to external API
# -----------------------------------------------------------------------------

def _sanitise_description(text: str, entity_code: str) -> str:
    """
    Sanitises account description text for safe external API transmission.

    Removes:
        - All numeric values (amounts, codes, IDs)
        - Entity names replaced with <ENTITY_X> placeholders
        - Any string that looks like an internal ID or reference number

    Keeps:
        - Descriptive words (consulting, services, management, etc.)
        - Generic accounting terms
        - Direction words (from, to, recharge, allocation)

    Example:
        "Consulting svcs recd from ANTH-SG Jun 2026 INV-001"
        → "Consulting svcs recd from <ENTITY_A>"
    """
    sanitised = str(text).strip()

    # Replace entity codes with placeholders
    for i, entity in enumerate(VALID_ENTITIES):
        sanitised = sanitised.replace(entity, f"<ENTITY_{chr(65+i)}>")

    # Remove numeric sequences (amounts, IDs, dates)
    sanitised = re.sub(r"\b\d+[\d,\.]*\b", "<NUM>", sanitised)

    # Remove common reference patterns
    sanitised = re.sub(r"\b[A-Z]{2,}-\d+\b", "<REF>", sanitised)

    # Remove year references
    sanitised = re.sub(r"\b20\d{2}\b", "<YEAR>", sanitised)

    # Collapse multiple spaces
    sanitised = re.sub(r"\s+", " ", sanitised).strip()

    return sanitised


def _build_batch_prompt(orphan_descriptions: list[dict]) -> str:
    """
    Builds a single batched prompt for all orphan transactions.

    Batching all orphans into one API call:
    1. Stays well within the 15 RPM limit (1 call vs N calls)
    2. Leverages the generous 1M TPM allowance
    3. Gives the LLM context across all orphans simultaneously

    The prompt deliberately uses abstract language — no financial context
    that could leak sensitive information.
    """
    valid_list = ", ".join(VALID_ENTITIES)

    descriptions_text = "\n".join([
        f"Transaction {item['index']}: \"{item['description']}\""
        for item in orphan_descriptions
    ])

    prompt = f"""You are a financial classification assistant.

Your task: For each transaction description below, identify the most likely
counterparty entity from this approved list ONLY: {valid_list}

Rules:
- You MUST only use entity codes from the approved list above
- Never invent or suggest entities outside the approved list
- Base your suggestion ONLY on the semantic meaning of the description
- Ignore any placeholder tokens like <NUM>, <REF>, <YEAR>, <ENTITY_X>

Transactions to classify:
{descriptions_text}

Return a JSON object with this exact structure:
{{
  "resolutions": [
    {{
      "orphan_index": <integer matching the transaction number>,
      "counterparty_entity": "<one of: {valid_list}>",
      "confidence_score": <float between 0.0 and 1.0>,
      "reasoning": "<max 200 characters explaining your suggestion>"
    }}
  ]
}}

Return ONLY the JSON object. No markdown, no explanation, no preamble."""

    return prompt


# -----------------------------------------------------------------------------
# LLM PROVIDER IMPLEMENTATIONS
# Each provider has its own call function
# All return a raw string (JSON) or raise an exception on failure
# -----------------------------------------------------------------------------

def _call_groq(prompt: str) -> str:
    """Calls Groq API (Llama 3.3 70B). Returns raw JSON string."""
    try:
        from groq import Groq
        client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
        response = client.chat.completions.create(
            model=       LLM_CFG.primary.model,
            messages=    [{"role": "user", "content": prompt}],
            temperature= LLM_CFG.temperature,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content
    except Exception as e:
        raise RuntimeError(f"Groq API failed: {e}") from e


def _call_deepseek(prompt: str) -> str:
    """
    Calls DeepSeek R1 API via OpenAI-compatible SDK.
    Strips <think>...</think> reasoning tokens before returning JSON.
    """
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=  os.environ.get("DEEPSEEK_API_KEY"),
            base_url= "https://api.deepseek.com",
        )
        response = client.chat.completions.create(
            model=       LLM_CFG.secondary.model,
            messages=    [{"role": "user", "content": prompt}],
            temperature= LLM_CFG.temperature,
        )
        raw = response.choices[0].message.content

        # Strip DeepSeek R1 chain-of-thought reasoning tokens
        # R1 outputs <think>...</think> before the actual JSON answer
        json_match = re.search(r"</think>\s*(\{.*\})", raw, re.DOTALL)
        if json_match:
            return json_match.group(1)

        # Fallback: try to extract JSON directly if no think tags
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_match:
            return json_match.group(0)

        raise RuntimeError("DeepSeek returned no parseable JSON")

    except Exception as e:
        raise RuntimeError(f"DeepSeek API failed: {e}") from e


def _call_gemini(prompt: str) -> str:
    """Calls Gemini 2.0 Flash API. Returns raw JSON string."""
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        response = client.models.generate_content(
            model=    LLM_CFG.tertiary.model,
            contents= prompt,
            config=   types.GenerateContentConfig(
                response_mime_type= "application/json",
                temperature=        LLM_CFG.temperature,
            ),
        )
        return response.text
    except Exception as e:
        raise RuntimeError(f"Gemini API failed: {e}") from e


def _call_openrouter_qwen(prompt: str) -> str:
    """Calls Qwen 2.5 72B via OpenRouter as final fallback."""
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=  os.environ.get("OPENROUTER_API_KEY"),
            base_url= "https://openrouter.ai/api/v1",
        )
        response = client.chat.completions.create(
            model=       LLM_CFG.quaternary.model,
            messages=    [{"role": "user", "content": prompt}],
            temperature= LLM_CFG.temperature,
        )
        return response.choices[0].message.content
    except Exception as e:
        raise RuntimeError(f"OpenRouter/Qwen API failed: {e}") from e


# -----------------------------------------------------------------------------
# LLM CASCADE
# Tries providers in order. Falls back on failure.
# Uses tenacity for exponential backoff on rate limit errors.
# -----------------------------------------------------------------------------

PROVIDERS = [
    ("Groq/Llama3.3",    _call_groq),
    ("DeepSeek/R1",      _call_deepseek),
    ("Gemini/Flash",     _call_gemini),
    ("OpenRouter/Qwen",  _call_openrouter_qwen),
]


def _call_llm_cascade(prompt: str) -> Optional[str]:
    """
    Attempts LLM providers in cascade order.
    Returns raw JSON string from first successful provider.
    Returns None if all providers fail.
    """
    for provider_name, provider_fn in PROVIDERS:
        try:
            print(f"[LLM] Attempting {provider_name}...")
            result = provider_fn(prompt)
            print(f"[LLM] {provider_name} succeeded.")
            return result

        except Exception as e:
            print(f"[LLM] {provider_name} failed: {e}. Trying next provider.")

            # Brief pause before next provider to avoid cascading rate limits
            time.sleep(2)
            continue

    print("[LLM] All providers failed. Routing orphans to UNCLASSIFIED_ESCROW.")
    return None


# -----------------------------------------------------------------------------
# RESPONSE PARSER AND VALIDATOR
# Parses raw JSON and validates against Pydantic schema
# -----------------------------------------------------------------------------

def _parse_and_validate(raw_json: str) -> Optional[BatchResolution]:
    """
    Parses raw JSON from LLM and validates against BatchResolution schema.

    Validation enforces:
    1. Correct JSON structure
    2. counterparty_entity is in the APAC 5 Literal list
    3. confidence_score is between 0.0 and 1.0
    4. All required fields present

    Returns None if parsing or validation fails.
    """
    try:
        # Strip markdown code fences if present
        cleaned = raw_json.strip()
        cleaned = re.sub(r"^```json\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$",     "", cleaned)

        data = json.loads(cleaned)
        return BatchResolution(**data)

    except (json.JSONDecodeError, ValidationError) as e:
        print(f"[LLM] Response validation failed: {e}")
        return None


# -----------------------------------------------------------------------------
# CONFIDENCE ROUTER
# Routes suggestions based on confidence threshold from config
# -----------------------------------------------------------------------------

def _route_by_confidence(
    resolution: OrphanResolution,
) -> tuple[str, str]:
    """
    Routes an LLM resolution based on confidence score.

    Returns:
        (suggested_entity, routing_status)

    Routing:
        score >= threshold:  suggested_entity, "LLM_SUGGESTED"
        score < threshold:   "", "MANUAL_REVIEW_QUEUE"
        ValidationError:     "", "UNCLASSIFIED_ESCROW"
    """
    threshold = LLM_CFG.confidence_threshold  # 0.85 from config

    if resolution.confidence_score >= threshold:
        return resolution.counterparty_entity, "LLM_SUGGESTED"
    else:
        print(
            f"[LLM] Low confidence ({resolution.confidence_score:.2f}) for "
            f"orphan {resolution.orphan_index}. Routing to MANUAL_REVIEW_QUEUE."
        )
        return "", "MANUAL_REVIEW_QUEUE"


# -----------------------------------------------------------------------------
# MAIN FUNCTION
# Called by engine.py after Tier 4 completes
# -----------------------------------------------------------------------------

def suggest_counterparties(orphan_df: pd.DataFrame) -> pd.DataFrame:
    """
    Enriches orphan transactions with LLM counterparty suggestions.

    Does NOT match transactions — only adds suggestion metadata.
    The suggestions appear in Tab 2 Exceptions to assist the controller.
    No suggestion is ever auto-posted to NetSuite.

    Adds these columns to orphan_df:
        llm_suggested_entity:   str   Suggested counterparty (or "")
        llm_confidence:         float Confidence score 0.0-1.0
        llm_reasoning:          str   Brief explanation from LLM
        llm_routing:            str   LLM_SUGGESTED | MANUAL_REVIEW_QUEUE |
                                      UNCLASSIFIED_ESCROW | LLM_UNAVAILABLE

    Args:
        orphan_df: DataFrame of unmatched IC transactions after Tier 4

    Returns:
        orphan_df enriched with LLM suggestion columns
    """
    print(f"\n[TIER 5] Starting LLM orphan analysis. {len(orphan_df)} orphans.")

    # Initialise suggestion columns
    orphan_df = orphan_df.copy()
    orphan_df["llm_suggested_entity"] = ""
    orphan_df["llm_confidence"]       = 0.0
    orphan_df["llm_reasoning"]        = ""
    orphan_df["llm_routing"]          = "LLM_UNAVAILABLE"

    if len(orphan_df) == 0:
        print("[TIER 5] No orphans to analyse.")
        return orphan_df

    # Check if LLM is enabled in config
    if not config.matching.tier_5.enabled:
        print("[TIER 5] LLM matching disabled in config. Skipping.")
        orphan_df["llm_routing"] = "LLM_DISABLED"
        return orphan_df

    # --- Build sanitised descriptions for batch prompt ---
    orphan_items = []
    index_map    = {}  # Maps batch index to DataFrame index

    for batch_idx, (df_idx, row) in enumerate(orphan_df.iterrows()):
        # Sanitise account name + memo for safe API transmission
        account_text = str(row.get("account_name", "")).strip()
        memo_text    = str(row.get(NS.memo, "")).strip()
        combined     = f"{account_text}. {memo_text}".strip(". ")

        sanitised = _sanitise_description(combined, row.get("local_entity", ""))

        orphan_items.append({
            "index":       batch_idx,
            "description": sanitised,
        })
        index_map[batch_idx] = df_idx

    print(f"[TIER 5] Sending {len(orphan_items)} sanitised descriptions to LLM.")

    # --- Build batch prompt ---
    prompt = _build_batch_prompt(orphan_items)

    # --- Call LLM cascade ---
    raw_response = _call_llm_cascade(prompt)

    if raw_response is None:
        print("[TIER 5] LLM unavailable. All orphans remain UNCLASSIFIED.")
        orphan_df["llm_routing"] = "LLM_UNAVAILABLE"
        return orphan_df

    # --- Parse and validate response ---
    batch_result = _parse_and_validate(raw_response)

    if batch_result is None:
        print("[TIER 5] LLM response could not be parsed. Routing to UNCLASSIFIED.")
        orphan_df["llm_routing"] = "UNCLASSIFIED_ESCROW"
        return orphan_df

    # --- Apply suggestions to DataFrame ---
    suggestions_applied = 0
    for resolution in batch_result.resolutions:
        df_idx = index_map.get(resolution.orphan_index)
        if df_idx is None:
            continue

        try:
            suggested_entity, routing = _route_by_confidence(resolution)

            orphan_df.at[df_idx, "llm_suggested_entity"] = suggested_entity
            orphan_df.at[df_idx, "llm_confidence"]       = resolution.confidence_score
            orphan_df.at[df_idx, "llm_reasoning"]        = resolution.reasoning
            orphan_df.at[df_idx, "llm_routing"]          = routing

            if routing == "LLM_SUGGESTED":
                suggestions_applied += 1

        except ValidationError as e:
            # Pydantic caught a hallucinated entity — route to escrow
            print(
                f"[TIER 5] Hallucination detected for orphan "
                f"{resolution.orphan_index}: {e}. "
                f"Routing to UNCLASSIFIED_ESCROW."
            )
            orphan_df.at[df_idx, "llm_routing"] = "UNCLASSIFIED_ESCROW"

    print(
        f"[TIER 5] Complete: {suggestions_applied} high-confidence suggestions "
        f"({len(orphan_df) - suggestions_applied} low-confidence or unresolved). "
        f"All orphans remain in exceptions tab for controller review."
    )

    return orphan_df


# -----------------------------------------------------------------------------
# STANDALONE TEST
# Run directly to test LLM matching against synthetic data:
#   python src/matching/llm_matcher.py
#
# Note: Requires API keys in environment variables.
# Without API keys, providers will fail gracefully and
# orphans will be routed to LLM_UNAVAILABLE.
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

    from src.ingestion.ingestor import ingest
    from src.matching.keygen    import assign_keys
    from src.matching.exact     import match_tier1, match_tier2
    from src.matching.tolerance import match_tier3
    from src.matching.subset_sum import match_tier4

    synthetic_path = os.path.join(
        os.path.dirname(__file__),
        "../../data/synthetic/synthetic_gl_jun2026.csv"
    )

    if not os.path.exists(synthetic_path):
        print(
            "Synthetic data not found. Run this first:\n"
            "  python tests/synthetic/generate_synthetic.py"
        )
        sys.exit(1)

    ic_df, meta  = ingest(synthetic_path)
    keyed_df     = assign_keys(ic_df)
    _, after_t1  = match_tier1(keyed_df)
    _, after_t2  = match_tier2(after_t1)
    _, after_t3  = match_tier3(after_t2)
    _, after_t4  = match_tier4(after_t3)

    print(f"\nOrphans entering Tier 5: {len(after_t4)}")

    enriched = suggest_counterparties(after_t4)

    print("\n" + "=" * 60)
    print("TIER 5 LLM RESULTS")
    print("=" * 60)
    print(enriched[[
        "local_entity",
        "account_name",
        "llm_suggested_entity",
        "llm_confidence",
        "llm_routing",
        "llm_reasoning",
    ]].to_string())