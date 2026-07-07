"""
Automates the Chrome offline Dino game by watching a screen region for
obstacles and pressing space to jump.

Detection strategy: grab pixels from a screen region just in front of the
dino, and check whether that region contains "ground" pixels (grey in day
mode, white in night mode) instead of pure background. If it does, an
obstacle has entered the danger zone, so send a space keypress to jump.
Day/night mode is detected first by comparing white vs. grey pixel counts
in a second, larger region, since the color check flips between modes.

Status dashboard: the bot has no visual output of its own beyond the
Selenium-controlled browser window, so a small status overlay makes its
detection loop visible while it runs. `StatusTracker` holds the current
mode, obstacle flag, jump count, run time, and a rolling event log. It is
updated once per loop iteration and rendered either as a tkinter window
(if a display is available) or as periodic console prints (headless
fallback). See `build_status_view` for the selection logic.

Demo mode (`--demo`): there is no live Chrome/display actually playing the
game available in every environment this runs in (a fresh clone, CI, this
project's own review sandbox), so `--demo` replays a small set of
synthetic screenshots (see `tools/generate_demo_frames.py`) through the
real, unmodified `check_day` / `check_obstacle` functions instead of a
Selenium browser and `ImageGrab.grab()` against a real screen. It works by
swapping out `ImageGrab.grab` for a stand-in that crops the current demo
frame instead of the screen; `check_day`/`check_obstacle` themselves never
know the difference, so this exercises the exact same detection code path
live mode uses, just against pre-drawn frames instead of a real game. See
`run_demo` for the loop and `DemoTkStatusView`/`DemoConsoleStatusView` for
the extra per-frame preview.
"""

import argparse
import os
import time
from collections import deque
from datetime import datetime

import keyboard
from PIL import Image, ImageDraw, ImageGrab
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.keys import Keys

# Screen regions are in absolute display pixels, tuned for a maximized
# Chrome window on a specific screen resolution. They will need to be
# re-measured for any other resolution or window layout.
OBSTACLE_AREA_COORDINATES = (380, 645, 950, 760)
GAME_AREA_COORDINATES = (20, 580, 800, 760)

DAY_GROUND_COLOR = (255, 255, 255)
NIGHT_GROUND_COLOR = (83, 83, 83)

# How many recent detection events the status view keeps in its history
# panel/log.
STATUS_LOG_SIZE = 8

# Minimum seconds between console status re-prints in headless mode, so the
# fallback doesn't spam a line on every ~10ms loop iteration.
CONSOLE_STATUS_INTERVAL = 1.0

# --demo mode: directory of synthetic PNG frames (see
# tools/generate_demo_frames.py) and the seconds to pause between them so a
# human watching the dashboard can follow along, plus the colors drawn on
# the annotated preview showing exactly what each frame sampled.
DEFAULT_DEMO_FRAME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_frames")
DEFAULT_DEMO_DELAY_SECONDS = 1.0

GAME_AREA_BOX_COLOR = (0, 120, 255)
OBSTACLE_BOX_COLOR_CLEAR = (0, 170, 0)
OBSTACLE_BOX_COLOR_DETECTED = (220, 30, 30)


def _color_distance(pixel, target):
    """Max per-channel absolute difference between an RGB pixel and a target.

    Used as a cheap, easy-to-reason-about alternative to exact tuple
    equality: a pixel "matches" a target color if every channel is within
    `tolerance` of it, instead of requiring a bit-for-bit identical RGB
    triple. See `tools/compare_detection_robustness.py` for why this exists
    and the before/after numbers backing the default tolerance value.
    """
    return max(abs(pixel[i] - target[i]) for i in range(3))


