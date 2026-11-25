import json
import requests
import time
import uuid
from pathlib import Path


INPUT_FILE = Path("agent_analysis.json")
OUTPUT_FILE = Path("spotloc_locations.json")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

HEADERS = {
    "User-Agent": "SpotLoc/1.0 (local location discovery prototype)"
}


def load_analysis():

    with open(INPUT_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def normalize_address(analysis):

    business = analysis.get("primary_business", {})
    location = analysis.get("location_analysis", {})

    address_parts = [
        location.get("building_number"),
        location.get("street"),
        location.get("block"),
        location.get("locality"),
        location.get("city"),
        location.get("state"),
        location.get("postal_code"),
        location.get("country")
    ]

    address = ", ".join(
        str(part).strip()
        for part in address_parts
        if part and str(part).strip()
    )

    return {
        "business_name": business.get("name"),
        "category": business.get("category"),
        "instagram_username": business.get("instagram_username"),
        "address": address,
        "building_number": location.get("building_number"),
        "street": location.get("street"),
        "block": location.get("block"),
        "locality": location.get("locality"),
        "city": location.get("city"),
        "district": location.get("district"),
        "state": location.get("state"),
        "country": location.get("country"),
        "postal_code": location.get("postal_code")
    }


def geocode_address(address, verbose=True):

    if verbose:
        print("\nSearching coordinates...")
        print("Address:", address)

    params = {
        "q": address,
        "format": "jsonv2",
        "limit": 3,
        "addressdetails": 1
    }

    try:

        response = requests.get(
            NOMINATIM_URL,
            params=params,
            headers=HEADERS,
            timeout=30
        )

        response.raise_for_status()

        results = response.json()

        if not results:
            if verbose:
                print("No matching coordinates found.")

            return {
                "latitude": None,
                "longitude": None,
                "geocoding_status": "not_found",
                "display_name": None
            }

        result = results[0]

        return {
            "latitude": float(result["lat"]),
            "longitude": float(result["lon"]),
            "geocoding_status": "coordinates_found",
            "display_name": result.get("display_name"),
            "osm_type": result.get("osm_type"),
            "osm_id": result.get("osm_id")
        }

    except requests.RequestException as error:

        if verbose:
            print("Geocoding error:", error)

        return {
            "latitude": None,
            "longitude": None,
            "geocoding_status": "error",
            "display_name": None
        }


def process_location():

    analysis = load_analysis()

    business_data = normalize_address(analysis)

    coordinates = geocode_address(
        business_data["address"]
    )

    location_record = {

        "id": "spotloc_" + str(uuid.uuid4()),

        "business": {
            "name": business_data["business_name"],
            "category": business_data["category"],
            "instagram_username": business_data["instagram_username"]
        },

        "address": {
            "full_address": business_data["address"],
            "building_number": business_data["building_number"],
            "street": business_data["street"],
            "block": business_data["block"],
            "locality": business_data["locality"],
            "city": business_data["city"],
            "district": business_data["district"],
            "state": business_data["state"],
            "country": business_data["country"],
            "postal_code": business_data["postal_code"]
        },

        "coordinates": {
            "latitude": coordinates["latitude"],
            "longitude": coordinates["longitude"]
        },

        "geocoding": {
            "status": coordinates["geocoding_status"],
            "display_name": coordinates["display_name"],
            "provider": "OpenStreetMap Nominatim",
            "osm_type": coordinates.get("osm_type"),
            "osm_id": coordinates.get("osm_id")
        },

        "source": {
            "reel_url": analysis.get("reel_url"),
            "creator": analysis.get("creator", {})
        },

        "verification": {
            "business_verified": False,
            "address_verified": False,
            "coordinates_verified": False,
            "user_confirmed": False
        }
    }

    locations = []

    if OUTPUT_FILE.exists():

        with open(OUTPUT_FILE, "r", encoding="utf-8") as file:
            locations = json.load(file)

    locations.append(location_record)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as file:

        json.dump(
            locations,
            file,
            indent=4,
            ensure_ascii=False
        )

    print("\n========== SPOTLOC LOCATION ==========")

    print("Business:", business_data["business_name"])
    print("Address:", business_data["address"])

    print(
        "Latitude:",
        coordinates["latitude"]
    )

    print(
        "Longitude:",
        coordinates["longitude"]
    )

    print(
        "Geocoding:",
        coordinates["geocoding_status"]
    )

    print("\nSaved:", OUTPUT_FILE)


if __name__ == "__main__":
    process_location()
