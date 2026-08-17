from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import json
import re
import time
import os


BASE_URL = (
    "https://www.booli.se/sok/slutpriser"
    "?areaIds=143&objectType=Lägenhet"
)

OUTPUT_FILE = "sold_listings.json"
START_PAGE = 1
MAX_PAGES = 2


def parse_display_attributes(property):
    display_key = 'displayAttributes({"queryContext":"SERP_LIST_LISTING"})'
    display = property.get(display_key)

    result = {
        "living_area": None,
        "rooms": None,
        "floor": None
    }

    if not display:
        return result

    for point in display.get("dataPoints", []):
        value = point.get("value", {})
        text = value.get("plainText", "")
        screen_reader = point.get("screenReaderLabel", "")

        if "kvadratmeter" in screen_reader.lower():
            match = re.search(
                r"([\d\s,.]+)\s*(?:m²|kvadratmeter)",
                text + " " + screen_reader,
                re.IGNORECASE
            )

            if match:
                number = match.group(1).replace(" ", "").replace(",", ".")

                try:
                    result["living_area"] = float(number)
                except ValueError:
                    pass

        elif "rum" in screen_reader.lower():
            match = re.search(
                r"([\d,.]+)\s*rum",
                text,
                re.IGNORECASE
            )

            if match:
                number = match.group(1).replace(",", ".")

                try:
                    result["rooms"] = float(number)
                except ValueError:
                    pass

        elif "vån" in text.lower():
            match = re.search(
                r"vån\s*(.+)",
                text,
                re.IGNORECASE
            )

            if match:
                result["floor"] = match.group(1).strip()

    return result


def parse_amenities(property):
    result = {
        "elevator": False,
        "balcony": False,
        "fireplace": False
    }

    amenities = property.get("amenities", [])

    if not isinstance(amenities, list):
        return result

    for amenity in amenities:
        if not isinstance(amenity, dict):
            continue

        ref = amenity.get("__ref", "").lower()

        if '"key":"elevator"' in ref:
            result["elevator"] = True
        elif '"key":"balcony"' in ref:
            result["balcony"] = True
        elif '"key":"fireplace"' in ref:
            result["fireplace"] = True

    return result


def get_raw(property, field):
    value = property.get(field)

    if isinstance(value, dict):
        raw = value.get("raw")

        if raw is not None:
            return raw

    return None


def get_property_id(property):
    property_url = property.get("url")

    if property_url:
        match = re.search(r"/bostad/(\d+)", property_url)

        if match:
            return match.group(1)

    residence_id = property.get("residenceId")

    if residence_id:
        return str(residence_id)

    return None


def extract_apollo_state(html):
    soup = BeautifulSoup(html, "html.parser")

    for script in soup.find_all("script"):
        if script.get("type") != "application/json":
            continue

        text = script.string or script.get_text()

        if "__APOLLO_STATE__" not in text:
            continue

        try:
            data = json.loads(text)

            state = (
                data
                .get("props", {})
                .get("pageProps", {})
                .get("__APOLLO_STATE__")
            )

            if state:
                return state

        except Exception:
            continue

    return None


def extract_detailed_property(state, property_id):
    if not state:
        return None

    root_query = state.get("ROOT_QUERY", {})

    for key, value in root_query.items():
        if not key.startswith("propertyByResidenceId("):
            continue

        if not isinstance(value, dict):
            continue

        reference = value.get("__ref")

        if reference:
            detailed = state.get(reference)

            if detailed:
                return detailed

    for key, value in state.items():
        if not key.startswith("SoldProperty:"):
            continue

        if not isinstance(value, dict):
            continue

        residence_id = value.get("residenceId")
        booli_id = value.get("booliId")
        object_id = value.get("id")

        if (
            str(residence_id) == str(property_id)
            or str(booli_id) == str(property_id)
            or str(object_id) == str(property_id)
        ):
            return value

    return None


def fetch_property_details(page, property_url, attempts=3):
    empty_result = {
        "monthly_fee": None,
        "operating_cost": None,
        "elevator": False,
        "balcony": False,
        "fireplace": False
    }

    if not property_url:
        return empty_result

    if property_url.startswith("/"):
        property_url = "https://www.booli.se" + property_url

    print("   Opening property page:", property_url)

    for attempt in range(1, attempts + 1):
        try:
            response = page.goto(
                property_url,
                wait_until="domcontentloaded",
                timeout=60000
            )

            if response is None:
                print(f"   Attempt {attempt}: no response")
                continue

            print(f"   HTTP: {response.status}")

            if response.status >= 400:
                print(
                    f"   Attempt {attempt}: HTTP {response.status}"
                )
                time.sleep(attempt)
                continue

            try:
                page.wait_for_function(
                    """
                    () => document.documentElement.innerHTML.includes(
                        "__APOLLO_STATE__"
                    )
                    """,
                    timeout=15000
                )
            except Exception:
                pass

            html = page.content()
            state = extract_apollo_state(html)

            if not state:
                print(
                    f"   Attempt {attempt}: Apollo state not found"
                )
                time.sleep(attempt)
                continue

            detailed_property = None

            match = re.search(r"/bostad/(\d+)", property_url)

            if match:
                property_id = match.group(1)
                detailed_property = extract_detailed_property(
                    state,
                    property_id
                )

            if detailed_property is None:
                for key, value in state.items():
                    if (
                        key.startswith("SoldProperty:")
                        and isinstance(value, dict)
                    ):
                        detailed_property = value
                        break

            if detailed_property is None:
                print("   Could not find detailed property")
                continue

            monthly_fee = get_raw(detailed_property, "rent")
            operating_cost = get_raw(
                detailed_property,
                "operatingCost"
            )

            amenities = parse_amenities(detailed_property)

            print("   Monthly fee:", monthly_fee)
            print("   Operating cost:", operating_cost)
            print("   Elevator:", amenities["elevator"])
            print("   Balcony:", amenities["balcony"])
            print("   Fireplace:", amenities["fireplace"])

            return {
                "monthly_fee": monthly_fee,
                "operating_cost": operating_cost,
                "elevator": amenities["elevator"],
                "balcony": amenities["balcony"],
                "fireplace": amenities["fireplace"]
            }

        except Exception as e:
            print(f"   Attempt {attempt} error:", e)
            time.sleep(attempt)

    print("   FAILED after all attempts")
    return empty_result