def check_day(tolerance=0):
    """Return True if the game is currently in day mode (light background).

    `tolerance` (default 0) reproduces the original exact-color-match
    behavior bit for bit. A `tolerance > 0` instead counts pixels within
    that per-channel distance of `DAY_GROUND_COLOR`/`NIGHT_GROUND_COLOR`,
    which tolerates the kind of off-by-a-few-values color drift real screen
    captures can have (anti-aliasing, dithering, lossy compression) that a
    synthetic solid-color frame never does. Off by default so live-mode
    behavior is unchanged unless a caller opts in.
    """
    image = ImageGrab.grab(bbox=GAME_AREA_COORDINATES)
    pixels = list(image.getdata())
    if tolerance <= 0:
        day_count = pixels.count(DAY_GROUND_COLOR)
        night_count = pixels.count(NIGHT_GROUND_COLOR)
    else:
        day_count = sum(1 for p in pixels if _color_distance(p, DAY_GROUND_COLOR) <= tolerance)
        night_count = sum(1 for p in pixels if _color_distance(p, NIGHT_GROUND_COLOR) <= tolerance)
    return day_count > night_count


def check_obstacle(is_day=None, tolerance=0):
    """Return True if an obstacle pixel is present in the jump-trigger zone.

    `is_day` is an optional precomputed result of `check_day()`. It exists so
    callers that already need the day/night mode for their own purposes (e.g.
    the status dashboard) can pass it in and avoid a second screen grab. If
    omitted, this calls `check_day()` itself, exactly as before.

    `tolerance` (default 0) reproduces the original exact-color-match
    behavior bit for bit: an obstacle is present if the exact target color
    tuple appears anywhere in the region. `tolerance > 0` instead treats any
    pixel within that per-channel distance of the target as a match; see
    `check_day` and `tools/compare_detection_robustness.py`.
    """
    image = ImageGrab.grab(bbox=OBSTACLE_AREA_COORDINATES)
    pixels = list(image.getdata())
    if is_day is None:
        is_day = check_day(tolerance=tolerance)
    target = NIGHT_GROUND_COLOR if is_day else DAY_GROUND_COLOR
    if tolerance <= 0:
        return target in pixels
    return any(_color_distance(p, target) <= tolerance for p in pixels)


class StatusTracker:
    """Holds the bot's current decision-making state for display.

    This is pure, display-independent bookkeeping: `record` is called once
    per loop iteration with the raw detection results, and it updates mode,
    obstacle flag, jump count, and a rolling event log. Any view (tkinter,
    console, or none) just reads the fields off this object, so the state
    logic can be unit-tested without a display or a browser.
    """

    def __init__(self, log_size=STATUS_LOG_SIZE, clock=time.monotonic):
        self._clock = clock
        self.start_time = self._clock()
        self.is_day = None
        self.obstacle_detected = False
        self.jump_count = 0
        self.log = deque(maxlen=log_size)

    def elapsed_seconds(self):
        return self._clock() - self.start_time

    def record(self, is_day, obstacle_detected, jumped, timestamp=None):
        """Update state from one loop iteration's detection results.

        `is_day` / `obstacle_detected` are the raw results of `check_day` /
        `check_obstacle`. `jumped` is True if this iteration sent a jump
        keypress. Returns the log entry appended, mainly for testability.
        """
        mode_changed = self.is_day is not None and is_day != self.is_day
        self.is_day = is_day
        self.obstacle_detected = obstacle_detected

        if jumped:
            self.jump_count += 1

        if timestamp is None:
            timestamp = datetime.now().strftime("%H:%M:%S")

        event = "jump" if jumped else ("obstacle" if obstacle_detected else "clear")
        if mode_changed:
            event += " (mode flip)"

        entry = {
            "time": timestamp,
            "mode": "day" if is_day else "night",
            "event": event,
        }
        self.log.append(entry)
        return entry

    def snapshot(self):
        """Return a plain dict of the current display-relevant state."""
        return {
            "mode": "day" if self.is_day else ("night" if self.is_day is False else "unknown"),
            "obstacle_detected": self.obstacle_detected,
            "jump_count": self.jump_count,
            "elapsed_seconds": self.elapsed_seconds(),
            "log": list(self.log),
        }


