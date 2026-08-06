"""
LLM provider interface (M2).
This is the ONLY file allowed to import the OpenAI SDK directly.
Every other module talks to LLMClient, so swapping providers/models
later is a one-line config change, not a code change.

Currently configured for Groq's OpenAI-compatible endpoint (free tier,
no credit card required, broad geographic availability). To switch
providers again later, change GROQ_BASE_URL and the env var read below.
"""

import json
import os

from openai import OpenAI

GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class LLMError(Exception):
    """Raised when the LLM call fails or output can't be parsed as JSON."""


class LLMClient:
    def __init__(self, model: str):
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise LLMError("GROQ_API_KEY not set in environment (.env)")
        self._client = OpenAI(api_key=api_key, base_url=GROQ_BASE_URL)
        self._model = model
        self.last_usage = {"prompt_tokens": None, "completion_tokens": None}

    def complete_json(self, system: str, user: str) -> dict:
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
            )
        except Exception as e:
            raise LLMError(f"LLM API call failed: {e}")

        raw_text = response.choices[0].message.content or ""

        usage = response.usage
        self.last_usage = {
            "prompt_tokens": usage.prompt_tokens if usage else None,
            "completion_tokens": usage.completion_tokens if usage else None,
        }

        cleaned = self._strip_code_fences(raw_text)

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise LLMError(f"Model output was not valid JSON: {e}\nRaw output: {raw_text[:500]}")

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return text