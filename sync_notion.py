# sync_notion.py
import os
import requests
from ics import Calendar
from notion_client import Client
from dotenv import load_dotenv

load_dotenv()

notion = Client(auth=os.getenv("NOTION_TOKEN"))
DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

CAL_A_URL = os.getenv("CALENDAR_A_URL").replace("webcal://", "https://")
CAL_B_URL = os.getenv("CALENDAR_B_URL").replace("webcal://", "https://")

SYNC_MARKER = "[SYNCED DESCRIPTION]"
SYNC_CHILD_MARKER = "[SYNCED CONTENT]"

def fetch_calendar(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
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
            print(f"Warning: Event ID query failed: {e}")

    # Fallback to title (retrofit old pages)
    try:
        res = notion.databases.query(
            database_id=DATABASE_ID,
            filter={"property": "Assignment Title", "title": {"equals": title}},
        )
        if res["results"]:
            page = res["results"][0]
            # If Event ID missing, retroactively assign
            if not page["properties"].get("Event ID", {}).get("rich_text"):
                notion.pages.update(
                    page_id=page["id"],
                    properties={"Event ID": {"rich_text": [{"text": {"content": event_id}}]}}
                )
            return page
    except Exception as e:
        print(f"Warning: Title query failed: {e}")

    return None

def events_by_day(calendar):
    by_day = {}
    for event in calendar.events:
        day = event.begin.date()
        by_day.setdefault(day, []).append(event)
    return by_day

def find_matching_event(target_event, same_day_events):
    for e in same_day_events:
        if e.name and target_event.name and e.name.lower() in target_event.name.lower():
            return e
    return None

def update_page_body(page_id, full_description):
    children = notion.blocks.children.list(page_id).get("results", [])

    marker_id = None
    for block in children:
        if block["type"] == "paragraph":
            texts = block["paragraph"]["rich_text"]
            if texts and SYNC_CHILD_MARKER in texts[0]["plain_text"]:
                marker_id = block["id"]
                break

    # If no marker, create at top
    if not marker_id:
        res = notion.blocks.children.append(
            page_id,
            children=[{
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": SYNC_CHILD_MARKER}}]
                }
            }]
        )
        marker_id = res["results"][0]["id"]
        children = notion.blocks.children.list(page_id).get("results", [])

    # Collect existing synced lines after the marker
    seen = False
    existing_lines = set()
    for block in children:
        if block["id"] == marker_id:
            seen = True
            continue
        if seen and block["type"] == "paragraph":
            texts = block["paragraph"]["rich_text"]
            if texts:
                existing_lines.add(texts[0]["text"]["content"])

    # Prepare only new lines to append
    lines = [ln for ln in full_description.splitlines() if ln.strip() and ln not in existing_lines]

    new_blocks = [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": ln}}]}
        } for ln in lines
    ]

    if new_blocks:
        notion.blocks.children.append(marker_id, children=new_blocks)

def upsert_notion_event(event, accurate_event):
    event_id = getattr(event, "uid", None) or ""
    title = event.name or "Untitled Event"
    class_name = title.split(":")[0] if ":" in title else "Unknown"
    description = event.description or ""

    start_time = accurate_event.begin.isoformat()
    end_time = accurate_event.end.isoformat() if accurate_event.end else None

    props = {
        "Assignment Title": {"title": [{"text": {"content": title}}]},
        "Class": {"select": {"name": class_name}},
        "Start Time": {"date": {"start": start_time}},
        "End Time": {"date": {"start": end_time}},
        "Event ID": {"rich_text": [{"text": {"content": event_id}}]},
        "Description": {"rich_text": [{"text": {"content": "See page for details"}}]},
    }

    page = find_existing_page(event_id, title)

    if page:
        page_id = page["id"]
        print(f"Updating: {title}")
        notion.pages.update(page_id=page_id, properties=props)
        update_page_body(page_id, description)
    else:
        print(f"Creating: {title}")
        new_page = notion.pages.create(parent={"database_id": DATABASE_ID}, properties=props)
        page_id = new_page["id"]

        # Add synced content marker + description
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
        update_page_body(toggle_id, description)

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
