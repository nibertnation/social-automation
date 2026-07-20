"""
post_engine.py

Reads a schedule CSV, finds any posts whose scheduled time has arrived
and haven't been posted yet, publishes them to Facebook and/or Instagram,
and marks them as posted so they never go out twice.

Run this on a timer (e.g. every 30 minutes via Task Scheduler / cron).
Each run does one pass: check schedule, post what's due, exit.

CSV FORMAT (schedule_menopause_clarity.csv):
platform,media_type,image_url,caption,post_time,posted,fb_posted,ig_posted
both,image,https://storage.googleapis.com/yourbucket/img1.jpg,"Real talk about hot flashes",2026-07-14 09:00,False,False,False
facebook,video,https://storage.googleapis.com/yourbucket/reel1.mp4,"New reel is up",2026-07-14 13:00,False,False,False
instagram,image,https://storage.googleapis.com/yourbucket/img3.jpg,"Reclaim your power",2026-07-14 17:00,False,False,False

- platform: "facebook", "instagram", or "both"
- media_type: "image" or "video" -- video posts as a Facebook video / Instagram Reel
- image_url: must be a PUBLIC url (Instagram/Facebook cannot use local files).
  Despite the column name, this holds video URLs too when media_type is "video".
- caption: text for the post
- post_time: format YYYY-MM-DD HH:MM (24-hour), in your local time
- posted: True or False -- True only once every platform this row needs has
  succeeded. The script updates this automatically.
- fb_posted / ig_posted: True or False -- tracks each platform independently
  so that on platform="both" rows, a platform that already succeeded is never
  re-posted just because the other platform failed or is still rate-limited.
  These columns are optional in older CSVs -- missing values default to
  False and get backfilled automatically the first time the row is touched.
"""

import csv
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

EASTERN = ZoneInfo("America/New_York")

# Try to read credentials from environment variables first (this is how
# GitHub Actions provides the Secrets you configured). If they're not set
# (e.g. running locally on your laptop), fall back to the local config file.
PAGE_ID = os.environ.get("PAGE_ID")
IG_USER_ID = os.environ.get("IG_USER_ID")
PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN")
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v25.0")
SCHEDULE_CSV = os.environ.get("SCHEDULE_CSV", "schedule_menopause_clarity.csv")

if not PAGE_ID or not IG_USER_ID or not PAGE_ACCESS_TOKEN:
    try:
        import config_menopause_clarity as cfg
        PAGE_ID = PAGE_ID or cfg.PAGE_ID
        IG_USER_ID = IG_USER_ID or cfg.IG_USER_ID
        PAGE_ACCESS_TOKEN = PAGE_ACCESS_TOKEN or cfg.PAGE_ACCESS_TOKEN
        GRAPH_API_VERSION = cfg.GRAPH_API_VERSION
        SCHEDULE_CSV = cfg.SCHEDULE_CSV
    except ImportError:
        print("ERROR: No credentials found in environment variables or config file.")
        sys.exit(1)

GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")


def post_to_facebook(media_url, caption, media_type="image"):
    """Posts a photo or video with caption to the Facebook Page. Returns the post ID or None."""
    if media_type == "video":
        endpoint = f"{GRAPH_URL}/{PAGE_ID}/videos"
        data = {
            "file_url": media_url,
            "description": caption,
            "access_token": PAGE_ACCESS_TOKEN,
        }
    else:
        endpoint = f"{GRAPH_URL}/{PAGE_ID}/photos"
        data = {
            "url": media_url,
            "caption": caption,
            "access_token": PAGE_ACCESS_TOKEN,
        }

    resp = requests.post(endpoint, data=data)
    result = resp.json()
    if "id" in result or "post_id" in result:
        log(f"  Facebook post succeeded: {result}")
        return result.get("post_id", result.get("id"))
    else:
        log(f"  Facebook post FAILED: {result}")
        return None


def wait_for_instagram_container_ready(creation_id, max_wait_seconds=280, poll_interval=10):
    """Polls Instagram's container status until it's FINISHED (ready to
    publish) or ERROR, or until max_wait_seconds is exceeded. Videos/Reels
    take real processing time (often 30s-3min+), much longer than photos,
    so this checks the actual status instead of just guessing with a fixed
    retry count."""
    elapsed = 0
    while elapsed < max_wait_seconds:
        status_resp = requests.get(
            f"{GRAPH_URL}/{creation_id}",
            params={
                "fields": "status_code",
                "access_token": PAGE_ACCESS_TOKEN,
            },
        )
        status_data = status_resp.json()
        status_code = status_data.get("status_code")

        if status_code == "FINISHED":
            return True
        if status_code == "ERROR":
            log(f"  Instagram media processing failed: {status_data}")
            return False

        log(f"  Instagram media still processing (status: {status_code}), "
            f"waiting {poll_interval}s...")
        time.sleep(poll_interval)
        elapsed += poll_interval

    log("  Instagram media took too long to process -- giving up for this run, will retry next time.")
    return False


