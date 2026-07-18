"""
validate_schedule.py

Checks a Weekly Scheduler CSV output against the Menopause Clarity prompt's
own rules, before it gets fed into engine.py. Catches the kind of thing a
chat's own self-report can miss -- like claiming "zero duplicates" while
still shipping 48 repeated First_Comment values.

Usage:
    python3 validate_schedule.py schedule.csv
"""

import csv
import sys
from collections import Counter

BANNED_PHRASES = [
    "drop a comment", "drop a heart", "comment below", "let me know in the comments",
    "share if you agree", "share this with", "share for a chance",
    "tag a friend", "tag someone", "tag a fellow",
    "like if you", "like and share", "hit like",
    "raise your hand if", "react if", "type yes if", "where you at",
    "you won't believe", "shocking truth",
]

# Expected quotas per week -- extend this if later weeks have different totals.
WEEK_QUOTAS = {
    1: {"total": 105, "image_banner": 84, "reel": 21, "manual": 0},
    2: {"total": 140, "image_banner": 119, "reel": 21, "manual": 0},
    3: {"total": 175, "image_banner": 133, "reel": 21, "manual": 21},
    4: {"total": 210, "image_banner": 168, "reel": 21, "manual": 21},
}


def is_negated_illustration_word(text_lower, idx):
    """Checks if an illustration-style word is preceded by a negation like
    'no illustration' -- which is correct (an instruction to AVOID that
    style), not a violation."""
    preceding = text_lower[max(0, idx - 5):idx]
    return "no " in preceding or "not " in preceding


def validate(path, week_num=None):
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    issues = []
    warnings = []

    total = len(rows)
    asset_counts = Counter(r["Asset_Type"].strip().lower() for r in rows)

    if week_num and week_num in WEEK_QUOTAS:
        q = WEEK_QUOTAS[week_num]
        if total != q["total"]:
            issues.append(f"Row count is {total}, expected {q['total']} for Week {week_num}")
        actual_img_banner = asset_counts.get("image", 0) + asset_counts.get("banner", 0)
        if actual_img_banner != q["image_banner"]:
            issues.append(f"image+banner count is {actual_img_banner}, expected {q['image_banner']}")
        if asset_counts.get("reel", 0) != q["reel"]:
            issues.append(f"reel count is {asset_counts.get('reel', 0)}, expected {q['reel']}")
        if q["manual"] and asset_counts.get("manual", 0) != q["manual"]:
            issues.append(f"manual count is {asset_counts.get('manual', 0)}, expected {q['manual']}")

    illustration_words = ["illustration", "vector", "clipart", "icon", "infographic",
                          "cartoon", "line art", "flat design", "canva"]

    for i, r in enumerate(rows, start=2):
        cap = r.get("Caption", "")
        com = r.get("First_Comment", "")
        cap_l, com_l = cap.lower(), com.lower()
        asset = r.get("Asset_Type", "").strip().lower()

        for bp in BANNED_PHRASES:
            if bp in cap_l:
                issues.append(f"Row {i}: banned phrase in Caption: '{bp}'")
            if bp in com_l:
                issues.append(f"Row {i}: banned phrase in First_Comment: '{bp}'")

        if asset == "banner":
            overlay = r.get("Overlay_Text", "")
            wc = len(overlay.split())
            if wc == 0:
                issues.append(f"Row {i}: banner has no Overlay_Text")
            elif not (3 <= wc <= 8):
                issues.append(f"Row {i}: Overlay_Text is {wc} words (limit 5-8): '{overlay}'")

        if asset == "reel":
            script = r.get("Script_Text", "")
            wc = len(script.split())
            if wc == 0:
                issues.append(f"Row {i}: reel has no Script_Text")
            elif not (40 <= wc <= 120):
                warnings.append(f"Row {i}: Script_Text is {wc} words (target ~75)")
            if "[" in script or "on-screen" in script.lower() or "camera" in script.lower():
                issues.append(f"Row {i}: possible camera direction in Script_Text")

        image_prompt = r.get("Image_Prompt", "")
        ip_lower = image_prompt.lower()
        for w in illustration_words:
            idx = ip_lower.find(w)
            while idx != -1:
                if not is_negated_illustration_word(ip_lower, idx):
                    issues.append(f"Row {i}: non-negated '{w}' in Image_Prompt (may not be photorealistic)")
                idx = ip_lower.find(w, idx + 1)

        if not cap.strip():
            issues.append(f"Row {i}: empty Caption")
        if not com.strip():
            issues.append(f"Row {i}: empty First_Comment")

    # --- The check that matters most: uniqueness across the whole batch ---
    def uniqueness_report(field_name, values):
        non_empty = [v for v in values if v.strip()]
        counts = Counter(non_empty)
        dupes = {k: v for k, v in counts.items() if v > 1}
        if dupes:
            affected = sum(dupes.values())
            issues.append(
                f"{field_name}: {len(dupes)} distinct value(s) repeated, "
                f"affecting {affected}/{len(non_empty)} rows"
            )
            for text, count in list(dupes.items())[:5]:
                issues.append(f"    x{count}: {text[:80]}")

    uniqueness_report("Caption", [r.get("Caption", "") for r in rows])
    uniqueness_report("First_Comment", [r.get("First_Comment", "") for r in rows])
    uniqueness_report("Image_Prompt", [r.get("Image_Prompt", "") for r in rows])

    return issues, warnings, total, asset_counts


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 validate_schedule.py <schedule.csv> [week_number]")
        sys.exit(1)

    path = sys.argv[1]
    week_num = int(sys.argv[2]) if len(sys.argv) > 2 else None

    issues, warnings, total, asset_counts = validate(path, week_num)

    print(f"Checked {total} rows in '{path}'")
    print(f"Asset type breakdown: {dict(asset_counts)}")
    print()

    if warnings:
        print(f"⚠️  {len(warnings)} warning(s) (not blocking, worth a look):")
        for w in warnings[:10]:
            print(f"   - {w}")
        print()

    if issues:
        print(f"❌ {len(issues)} issue(s) found -- do NOT run engine.py until these are fixed:")
        for i in issues:
            print(f"   - {i}")
        sys.exit(1)
    else:
        print("✅ All checks passed. Safe to proceed to engine.py.")
        sys.exit(0)


if __name__ == "__main__":
    main()