import json
import logging
import os
import re

import requests

logger = logging.getLogger("spotloc.agent")

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    f"models/{MODEL}:generateContent"
)

REQUEST_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "90"))


def create_prompt(data):

    reel_information = {
        "caption": data.get("caption"),
        "hashtags": data.get("hashtags"),
        "creator": data.get("creator"),
        "tagged_users": data.get("tagged_users"),
        "location_metadata": data.get("location"),
        "reel_url": data.get("reel_url")
    }

    return f"""
You are SpotLoc, an advanced Instagram Reel Business
Identification and Location Intelligence Agent.

YOUR PRIMARY OBJECTIVE:

Identify the actual business featured in the Instagram Reel
and determine its most precise possible real-world address.

LOCATION IDENTIFICATION IS YOUR HIGHEST PRIORITY.

Do not simply summarize the caption.
Understand the complete context and relationships between
business names, locations, hashtags, mentions and tagged accounts.

==================================================
REEL INFORMATION
==================================================

{json.dumps(reel_information, ensure_ascii=False, indent=2)}

==================================================
ANALYSIS INSTRUCTIONS
==================================================

1. BUSINESS IDENTIFICATION

Identify the actual business featured in the Reel.

Analyze:
- Complete caption.
- Business names mentioned in sentences.
- Instagram usernames and mentions.
- Tagged accounts.
- Hashtags containing business names.
- Location metadata.
- Contextual references to shops, restaurants, hotels,
  landmarks or other establishments.

IMPORTANT:

- Distinguish the business from the content creator.
- A food item is not automatically a business name.
- A promotional phrase is not automatically a business name.
- A tagged account may be the business's official account.
- Do not assume the creator owns or operates the business.
- If the business name is identifiable but not confirmed,
  return it as a candidate rather than returning null.
- Never invent a formal business name.

2. LOCATION EXTRACTION

Extract every available address component:

- Building number.
- Street or road.
- Block, sector, phase or zone.
- Neighbourhood or locality.
- City.
- District.
- State.
- Country.
- PIN/postal code.

Consider:
- Direct address references.
- Informal locality names.
- Nearby landmarks.
- Location-related hashtags.
- Geographic context in the caption.
- Instagram location metadata.

Do not assume that a location mentioned in the caption
necessarily belongs to the featured business.

3. LOCATION CONTEXT AND RELATIONSHIP ANALYSIS

Determine:

- Which location is associated with the business?
- Is the location the business address or merely a nearby landmark?
- Does the caption connect the business to that locality?
- Are multiple branches or locations mentioned?
- Are there conflicting location references?
- Does the Instagram location metadata support the caption?

Identify the most likely location of the featured business.

4. ADDRESS RECONSTRUCTION

Construct a readable address using only supported components.

Preserve the original address text separately.

Rules:

- Never invent missing address components.
- Never silently modify building numbers.
- Do not treat a city or locality hashtag as a complete address.
- Do not combine unrelated locations into one address.
- If only a locality is known, return only that locality.
- Clearly identify incomplete or suspicious address details.

5. LOCATION CLUES AND EVIDENCE

For each important clue, explain:

- Original text.
- What it indicates.
- Where it came from.
- Whether it supports the business identity or location.

Possible sources:
caption, hashtag, tagged_user, creator, location_metadata.

Do not claim that external verification has occurred.

6. CONFIDENCE SCORING

Assign separate confidence scores between 0.0 and 1.0.

Evaluate:

- Business identity.
- Street address.
- Locality.
- City.
- PIN code.

Confidence must reflect the actual evidence.

Examples:

1.0 = Explicit and unambiguous information.
0.8 = Strong contextual evidence.
0.5 = Plausible but incomplete evidence.
0.2 = Weak indication.
0.0 = No supporting evidence.

Do not give high confidence merely because a location
sounds geographically plausible.

7. EXTERNAL VERIFICATION QUERIES

Generate targeted browser search queries to verify:

- Business name and locality.
- Business name and street.
- Business Instagram account.
- Business name and city.
- Address and postal code.

Do not claim that these searches have already been performed.

8. MULTIPLE BUSINESSES AND LOCATIONS

If multiple businesses or locations are mentioned:

- Identify each separately.
- Distinguish the primary featured business.
- Explain why it is considered primary.
- Do not merge different businesses or branches.

9. STRICT OUTPUT RULES

Return ONLY valid JSON.

- Use null for unknown scalar values.
- Use [] for empty arrays.
- Use true/false for boolean values.
- Confidence values must be numeric.
- Do not include Markdown.
- Do not include explanations outside JSON.
- Do not generate fictional coordinates.
- Do not mark any business or address as externally verified.

==================================================
REQUIRED JSON STRUCTURE
==================================================

{{
  "primary_business": {{
    "name": null,
    "name_status": "unknown",
    "category": null,
    "instagram_username": null,
    "is_primary_location": true
  }},

  "location_analysis": {{
    "original_address_text": null,
    "building_number": null,
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
      "text": null,
      "meaning": null,
      "source": null
    }}
  ],

  "other_places": [],

  "creator": {{
    "username": null,
    "name": null
  }},

  "confidence": {{
    "business_identity": 0.0,
    "street_address": 0.0,
    "locality": 0.0,
    "city": 0.0,
    "postal_code": 0.0
  }},

  "uncertainties": [],

  "browser_search_queries": [],

  "verification": {{
    "business_verified": false,
    "address_verified": false,
    "coordinates_verified": false,
    "status": "pending_external_research"
  }}
}}
"""
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
    if "\n" in GEMINI_API_KEY or "\r" in GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY contains invalid newline characters.")

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
