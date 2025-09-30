from __future__ import annotations

import json
from textwrap import dedent
from typing import List

from .models import StatusEntry


def build_validation_messages(
    entry: StatusEntry,
    rules_text: str,
    allowed_statuses: List[str],
) -> List[dict]:
    """Compose chat messages for the LLM validation call."""

    allowed_statuses_text = (
        "; ".join(allowed_statuses) if allowed_statuses else "(constraints not provided)"
    )

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

    payload = {
        "status_value": entry.status_text,
        "comment": entry.comment_text,
        "completion_date": entry.completion_date,
        "allowed_statuses": allowed_statuses,
        "rules": rules_text,
    }

    user_prompt = dedent(
        """
        Validate the following project status according to the specified rules.
        Provide remarks in Russian and follow the JSON schema from the system prompt.

        Allowed statuses: {allowed_statuses_text}

        Row data:
        {payload_json}
        """
    ).strip()

    user_prompt = user_prompt.replace("{payload_json}", json.dumps(payload, ensure_ascii=False, indent=2))

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