def format_elapsed(seconds):
    """Format a seconds count as MM:SS for display."""
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class ConsoleStatusView:
    """Headless fallback: prints a status line on a throttled interval.

    Used whenever tkinter can't create a window (no display, e.g. a CI
    container), so the bot still surfaces its decision state instead of
    running silently or crashing.
    """

    def __init__(self, interval=CONSOLE_STATUS_INTERVAL, clock=time.monotonic):
        self._interval = interval
        self._clock = clock
        self._last_print = 0.0

    def update(self, tracker):
        now = self._clock()
        if now - self._last_print < self._interval:
            return
        self._last_print = now
        snap = tracker.snapshot()
        obstacle = "yes" if snap["obstacle_detected"] else "no"
        print(
            f"[status] mode={snap['mode']:<6} obstacle={obstacle:<3} "
            f"jumps={snap['jump_count']} elapsed={format_elapsed(snap['elapsed_seconds'])}"
        )

    def close(self):
        pass


class TkStatusView:
    """Non-blocking tkinter status dashboard.

    Runs on the main thread alongside the Selenium polling loop. It never
    calls `mainloop()` (which would block); instead the caller calls
    `update()` once per loop iteration, which refreshes the labels/log and
    calls `root.update_idletasks()` plus `root.update()` to pump pending
    tkinter events. This keeps the window responsive without handing
    control away from the detection loop.
    """

    def __init__(self):
        import tkinter as tk

        self._tk = tk
        self.root = tk.Tk()
        self.root.title("Dino Bot Status")
        self.root.geometry("320x260")
        self.root.resizable(False, False)

        self.mode_var = tk.StringVar(value="Mode: unknown")
        self.obstacle_var = tk.StringVar(value="Obstacle: no")
        self.jump_var = tk.StringVar(value="Jumps: 0")
        self.time_var = tk.StringVar(value="Elapsed: 00:00")

        pad = {"anchor": "w", "padx": 10, "pady": 2}
        tk.Label(self.root, textvariable=self.mode_var, font=("TkDefaultFont", 12, "bold")).pack(**pad)
        tk.Label(self.root, textvariable=self.obstacle_var, font=("TkDefaultFont", 12)).pack(**pad)
        tk.Label(self.root, textvariable=self.jump_var, font=("TkDefaultFont", 12)).pack(**pad)
        tk.Label(self.root, textvariable=self.time_var, font=("TkDefaultFont", 12)).pack(**pad)

        tk.Label(self.root, text="Recent events:", anchor="w").pack(fill="x", padx=10, pady=(8, 0))
        self.log_box = tk.Listbox(self.root, height=STATUS_LOG_SIZE)
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self._closed = False
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        self._closed = True

    @property
    def closed(self):
        return self._closed

    def update(self, tracker):
        if self._closed:
            return
        snap = tracker.snapshot()
        self.mode_var.set(f"Mode: {snap['mode']}")
        self.obstacle_var.set(f"Obstacle: {'yes' if snap['obstacle_detected'] else 'no'}")
        self.jump_var.set(f"Jumps: {snap['jump_count']}")
        self.time_var.set(f"Elapsed: {format_elapsed(snap['elapsed_seconds'])}")

        self.log_box.delete(0, self._tk.END)
        for entry in snap["log"]:
            self.log_box.insert(self._tk.END, f"{entry['time']}  {entry['mode']:<5} {entry['event']}")

        # Pump pending tkinter events without entering mainloop, so this
        # call returns immediately and never blocks the polling loop.
        self.root.update_idletasks()
        self.root.update()

    def close(self):
        if not self._closed:
            try:
                self.root.destroy()
            except Exception:
                pass
            self._closed = True


