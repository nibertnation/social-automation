import csv
import os
import time
import base64
import asyncio
import subprocess
from google.cloud import aiplatform
import edge_tts

# -------------------------------------------------------------------
# Configuration -- mirrors engine.py's settings so this drops into the
# same project/workflow with no surprises.
# -------------------------------------------------------------------
PROJECT_ID = "menopause-automation"
LOCATION = "us-central1"
CSV_FILE_PATH = "schedule.csv"

RAW_DIR = "raw_generated_reels"       # backdrop stills, straight from Imagen
AUDIO_DIR = "generated_audio"          # voiceover mp3s
CAPTION_DIR = "generated_captions"     # per-reel .ass caption files
REEL_FINAL_DIR = "generated_reels"     # finished .mp4s, ready for Publer
TEMP_DIR = "generated_reels_tmp"       # intermediate silent zoom/pan videos (auto-cleaned)

# How many words appear on screen at once. 3-5 is the standard readable
# range for vertical reel captions -- higher feels cluttered, lower
# feels too flickery/fast.
WORDS_PER_CAPTION = 4

# Reuse the same bold font engine.py uses for banner overlay text, so
# captions look consistent with the rest of your content. If this font
# name/file isn't found on your system, ffmpeg just falls back to a
# default font -- it won't crash, it'll just look a little plainer.
CAPTION_FONT_NAME = "Montserrat"
CAPTION_FONT_DIR = "."  # folder containing Montserrat-Bold.ttf, if you have it
CAPTION_FONT_SIZE = 78

WAIT_SECONDS = 65   # same pacing as engine.py, same Imagen quota

# Set to a number (e.g. 1) to only process the first N reel rows -- handy
# for testing backdrop/voiceover/captions on one reel before committing
# to the full batch. Set to None to process every reel row as normal.
TEST_ROW_LIMIT = None

# Absolute duration guardrails you set: 10s floor, 60s ceiling.
MIN_DURATION = 10
MAX_DURATION = 60

# Reels are vertical.
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
FPS = 30

# Free Microsoft Edge neural voice. Swap this for another free edge-tts
# voice any time -- run `edge-tts --list-voices` in your terminal to see
# the full list (there are dozens of English options, different accents
# and genders).
VOICE = "en-US-JennyNeural"

aiplatform.init(project=PROJECT_ID, location=LOCATION)


def build_backdrop_prompt(post_title):
    """
    Post_Title looks like 'Pillar Name - short scene fragment'. Turn the
    fragment into a full Imagen prompt using the same cinematic style
    rules your scheduler prompt already specifies for image/banner rows,
    so reel backdrops look consistent with the rest of the week's posts.
    """
    if " - " in post_title:
        scene = post_title.split(" - ", 1)[1].strip()
    else:
        scene = post_title.strip()

    return (
        f"Cinematic, photorealistic lifestyle photograph of {scene}. "
        "Natural lighting, shallow depth of field, natural skin texture, "
        "candid unposed expression, emotionally grounded and real, shot "
        "like a 35mm documentary photograph. No text, no illustration, "
        "no graphic overlays, no on-screen captions, no infographic style."
    )


def generate_vertex_image(prompt_text, output_path, aspect_ratio="9:16"):
    """
    Same Imagen 3 call as engine.py, just requesting a 9:16 frame
    directly instead of the 3:4 used for feed images -- no cropping
    step needed for reels.
    """
    endpoint_name = (
        f"projects/{PROJECT_ID}/locations/{LOCATION}"
        f"/publishers/google/models/imagen-3.0-generate-002"
    )
    client_options = {"api_endpoint": f"{LOCATION}-aiplatform.googleapis.com"}
    client = aiplatform.gapic.PredictionServiceClient(client_options=client_options)

    instances = [{"prompt": prompt_text}]
    parameters = {
        "sampleCount": 1,
        "aspectRatio": aspect_ratio,
        "outputOptions": {"mimeType": "image/png"},
    }

    try:
        response = client.predict(
            endpoint=endpoint_name, instances=instances, parameters=parameters
        )
        for prediction in response.predictions:
            image_bytes = base64.b64decode(prediction["bytesBase64Encoded"])
            with open(output_path, "wb") as f:
                f.write(image_bytes)
        print(f"✅ Generated backdrop: {output_path}")
        return True
    except Exception as e:
        print(f"❌ Image generation failed for '{prompt_text[:40]}...'. Error: {e}")
        return False


