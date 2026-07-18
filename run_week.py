"""
run_week.py

Runs the whole local half of the Columbus Luxury pipeline in one command:
  1. engine.py            -- generates + watermarks images/banners
  2. csv_to_schedule.py   -- uploads to GCS, builds schedule_columbus_luxury.csv
  3. git pull, then git add/commit/push -- syncs everything to GitHub

This does NOT touch schedule.csv itself -- you still generate that from
Claude and save it yourself first, since reviewing the week's content
before it becomes real image-generation costs and real posts is worth
keeping as a manual checkpoint.

USAGE:
  python3 run_week.py "Week of 07/20/2026"
  (the text in quotes becomes your git commit message -- optional, will
  use today's date if you leave it out)
"""

import subprocess
import sys
from datetime import datetime


def run_step(cmd, description):
    print(f"\n{'=' * 60}\n{description}\n{'=' * 60}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\n❌ '{description}' failed (exit code {result.returncode}).")
        print("   Stopping here so you can check what happened before anything gets pushed.")
        sys.exit(1)


def main():
    commit_message = sys.argv[1] if len(sys.argv) > 1 else f"Weekly content update {datetime.now().strftime('%Y-%m-%d')}"

    run_step([sys.executable, "engine.py"], "Step 1/3: Generating images (engine.py)")
    run_step([sys.executable, "csv_to_schedule.py"], "Step 2/3: Uploading + building schedule (csv_to_schedule.py)")

    print(f"\n{'=' * 60}\nStep 3/3: Syncing with GitHub\n{'=' * 60}")

    # Pull first, to avoid the "rejected, fetch first" situation from
    # GitHub Actions' bot committing posted-status updates in between runs.
    pull_result = subprocess.run(["git", "pull", "--no-edit"], capture_output=True, text=True)
    print(pull_result.stdout)
    if "CONFLICT" in pull_result.stdout or "CONFLICT" in pull_result.stderr:
        print("⚠️  Merge conflict detected during pull -- stopping here, this needs a human decision.")
        print("   Open the conflicted file(s) in VS Code, resolve the <<<<<<< / ======= / >>>>>>> markers,")
        print("   then run: git add .  &&  git commit -m \"Resolve conflict\"  &&  git push")
        sys.exit(1)

    subprocess.run(["git", "add", "."], check=True)
    commit_result = subprocess.run(["git", "commit", "-m", commit_message])
    if commit_result.returncode != 0:
        print("\n⚠️  Nothing new to commit (this is normal if nothing actually changed) -- skipping push.")
        return

    subprocess.run(["git", "push"], check=True)

    print("\n✅ All done! Your new content is uploaded and pushed.")
    print("   GitHub Actions will post it automatically on its normal hourly schedule --")
    print("   nothing else to do unless you want to trigger it manually to see it sooner.")


if __name__ == "__main__":
    main()
