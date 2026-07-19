import csv
import os
import time
from google import genai
from google.genai.types import GenerateContentConfig
from PIL import Image, ImageDraw, ImageFont

# -------------------------------------------------------------------
# 1. Configuration & Project Settings
# -------------------------------------------------------------------
PROJECT_ID = "menopause-automation"
LOCATION = "us-central1"
CSV_FILE_PATH = "schedule.csv"

RAW_DIR = "raw_generated"          # untouched output straight from the model
IMAGE_FINAL_DIR = "generated_images"    # finished 4:5 images, no text, ready for posting
BANNER_FINAL_DIR = "generated_banners"  # finished 4:5 banners, text burned in, ready for posting

WAIT_SECONDS = 65

# Path to a bold sans-serif TTF/OTF font file on your machine.
FONT_PATH = "Montserrat-Bold.ttf"

# Final crop target (4:5). Source images come out taller than this and get
# center-cropped down.
TARGET_WIDTH = 1080
TARGET_HEIGHT = 1350

# Gemini image models use "global" as the location rather than a specific
# region -- unlike the old Imagen setup, which used us-central1.
genai_client = genai.Client(vertexai=True, project=PROJECT_ID, location="global")


def generate_vertex_image(prompt_text, output_path):
    """Sends a text prompt to Gemini 3 Pro Image (Google's current,
    actively-maintained image model -- "Nano Banana Pro") and saves the
    resulting image locally.

    This replaces the old Imagen 3 model (imagen-3.0-generate-002), which
    is on Google's deprecation path -- the recommended migration date has
    already passed as of this update. Also gets you a real quality
    upgrade: native higher-resolution output and better prompt adherence
    than Imagen 3 had.
    """
    response = genai_client.models.generate_content(
        model="gemini-3-pro-image-preview",
        contents=prompt_text,
        config=GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config={"image_size": "2K", "aspect_ratio": "3:4"},
        ),
    )

    try:
        for part in response.candidates[0].content.parts:
            if part.inline_data is not None:
                with open(output_path, "wb") as f:
                    f.write(part.inline_data.data)
                print(f"✅ Generated raw image: {output_path}")
                return True
        print(f"❌ No image data in response for prompt: '{prompt_text[:30]}...'")
        return False
    except Exception as e:
        print(f"❌ Failed to generate image for prompt: '{prompt_text[:30]}...'. Error: {e}")
        return False


def crop_to_4_5(image):
    """
    Center-crops the generated image down to 4:5 (1080x1350).
    Trims evenly off the top and bottom so subjects centered in the
    original frame stay centered.
    """
    w, h = image.size
    if w != TARGET_WIDTH:
        scale = TARGET_WIDTH / w
        image = image.resize((TARGET_WIDTH, int(h * scale)))
        w, h = image.size

    if h <= TARGET_HEIGHT:
        return image  # already short enough, nothing to trim

    excess = h - TARGET_HEIGHT
    top_trim = excess // 2
    return image.crop((0, top_trim, TARGET_WIDTH, top_trim + TARGET_HEIGHT))


MAX_FONT_SIZE = 190
MIN_FONT_SIZE = 60
MAX_LINES = 2
BAR_COLOR = (0, 0, 0, 110)  # semi-transparent black bar behind the text


def load_font(size):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:
        print(f"⚠️  Could not load font at '{FONT_PATH}', falling back to default font.")
        return ImageFont.load_default()


def wrap_text_to_lines(draw, text, font, max_width, max_lines=MAX_LINES):
    """Greedily wraps text into at most max_lines lines that each fit
    within max_width. Returns None if it can't be made to fit even at
    max_lines (caller should try a smaller font size)."""
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        w = draw.textbbox((0, 0), test, font=font)[2]
        if w <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            if len(lines) >= max_lines:
                return None
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        return None
    for line in lines:
        if draw.textbbox((0, 0), line, font=font)[2] > max_width:
            return None  # a single word is too long even alone
    return lines


def fit_wrapped_text(draw, text, max_width):
    """Finds the largest font size where the text wraps into 1-2 lines
    that all fit within max_width. Prefers going big -- this is what lets
    banners have the same bold, confident scale as AI-rendered text,
    while every letter is still guaranteed correctly spelled since we're
    drawing it ourselves."""
    for size in range(MAX_FONT_SIZE, MIN_FONT_SIZE, -6):
        font = load_font(size)
        lines = wrap_text_to_lines(draw, text, font, max_width)
        if lines:
            return font, lines
    font = load_font(MIN_FONT_SIZE)
    lines = wrap_text_to_lines(draw, text, font, max_width, max_lines=3) or [text]
    return font, lines


