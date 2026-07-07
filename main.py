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
"""

import os
import time
from collections import deque
from datetime import datetime

import keyboard
from PIL import ImageGrab
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


def check_day():
    """Return True if the game is currently in day mode (light background)."""
    image = ImageGrab.grab(bbox=GAME_AREA_COORDINATES)
    pixels = list(image.getdata())
    return pixels.count(DAY_GROUND_COLOR) > pixels.count(NIGHT_GROUND_COLOR)


def check_obstacle(is_day=None):
    """Return True if an obstacle pixel is present in the jump-trigger zone.

    `is_day` is an optional precomputed result of `check_day()`. It exists so
    callers that already need the day/night mode for their own purposes (e.g.
    the status dashboard) can pass it in and avoid a second screen grab. If
    omitted, this calls `check_day()` itself, exactly as before.
    """
    image = ImageGrab.grab(bbox=OBSTACLE_AREA_COORDINATES)
    pixels = list(image.getdata())
    if is_day is None:
        is_day = check_day()
    if is_day:
        return NIGHT_GROUND_COLOR in pixels
    return DAY_GROUND_COLOR in pixels


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


def build_status_view():
    """Return a status view, preferring tkinter and falling back to console.

    Any failure to construct the tkinter window (no DISPLAY, Tk not
    installed, headless CI, etc.) is caught here so the bot degrades to
    printing status lines instead of crashing.
    """
    try:
        return TkStatusView()
    except Exception as exc:
        print(f"[status] no display available ({exc}); using console status output")
        return ConsoleStatusView()


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


if __name__ == "__main__":
    main()