def build_page_url(page_number):
    if page_number == 1:
        return BASE_URL

    return f"{BASE_URL}&page={page_number}"


def load_existing_listings():
    if not os.path.exists(OUTPUT_FILE):
        return []

    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return data

    except Exception as e:
        print("Could not read existing JSON:", e)

    return []


def save_listings(listings):
    temp_file = OUTPUT_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(
            listings,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(temp_file, OUTPUT_FILE)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)

    context = browser.new_context(
        locale="sv-SE",
        viewport={
            "width": 1440,
            "height": 1000
        }
    )

    page = context.new_page()

    existing_listings = load_existing_listings()

    print(
        "\nExisting listings in JSON:",
        len(existing_listings)
    )

    existing_ids = set()

    for listing in existing_listings:
        listing_id = listing.get("sold_property_id")

        if listing_id:
            existing_ids.add(str(listing_id))

    page_number = START_PAGE

    while True:
        if MAX_PAGES is not None and page_number > MAX_PAGES: # First condition is evaluated first so when its false if max_pages is None and we hit and, it just goes to false without eval the 2nd check which would raise a typeError
            break

        search_url = build_page_url(page_number)

        print("\n\n" + "=" * 60)
        print(f"PAGE {page_number}")
        print("=" * 60)
        print("Opening:", search_url)

        try:
            response = page.goto(
                search_url,
                wait_until="networkidle",
                timeout=60000
            )

            if response is not None:
                print("Search HTTP:", response.status)

                if response.status >= 400:
                    print("Search page failed.")
                    break

        except Exception as e:
            print("ERROR opening search page:", e)
            break

        print("Title:", page.title())

        html = page.content()
        print("HTML length:", len(html))

        with open(
            "sold_search.html",
            "w",
            encoding="utf-8"
        ) as f:
            f.write(html)

        state = extract_apollo_state(html)

        if state is None:
            print("ERROR: Apollo state not found.")
            break

        sold_properties = []

        for key, value in state.items():
            if (
                key.startswith("SoldProperty:")
                and isinstance(value, dict)
            ):
                sold_properties.append(value)

        print(
            f"Found {len(sold_properties)} SoldProperty objects."
        )

        if not sold_properties:
            print("\nNo properties found.")
            print("Reached the end of the search.")
            break

        page_listings = []

        for index, property in enumerate(
            sold_properties,
            start=1
        ):
            print("\n" + "-" * 40)
            print(
                f"PAGE {page_number} - "
                f"PROPERTY {index}/{len(sold_properties)}"
            )
            print("-" * 40)

            print(
                "Address:",
                property.get("streetAddress")
            )

            display = parse_display_attributes(property)

            sold_price = property.get("soldPrice")
            sold_price_raw = (sold_price or {}).get("raw")

            list_price = property.get("listPrice")
            list_price_raw = (list_price or {}).get("raw")

            property_url = property.get("url")
            property_id = get_property_id(property)

            print("Property ID:", property_id)
            print("Property URL:", property_url)

            sold_property_id = property.get("id")

            if (
                sold_property_id
                and str(sold_property_id) in existing_ids
            ):
                print("Already saved - skipping.")
                continue

            details = fetch_property_details(
                page,
                property_url
            )

            row = {
                "sold_property_id": property.get("id"),
                "booli_id": property.get("booliId"),
                "address": property.get("streetAddress"),
                "area": property.get("descriptiveAreaName"),
                "object_type": property.get("objectType"),
                "living_area": display["living_area"],
                "rooms": display["rooms"],
                "floor": display["floor"],
                "sold_price": sold_price_raw,
                "list_price": list_price_raw,
                "monthly_fee": details["monthly_fee"],
                "operating_cost": details["operating_cost"],
                "elevator": details["elevator"],
                "balcony": details["balcony"],
                "fireplace": details["fireplace"],
                "sold_price_type": property.get("soldPriceType"),
                "days_active": property.get("daysActive"),
                "sold_date": property.get("soldDate"),
                "url": property.get("url"),
                "latitude": property.get("latitude"),
                "longitude": property.get("longitude")
            }

            page_listings.append(row)

            if sold_property_id:
                existing_ids.add(str(sold_property_id))

            time.sleep(0.5)

        existing_listings.extend(page_listings)
        save_listings(existing_listings)

        print("\n" + "=" * 60)
        print(f"PAGE {page_number} SAVED")
        print("=" * 60)
        print("Listings found:", len(sold_properties))
        print("New listings saved:", len(page_listings))
        print("Total listings:", len(existing_listings))
        print("Saved to:", OUTPUT_FILE)

        page_number += 1
        time.sleep(1)

    print("\n\n" + "=" * 60)
    print("COMPLETE")
    print("=" * 60)
    print("Pages processed:", page_number - START_PAGE)
    print("Total listings:", len(existing_listings))
    print("JSON file:", OUTPUT_FILE)

    input("\nPress ENTER to close...")

    browser.close()