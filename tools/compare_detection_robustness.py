"""
Before/after comparison for the tolerance-based color matching added to
`check_day`/`check_obstacle` (see main.py).

The original detection code required an exact RGB tuple match: a pixel only
counts as "ground" or "obstacle" if it is bit-for-bit equal to
`DAY_GROUND_COLOR`/`NIGHT_GROUND_COLOR`. That works perfectly on the clean
synthetic frames in demo_frames/, because this project draws them as flat,
exact colors. It is fragile against the kind of small color drift a real
screen capture can introduce (anti-aliasing at sprite edges, dithering,
lossy video/compression artifacts) that a synthetic solid-color frame never
has, because it can never see: a pixel that is (250, 250, 250) instead of
exactly (255, 255, 255) fails an exact match completely, regardless of how
close it is.

This script builds a second frame set with a deliberate, deterministic
dithering pattern (alternating +/-6 per channel in a checkerboard, clipped
to 0-255) laid over the same clean frames, so no pixel in the "noisy" set is
ever exactly equal to the original ground/obstacle color, then runs both the
old exact-match behavior (`tolerance=0`, the default, byte-for-byte the
original logic) and the new tolerance-based behavior (`tolerance=8`) against
both frame sets and reports per-frame correctness against the known ground
truth (encoded in each frame's filename by generate_demo_frames.py).

Run directly: `python3 tools/compare_detection_robustness.py`.
"""

import os
import sys

from PIL import Image, ImageGrab

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import (  # noqa: E402
    DEFAULT_DEMO_FRAME_DIR,
    check_day,
    check_obstacle,
)

DITHER_OFFSET = 6  # per-channel checkerboard offset applied to build the noisy set
NEW_TOLERANCE = 8  # must be >= DITHER_OFFSET for the tolerant path to recover the noisy set


def expected_result(filename):
    """Derive the ground-truth (is_day, obstacle_expected) from a frame's name.

    generate_demo_frames.py names frames `NN_<day|night>_<clear|obstacle_far|obstacle_trigger>.png`,
    so the expected result is recoverable from the filename alone rather than
    needing a separate hand-maintained table that could drift out of sync.
    """
    is_day = "day" in filename and "night" not in filename
    obstacle_expected = "trigger" in filename
    return is_day, obstacle_expected


def dither(image, offset=DITHER_OFFSET):
    """Return a copy of `image` with a deterministic checkerboard color shift.

    Every pixel is nudged by `+offset` or `-offset` per channel depending on
    `(x + y) % 2`, clipped to the valid 0-255 range. This guarantees no pixel
    in the output is ever exactly equal to a color present in the input, a
    stand-in for the small, unavoidable color noise a real screen capture has
    that a hand-drawn solid-color synthetic frame does not.
    """
    pixels = image.load()
    width, height = image.size
    out = Image.new("RGB", image.size)
    out_pixels = out.load()
    for y in range(height):
        row_sign = offset if y % 2 == 0 else -offset
        for x in range(width):
            r, g, b = pixels[x, y]
            sign = row_sign if x % 2 == 0 else -row_sign
            out_pixels[x, y] = (
                max(0, min(255, r + sign)),
                max(0, min(255, g + sign)),
                max(0, min(255, b + sign)),
            )
    return out


def run_pass(frames, tolerance):
    """Run check_day/check_obstacle (via a monkeypatched ImageGrab.grab) over
    `frames` at the given tolerance and return (correct_count, total, details).
    """
    original_grab = ImageGrab.grab
    correct = 0
    details = []
    try:
        for filename, frame in frames:
            ImageGrab.grab = lambda bbox=None, _frame=frame: _frame.crop(bbox)
            expected_day, expected_obstacle = expected_result(filename)

            is_day = check_day(tolerance=tolerance)
            obstacle = check_obstacle(is_day, tolerance=tolerance)

            ok = (is_day == expected_day) and (obstacle == expected_obstacle)
            correct += ok
            details.append((filename, expected_day, is_day, expected_obstacle, obstacle, ok))
    finally:
        ImageGrab.grab = original_grab
    return correct, len(frames), details


def print_pass(label, correct, total, details):
    print(f"\n{label}: {correct}/{total} correct")
    for filename, exp_day, got_day, exp_obs, got_obs, ok in details:
        status = "OK  " if ok else "FAIL"
        print(
            f"  [{status}] {filename:<28} "
            f"day expected={exp_day!s:<5} got={got_day!s:<5} "
            f"obstacle expected={exp_obs!s:<5} got={got_obs!s:<5}"
        )


def main():
    clean_dir = DEFAULT_DEMO_FRAME_DIR
    filenames = sorted(f for f in os.listdir(clean_dir) if f.lower().endswith(".png"))
    if not filenames:
        raise FileNotFoundError(
            f"no PNG frames found in {clean_dir!r}; run tools/generate_demo_frames.py first"
        )

    clean_frames = [(name, Image.open(os.path.join(clean_dir, name)).convert("RGB")) for name in filenames]
    noisy_frames = [(name, dither(image)) for name, image in clean_frames]

    print(f"Loaded {len(clean_frames)} clean frame(s) from {clean_dir}")
    print(f"Built {len(noisy_frames)} noisy (dithered, +/-{DITHER_OFFSET} per channel) frame(s)")

    old_clean = run_pass(clean_frames, tolerance=0)
    new_clean = run_pass(clean_frames, tolerance=NEW_TOLERANCE)
    old_noisy = run_pass(noisy_frames, tolerance=0)
    new_noisy = run_pass(noisy_frames, tolerance=NEW_TOLERANCE)

    print_pass("OLD (tolerance=0, exact match) on CLEAN frames", *old_clean)
    print_pass(f"NEW (tolerance={NEW_TOLERANCE}) on CLEAN frames", *new_clean)
    print_pass("OLD (tolerance=0, exact match) on NOISY frames", *old_noisy)
    print_pass(f"NEW (tolerance={NEW_TOLERANCE}) on NOISY frames", *new_noisy)

    print("\nSummary:")
    print(f"  clean frames: old {old_clean[0]}/{old_clean[1]}, new {new_clean[0]}/{new_clean[1]}")
    print(f"  noisy frames: old {old_noisy[0]}/{old_noisy[1]}, new {new_noisy[0]}/{new_noisy[1]}")

    regressed = new_clean[0] < old_clean[0]
    improved = new_noisy[0] > old_noisy[0]
    if regressed:
        print("\nRESULT: tolerance-based matching regressed accuracy on clean frames. Do not adopt.")
    elif not improved:
        print("\nRESULT: tolerance-based matching did not improve noisy-frame accuracy. Inconclusive, do not adopt.")
    else:
        print("\nRESULT: tolerance-based matching matches old accuracy on clean frames and improves on noisy frames.")


if __name__ == "__main__":
    main()