def post_to_instagram(media_url, caption, media_type="image"):
    """Two-step Instagram publish: create container, then publish it. Returns media ID or None."""
    # Step 1: create container
    container_data_payload = {
        "caption": caption,
        "access_token": PAGE_ACCESS_TOKEN,
        "is_ai_generated": True,
    }
    if media_type == "video":
        container_data_payload["video_url"] = media_url
        container_data_payload["media_type"] = "REELS"
    else:
        container_data_payload["image_url"] = media_url

    container_resp = requests.post(
        f"{GRAPH_URL}/{IG_USER_ID}/media",
        data=container_data_payload,
    )
    container_data = container_resp.json()
    if "id" not in container_data:
        log(f"  Instagram container FAILED: {container_data}")
        return None

    creation_id = container_data["id"]

    # Videos need to finish processing before they can be published --
    # check status explicitly rather than guessing with fixed retries.
    if media_type == "video":
        if not wait_for_instagram_container_ready(creation_id):
            return None

    # Step 2: publish container -- photos sometimes need a few extra seconds
    # even after creation, so still retry a few times here too.
    max_attempts = 5
    wait_seconds = 5
    for attempt in range(1, max_attempts + 1):
        publish_resp = requests.post(
            f"{GRAPH_URL}/{IG_USER_ID}/media_publish",
            data={
                "creation_id": creation_id,
                "access_token": PAGE_ACCESS_TOKEN,
            },
        )
        publish_data = publish_resp.json()
        if "id" in publish_data:
            log(f"  Instagram post succeeded: {publish_data}")
            return publish_data["id"]

        error_subcode = publish_data.get("error", {}).get("error_subcode")
        if error_subcode == 2207027 and attempt < max_attempts:
            log(f"  Instagram media still processing, waiting {wait_seconds}s (attempt {attempt}/{max_attempts})...")
            time.sleep(wait_seconds)
            continue

        log(f"  Instagram publish FAILED: {publish_data}")
        return None


def check_token_expiry():
    """Warns in the log if the Page token is close to expiring (informational only)."""
    resp = requests.get(
        f"{GRAPH_URL}/debug_token",
        params={
            "input_token": PAGE_ACCESS_TOKEN,
            "access_token": PAGE_ACCESS_TOKEN,
        },
    )
    data = resp.json()
    try:
        expires_at = data["data"]["expires_at"]
        if expires_at == 0:
            return  # token doesn't expire
        days_left = (expires_at - datetime.now().timestamp()) / 86400
        if days_left < 7:
            log(f"  WARNING: Page token expires in {days_left:.1f} days! Time to refresh it.")
    except (KeyError, TypeError):
        pass  # don't block posting just because the check failed


def run():
    if not os.path.exists(SCHEDULE_CSV):
        log(f"Schedule file not found: {SCHEDULE_CSV}")
        sys.exit(1)

    check_token_expiry()

    now = datetime.now(EASTERN).replace(tzinfo=None)
    rows = []
    made_a_change = False

    with open(SCHEDULE_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        # Backfill any columns older CSVs might not have yet, same pattern
        # as the existing media_type backfill below.
        for extra_col in ("media_type", "fb_posted", "ig_posted"):
            if fieldnames and extra_col not in fieldnames:
                fieldnames = list(fieldnames) + [extra_col]
        for row in reader:
            row.setdefault("media_type", "image")
            row.setdefault("fb_posted", "False")
            row.setdefault("ig_posted", "False")
            # Treat blank values (old rows that never had the column) as False too.
            if not row.get("fb_posted", "").strip():
                row["fb_posted"] = "False"
            if not row.get("ig_posted", "").strip():
                row["ig_posted"] = "False"
            rows.append(row)

    for row in rows:
        if row["posted"].strip().lower() == "true":
            continue

        try:
            post_time = datetime.strptime(row["post_time"].strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            log(f"Skipping row with bad date format: {row['post_time']}")
            continue

        if post_time > now:
            continue  # not due yet

        platform = row["platform"].strip().lower()
        media_type = row.get("media_type", "image").strip().lower() or "image"
        image_url = row["image_url"].strip()
        caption = row["caption"].strip()

        needs_facebook = platform in ("facebook", "both")
        needs_instagram = platform in ("instagram", "both")
        fb_already_posted = row["fb_posted"].strip().lower() == "true"
        ig_already_posted = row["ig_posted"].strip().lower() == "true"

        log(f"Checking row scheduled for {post_time} -> platform: {platform}, type: {media_type}")

        # Only attempt each platform if it's needed AND hasn't already
        # succeeded on a prior run. This is what prevents Facebook from
        # getting re-posted just because Instagram failed/rate-limited.
        if needs_facebook and not fb_already_posted:
            if post_to_facebook(image_url, caption, media_type=media_type):
                row["fb_posted"] = "True"
                made_a_change = True
            else:
                log("  Facebook failed -- will retry Facebook next run.")
        elif needs_facebook and fb_already_posted:
            log("  Facebook already posted for this row -- skipping to avoid duplicate.")

        if needs_instagram and not ig_already_posted:
            if post_to_instagram(image_url, caption, media_type=media_type):
                row["ig_posted"] = "True"
                made_a_change = True
            else:
                log("  Instagram failed -- will retry Instagram next run.")
        elif needs_instagram and ig_already_posted:
            log("  Instagram already posted for this row -- skipping to avoid duplicate.")

        # Row is fully done only once every platform it needs has succeeded.
        fb_ok = (not needs_facebook) or row["fb_posted"].strip().lower() == "true"
        ig_ok = (not needs_instagram) or row["ig_posted"].strip().lower() == "true"
        if fb_ok and ig_ok:
            if row["posted"].strip().lower() != "true":
                row["posted"] = "True"
                made_a_change = True
        else:
            log("  Row not fully posted yet -- leaving posted=False so remaining platform(s) retry next run.")

    if made_a_change:
        with open(SCHEDULE_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        log("Schedule file updated.")
    else:
        log("Nothing due to post right now.")


if __name__ == "__main__":
    run()