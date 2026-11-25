
import os
import re
import json
from pathlib import Path
from dotenv import load_dotenv
from apify_client import ApifyClient

load_dotenv(Path(__file__).resolve().parent / ".env")

APIFY_TOKEN = os.getenv("APIFY_TOKEN")

ACTOR_ID = "apify/instagram-scraper"

OUTPUT_FILE = "minimal_reel.json"


def get_reel_url():
    url = input("\nPaste Instagram Reel URL: ").strip()

    if not url:
        raise ValueError("Reel URL cannot be empty.")

    if "instagram.com" not in url:
        raise ValueError("Please enter a valid Instagram URL.")

    return url


def extract_hashtags(caption):
    if not caption:
        return []

    return list(dict.fromkeys(
        re.findall(r"#([a-zA-Z0-9_]+)", caption)
    ))


def extract_tagged_users(tagged_users):
    users = []

    if not isinstance(tagged_users, list):
        return users

    for item in tagged_users:

        if not isinstance(item, dict):
            continue

        user_data = item.get("user", item)

        if not isinstance(user_data, dict):
            continue

        username = (
            user_data.get("username")
            or user_data.get("userName")
        )

        full_name = (
            user_data.get("fullName")
            or user_data.get("full_name")
            or user_data.get("name")
        )

        if username or full_name:
            users.append({
                "username": username,
                "name": full_name
            })

    return users


def extract_location(item):
    location = item.get("locationName")

    if not location:
        location = item.get("location")

    if isinstance(location, dict):
        return {
            "name": (
                location.get("name")
                or location.get("locationName")
            ),
            "address": location.get("address")
        }

    if isinstance(location, str):
        return {
            "name": location,
            "address": None
        }

    return {
        "name": None,
        "address": None
    }


def build_minimal_output(item, reel_url):

    caption = item.get("caption") or ""

    hashtags = item.get("hashtags")

    if not isinstance(hashtags, list):
        hashtags = extract_hashtags(caption)

    else:
        hashtags = [
            tag.get("name") if isinstance(tag, dict) else tag
            for tag in hashtags
        ]

    creator = {
        "username": item.get("ownerUsername"),
        "name": item.get("ownerFullName")
    }

    location = extract_location(item)

    result = {
        "reel_url": reel_url,

        "reel_id": item.get("id"),

        "caption": caption,

        "hashtags": hashtags,

        "creator": creator,

        "thumbnail": item.get("displayUrl"),

        "video_url": item.get("videoUrl"),

        "tagged_users": extract_tagged_users(
            item.get("taggedUsers")
        ),

        "location": location,

        "timestamp": item.get("timestamp")
    }

    return result


def scrape_reel(reel_url, output_file=OUTPUT_FILE, verbose=True):

    if verbose:
        print("\nConnecting to Apify...")

    if not APIFY_TOKEN:
        raise RuntimeError("Reel extraction is not configured on this server.")

    client = ApifyClient(APIFY_TOKEN)

    if verbose:
        print("Starting Instagram Reel extraction...")

    run = client.actor(ACTOR_ID).call(
        run_input={
            "resultsType": "reels",
            "directUrls": [reel_url],
            "resultsLimit": 1
        }
    )

    if verbose:
        print("\nScraper status:", run.status)

    if run.status != "SUCCEEDED":
        raise RuntimeError(
            f"Apify scraper failed. Status: {run.status}"
        )

    dataset_id = run.default_dataset_id

    if not dataset_id:
        raise RuntimeError("No dataset ID returned by Apify.")

    items = list(
        client.dataset(dataset_id).iterate_items()
    )

    if not items:
        raise RuntimeError(
            "No Reel data received. Check the Reel URL."
        )

    if verbose:
        print("Reel data received successfully.")

    minimal_data = build_minimal_output(
        items[0],
        reel_url
    )

    if output_file:
        with open(
            output_file,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                minimal_data,
                file,
                indent=4,
                ensure_ascii=False
            )

    if verbose:
        print("\n========== MINIMAL REEL DATA ==========\n")

        print(
            json.dumps(
                minimal_data,
                indent=4,
                ensure_ascii=False
            )
        )

        print("\n=======================================")

    if output_file and verbose:
        print(f"\nSaved successfully: {output_file}")

    return minimal_data


def main():

    if not APIFY_TOKEN:
        print("ERROR: APIFY_TOKEN missing in .env")
        return

    try:
        reel_url = get_reel_url()

        scrape_reel(reel_url)

    except Exception as error:
        print("\nERROR:", error)


if __name__ == "__main__":
    main()
