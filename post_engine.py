"""
post_engine.py

Reads a schedule CSV, finds any posts whose scheduled time has arrived
and haven't been posted yet, publishes them to Facebook and/or Instagram,
and marks them as posted so they never go out twice.

Run this on a timer (e.g. every 30 minutes via Task Scheduler / cron).
Each run does one pass: check schedule, post what's due, exit.

CSV FORMAT (schedule_menopause_clarity.csv):
platform,image_url,caption,post_time,posted
both,https://storage.googleapis.com/yourbucket/img1.jpg,"Real talk about hot flashes",2026-07-14 09:00,False
facebook,https://storage.googleapis.com/yourbucket/img2.jpg,"New blog post is up",2026-07-14 13:00,False
instagram,https://storage.googleapis.com/yourbucket/img3.jpg,"Reclaim your power",2026-07-14 17:00,False

- platform: "facebook", "instagram", or "both"
- image_url: must be a PUBLIC url (Instagram cannot use local files)
- caption: text for the post
- post_time: format YYYY-MM-DD HH:MM (24-hour), in your local time
- posted: True or False -- the script updates this automatically
"""

import csv
import os
import sys
from datetime import datetime

import requests

import config_menopause_clarity as cfg

GRAPH_URL = f"https://graph.facebook.com/{cfg.GRAPH_API_VERSION}"


def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")


def post_to_facebook(image_url, caption):
    """Posts a photo with caption to the Facebook Page. Returns the post ID or None."""
    resp = requests.post(
        f"{GRAPH_URL}/{cfg.PAGE_ID}/photos",
        data={
            "url": image_url,
            "caption": caption,
            "access_token": cfg.PAGE_ACCESS_TOKEN,
        },
    )
    data = resp.json()
    if "id" in data or "post_id" in data:
        log(f"  Facebook post succeeded: {data}")
        return data.get("post_id", data.get("id"))
    else:
        log(f"  Facebook post FAILED: {data}")
        return None


def post_to_instagram(image_url, caption):
    """Two-step Instagram publish: create container, then publish it. Returns media ID or None."""
    # Step 1: create container
    container_resp = requests.post(
        f"{GRAPH_URL}/{cfg.IG_USER_ID}/media",
        data={
            "image_url": image_url,
            "caption": caption,
            "access_token": cfg.PAGE_ACCESS_TOKEN,
        },
    )
    container_data = container_resp.json()
    if "id" not in container_data:
        log(f"  Instagram container FAILED: {container_data}")
        return None

    creation_id = container_data["id"]

    # Step 2: publish container
    publish_resp = requests.post(
        f"{GRAPH_URL}/{cfg.IG_USER_ID}/media_publish",
        data={
            "creation_id": creation_id,
            "access_token": cfg.PAGE_ACCESS_TOKEN,
        },
    )
    publish_data = publish_resp.json()
    if "id" in publish_data:
        log(f"  Instagram post succeeded: {publish_data}")
        return publish_data["id"]
    else:
        log(f"  Instagram publish FAILED: {publish_data}")
        return None


def check_token_expiry():
    """Warns in the log if the Page token is close to expiring (informational only)."""
    resp = requests.get(
        f"{GRAPH_URL}/debug_token",
        params={
            "input_token": cfg.PAGE_ACCESS_TOKEN,
            "access_token": cfg.PAGE_ACCESS_TOKEN,
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
    if not os.path.exists(cfg.SCHEDULE_CSV):
        log(f"Schedule file not found: {cfg.SCHEDULE_CSV}")
        sys.exit(1)

    check_token_expiry()

    now = datetime.now()
    rows = []
    made_a_post = False

    with open(cfg.SCHEDULE_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
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
        image_url = row["image_url"].strip()
        caption = row["caption"].strip()

        log(f"Posting scheduled for {post_time} -> platform: {platform}")

        success = True
        if platform in ("facebook", "both"):
            if not post_to_facebook(image_url, caption):
                success = False
        if platform in ("instagram", "both"):
            if not post_to_instagram(image_url, caption):
                success = False

        if success:
            row["posted"] = "True"
            made_a_post = True
        else:
            log("  One or more platforms failed -- leaving marked as NOT posted so it retries next run.")

    if made_a_post:
        with open(cfg.SCHEDULE_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        log("Schedule file updated.")
    else:
        log("Nothing due to post right now.")


if __name__ == "__main__":
    run()
