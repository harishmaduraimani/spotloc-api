
import json
import os
import requests

# ==========================================
# SPOTLOC CLOUD AI CONFIGURATION
# ==========================================

MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.1-flash-lite"
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

GEMINI_API_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/"
    f"models/{MODEL}:generateContent"
)

INPUT_FILE = "minimal_reel.json"
OUTPUT_FILE = "agent_analysis.json"

REQUEST_TIMEOUT = 90


# ==========================================
# LOAD REEL DATA
# ==========================================

def load_reel():

    with open(INPUT_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


# ==========================================
# LOCATION INTELLIGENCE PROMPT
# ==========================================

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
You are SpotLoc Location Intelligence Agent.

Your primary objective is to identify the EXACT REAL-WORLD
LOCATION of a business featured in an Instagram Reel.

Location identification is more important than simply
extracting the business name.

Analyze the complete caption context.

REEL DATA:

{json.dumps(reel_information, ensure_ascii=False)}

ANALYSIS TASKS:

1. BUSINESS IDENTIFICATION
   - Identify the actual business.
   - Distinguish the business from the content creator.
   - Use tagged accounts as supporting clues.

2. LOCATION EXTRACTION
   - Extract building number.
   - Extract street or avenue.
   - Extract block, sector or phase.
   - Extract neighbourhood/locality.
   - Extract city.
   - Extract district if available.
   - Extract state.
   - Extract country.
   - Extract PIN/postal code.

3. LOCATION CONTEXT ANALYSIS
   - Understand how the caption connects the business
     to the mentioned locality.
   - Determine whether the address belongs to the business.
   - Identify repeated or conflicting location references.
   - Use hashtags as supporting locality clues.
   - Do not mistake a hashtag for a confirmed address.

4. ADDRESS RECONSTRUCTION
   - Combine address components into a readable address.
   - Preserve the original address separately.
   - Do not silently correct malformed numbers.
   - Identify missing or suspicious address components.

5. LOCATION CONFIDENCE
   Evaluate confidence separately for:
   - Business identity
   - Street address
   - Locality
   - City
   - PIN code

6. VERIFICATION REQUIREMENTS
   - Identify information that needs browser research.
   - Generate specific search queries for verification.
   - Do not claim external verification has happened.
   - Never invent missing coordinates.

7. MULTIPLE LOCATIONS
   - If several places are mentioned, identify each.
   - Identify which one is the primary featured business.

IMPORTANT RULES:

- Never invent an address.
- Never invent GPS coordinates.
- Never treat hashtags as proof of an address.
- Never claim browser verification without actual research.
- Return null for unknown information.
- Confidence values must be between 0 and 1.
- Preserve the requested JSON structure.

RETURN ONLY VALID JSON:

{{
  "primary_business": {{
    "name": null,
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

  "location_clues": [],

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


# ==========================================
# GEMINI CLOUD API
# ==========================================

def call_gemini(prompt):

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
                    {
                        "text": prompt
                    }
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

            # Do not expose API key or request headers.
            try:
                error_data = response.json()
                error_message = (
                    error_data.get("error", {}).get(
                        "message",
                        "Unknown Gemini API error"
                    )
                )
            except ValueError:
                error_message = "Gemini API request failed"

            raise RuntimeError(
                f"Gemini API error {response.status_code}: "
                f"{error_message}"
            )

        result = response.json()

        candidates = result.get("candidates", [])

        if not candidates:
            raise RuntimeError(
                "Gemini returned no analysis candidates."
            )

        candidate = candidates[0]

        content = candidate.get("content", {})

        parts = content.get("parts", [])

        output = "".join(
            part.get("text", "")
            for part in parts
        ).strip()

        if not output:
            raise RuntimeError(
                "Gemini returned empty analysis."
            )

        analysis = json.loads(output)

        if not isinstance(analysis, dict):
            raise RuntimeError(
                "Gemini response is not a JSON object."
            )

        return analysis

    except requests.exceptions.Timeout as error:

        raise RuntimeError(
            "Gemini request timed out."
        ) from error

    except requests.exceptions.ConnectionError as error:

        raise RuntimeError(
            "Could not connect to Gemini cloud API."
        ) from error

    except requests.exceptions.RequestException as error:

        raise RuntimeError(
            f"Gemini network request failed: {error}"
        ) from error

    except json.JSONDecodeError as error:

        raise RuntimeError(
            "Gemini returned invalid JSON."
        ) from error


# ==========================================
# MAIN AGENT INTERFACE
# ==========================================

def run_agent(data):

    prompt = create_prompt(data)

    print(
        "\nSpotLoc Cloud Location Intelligence Agent running..."
    )

    print(f"AI Model: {MODEL}")

    result = call_gemini(prompt)

    return result


# ==========================================
# LOCAL TESTING
# ==========================================

def main():

    try:

        if not os.path.exists(INPUT_FILE):
            raise FileNotFoundError(INPUT_FILE)

        data = load_reel()

        result = run_agent(data)

        result["reel_url"] = data.get("reel_url")

        with open(
            OUTPUT_FILE,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                result,
                file,
                indent=4,
                ensure_ascii=False
            )

        print("\n========== LOCATION ANALYSIS ==========\n")

        print(
            json.dumps(
                result,
                indent=4,
                ensure_ascii=False
            )
        )

        print("\nSaved:", OUTPUT_FILE)

    except Exception as error:

        print("\nSPOTLOC AGENT ERROR:", error)


if __name__ == "__main__":
    main()
