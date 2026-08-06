"""
Script generation stage (M2).
article.txt -> validated script.json, using the LLM behind LLMClient.
"""

import json
import unicodedata
from pathlib import Path

from pydantic import ValidationError

from pipeline.providers.llm import LLMClient, LLMError
from pipeline.schemas import Script

MAX_RETRIES = 3  # total attempts = 1 + MAX_RETRIES = 4
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


class ScriptGenError(Exception):
    """Raised when script generation fails after all retries."""


def count_words(text: str) -> int:
    normalized = unicodedata.normalize("NFC", text)
    return len(normalized.split())


def load_prompt_template(language: str) -> str:
    prompt_path = PROMPTS_DIR / f"script_{language}.txt"
    if not prompt_path.exists():
        raise ScriptGenError(f"No prompt template found for language '{language}' at {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def run_script_gen(job_dir: Path, config: dict) -> dict:
    article_path = job_dir / "article.txt"
    if not article_path.exists():
        raise ScriptGenError("article.txt not found — run ingest stage first.")
    article_text = article_path.read_text(encoding="utf-8")

    language = config.get("language", "en")
    min_seconds = config["script"]["min_seconds"]
    max_seconds = config["script"]["max_seconds"]
    words_per_second = config["script"]["words_per_second"][language]
    model = config["llm"]["model"]

    target_words = int(((min_seconds + max_seconds) / 2) * words_per_second)
    min_words = int(min_seconds * words_per_second * 0.80)
    max_words = int(max_seconds * words_per_second * 1.20)

    template = load_prompt_template(language)
    system_prompt = "You are a precise, JSON-only assistant. Always respond with valid JSON only."

    client = LLMClient(model=model)

    last_error = None
    extra_instruction = ""
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    best_attempt = None

    for attempt in range(1, MAX_RETRIES + 2):
        user_prompt = (
            template
            .replace("{target_words}", str(target_words))
            .replace("{min_seconds}", str(min_seconds))
            .replace("{max_seconds}", str(max_seconds))
            .replace("{article_text}", article_text)
        )
        if extra_instruction:
            user_prompt += f"\n\nIMPORTANT CORRECTION NEEDED: {extra_instruction}"

        try:
            raw = client.complete_json(system_prompt, user_prompt)
        except LLMError as e:
            last_error = str(e)
            extra_instruction = f"Your previous response caused this error: {last_error}. Respond with ONLY valid JSON matching the required schema."
            continue

        if hasattr(client, "last_usage") and client.last_usage.get("prompt_tokens"):
            total_usage["prompt_tokens"] += client.last_usage["prompt_tokens"] or 0
            total_usage["completion_tokens"] += client.last_usage["completion_tokens"] or 0

        try:
            script = Script.model_validate(raw)
        except ValidationError as e:
            last_error = str(e)
            extra_instruction = f"Your previous response failed schema validation: {last_error}. Fix the JSON structure exactly."
            continue

        word_count = count_words(script.full_text())

        if min_words <= word_count <= max_words:
            return _save_and_return(job_dir, script, word_count, attempt, total_usage)

        distance = max(min_words - word_count, word_count - max_words, 0)
        if best_attempt is None or distance < best_attempt[0]:
            best_attempt = (distance, script, word_count, attempt)

        last_error = f"Script was {word_count} words, needed between {min_words}-{max_words}."
        if word_count < min_words:
            deficit = target_words - word_count
            extra_instruction = (
                f"CRITICAL: Your last script was only {word_count} words, but it MUST be "
                f"between {min_words} and {max_words} words (target: {target_words}). "
                f"That is {deficit} MORE words needed. Expand EVERY scene with more detail, "
                f"examples, and explanation. Count your words before responding."
            )
        else:
            excess = word_count - target_words
            extra_instruction = (
                f"CRITICAL: Your last script was {word_count} words, {excess} words OVER the "
                f"target of {target_words}. Trim it down by tightening each scene's wording, "
                f"while keeping the same structure and all scene roles."
            )

    if best_attempt is not None:
        distance, script, word_count, attempt = best_attempt
        result = _save_and_return(job_dir, script, word_count, attempt, total_usage)
        result["note"] = (
            f"Accepted closest attempt ({word_count} words) after {MAX_RETRIES + 1} tries — "
            f"did not land exactly within {min_words}-{max_words}."
        )
        return result

    raise ScriptGenError(
        f"Script generation failed after {MAX_RETRIES + 1} attempts. Last error: {last_error}"
    )


def _save_and_return(job_dir: Path, script: Script, word_count: int, attempt: int, total_usage: dict) -> dict:
    script_dict = script.model_dump()
    script_dict["_meta"] = {
        "word_count": word_count,
        "attempts": attempt,
        "token_usage": total_usage,
    }
    script_path = job_dir / "script.json"
    script_path.write_text(
        json.dumps(script_dict, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return {
        "word_count": word_count,
        "attempts": attempt,
        "scenes": len(script.scenes),
        "token_usage": total_usage,
    }