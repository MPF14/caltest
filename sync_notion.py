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
    
def find_existing_page(event_id):
    results = notion.databases.query(
        **{
            "database_id": DATABASE_ID,
            "filter": {
                "property": "Event ID",
                "rich_text": {"equals": event_id}
            },
        }
    )
    return results["results"][0] if results["results"] else None

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

SYNC_MARKER = "[SYNCED DESCRIPTION]"
SYNC_CHILD_MARKER = "[SYNCED CONTENT]"

def upsert_notion_event(event, accurate_event):
    start_time = accurate_event.begin.isoformat()
    end_time = accurate_event.end.isoformat() if accurate_event.end else None

    title = event.name
    class_name = title.split(":")[0] if ":" in title else "Unknown"
    description = event.description or ""
    event_id = event.uid

    # Check if page already exists
    existing_page = find_existing_page(event_id)

    if existing_page:
        page_id = existing_page["id"]

        # ✅ Update properties
        notion.pages.update(
            page_id=page_id,
            properties={
                "Assignment Title": {"title": [{"text": {"content": title}}]},
                "Class": {"select": {"name": class_name}},
                "Start Time": {"date": {"start": start_time}},
                "End Time": {"date": {"start": end_time}},
                "Description": {
                    "rich_text": [
                        {"text": {"content": "See full description inside page →"}}
                    ]
                },
            },
        )

        # ✅ Find existing synced toggle
        toggle_block = None
        children = notion.blocks.children.list(page_id).get("results", [])
        for block in children:
            if block["type"] == "toggle":
                texts = block[block["type"]]["rich_text"]
                if texts and SYNC_MARKER in texts[0]["plain_text"]:
                    toggle_block = block
                    break

        if toggle_block:
            toggle_id = toggle_block["id"]

            # ✅ Get children of toggle
            toggle_children = notion.blocks.children.list(toggle_id).get("results", [])

            # Find marker child
            marker_child = None
            for child in toggle_children:
                if child["type"] == "paragraph":
                    texts = child[child["type"]]["rich_text"]
                    if texts and SYNC_CHILD_MARKER in texts[0]["plain_text"]:
                        marker_child = child
                        break

            # If marker exists, delete everything after it
            if marker_child:
                found_marker = False
                for child in toggle_children:
                    if child["id"] == marker_child["id"]:
                        found_marker = True
                        continue
                    if found_marker:
                        notion.blocks.delete(child["id"])
            else:
                # If no marker, insert one at the top
                notion.blocks.children.append(
                    toggle_id,
                    children=[{
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [
                                {"type": "text", "text": {"content": SYNC_CHILD_MARKER}}
                            ]
                        },
                    }]
                )

                # Refresh child list
                toggle_children = notion.blocks.children.list(toggle_id).get("results", [])

            # ✅ Insert new description lines *after marker*
            if description:
                lines = description.splitlines()
                new_blocks = []
                for line in lines:
                    if line.strip():
                        new_blocks.append({
                            "object": "block",
                            "type": "paragraph",
                            "paragraph": {
                                "rich_text": [{"type": "text", "text": {"content": line}}]
                            },
                        })
                    else:
                        new_blocks.append({
                            "object": "block",
                            "type": "paragraph",
                            "paragraph": {"rich_text": []},
                        })

                notion.blocks.children.append(toggle_id, children=new_blocks)

        else:
            # ✅ If no toggle, create fresh
            toggle_block = {
                "object": "block",
                "type": "toggle",
                "toggle": {
                    "rich_text": [
                        {"type": "text", "text": {"content": "Synced Description"}},
                        {
                            "type": "text",
                            "text": {"content": SYNC_MARKER},
                            "annotations": {"color": "gray"},
                        },
                    ],
                    "children": [
                        {
                            "object": "block",
                            "type": "paragraph",
                            "paragraph": {
                                "rich_text": [
                                    {"type": "text", "text": {"content": SYNC_CHILD_MARKER}}
                                ]
                            },
                        }
                    ],
                },
            }

            notion.blocks.children.append(page_id, children=[toggle_block])

            # Then append lines
            if description:
                lines = description.splitlines()
                new_blocks = []
                for line in lines:
                    if line.strip():
                        new_blocks.append({
                            "object": "block",
                            "type": "paragraph",
                            "paragraph": {
                                "rich_text": [{"type": "text", "text": {"content": line}}]
                            },
                        })
                    else:
                        new_blocks.append({
                            "object": "block",
                            "type": "paragraph",
                            "paragraph": {"rich_text": []},
                        })

                # append into the toggle we just created
                toggle_id = notion.blocks.children.list(page_id)["results"][-1]["id"]
                notion.blocks.children.append(toggle_id, children=new_blocks)

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
                upsert_notion_event(b_event, match)
                print(f"Created: {title}")
            else:
                print(f"No match for: {title}")

if __name__ == "__main__":
    main()
