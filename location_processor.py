import json
import requests
import time
import uuid
import threading

from pathlib import Path


INPUT_FILE = Path("agent_analysis.json")
OUTPUT_FILE = Path("spotloc_locations.json")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

HEADERS = {
    "User-Agent": "SpotLoc/1.0 (location discovery application)"
}

REQUEST_TIMEOUT = 30
REQUEST_INTERVAL = 1.1

_last_request_time = 0.0
_request_lock = threading.Lock()


# ==================================================
# LOAD ANALYSIS
# ==================================================

def load_analysis():

    with open(INPUT_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


# ==================================================
# NORMALIZE ADDRESS
# ==================================================

def normalize_address(analysis):

    business = analysis.get("primary_business") or {}
    location = analysis.get("location_analysis") or {}

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

    if not address:
        address = (
            location.get("original_address_text")
            or location.get("reconstructed_address")
            or ""
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


# ==================================================
# NOMINATIM RATE LIMIT
# ==================================================

def wait_for_nominatim():

    global _last_request_time

    with _request_lock:

        elapsed = time.monotonic() - _last_request_time

        if elapsed < REQUEST_INTERVAL:
            time.sleep(REQUEST_INTERVAL - elapsed)

        _last_request_time = time.monotonic()


# ==================================================
# BUILD FALLBACK QUERIES
# ==================================================

def build_search_queries(data):

    queries = []

    def add(parts):

        query = ", ".join(
            str(part).strip()
            for part in parts
            if part and str(part).strip()
        )

        if query and query not in queries:
            queries.append(query)

    # 1. Complete address
    add([
        data.get("building_number"),
        data.get("street"),
        data.get("block"),
        data.get("locality"),
        data.get("city"),
        data.get("state"),
        data.get("postal_code"),
        data.get("country")
    ])

    # 2. Street + locality
    add([
        data.get("street"),
        data.get("locality"),
        data.get("city"),
        data.get("state"),
        data.get("country")
    ])

    # 3. Locality + city
    add([
        data.get("locality"),
        data.get("city"),
        data.get("state"),
        data.get("country")
    ])

    # 4. Street + city
    add([
        data.get("street"),
        data.get("city"),
        data.get("state"),
        data.get("country")
    ])

    # 5. City-level fallback
    add([
        data.get("city"),
        data.get("state"),
        data.get("country")
    ])

    return queries


# ==================================================
# DETERMINE MATCH PRECISION
# ==================================================

def identify_precision(result):

    address = result.get("address") or {}

    if address.get("house_number") and address.get("road"):
        return "building"

    if address.get("road"):
        return "street"

    if any(address.get(key) for key in [
        "neighbourhood",
        "suburb",
        "quarter",
        "city_district"
    ]):
        return "locality"

    if any(address.get(key) for key in [
        "city",
        "town",
        "village"
    ]):
        return "city"

    return "unknown"


# ==================================================
# GEOCODE ADDRESS
# ==================================================

def geocode_address(address, verbose=True):

    if isinstance(address, dict):

        address_data = address

    else:

        # Backward compatibility with existing string calls.
        address_data = {
            "address": address
        }

    original_address = address_data.get("address") or ""

    if not original_address.strip():

        return {
            "latitude": None,
            "longitude": None,
            "geocoding_status": "not_attempted",
            "display_name": None,
            "match_precision": None,
            "matched_query": None,
            "error": "Address is empty"
        }

    if verbose:
        print("\nSearching coordinates...")
        print("Address:", original_address)

    # Structured address enables fallback queries.
    if any(address_data.get(key) for key in [
        "street", "locality", "city", "state"
    ]):

        queries = build_search_queries(address_data)

    else:

        queries = [original_address]

    precision_rank = {
        "building": 4,
        "street": 3,
        "locality": 2,
        "city": 1,
        "unknown": 0
    }

    best_result = None
    last_error = None

    for index, query in enumerate(queries):

        if verbose:
            print(
                f"\nGeocoding attempt {index + 1}/{len(queries)}"
            )
            print("Query:", query)

        try:

            wait_for_nominatim()

            response = requests.get(
                NOMINATIM_URL,
                params={
                    "q": query,
                    "format": "jsonv2",
                    "limit": 5,
                    "addressdetails": 1
                },
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT
            )

            response.raise_for_status()

            results = response.json()

            if not results:

                if verbose:
                    print("No result. Trying next address variation.")

                continue

            for result in results:

                try:

                    latitude = float(result["lat"])
                    longitude = float(result["lon"])

                except (KeyError, ValueError, TypeError):
                    continue

                precision = identify_precision(result)

                candidate = {

                    "latitude": latitude,
                    "longitude": longitude,

                    "geocoding_status": "coordinates_found",

                    "display_name": result.get("display_name"),

                    "match_precision": precision,

                    "matched_query": query,

                    "osm_type": result.get("osm_type"),
                    "osm_id": result.get("osm_id"),

                    "address_details": result.get("address", {}),

                    "error": None
                }

                if (
                    best_result is None
                    or precision_rank[precision]
                    > precision_rank[best_result["match_precision"]]
                ):

                    best_result = candidate

            # A building-level match is the best available.
            if (
                best_result
                and best_result["match_precision"] == "building"
            ):
                break

        except requests.RequestException as error:

            last_error = str(error)

            if verbose:
                print("Geocoding request error:", error)

            # Continue with another query unless rate-limited.
            if response.status_code == 429:
                break

    if best_result:

        if verbose:

            print("\nCoordinates found!")

            print("Latitude:", best_result["latitude"])
            print("Longitude:", best_result["longitude"])

            print("Precision:", best_result["match_precision"])

            print("Matched address:", best_result["display_name"])

        return best_result

    if verbose:
        print("\nNo matching coordinates found.")

    return {

        "latitude": None,
        "longitude": None,

        "geocoding_status": (
            "error" if last_error else "not_found"
        ),

        "display_name": None,

        "match_precision": None,

        "matched_query": None,

        "error": last_error or "No matching geographic result found"
    }


# ==================================================
# PROCESS LOCATION
# ==================================================

def process_location():

    analysis = load_analysis()

    business_data = normalize_address(analysis)

    coordinates = geocode_address(business_data)

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
            "osm_id": coordinates.get("osm_id"),

            "match_precision": coordinates.get("match_precision"),

            "matched_query": coordinates.get("matched_query"),

            "error": coordinates.get("error")
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

    print("Latitude:", coordinates["latitude"])
    print("Longitude:", coordinates["longitude"])

    print("Geocoding:", coordinates["geocoding_status"])

    print("Precision:", coordinates.get("match_precision"))

    print("\nSaved:", OUTPUT_FILE)

    return location_record


if __name__ == "__main__":
    process_location()
