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
    
def find_existing_page(event_id, title):
    # Try Event ID first
    if event_id:
        try:
            res = notion.databases.query(
                database_id=DATABASE_ID,
                filter={"property": "Event ID", "rich_text": {"equals": event_id}},
            )
            if res["results"]:
                return res["results"][0]
        except Exception as e:
            print(f"Warn: query by Event ID failed: {e}")

    # Fallback to Assignment Title (for retrofitting legacy rows)
    try:
        res = notion.databases.query(
            database_id=DATABASE_ID,
            filter={"property": "Assignment Title", "title": {"equals": title}},
        )
        if res["results"]:
            return res["results"][0]
    except Exception as e:
        print(f"Warn: query by title failed: {e}")

    return None

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
    event_id = getattr(event, "uid", None) or ""  # make sure it's a string
    title = event.name or "Untitled Event"
    class_name = title.split(":")[0] if ":" in title else "Unknown"
    full_description = event.description or ""

    start_time = accurate_event.begin.isoformat()
    end_time = accurate_event.end.isoformat() if accurate_event.end else None

    # Properties: keep Description short (pointer), always set Event ID
    props = {
        "Assignment Title": {"title": [{"text": {"content": title}}]},
        "Class": {"select": {"name": class_name}},
        "Start Time": {"date": {"start": start_time}},
        "End Time": {"date": {"start": end_time}},
        "Description": {"rich_text": [{"text": {"content": "See full description inside page →"}}]},
        "Event ID": {"rich_text": [{"text": {"content": event_id}}]},
    }

    page = find_existing_page(event_id, title)

    if page:
        page_id = page["id"]
        print(f"Updating: {title}")

        # Update properties (including Event ID retrofit)
        notion.pages.update(page_id=page_id, properties=props)

        # Find existing synced toggle (by marker in toggle title rich_text[0])
        toggle_id = None
        children = notion.blocks.children.list(page_id).get("results", [])
        for block in children:
            if block["type"] == "toggle":
                texts = block["toggle"]["rich_text"]
                if texts and SYNC_MARKER in texts[-1]["plain_text"]:
                    toggle_id = block["id"]
                    break

        # If no toggle, create it
        if not toggle_id:
            new_toggle = {
                "object": "block",
                "type": "toggle",
                "toggle": {
                    "rich_text": [
                        {"type": "text", "text": {"content": "Synced Description"}},
                        {"type": "text", "text": {"content": SYNC_MARKER}, "annotations": {"color": "gray"}},
                    ],
                    "children": [],
                },
            }
            res = notion.blocks.children.append(page_id, children=[new_toggle])
            toggle_id = res["results"][0]["id"]

        # Ensure marker child exists; then wipe only content after it
        toggle_children = notion.blocks.children.list(toggle_id).get("results", [])
        marker_child = None
        for child in toggle_children:
            if child["type"] == "paragraph":
                texts = child["paragraph"]["rich_text"]
                if texts and SYNC_CHILD_MARKER in texts[0]["plain_text"]:
                    marker_child = child
                    break

        if not marker_child:
            # create marker at top
            notion.blocks.children.append(
                toggle_id,
                children=[{
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": SYNC_CHILD_MARKER}}]},
                }],
            )
            toggle_children = notion.blocks.children.list(toggle_id).get("results", [])

        # Delete everything AFTER the marker
        seen = False
        for child in toggle_children:
            if not seen and child["type"] == "paragraph":
                texts = child["paragraph"]["rich_text"]
                if texts and SYNC_CHILD_MARKER in texts[0]["plain_text"]:
                    seen = True
                    continue
            elif seen:
                notion.blocks.delete(child["id"])

        # Append the fresh description as lines after the marker
        if full_description is not None:
            lines = full_description.splitlines()
            new_blocks = [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": ln}}]} if ln.strip() else {"rich_text": []},
                }
                for ln in lines
            ]
            if new_blocks:
                notion.blocks.children.append(toggle_id, children=new_blocks)

    else:
        print(f"Creating: {title}")
        # Create the page
        new_page = notion.pages.create(parent={"database_id": DATABASE_ID}, properties=props)
        page_id = new_page["id"]

        # Create the synced toggle with marker + description lines
        lines = (full_description or "").splitlines()
        toggle = {
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": [
                    {"type": "text", "text": {"content": "Synced Description"}},
                    {"type": "text", "text": {"content": SYNC_MARKER}, "annotations": {"color": "gray"}},
                ],
                "children": [
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": SYNC_CHILD_MARKER}}]},
                    }
                ],
            },
        }
        res = notion.blocks.children.append(page_id, children=[toggle])
        toggle_id = res["results"][0]["id"]

        if lines:
            new_blocks = [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": ln}}]} if ln.strip() else {"rich_text": []},
                }
                for ln in lines
            ]
            notion.blocks.children.append(toggle_id, children=new_blocks)

def main():
    print("Fetching calendars...")
    cal_a = fetch_calendar(CAL_A_URL)
    cal_b = fetch_calendar(CAL_B_URL)

    events_a_by_day = events_by_day(cal_a)
    events_b_by_day = events_by_day(cal_b)

    print("Syncing events...")

    for day, events in events_b_by_day.items():
        if day not in events_a_by_day:
            continue
        accurate_events = events_a_by_day[day]

        for b_event in events:
            match = find_matching_event(b_event, accurate_events)
            if not match:
                print(f"No match for: {b_event.name}")
                continue

            upsert_notion_event(b_event, match)
        
if __name__ == "__main__":
    main()