def add_overlay_text(image, overlay_text, position="upper"):
    """Burns the banner's overlay text onto the image: bold white text,
    wrapped across up to 2 lines at the largest size that fits, with a
    heavy black outline/shadow for contrast -- matching the master prompt's own banner spec.

    position: "upper", "center", or "lower" -- where the text block sits
    vertically in the frame. Defaults to "upper" if not specified."""
    image = image.convert("RGBA")
    overlay_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay_layer)

    max_text_width = int(TARGET_WIDTH * 0.88)
    font, lines = fit_wrapped_text(draw, overlay_text, max_text_width)

    line_heights = []
    line_widths = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_widths.append(bbox[2] - bbox[0])
        line_heights.append(bbox[3] - bbox[1])
    line_spacing = int(max(line_heights) * 0.25)
    block_height = sum(line_heights) + line_spacing * (len(lines) - 1)
    block_width = max(line_widths)

    if position == "center":
        block_top = (TARGET_HEIGHT - block_height) // 2
    elif position == "lower":
        block_top = int(TARGET_HEIGHT * 0.72) - block_height
    else:  # "upper" default
        block_top = int(TARGET_HEIGHT * 0.08)

    bar_pad_x, bar_pad_y = 36, 24
    bar_box = [
        (TARGET_WIDTH - block_width) // 2 - bar_pad_x,
        block_top - bar_pad_y,
        (TARGET_WIDTH + block_width) // 2 + bar_pad_x,
        block_top + block_height + bar_pad_y,
    ]
    draw.rounded_rectangle(bar_box, radius=18, fill=BAR_COLOR)

    outline_width = 6
    y = block_top
    for i, line in enumerate(lines):
        x = (TARGET_WIDTH - line_widths[i]) // 2
        for dx in range(-outline_width, outline_width + 1, 2):
            for dy in range(-outline_width, outline_width + 1, 2):
                if dx == 0 and dy == 0:
                    continue
                draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0, 255))
        draw.text((x + 8, y + 8), line, font=font, fill=(0, 0, 0, 140))
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_heights[i] + line_spacing

    composited = Image.alpha_composite(image, overlay_layer)
    return composited.convert("RGB")


def process_image(raw_path, asset_type, overlay_text, overlay_position, filename):
    """
    Opens the raw generated image, crops it to 4:5, and (for banners)
    burns in the overlay text. Saves the finished file to the correct
    final folder.
    """
    image = Image.open(raw_path)
    cropped = crop_to_4_5(image)

    if asset_type == "banner":
        if not overlay_text:
            print(f"⚠️  Banner row '{filename}' has no Overlay_Text -- saving without text.")
            final_image = cropped
        else:
            final_image = add_overlay_text(cropped, overlay_text, position=overlay_position)
        destination = os.path.join(BANNER_FINAL_DIR, filename)
    else:
        final_image = cropped
        destination = os.path.join(IMAGE_FINAL_DIR, filename)

    final_image.save(destination)
    print(f"🖼️  Finished {asset_type}: {destination}")


def main():
    for folder in (RAW_DIR, IMAGE_FINAL_DIR, BANNER_FINAL_DIR):
        if not os.path.exists(folder):
            os.makedirs(folder)

    print("🚀 Reading schedule sheet and initializing engine...")

    with open(CSV_FILE_PATH, mode='r', encoding='utf-8') as infile:
        reader = csv.DictReader(infile)

        for row in reader:
            row_id = reader.line_num - 1
            asset_type = row['Asset_Type'].strip().lower()
            prompt = row['Image_Prompt'].strip()
            overlay_text = row.get('Overlay_Text', '').strip()
            overlay_position = row.get('Overlay_Position', '').strip().lower() or 'upper'
            filename = row['Image_Filename'].strip()

            if asset_type not in ["image", "banner"]:
                print(f"⏭️  Skipping Row {row_id}: {asset_type} does not need an image.")
                continue
            if not prompt:
                print(f"⏭️  Skipping Row {row_id}: no image prompt.")
                continue

            raw_path = os.path.join(RAW_DIR, filename)
            final_dir = BANNER_FINAL_DIR if asset_type == "banner" else IMAGE_FINAL_DIR
            final_path = os.path.join(final_dir, filename)

            # COST DEFENSE: skip if the finished file already exists.
            if os.path.exists(final_path):
                print(f"⏭️  Skipping Row {row_id}: '{filename}' already finished.")
                continue

            # If the raw file already exists (e.g. crashed mid-run after
            # generating but before processing), skip regeneration and
            # just process what's already there.
            if not os.path.exists(raw_path):
                print(f"🤖 Processing Row {row_id} | Generating image for: {filename}")
                success = generate_vertex_image(prompt, raw_path)
                if not success:
                    continue
                print(f"⏳ Waiting {WAIT_SECONDS} seconds before next image...")
                time.sleep(WAIT_SECONDS)

            process_image(raw_path, asset_type, overlay_text, overlay_position, filename)


if __name__ == "__main__":
    main()
