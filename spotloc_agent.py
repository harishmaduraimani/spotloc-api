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


def create_prompt(data: dict) -> str:
    """
    Build SpotLoc's location-first Reel analysis prompt.

    The model must distinguish information explicitly present in the Reel
    from hypotheses that need external verification. Missing facts must be
    null/empty rather than guessed.
    """
    if not isinstance(data, dict):
        raise ValueError("Reel data must be a JSON object.")

    creator = data.get("creator")
    if not isinstance(creator, dict):
        creator = {}

    location = data.get("location")
    if not isinstance(location, dict):
        if isinstance(location, str):
            location = {"name": location, "address": None}
        else:
            location = {"name": None, "address": None}

    tagged_users = data.get("tagged_users")
    if not isinstance(tagged_users, list):
        tagged_users = []

    hashtags = data.get("hashtags")
    if not isinstance(hashtags, list):
        hashtags = []

    reel_information = {
        "reel_url": data.get("reel_url"),
        "reel_id": data.get("reel_id"),
        "caption": data.get("caption") or "",
        "hashtags": hashtags,
        "creator": {
            "username": creator.get("username"),
            "name": creator.get("name")
        },
        "tagged_users": tagged_users,
        "instagram_location": {
            "name": location.get("name"),
            "address": location.get("address")
        },
        "thumbnail": data.get("thumbnail"),
        "video_url": data.get("video_url"),
        "timestamp": data.get("timestamp")
    }

    input_json = json.dumps(
        reel_information,
        ensure_ascii=False,
        indent=2,
        default=str
    )

    return f"""
You are SpotLoc's Instagram Reel location-intelligence analyst.

TASK
Analyze the supplied Instagram Reel metadata and identify the real-world
businesses and places that the Reel provides evidence for. Prioritize
identifying the featured business and reconstructing its location. Use only
the supplied evidence. Do not claim that you watched the video: the input
contains metadata and may include a thumbnail/video URL, but you must not
pretend to have opened or inspected those URLs.

IMPORTANT EVIDENCE RULES
1. Separate explicit evidence from inference.
2. Never invent a business name, address, street, locality, city, postal code,
   phone number, or social-media account.
3. If a field cannot be supported, return null. Do not fill gaps with guesses.
4. An Instagram tagged location is a clue, not proof that the featured
   business is located there.
5. Distinguish the Reel creator from the business owner. Do not assume they
   are the same person.
6. A hashtag or tagged user alone is weak evidence. Explain the evidence
   supporting each identified business.
7. If multiple businesses or places are mentioned, put the main featured
   business in primary_business and list other relevant places in other_places.
8. If the Reel does not provide enough evidence to identify a business,
   return primary_business.business_name as null and explain why.
9. Confidence values must be numbers from 0.0 to 1.0. Use conservative values.
10. Do not state that an address or business has been externally verified.
    This task is metadata analysis only. Mark verification as pending unless
    verification evidence is explicitly included in the input.
11. Preserve original text where useful. Do not translate or silently rewrite
    names and addresses.
12. Output valid JSON only. No Markdown fences, introduction, or commentary.

ANALYSIS GUIDANCE
- Inspect caption, hashtags, creator, tagged users and Instagram location
  together; do not rely on a single clue.
- Identify whether the content appears to promote, review, visit, mention or
  merely tag a place.
- Extract any address fragments and organize them into building, street,
  block, locality, city, district, state, country and postal code.
- If only a partial address is present, retain that partial address and list
  missing components as null.
- Reconstructed address must contain only components supported by the input.
- Suggest useful browser_search_queries for a later verification stage.
  Search queries are suggestions, not proof.

REQUIRED JSON SCHEMA
Return exactly one JSON object with this structure:

{{
  "primary_business": {{
    "business_name": null,
    "business_type": null,
    "instagram_username": null,
    "relationship_to_reel": null,
    "address": null,
    "building": null,
    "street": null,
    "block": null,
    "locality": null,
    "city": null,
    "district": null,
    "state": null,
    "country": null,
    "postal_code": null,
    "evidence": [],
    "confidence": 0.0,
    "search_required": true
  }},
  "location_analysis": {{
    "original_location": {{
      "name": null,
      "address": null
    }},
    "building": null,
    "street": null,
    "block": null,
    "locality": null,
    "city": null,
    "district": null,
    "state": null,
    "country": null,
    "postal_code": null,
    "reconstructed_address": null
  }},
  "location_clues": [
    {{
      "clue": "",
      "source": "caption|hashtag|creator|tagged_user|instagram_location|other",
      "supports": "",
      "confidence": 0.0
    }}
  ],
  "other_places": [
    {{
      "name": null,
      "place_type": null,
      "address": null,
      "city": null,
      "state": null,
      "country": null,
      "relationship_to_reel": null,
      "evidence": [],
      "confidence": 0.0
    }}
  ],
  "creator": {{
    "username": null,
    "name": null,
    "is_business_account": null,
    "relationship_to_business": null
  }},
  "confidence": {{
    "business": 0.0,
    "street": 0.0,
    "locality": 0.0,
    "city": 0.0,
    "postal_code": 0.0
  }},
  "uncertainties": [],
  "browser_search_queries": [],
  "verification": {{
    "business_verified": false,
    "address_verified": false,
    "verification_status": "pending",
    "verification_notes": []
  }},
  "analysis_status": "completed|insufficient_evidence"
}}

FIELD REQUIREMENTS
- primary_business.evidence: short strings quoting or accurately describing
  the input evidence that supports the proposed business.
- location_clues: include only actual clues found in the supplied data.
  If there are no clues, return [].
- other_places: return [] when no additional places are supported.
- uncertainties: list unresolved questions or missing evidence.
- browser_search_queries: provide targeted searches for later verification;
  return [] if no meaningful query can be constructed.
- verification_status must be "pending" because this prompt does not perform
  external verification. Use "not_applicable" only when no place is identified.
- analysis_status must be "insufficient_evidence" when there is not enough
  evidence to identify a primary business or meaningful location; otherwise
  use "completed".
- Use JSON null for unknown scalar values and [] for unknown lists.
- Do not add extra top-level keys.

REEL INPUT
{input_json}
""".strip()


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
        logger.error("Gemini JSON parsing failed: %s", error)
        raise RuntimeError("Gemini returned malformed JSON.") from error

    if not isinstance(result, dict):
        raise RuntimeError("Gemini response must be a JSON object.")

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
                "Gemini returned no candidates. "
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
                "Gemini returned no text. Finish reason: "
                f"{finish_reason or 'unknown'}"
            )

        return parse_gemini_json(output)

    except requests.exceptions.Timeout as error:
        logger.exception("Gemini request timed out")
        raise RuntimeError("Gemini request timed out.") from error

    except requests.exceptions.ConnectionError as error:
        logger.exception("Gemini connection failed")
        raise RuntimeError("Could not connect to Gemini.") from error

    except requests.exceptions.RequestException as error:
        logger.exception("Gemini network request failed")
        raise RuntimeError("Gemini network request failed.") from error


def run_agent(data: dict) -> dict:

    if not isinstance(data, dict):
        raise ValueError("Reel data must be a JSON object.")

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
