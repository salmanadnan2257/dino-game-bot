"""
Generates a small set of synthetic Dino-game screenshots for offline demo mode.

These are not screen captures of a live game; there is no running Chrome/dino
game in this environment to capture from. Instead this script draws frames by
hand with PIL, using the exact colors and coordinates `main.py` actually reads
(`DAY_GROUND_COLOR`, `NIGHT_GROUND_COLOR`, `GAME_AREA_COORDINATES`,
`OBSTACLE_AREA_COORDINATES`, imported directly from main.py rather than
copied, so this can't silently drift out of sync with the real detection
code), so that replaying them through the real, unmodified `check_day` /
`check_obstacle` functions exercises the same logic a live capture would.

Run directly: `python3 tools/generate_demo_frames.py`. Writes PNGs to
`demo_frames/` next to this project's main.py.
"""

import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import (  # noqa: E402  (import after sys.path fixup, by design)
    DAY_GROUND_COLOR,
    GAME_AREA_COORDINATES,
    NIGHT_GROUND_COLOR,
    OBSTACLE_AREA_COORDINATES,
)

# Canvas is a bit wider than OBSTACLE_AREA_COORDINATES's right edge so an
# obstacle can be placed fully outside the trigger box ("approaching, not
# triggered yet") as well as inside it ("in the trigger zone, jump now").
CANVAS_WIDTH = 1050
CANVAS_HEIGHT = 800

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo_frames")


def _obstacle_color_for(is_day):
    """Return the color an obstacle sprite renders in, for a given mode.

    Matches the logic in `check_obstacle`: obstacles render in the *other*
    mode's ground color (dark grey on a white day background, white on a
    dark night background), which is exactly what that function scans for.
    """
    return NIGHT_GROUND_COLOR if is_day else DAY_GROUND_COLOR


def make_frame(is_day, obstacle_x=None, obstacle_width=40, obstacle_height=60):
    """Build one synthetic frame.

    `is_day` fills the whole canvas with the day or night ground color, which
    is the simplification this synthetic set makes: the real game has sky and
    a thin ground strip, but `check_day` only ever compares white-pixel count
    vs. grey-pixel count across `GAME_AREA_COORDINATES`, so a solid fill of
    the dominant color drives that comparison the same way. `obstacle_x` is
    the left edge of a simple obstacle rectangle; `None` means no obstacle in
    the frame at all.
    """
    background = DAY_GROUND_COLOR if is_day else NIGHT_GROUND_COLOR
    image = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), background)

    if obstacle_x is not None:
        draw = ImageDraw.Draw(image)
        # Vertically centered inside OBSTACLE_AREA_COORDINATES's row span so
        # the sprite actually lands inside the box check_obstacle reads.
        _, top, _, bottom = OBSTACLE_AREA_COORDINATES
        obstacle_top = bottom - obstacle_height - 10
        obstacle_bottom = obstacle_top + obstacle_height
        draw.rectangle(
            [obstacle_x, obstacle_top, obstacle_x + obstacle_width, obstacle_bottom],
            fill=_obstacle_color_for(is_day),
        )

    return image


def build_frame_set():
    """Return an ordered list of (filename, is_day, obstacle_x, description).

    `obstacle_x` positions are chosen relative to the real
    `OBSTACLE_AREA_COORDINATES` bounds so each frame's expected detection
    result follows directly from where the obstacle was drawn:
    - clear frames: no obstacle at all.
    - "far" frames: obstacle drawn to the right of the box's right edge, i.e.
      visible on screen (approaching) but not yet in the trigger zone.
    - "trigger" frames: obstacle drawn well inside the box, so it must be
      detected and must cause a jump.
    """
    left, _, right, _ = OBSTACLE_AREA_COORDINATES
    inside_x = left + (right - left) // 2  # comfortably inside the box
    far_x = right + 60  # comfortably outside (to the right of) the box

    return [
        ("01_day_clear.png", True, None, "day mode, empty track, no jump expected"),
        ("02_day_obstacle_far.png", True, far_x, "day mode, obstacle approaching but outside the trigger zone"),
        ("03_day_obstacle_trigger.png", True, inside_x, "day mode, obstacle inside the trigger zone, jump expected"),
        ("04_night_clear.png", False, None, "night mode, empty track, no jump expected"),
        ("05_night_obstacle_far.png", False, far_x, "night mode, obstacle approaching but outside the trigger zone"),
        ("06_night_obstacle_trigger.png", False, inside_x, "night mode, obstacle inside the trigger zone, jump expected"),
    ]


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    frame_set = build_frame_set()
    written = []
    for filename, is_day, obstacle_x, description in frame_set:
        image = make_frame(is_day, obstacle_x)
        path = os.path.join(OUTPUT_DIR, filename)
        image.save(path)
        written.append((filename, description))
        print(f"wrote {path}  ({description})")
    print(f"\n{len(written)} frames written to {OUTPUT_DIR}")
    return written


if __name__ == "__main__":
    main()
