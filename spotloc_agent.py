import json
import logging
import os
import re

import requests

logger = logging.getLogger("spotloc.agent")

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    f"models/{MODEL}:generateContent"
)

REQUEST_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "90"))


def parse_gemini_json(output: str) -> dict:
    """Parse and validate Gemini's JSON response."""

    if not output or not output.strip():
        raise RuntimeError("Gemini returned an empty response.")

    output = output.strip()

    # Remove Markdown JSON fences if Gemini includes them.
    output = re.sub(
        r"^```(?:json)?\s*",
        "",
        output,
        flags=re.IGNORECASE
    )

    output = re.sub(r"\s*```$", "", output)

    try:
        result = json.loads(output)
    except json.JSONDecodeError as error:
        logger.error(
            "Gemini JSON parsing failed: %s",
            error
        )
        raise RuntimeError(
            "Gemini returned malformed JSON."
        ) from error

    if not isinstance(result, dict):
        raise RuntimeError(
            "Gemini response must be a JSON object."
        )

    return result


def call_gemini(prompt: str) -> dict:

    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is missing from server environment."
        )

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY
    }

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "maxOutputTokens": 4096
        }
    }

    try:
        response = requests.post(
            GEMINI_API_URL,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT
        )

        if not response.ok:

            try:
                error_data = response.json()
                error_message = (
                    error_data.get("error", {}).get("message")
                    or "Unknown Gemini API error"
                )
                error_status = (
                    error_data.get("error", {}).get("status")
                    or "UNKNOWN"
                )
            except ValueError:
                error_message = response.text[:1000]
                error_status = "UNKNOWN"

            logger.error(
                "Gemini API failure: HTTP %s, status=%s, message=%s",
                response.status_code,
                error_status,
                error_message
            )

            raise RuntimeError(
                f"Gemini API returned HTTP {response.status_code}: "
                f"{error_message}"
            )

        try:
            response_data = response.json()
        except ValueError as error:
            raise RuntimeError(
                "Gemini returned an invalid HTTP response."
            ) from error

        candidates = response_data.get("candidates") or []

        if not candidates:
            block_reason = (
                response_data.get("promptFeedback", {})
                .get("blockReason")
            )

            raise RuntimeError(
                f"Gemini returned no candidates. "
                f"Block reason: {block_reason or 'unknown'}"
            )

        candidate = candidates[0]

        finish_reason = candidate.get("finishReason")

        if finish_reason == "MAX_TOKENS":
            raise RuntimeError(
                "Gemini response was truncated because the output "
                "token limit was reached."
            )

        parts = (
            candidate.get("content", {})
            .get("parts", [])
        )

        output = "".join(
            part.get("text", "")
            for part in parts
            if isinstance(part, dict)
        ).strip()

        if not output:
            raise RuntimeError(
                f"Gemini returned no text. Finish reason: "
                f"{finish_reason or 'unknown'}"
            )

        return parse_gemini_json(output)

    except requests.exceptions.Timeout as error:
        logger.exception("Gemini request timed out")
        raise RuntimeError(
            "Gemini request timed out."
        ) from error

    except requests.exceptions.ConnectionError as error:
        logger.exception("Gemini connection failed")
        raise RuntimeError(
            "Could not connect to Gemini."
        ) from error

    except requests.exceptions.RequestException as error:
        logger.exception("Gemini network request failed")
        raise RuntimeError(
            "Gemini network request failed."
        ) from error


def run_agent(data: dict) -> dict:

    if not isinstance(data, dict):
        raise ValueError(
            "Reel data must be a JSON object."
        )

    prompt = create_prompt(data)

    logger.info(
        "SpotLoc Gemini analysis started. Model=%s",
        MODEL
    )

    result = call_gemini(prompt)

    result["reel_url"] = data.get("reel_url")

    logger.info(
        "SpotLoc Gemini analysis completed successfully."
    )

    return result
