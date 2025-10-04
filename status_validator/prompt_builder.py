from __future__ import annotations

import json
from hashlib import sha256
from textwrap import dedent
from typing import List, Tuple

from .models import StatusEntry


def compute_cache_key(rules_text: str, allowed_statuses: List[str]) -> str:
    """Compute a stable cache key based on validation rules.
    
    This key is used as prompt_cache_key parameter to help OpenAI
    identify requests that should share the same cached context.
    
    Args:
        rules_text: Full validation rules text
        allowed_statuses: List of allowed status values
        
    Returns:
        SHA256 hash (first 16 chars) of the rules configuration
    """
    # Combine rules and statuses into a stable string
    combined = f"{rules_text}\n---\n{';'.join(sorted(allowed_statuses))}"
    hash_value = sha256(combined.encode('utf-8')).hexdigest()
    # Use first 16 chars for brevity (still unique enough)
    return f"rules_{hash_value[:16]}"


def build_validation_messages(
    entry: StatusEntry,
    rules_text: str,
    allowed_statuses: List[str],
) -> Tuple[List[dict], str]:
    """Compose chat messages for the LLM validation call.
    
    Optimized for OpenAI prompt caching:
    - Static content (system prompt, rules, allowed statuses) placed first
    - Dynamic content (row data) placed last
    - Ensures caching of repetitive parts (>1024 tokens)
    - Returns prompt_cache_key to improve cache hit rates
    
    Returns:
        Tuple of (messages list, prompt_cache_key string)
    """

    allowed_statuses_text = (
        "; ".join(allowed_statuses) if allowed_statuses else "(constraints not provided)"
    )

    # STATIC PART 1: System prompt with JSON schema
    system_prompt = dedent(
        """
        You are an auditor that verifies project status updates. Assess whether the provided
        status and comment comply with the expectations. Think carefully and explain your reasoning step-by-step.
        Always reply in Russian. Respond only with valid JSON that matches the required schema.

        Required JSON schema (all fields mandatory):
        {
          "is_valid": boolean,
          "issues": ["text"],
          "rewrite_suggestion": "text"
        }

        - "issues" must be a non-empty array with human readable bullet texts even if the
          status is compliant (use a positive confirmation in that case and place the ✅ symbol at the beginning of the line 
          or place the ❌ symbol if it is not compliant). 
        - The rewrite suggestion must fix every issue and keep concrete dates and facts when
          available. Produce a fully rewritten status that satisfies every rule. Keep it brief.
          Use only the facts present in the original status during the rewriting.
          Do not add any new facts or dates that are not present in the original status.
        - Validate format requirements, date ranges, allowed status names, and logical
          completeness (what is done, current work, next milestones, risks).
        - If information is missing, explain exactly what is missing and why it matters.
        - Pay attention to mismatches between the status column value and the comment body.
        - If the status column contains a value that is not in the allowed list, include this as
          a critical issue.
        """
    ).strip()

    # STATIC PART 2: Rules and allowed statuses (this will be cached by OpenAI)
    rules_prompt = dedent(
        """
        Validation rules that must be followed:
        
        {rules_text}
        
        ---
        
        Allowed status values:
        {allowed_statuses_text}
        
        If the status column contains a value that is not in this list, it must be marked as invalid.
        """
    ).strip()
    
    rules_prompt = rules_prompt.replace("{rules_text}", rules_text)
    rules_prompt = rules_prompt.replace("{allowed_statuses_text}", allowed_statuses_text)

    # DYNAMIC PART: Row-specific data (this changes for every request)
    row_data = {
        "status_value": entry.status_text,
        "comment": entry.comment_text,
        "completion_date": entry.completion_date,
    }
    
    data_prompt = dedent(
        """
        Validate the following project status according to the rules above.
        Provide remarks in Russian and follow the JSON schema from the system prompt.

        Row data:
        {row_json}
        """
    ).strip()
    
    data_prompt = data_prompt.replace("{row_json}", json.dumps(row_data, ensure_ascii=False, indent=2))

    # Compute cache key for this validation context
    cache_key = compute_cache_key(rules_text, allowed_statuses)
    
    # Structure optimized for caching: static content first, dynamic last
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": rules_prompt},
        {"role": "assistant", "content": "Понял правила валидации. Готов проверить данные строки."},
        {"role": "user", "content": data_prompt},
    ]
    
    return messages, cache_key