def _format_ass_timestamp(td):
    total_seconds = td.total_seconds()
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = int(total_seconds % 60)
    centis = int(round((total_seconds - int(total_seconds)) * 100))
    return f"{hours:d}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def _write_grouped_ass(cues, ass_path, words_per_caption):
    """
    SubMaker gives one cue per word. Group them into short phrases
    (words_per_caption at a time) so captions read like normal on-screen
    text instead of flashing one word at a time.

    Written as a full .ass file with the style baked in (font, size,
    color, outline, position), so the ffmpeg command only ever needs to
    reference a plain file path -- no force_style string with commas
    and nested quotes for the filtergraph parser to trip over, which is
    what caused the "No option name near..." error on newer ffmpeg
    versions.
    """
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {VIDEO_WIDTH}\n"
        f"PlayResY: {VIDEO_HEIGHT}\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{CAPTION_FONT_NAME},{CAPTION_FONT_SIZE},"
        "&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,"
        "1,3,1,2,40,40,180,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    lines = [header]
    for i in range(0, len(cues), words_per_caption):
        group = cues[i:i + words_per_caption]
        start = group[0].start
        end = group[-1].end
        text = " ".join(c.content for c in group)
        lines.append(
            f"Dialogue: 0,{_format_ass_timestamp(start)},{_format_ass_timestamp(end)},"
            f"Default,,0,0,0,,{text}\n"
        )

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write("".join(lines))


