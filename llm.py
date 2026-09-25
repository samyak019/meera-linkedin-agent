import json
import os
import re
import time

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

import config  # noqa: F401  (loads .env)

API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash").strip()

client = genai.Client(api_key=API_KEY) if API_KEY else None


def set_api_key(key):
    """Validate a key without spending generation quota, then switch to it."""
    global API_KEY, client
    candidate = genai.Client(api_key=key)
    try:
        candidate.models.get(model=MODEL_NAME)
    except genai_errors.APIError as exc:
        raise LLMUnavailable(f"Google rejected that key ({exc.code}).") from exc
    API_KEY, client = key, candidate


MAX_RETRIES = 5


class LLMUnavailable(RuntimeError):
    pass


def generate(system, prompt, grounded=False, temperature=0.7):
    """Returns (text, sources). sources is a list of {title, url} from search grounding, if any."""
    if client is None:
        raise LLMUnavailable("GEMINI_API_KEY is not set")
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        tools=[types.Tool(google_search=types.GoogleSearch())] if grounded else None,
    )
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(model=MODEL_NAME, contents=prompt, config=config)
            break
        except genai_errors.APIError as exc:
            if "PerDay" in str(exc):
                raise LLMUnavailable(
                    "Gemini's free-tier daily quota is used up (resets midnight Pacific). "
                    "A key with billing enabled removes this limit."
                ) from exc
            # Per-minute 429s (free tier: 5 requests/min) and 503 (overloaded) are transient.
            if exc.code not in (429, 500, 503) or attempt == MAX_RETRIES:
                raise LLMUnavailable(f"Gemini error {exc.code}: {exc.message or exc}") from exc
            time.sleep(_retry_delay(exc, attempt))
    return (response.text or "").strip(), _grounding_sources(response)


def _retry_delay(exc, attempt):
    hinted = re.search(r"retry in ([\d.]+)s", str(exc))
    return float(hinted.group(1)) + 1 if hinted else min(60, 5 * 2 ** attempt)


def generate_json(system, prompt, grounded=False, temperature=0.4):
    text, sources = generate(system, prompt, grounded=grounded, temperature=temperature)
    return parse_json(text), sources


def parse_json(text):
    """Grounded calls can't use a response schema, so pull the first JSON object out of the text."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        raise LLMUnavailable(f"Model did not return valid JSON: {text[:300]}")


def _grounding_sources(response):
    out = []
    try:
        meta = response.candidates[0].grounding_metadata
        for chunk in (meta.grounding_chunks or []):
            if chunk.web:
                out.append({"title": chunk.web.title, "url": chunk.web.uri})
    except (AttributeError, IndexError, TypeError):
        pass
    return out


TRANSCRIBE_PROMPT = (
    "Transcribe this voice note verbatim in English (translate only if it isn't English). Keep the "
    "speaker's own words, numbers and phrasing; drop filler like 'um'. Output only the transcript."
)


def transcribe(audio_bytes, mime_type="audio/ogg"):
    if client is None:
        raise LLMUnavailable("GEMINI_API_KEY is not set")
    parts = [types.Part.from_bytes(data=audio_bytes, mime_type=mime_type), TRANSCRIBE_PROMPT]
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(model=MODEL_NAME, contents=parts)
            return (response.text or "").strip()
        except genai_errors.APIError as exc:
            if "PerDay" in str(exc) or exc.code not in (429, 500, 503) or attempt == MAX_RETRIES:
                raise LLMUnavailable(f"Transcription failed ({exc.code})") from exc
            time.sleep(_retry_delay(exc, attempt))
