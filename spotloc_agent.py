
import json
import requests
from pathlib import Path

MODEL = "qwen3:4b"

INPUT_FILE = "minimal_reel.json"
OUTPUT_FILE = "agent_analysis.json"

OLLAMA_API = "http://localhost:11434/api/chat"


def load_reel():

    with open(INPUT_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


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

IMPORTANT:
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


def run_agent(data):

    prompt = create_prompt(data)

    print("\nSpotLoc Location Intelligence Agent running...\n")

    response = requests.post(
        OLLAMA_API,
        json={
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "format": "json",
            "stream": False,
            "think": False,
            "options": {
                "temperature": 0.1,
                "num_ctx": 8192
            }
        },
        timeout=300
    )

    response.raise_for_status()

    content = response.json()["message"]["content"]

    return json.loads(content)


def main():

    try:

        if not Path(INPUT_FILE).exists():
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

        print(json.dumps(
            result,
            indent=4,
            ensure_ascii=False
        ))

        print("\nSaved:", OUTPUT_FILE)

    except requests.exceptions.ConnectionError:
        print("ERROR: Ollama is not running.")

    except requests.exceptions.Timeout:
        print("ERROR: Agent timed out.")

    except Exception as error:
        print("ERROR:", error)


if __name__ == "__main__":
    main()
