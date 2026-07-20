import csv
import mimetypes
import os
from datetime import datetime

from google.cloud import storage
from google.oauth2 import service_account

# -------------------------------------------------------------------
# CSV Converter: Weekly Scheduler -> post_engine.py format (public GCS URLs)
# -------------------------------------------------------------------
# Uploads each finished image/banner/reel to a PUBLIC Google Cloud Storage
# bucket and writes out schedule_menopause_clarity.csv in the format
# post_engine.py reads (platform, media_type, image_url, caption, post_time,
# posted).
#
# This replaces the old Publer-based workflow -- posts now go out directly
# via post_engine.py instead of being imported into Publer.
#
# DAILY INSTAGRAM SAFETY CAP: Instagram enforces a hard limit of 25 published
# posts per rolling 24-hour period. To leave headroom for content types this
# script doesn't handle yet (e.g. "creative posts"), this script automatically
# caps how many posts per calendar day get sent to Instagram at
# MAX_INSTAGRAM_POSTS_PER_DAY. Once that day's count is reached, any
# additional posts for that same day are automatically routed to
# Facebook-only instead of being skipped -- nothing is lost, it just won't
# double-post to Instagram past the safe limit. You don't need to manually
# tag rows for this -- it's automatic, counting in the order rows appear.

INPUT_CSV = "schedule.csv"
OUTPUT_CSV = "schedule_menopause_clarity.csv"

# Where generate_and_process.py wrote the finished files.
IMAGE_FINAL_DIR = "generated_images"
BANNER_FINAL_DIR = "generated_banners"
REEL_FINAL_DIR = "generated_reels"
REEL_RAW_DIR = "raw_generated_reels"

# --- Google Cloud Storage config -------------------------------------------
SERVICE_ACCOUNT_FILE = "service_account.json"
GCS_BUCKET_NAME = "menopause-clarity-images"

# Both Facebook and Instagram by default, since Menopause Clarity normally
# posts to both. Add a "Platform" column to schedule.csv (values: "both",
# "facebook", or "instagram") to manually force a specific row -- otherwise
# it falls back to this default AND to the automatic daily cap below.
DEFAULT_PLATFORM = "both"

# Leaves room for ~6 additional daily items (e.g. manually-posted "creative
# posts") that aren't part of this automated pipeline yet, while still
# staying under Instagram's real 25/day ceiling.
MAX_INSTAGRAM_POSTS_PER_DAY = 19

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


def find_local_file(filename, is_reel=False):
    """Looks for the finished file in the appropriate output folder(s)."""
    folders = (REEL_FINAL_DIR, REEL_RAW_DIR) if is_reel else (IMAGE_FINAL_DIR, BANNER_FINAL_DIR)
    for folder in folders:
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
        return f"{caption}\n\n{hashtags}\n\nAI-Assisted"
    return f"{caption or hashtags}\n\nAI-Assisted"


