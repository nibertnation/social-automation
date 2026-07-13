import csv
import mimetypes
import os
from datetime import datetime

from google.cloud import storage
from google.oauth2 import service_account

# -------------------------------------------------------------------
# CSV Converter: Weekly Scheduler -> post_engine.py format (public GCS URLs)
# -------------------------------------------------------------------
# Uploads each finished image/banner to a PUBLIC Google Cloud Storage bucket
# and writes out schedule_menopause_clarity.csv in the exact format
# post_engine.py reads (platform, image_url, caption, post_time, posted).
#
# This replaces the old Publer-based workflow -- posts now go out directly
# via post_engine.py instead of being imported into Publer.
#
# NOTE: Reel rows are intentionally skipped -- those are scheduled separately.
# Only "image" and "banner" Asset_Type rows are converted.

INPUT_CSV = "schedule.csv"
OUTPUT_CSV = "schedule_menopause_clarity.csv"

# Where generate_and_process.py wrote the finished files.
IMAGE_FINAL_DIR = "generated_images"
BANNER_FINAL_DIR = "generated_banners"

# --- Google Cloud Storage config -------------------------------------------
SERVICE_ACCOUNT_FILE = "service_account.json"
GCS_BUCKET_NAME = "menopause-clarity-images"

# Both Facebook and Instagram, since Menopause Clarity posts to both.
# Change to "facebook" or "instagram" per-row in schedule.csv if you ever
# want a post to go to only one platform (add a "Platform" column and
# read it below, same pattern as the other columns).
DEFAULT_PLATFORM = "both"

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def get_storage_client():
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(
            f"Missing '{SERVICE_ACCOUNT_FILE}'. Use the same service account JSON "
            f"key as your Vertex AI setup -- just make sure it has the "
            f"'Storage Object Admin' role on the target bucket."
        )
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return storage.Client(credentials=creds, project=creds.project_id)


def find_local_file(filename):
    """Looks for the finished file in either output folder from
    generate_and_process.py."""
    for folder in (IMAGE_FINAL_DIR, BANNER_FINAL_DIR):
        candidate = os.path.join(folder, filename)
        if os.path.exists(candidate):
            return candidate
    return None


def upload_and_get_public_url(bucket, filepath):
    """Uploads a file to the bucket (skipping upload if it's already there)
    and returns its plain public URL."""
    filename = os.path.basename(filepath)
    blob = bucket.blob(filename)

    if not blob.exists():
        mime_type, _ = mimetypes.guess_type(filepath)
        blob.upload_from_filename(filepath, content_type=mime_type or "application/octet-stream")
        print(f"☁️  Uploaded: {filename}")
    else:
        print(f"⏭️  Already in bucket, skipping upload: {filename}")

    return f"https://storage.googleapis.com/{bucket.name}/{filename}"


def format_post_time(date_val, time_val):
    """Converts 'MM/DD/YYYY' + '6:00 AM' style date/time into the
    'YYYY-MM-DD HH:MM' (24-hour) format post_engine.py expects.

    Falls back to a clearly-broken placeholder if parsing fails, so a
    single malformed row doesn't crash the whole batch -- it just won't
    match any real time, and will print a warning to fix it manually."""
    raw = f"{date_val} {time_val}".strip()
    try:
        dt = datetime.strptime(raw, "%m/%d/%Y %I:%M %p")
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        print(f"⚠️  Could not parse date/time '{raw}' -- check schedule.csv formatting for this row.")
        return "FIX_ME_BAD_DATE"


def build_caption_text(caption, hashtags):
    """Combines the Caption and Hashtags columns into a single caption,
    formatted the way Instagram/Facebook captions normally look --
    caption, blank line, then hashtags."""
    caption = (caption or "").strip()
    hashtags = (hashtags or "").strip()

    if caption and hashtags:
        return f"{caption}\n\n{hashtags}"
    return caption or hashtags


def convert_schedule(input_file, output_file, bucket_name):
    if not bucket_name:
        print("❌ Error: GCS_BUCKET_NAME is empty!")
        return False

    client = get_storage_client()
    bucket = client.bucket(bucket_name)

    rows = []
    skipped_count = 0
    processed_count = 0
    failed_count = 0

    try:
        with open(input_file, mode="r", encoding="utf-8") as infile:
            reader = csv.DictReader(infile)

            for row_num, row in enumerate(reader, start=2):
                asset_type = row.get("Asset_Type", "").strip().lower()
                caption = row.get("Caption", "").strip()
                hashtags = row.get("Hashtags", "").strip()
                filename = row.get("Image_Filename", "").strip()
                date_val = row.get("Date", "").strip()
                time_val = row.get("Time", "").strip()

                if asset_type not in ["image", "banner"]:
                    # Reels are scheduled separately -- skip on purpose.
                    skipped_count += 1
                    continue

                local_path = find_local_file(filename)
                if not local_path:
                    print(f"⚠️  '{filename}' not found -- did generate_and_process.py finish?")
                    failed_count += 1
                    continue

                url = upload_and_get_public_url(bucket, local_path)
                post_time = format_post_time(date_val, time_val)
                full_caption = build_caption_text(caption, hashtags)

                rows.append({
                    "platform": DEFAULT_PLATFORM,
                    "image_url": url,
                    "caption": full_caption,
                    "post_time": post_time,
                    "posted": "False",
                })
                processed_count += 1
                print(f"✅ Row {row_num}: {asset_type} -> {filename}")

        if rows:
            # If schedule_menopause_clarity.csv already has rows (e.g. some
            # already posted), preserve them and append the new ones rather
            # than overwriting the whole file.
            existing_rows = []
            if os.path.exists(output_file):
                with open(output_file, "r", newline="", encoding="utf-8") as f:
                    existing_rows = list(csv.DictReader(f))

            all_rows = existing_rows + rows
            fieldnames = ["platform", "image_url", "caption", "post_time", "posted"]

            with open(output_file, mode="w", encoding="utf-8", newline="") as outfile:
                writer = csv.DictWriter(outfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(all_rows)

            print(f"\n✅ Conversion complete!")
            print(f"   Added: {processed_count} new rows")
            print(f"   Skipped (reels/manual): {skipped_count} rows")
            if failed_count:
                print(f"   ⚠️  Failed (missing local file): {failed_count} rows")
            print(f"   Output file: {output_file} ({len(all_rows)} total rows)")
            return True
        else:
            print("\n❌ No image/banner rows produced a URL.")
            return False

    except FileNotFoundError:
        print(f"❌ Error: '{input_file}' not found in the current directory.")
        return False


if __name__ == "__main__":
    print("🚀 Converting Weekly Scheduler CSV into post_engine.py's schedule format...\n")
    success = convert_schedule(INPUT_CSV, OUTPUT_CSV, GCS_BUCKET_NAME)

    if success:
        print("\n📤 Next step: run post_engine.py (or wait for its scheduled run)")
        print("   to actually publish these posts.")
    else:
        exit(1)