class DemoTkStatusView(TkStatusView):
    """TkStatusView plus an annotated frame preview panel, for `--demo` only.

    Subclasses rather than modifies `TkStatusView`, so live mode (which
    always constructs a plain `TkStatusView`) is completely unaffected by
    this. Adds one extra widget, an image panel showing the current demo
    frame with the two regions `check_day`/`check_obstacle` actually sample
    drawn on top: blue for `GAME_AREA_COORDINATES` (used by `check_day`),
    green or red for `OBSTACLE_AREA_COORDINATES` (used by `check_obstacle`;
    red when that frame's `check_obstacle` call returned True).
    """

    def __init__(self):
        super().__init__()
        from PIL import ImageTk

        self._image_tk_module = ImageTk
        self.root.title("Dino Bot Status (demo mode)")
        self.root.geometry("420x600")

        self.frame_label = self._tk.Label(self.root)
        self.frame_label.pack(padx=10, pady=(4, 10))
        self._photo = None  # kept as an attribute so Tk doesn't garbage-collect it

    def update(self, tracker, frame_image=None):
        if frame_image is not None and not self.closed:
            preview = frame_image.copy()
            preview.thumbnail((380, 260))
            self._photo = self._image_tk_module.PhotoImage(preview)
            self.frame_label.configure(image=self._photo)
        super().update(tracker)


class DemoConsoleStatusView(ConsoleStatusView):
    """Headless fallback for `--demo` mode.

    Same throttled status line as `ConsoleStatusView`; the per-frame
    filename, sampled regions, and raw detection result are printed
    directly by `run_demo` instead, since there is no window here to draw
    an annotated preview into.
    """

    def update(self, tracker, frame_image=None):
        super().update(tracker)


def build_status_view(demo=False):
    """Return a status view, preferring tkinter and falling back to console.

    Any failure to construct the tkinter window (no DISPLAY, Tk not
    installed, headless CI, etc.) is caught here so the bot degrades to
    printing status lines instead of crashing. `demo=True` (used only by
    `--demo`) selects the variants that also display the annotated frame
    preview; the default `demo=False` path used by live mode is unchanged.
    """
    tk_view_cls = DemoTkStatusView if demo else TkStatusView
    console_view_cls = DemoConsoleStatusView if demo else ConsoleStatusView
    try:
        return tk_view_cls()
    except Exception as exc:
        print(f"[status] no display available ({exc}); using console status output")
        return console_view_cls()


def load_demo_frames(frame_dir):
    """Load the synthetic PNG frames written by tools/generate_demo_frames.py.

    Returns a list of `(filename, PIL.Image)` pairs, sorted by filename (the
    generator names them `01_...`, `02_...`, etc., so this replays them in
    the intended order). Raises `FileNotFoundError` with a helpful message
    if the directory is missing or empty, rather than a bare traceback.
    """
    if not os.path.isdir(frame_dir):
        raise FileNotFoundError(
            f"demo frame directory {frame_dir!r} does not exist; "
            "run `python3 tools/generate_demo_frames.py` first"
        )
    filenames = sorted(f for f in os.listdir(frame_dir) if f.lower().endswith(".png"))
    if not filenames:
        raise FileNotFoundError(
            f"no PNG frames found in {frame_dir!r}; run `python3 tools/generate_demo_frames.py` first"
        )
    return [(name, Image.open(os.path.join(frame_dir, name)).convert("RGB")) for name in filenames]


def annotate_frame(frame, obstacle_detected):
    """Return a copy of `frame` with the two sampled regions drawn on it.

    Draws `GAME_AREA_COORDINATES` (blue, what `check_day` reads) and
    `OBSTACLE_AREA_COORDINATES` (green if clear, red if this frame's
    `check_obstacle` call returned True) as rectangles, so a viewer can see
    exactly where the bot is looking and what it concluded, not just the
    resulting text status.
    """
    annotated = frame.copy()
    draw = ImageDraw.Draw(annotated)
    draw.rectangle(GAME_AREA_COORDINATES, outline=GAME_AREA_BOX_COLOR, width=4)
    obstacle_color = OBSTACLE_BOX_COLOR_DETECTED if obstacle_detected else OBSTACLE_BOX_COLOR_CLEAR
    draw.rectangle(OBSTACLE_AREA_COORDINATES, outline=obstacle_color, width=4)
    return annotated