def dedupe_by_image_url(all_rows):
    """Collapses rows that share the same image_url, keeping the LAST
    occurrence of each. This is the safeguard against the recurring
    duplicate-row problem: when schedule.csv is retimed and this script is
    re-run, every row gets processed fresh and appended on top of the
    existing output file. Without this, a 91-row file becomes 180 rows.

    Keeping the *last* occurrence is deliberate -- the freshly-processed row
    (appended after the existing rows) carries the corrected post_time /
    platform from the latest run, so it should win over the stale existing
    copy. Rows with an empty image_url are left untouched (never merged),
    so nothing without a URL is silently dropped.

    Original row order is otherwise preserved (by the position of each
    image_url's LAST appearance)."""
    seen_index = {}          # image_url -> index into deduped list
    deduped = []
    duplicates_removed = 0

    for row in all_rows:
        url = (row.get("image_url") or "").strip()

        # Rows without a usable image_url can't be deduped safely -- keep
        # every one of them as-is.
        if not url:
            deduped.append(row)
            continue

        if url in seen_index:
            # Overwrite the earlier copy in place, preserving its position
            # but taking the newer row's data.
            deduped[seen_index[url]] = row
            duplicates_removed += 1
        else:
            seen_index[url] = len(deduped)
            deduped.append(row)

    return deduped, duplicates_removed


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
    downgraded_count = 0

    # Tracks how many posts have been routed to Instagram so far, per
    # calendar date, so the daily cap can be enforced automatically.
    instagram_count_by_date = {}

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
                platform = row.get("Platform", "").strip().lower() or DEFAULT_PLATFORM

                is_reel = asset_type == "reel"
                if asset_type not in ["image", "banner", "reel"]:
                    skipped_count += 1
                    continue

                local_path = find_local_file(filename, is_reel=is_reel)
                if not local_path:
                    print(f"⚠️  '{filename}' not found -- did generate_and_process.py finish?")
                    failed_count += 1
                    continue

                # Enforce the daily Instagram cap automatically, regardless
                # of what the Platform column says.
                wants_instagram = platform in ("both", "instagram")
                if wants_instagram:
                    day_count = instagram_count_by_date.get(date_val, 0)
                    if day_count >= MAX_INSTAGRAM_POSTS_PER_DAY:
                        if platform == "instagram":
                            print(f"⚠️  Row {row_num}: daily Instagram cap reached for {date_val} -- "
                                  f"skipping this Instagram-only row entirely.")
                            skipped_count += 1
                            continue
                        else:
                            platform = "facebook"
                            downgraded_count += 1
                            print(f"⤵️  Row {row_num}: daily Instagram cap reached for {date_val} -- "
                                  f"routing to Facebook only.")
                    else:
                        instagram_count_by_date[date_val] = day_count + 1

                url = upload_and_get_public_url(bucket, local_path)
                post_time = format_post_time(date_val, time_val)
                full_caption = build_caption_text(caption, hashtags)

                rows.append({
                    "platform": platform,
                    "media_type": "video" if is_reel else "image",
                    "image_url": url,
                    "caption": full_caption,
                    "post_time": post_time,
                    "posted": "False",
                })
                processed_count += 1
                print(f"✅ Row {row_num}: {asset_type} -> {filename} [{platform}]")

        if rows:
            # If schedule_menopause_clarity.csv already has rows (e.g. some
            # already posted), preserve them and append the new ones rather
            # than overwriting the whole file.
            existing_rows = []
            if os.path.exists(output_file):
                with open(output_file, "r", newline="", encoding="utf-8") as f:
                    existing_rows = list(csv.DictReader(f))

            # Fill in media_type="image" for any pre-existing rows from before
            # this field existed, so the CSV stays consistent.
            for r in existing_rows:
                r.setdefault("media_type", "image")

            all_rows = existing_rows + rows

            # SAFEGUARD: collapse any rows sharing the same image_url, keeping
            # the newest (last) copy. This prevents the recurring duplicate-row
            # problem when schedule.csv is retimed and this script is re-run.
            all_rows, duplicates_removed = dedupe_by_image_url(all_rows)

            fieldnames = ["platform", "media_type", "image_url", "caption", "post_time", "posted"]

            with open(output_file, mode="w", encoding="utf-8", newline="") as outfile:
                writer = csv.DictWriter(outfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(all_rows)

            print(f"\n✅ Conversion complete!")
            print(f"   Added: {processed_count} new rows")
            print(f"   Routed to Facebook-only (daily IG cap): {downgraded_count} rows")
            print(f"   Skipped (unsupported type / missing file / IG cap): {skipped_count} rows")
            if failed_count:
                print(f"   ⚠️  Failed (missing local file): {failed_count} rows")
            if duplicates_removed:
                print(f"   🧹 Deduped: {duplicates_removed} duplicate image_url row(s) collapsed "
                      f"(kept newest copy of each)")
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