async def _generate_voiceover_with_captions(text, audio_path, ass_path, words_per_caption):
    communicate = edge_tts.Communicate(text, VOICE)
    submaker = edge_tts.SubMaker()

    with open(audio_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                submaker.feed(chunk)

    _write_grouped_ass(submaker.cues, ass_path, words_per_caption)


def generate_voiceover_with_captions(text, audio_path, ass_path):
    """
    Free TTS via edge-tts, plus a synced .ass caption file built from
    the exact word-level timing the TTS engine reports -- so captions
    are always perfectly in sync with the voice, no manual alignment
    or separate transcription step needed.
    """
    try:
        asyncio.run(
            _generate_voiceover_with_captions(text, audio_path, ass_path, WORDS_PER_CAPTION)
        )
        print(f"🎙️  Generated voiceover + captions: {audio_path}")
        return True
    except Exception as e:
        print(f"❌ Voiceover/caption generation failed: {e}")
        return False


def get_audio_duration(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def _escape_for_ffmpeg_filter(path):
    # ffmpeg's filtergraph syntax treats ':' and '\' specially, so paths
    # need escaping when passed into a filter like subtitles=...
    return path.replace("\\", "\\\\").replace(":", "\\:")


def build_reel_video(image_path, audio_path, ass_path, output_path, duration, temp_dir):
    """
    Two-pass build instead of one combined filter chain:

    Pass 1: still image -> silent Ken Burns zoom/pan video, exact duration.
    Pass 2: burn captions onto that video + mux in the voiceover.

    Splitting these into separate ffmpeg invocations sidesteps a
    filtergraph parsing bug seen on some ffmpeg 8.x builds where
    chaining zoompan directly into subtitles in one -vf string breaks
    (the parser stops recognizing "subtitles" as a new filter). Each
    pass here uses a much simpler filter string, which is more robust
    across ffmpeg versions even though it costs one extra encode step.
    """
    duration = max(MIN_DURATION, min(MAX_DURATION, duration))
    frames = int(duration * FPS)

    # Scale the zoom rate to the clip's actual length so the total zoom
    # amount (1.0x -> 1.25x) is reached by the last frame no matter how
    # long the reel runs. A fixed per-frame rate looks fine at ~28s but
    # goes nearly static if stretched to 60s, so we solve for the
    # per-frame increment instead of hardcoding it.
    total_zoom = 0.25  # ends at 1.25x
    per_frame_increment = total_zoom / frames
    zoompan = (
        "scale=8000:-1,"
        f"zoompan=z=zoom+{per_frame_increment:.6f}:d={frames}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={FPS}"
    )

    base_name = os.path.splitext(os.path.basename(output_path))[0]
    temp_video_path = os.path.join(temp_dir, base_name + "_silent.mp4")

    # --- Pass 1: silent zoom/pan video ---
    pass1_cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", image_path,
        "-vf", zoompan,
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-t", str(duration),
        temp_video_path,
    ]
    result = subprocess.run(pass1_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"❌ ffmpeg (pass 1 -- zoom/pan) failed for {output_path}:\n{result.stderr[-800:]}")
        return False

    # --- Pass 2: burn captions + mux audio ---
    escaped_ass = _escape_for_ffmpeg_filter(ass_path)
    escaped_fontdir = _escape_for_ffmpeg_filter(CAPTION_FONT_DIR)
    subtitles_filter = f"subtitles={escaped_ass}:fontsdir={escaped_fontdir}"

    pass2_cmd = [
        "ffmpeg", "-y",
        "-i", temp_video_path,
        "-i", audio_path,
        "-vf", subtitles_filter,
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        output_path,
    ]
    result = subprocess.run(pass2_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"❌ ffmpeg (pass 2 -- captions/audio) failed for {output_path}:\n{result.stderr[-800:]}")
        return False

    # Clean up the intermediate silent video now that the final file exists.
    if os.path.exists(temp_video_path):
        os.remove(temp_video_path)

    print(f"🎬 Finished reel: {output_path}")
    return True


def main():
    for folder in (RAW_DIR, AUDIO_DIR, CAPTION_DIR, REEL_FINAL_DIR, TEMP_DIR):
        if not os.path.exists(folder):
            os.makedirs(folder)

    print("🚀 Reading schedule sheet and initializing reel engine...")
    if TEST_ROW_LIMIT is not None:
        print(f"🧪 TEST MODE: only processing the first {TEST_ROW_LIMIT} reel row(s).")

    reels_processed = 0

    with open(CSV_FILE_PATH, mode="r", encoding="utf-8") as infile:
        reader = csv.DictReader(infile)

        for row in reader:
            row_id = reader.line_num - 1
            asset_type = row["Asset_Type"].strip().lower()

            if asset_type != "reel":
                continue

            if TEST_ROW_LIMIT is not None and reels_processed >= TEST_ROW_LIMIT:
                print(f"🧪 TEST_ROW_LIMIT of {TEST_ROW_LIMIT} reached -- stopping early.")
                break

            filename = row["Image_Filename"].strip()
            script_text = row["Script_Text"].strip()
            post_title = row["Post_Title"].strip()

            reels_processed += 1
            final_path = os.path.join(REEL_FINAL_DIR, filename)

            # COST DEFENSE: skip if the finished reel already exists.
            if os.path.exists(final_path):
                print(f"⏭️  Skipping Row {row_id}: '{filename}' already finished.")
                continue

            if not script_text:
                print(f"⏭️  Skipping Row {row_id}: no Script_Text.")
                continue

            base_name = os.path.splitext(filename)[0]
            image_path = os.path.join(RAW_DIR, base_name + ".png")
            audio_path = os.path.join(AUDIO_DIR, base_name + ".mp3")
            ass_path = os.path.join(CAPTION_DIR, base_name + ".ass")

            # --- Backdrop image (skip if already generated, e.g. after a crash) ---
            if not os.path.exists(image_path):
                print(f"🤖 Row {row_id}: generating backdrop for '{filename}'")
                prompt = build_backdrop_prompt(post_title)
                if not generate_vertex_image(prompt, image_path):
                    continue
                print(f"⏳ Waiting {WAIT_SECONDS} seconds before next Imagen call...")
                time.sleep(WAIT_SECONDS)

            # --- Voiceover + synced captions (skip if already generated) ---
            if not os.path.exists(audio_path) or not os.path.exists(ass_path):
                print(f"🎙️  Row {row_id}: generating voiceover + captions for '{filename}'")
                if not generate_voiceover_with_captions(script_text, audio_path, ass_path):
                    continue

            # --- Assemble final reel ---
            duration = get_audio_duration(audio_path)
            build_reel_video(image_path, audio_path, ass_path, final_path, duration, TEMP_DIR)


if __name__ == "__main__":
    main()