def run_demo(frame_dir=DEFAULT_DEMO_FRAME_DIR, delay=DEFAULT_DEMO_DELAY_SECONDS):
    """Replay synthetic frames through the real detection functions.

    No browser and no screen capture: for each frame, `ImageGrab.grab` (the
    exact function `check_day`/`check_obstacle` call) is swapped out for a
    stand-in that crops the current frame instead of grabbing the screen,
    the frame is run through those two unmodified functions, and the
    decision is fed to the same `StatusTracker`/status-view machinery live
    mode uses, plus an annotated preview of the frame. `ImageGrab.grab` is
    restored in a `finally` block so this can't leave the module patched
    for anyone importing it afterward.
    """
    frames = load_demo_frames(frame_dir)
    tracker = StatusTracker()
    status_view = build_status_view(demo=True)

    print(f"[demo] replaying {len(frames)} frame(s) from {frame_dir}")

    original_grab = ImageGrab.grab
    try:
        for filename, frame in frames:
            ImageGrab.grab = lambda bbox=None, _frame=frame: _frame.crop(bbox)

            is_day = check_day()
            obstacle = check_obstacle(is_day)
            jumped = obstacle  # no real browser to jump in; obstacle == "would jump"

            tracker.record(is_day, obstacle, jumped)
            annotated = annotate_frame(frame, obstacle)
            status_view.update(tracker, frame_image=annotated)

            print(
                f"[demo] {filename}: mode={'day' if is_day else 'night'} "
                f"obstacle={obstacle} jump={jumped}"
            )

            if getattr(status_view, "closed", False):
                break

            time.sleep(delay)
    finally:
        ImageGrab.grab = original_grab
        status_view.close()


def build_driver():
    """Create a Chrome WebDriver instance.

    CHROMEDRIVER_PATH is optional: Selenium 4.6+ can locate a matching
    chromedriver automatically via Selenium Manager. Set the env var only
    if you need to pin a specific driver binary.
    """
    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
    if chromedriver_path:
        from selenium.webdriver.chrome.service import Service

        return webdriver.Chrome(service=Service(chromedriver_path))
    return webdriver.Chrome()


def main():
    driver = build_driver()
    tracker = StatusTracker()
    status_view = build_status_view()

    try:
        driver.get("chrome://dino/")
    except WebDriverException:
        # chrome://dino/ throws in some driver versions even though the
        # page loads correctly; safe to ignore.
        pass
    driver.maximize_window()

    time.sleep(2)
    # Press space once to start the game.
    driver.find_element(value="t").send_keys(Keys.SPACE)
    print("key sent")

    try:
        while True:
            is_day = check_day()
            obstacle = check_obstacle(is_day)
            jumped = False

            if obstacle:
                driver.find_element(value="t").send_keys(Keys.SPACE)
                print("obstacle")
                jumped = True
                time.sleep(0.01)

            tracker.record(is_day, obstacle, jumped)
            status_view.update(tracker)

            if getattr(status_view, "closed", False):
                break

            if keyboard.is_pressed("q"):
                break
    finally:
        status_view.close()
        driver.quit()


def parse_args():
    parser = argparse.ArgumentParser(description="Chrome Dino game bot")
    parser.add_argument(
        "--demo",
        action="store_true",
        help=(
            "Replay synthetic frames from --demo-dir through the real "
            "check_day/check_obstacle functions instead of driving a live "
            "browser. Use tools/generate_demo_frames.py to (re)generate them."
        ),
    )
    parser.add_argument(
        "--demo-dir",
        default=DEFAULT_DEMO_FRAME_DIR,
        help=f"Directory of PNG frames for --demo (default: {DEFAULT_DEMO_FRAME_DIR})",
    )
    parser.add_argument(
        "--demo-delay",
        type=float,
        default=DEFAULT_DEMO_DELAY_SECONDS,
        help=f"Seconds to pause between frames in --demo mode (default: {DEFAULT_DEMO_DELAY_SECONDS})",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.demo:
        run_demo(frame_dir=args.demo_dir, delay=args.demo_delay)
    else:
        main()
