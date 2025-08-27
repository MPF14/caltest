# sync_notion.py
import os
import requests
from ics import Calendar
from notion_client import Client
from dotenv import load_dotenv
from datetime import datetime
from dateutil.parser import parse as parse_date

load_dotenv()

notion = Client(auth=os.getenv("NOTION_TOKEN"))
DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

CAL_A_URL = os.getenv("CALENDAR_A_URL").replace("webcal://", "https://")
CAL_B_URL = os.getenv("CALENDAR_B_URL").replace("webcal://", "https://")

def fetch_calendar(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()  # will raise a clear error if Google blocks the request
    return Calendar(response.text)

def events_by_day(calendar):
    by_day = {}
    for event in calendar.events:
        day = event.begin.date()
        by_day.setdefault(day, []).append(event)
    return by_day

def get_existing_titles():
    results = notion.databases.query(database_id=DATABASE_ID)
    existing = set()
    for page in results["results"]:
        title = page["properties"]["Assignment Title"]["title"]
        if title:
            existing.add(title[0]["text"]["content"])
    return existing

def find_matching_event(target_event, same_day_events):
    for e in same_day_events:
        if e.name and target_event.name and e.name.lower() in target_event.name.lower():
            return e
    return None

def create_notion_event(event, accurate_event):
    start_time = accurate_event.begin.isoformat()
    end_time = accurate_event.end.isoformat() if accurate_event.end else None

    title = event.name
    class_name = title.split(":")[0] if ":" in title else "Unknown"
    description = event.description or ""

    # Create the page in the database
    new_page = notion.pages.create(
        parent={"database_id": DATABASE_ID},
        properties={
            "Assignment Title": {"title": [{"text": {"content": title}}]},
            "Class": {"select": {"name": class_name}},
            "Start Time": {"date": {"start": start_time}},
            "End Time": {"date": {"start": end_time}},
            "Description": {
                "rich_text": [
                    {
                        "text": {
                            "content": "See full description inside page →"
                        }
                    }
                ]
            },
        }
    )

    # Append the full description as a block inside the page
    if description:
        notion.blocks.children.append(
            new_page["id"],
            children=[
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": description}
                            }
                        ]
                    },
                }
            ],
        )

def main():
    print("Fetching calendars...")
    cal_a = fetch_calendar(CAL_A_URL)
    cal_b = fetch_calendar(CAL_B_URL)

    events_a_by_day = events_by_day(cal_a)
    events_b_by_day = events_by_day(cal_b)
    existing_titles = get_existing_titles()

    print("Syncing events...")

    for day, events in events_b_by_day.items():
        if day not in events_a_by_day:
            continue
        accurate_events = events_a_by_day[day]
        for b_event in events:
            title = b_event.name
            if title in existing_titles:
                continue

            match = find_matching_event(b_event, accurate_events)
            if match:
                create_notion_event(b_event, match)
                print(f"Created: {title}")
            else:
                print(f"No match for: {title}")

if __name__ == "__main__":
    main()
