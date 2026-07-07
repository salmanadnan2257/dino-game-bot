# Dino Game Bot

A script that plays Chrome's offline Dino game (`chrome://dino/`) by watching the
screen and pressing space at the right moment.

## Why it exists

The Dino game only has one input (jump) and a simple visual language: obstacles
approach from the right, and hitting one ends the run. That makes it a small,
self-contained target for practicing screen-based automation: no game API to call,
no memory to read, just pixels on screen and a keyboard event. It's a way to
exercise Selenium (for driving Chrome) alongside raw screen capture and pixel
inspection (for "seeing" the game), rather than solving everything through one
library.

## Features

- Launches Chrome and opens the built-in offline Dino game directly via
  `chrome://dino/`.
- Starts the game automatically by sending a space keypress.
- Watches a fixed screen region in a loop and jumps when it detects an obstacle.
- Handles both day and night color schemes (the game inverts its palette
  periodically).
- Stops cleanly when the `q` key is pressed, and quits the browser in a `finally`
  block so a stray exception doesn't leave a Chrome process running.
- Shows a live status dashboard (day/night mode, obstacle flag, jump count,
  run time, and a rolling event log) alongside the Selenium-driven browser
  window, so the bot's detection loop is visible while it runs instead of
  being a black box.

## Architecture

The bot never touches game state or the DOM to find obstacles; it only reads
pixels, which is why it needs the browser window in a fixed, predictable
position.

1. **Day/night detection (`check_day`).** Grabs a fairly large screen region
   (`GAME_AREA_COORDINATES`) covering ground and sky, and counts how many pixels
   are pure white `(255, 255, 255)` vs. dark grey `(83, 83, 83)`. Day mode has a
   white background, night mode has a dark one, so whichever pixel value
   dominates tells you the current mode.
2. **Obstacle detection (`check_obstacle`).** Grabs a narrow, closer region
   (`OBSTACLE_AREA_COORDINATES`) positioned just ahead of the dino, in its jump
   trigger zone. In day mode, cactus/bird sprites render dark grey against a
   white background, so the presence of *any* `(83, 83, 83)` pixel in that
   region means an obstacle has entered it. In night mode the colors invert, so
   it checks for white pixels instead. This is why `check_day` has to run first:
   the "obstacle color" flips depending on the mode.
3. **Decision loop (`main`).** On every iteration, if `check_obstacle()` returns
   `True`, the loop immediately sends a space keypress to jump, then sleeps
   10ms before checking again. There's no distance or speed estimate here: the
   obstacle region is placed close enough to the dino that "obstacle present in
   that box" and "jump now" are treated as the same event.

Both coordinate tuples are absolute screen pixel boxes `(left, top, right,
bottom)`, tuned for one specific window size and screen resolution (a maximized
Chrome window on the original author's display). They are not resolution-aware
and will need to be re-measured with a screenshot tool on any other setup.

## Status dashboard

The bot has no visual output of its own beyond the Selenium-controlled browser
window, so a small dashboard makes the detection loop visible while it runs.

`StatusTracker` is a plain, display-independent object that gets one update per
loop iteration (`record(is_day, obstacle_detected, jumped)`) and keeps:

- the current mode (day/night),
- whether an obstacle is currently detected,
- a running jump count,
- elapsed run time, and
- a rolling log of the last 8 detection events, each with a timestamp, the
  mode at that moment, and what happened (`clear`, `obstacle`, `jump`, flagged
  with `(mode flip)` when day/night just changed).

`build_status_view()` picks a renderer for that state:

- **tkinter window (default, when a display is available).** A small always-on
  top-left window with four live labels (mode, obstacle, jumps, elapsed) and a
  listbox showing the rolling event log. It never calls `mainloop()`, which
  would block the polling loop; instead `main()` calls `view.update(tracker)`
  once per iteration, which refreshes the widgets and pumps pending tkinter
  events with `root.update_idletasks()` + `root.update()`, then returns
  immediately. Closing the window (its `X` button) sets a flag the main loop
  checks on the next iteration and exits cleanly.
- **Console fallback (headless).** If constructing the tkinter window raises
  anything (no `DISPLAY`, Tk not installed, running in a CI container), the
  bot catches it and falls back to `ConsoleStatusView`, which prints a
  throttled one-line status (`mode`, `obstacle`, `jumps`, `elapsed`) to stdout
  at most once a second instead of on every ~10ms loop iteration. The bot
  keeps running and jumping either way; only the presentation layer changes.

`check_obstacle` takes an optional `is_day` argument so the main loop can pass
in the mode it already computed for the dashboard, instead of paying for a
second screen grab (`check_day` grabs a region and counts pixels, which isn't
free). Calling `check_obstacle()` with no arguments still works exactly as
before and computes the mode itself, which is what the detection-logic tests
exercise.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate   # venv\Scripts\activate on Windows
pip install -r requirements.txt
```

Chrome must be installed. Selenium 4.6+ includes Selenium Manager, which
downloads and locates a matching chromedriver automatically, so no manual driver
setup is required in the common case. If you need to pin a specific driver
binary, copy `.env.example` to `.env` and set `CHROMEDRIVER_PATH`.

On Linux, the `keyboard` module's global hotkey detection (used here for the `q`
quit key) needs root access to read the keyboard device directly
(commonly run with `sudo`), because it hooks input at the OS level rather than
through the browser.

## Usage

```bash
python3 main.py
```

Chrome opens `chrome://dino/`, maximizes, and starts the game. The script then
watches the screen and jumps automatically. A small status window opens
alongside it showing the current mode, obstacle flag, jump count, elapsed
time, and a rolling event log; if no display is available it prints the same
information to the console instead (see "Status dashboard" above). Press `q`
to stop the bot (this does not close Chrome for you beyond the driver's own
`quit()` on exit), or close the status window, which also stops the loop.

Before relying on it, take a screenshot of your own maximized dino game window
and re-measure `OBSTACLE_AREA_COORDINATES` and `GAME_AREA_COORDINATES` in
`main.py`: they're pinned to one screen resolution and window layout, and will
misfire (or never trigger) on a different setup.

## Challenges

- **Coordinates are hardcoded to one screen setup.** `OBSTACLE_AREA_COORDINATES`
  and `GAME_AREA_COORDINATES` are absolute pixel boxes measured against a
  specific display resolution and window position. There's no logic to derive
  them from the actual browser window geometry, so the bot is only correct on
  the exact setup it was tuned for.
- **Day/night color inversion.** The Dino game periodically swaps its palette
  from light to dark. A single fixed color check for "is there an obstacle"
  would break every time the mode flipped, so `check_day` runs first on every
  obstacle check to pick the right target color, at the cost of doing two
  screen grabs per loop iteration instead of one.
- **`chrome://dino/` throwing on load.** Some ChromeDriver versions raise a
  `WebDriverException` when navigating directly to `chrome://dino/`, even
  though the page actually loads fine. The original code wraps the `driver.get`
  call in a try/except that swallows exactly that exception rather than the
  navigation failing the whole run.
- **Reaction time vs. polling cost.** The loop sleeps only 10ms after a
  detected jump, trading CPU usage for faster reaction time, since screen
  capture and pixel counting on every iteration isn't free.
- **No distance estimate, just proximity.** The obstacle box is deliberately
  narrow and close to the dino rather than wide enough to estimate an
  obstacle's distance or speed. That keeps the logic simple (binary presence
  check) but means there's no margin to tune jump timing, e.g. for taller
  obstacles or double cacti that need a different jump duration.
- **Driver path portability.** The original script hardcoded a Windows-only
  chromedriver path (`C:\Development\chromedriver.exe`). That's fixed here by
  using Selenium Manager as the default and making the driver path an optional
  environment variable instead.

## What I learned

- Screen-region pixel counting is a viable, cheap way to detect simple visual
  events (obstacle present or not) without needing computer vision libraries,
  as long as the visual target has a small, known set of colors.
- Conditions that look constant (like "obstacles are dark pixels") can flip
  based on game state (day/night mode here), so it's worth checking for that
  kind of hidden mode switch before assuming a single detection rule covers a
  full game.
- Keeping side effects (launching a browser, running an infinite loop) out of
  module import time makes a script far easier to test in isolation: with the
  original top-level code, simply importing the file would launch Chrome.

## What I'd do differently

- Compute the obstacle and game-area regions from the actual browser window's
  reported position and size instead of hardcoding absolute screen pixels, so
  the bot survives different resolutions and window placements.
- Replace the raw pixel-color heuristic with a small template match or a crop
  diff against a "clear track" reference image, which would hold up better against
  anti-aliasing and minor color variance than exact RGB tuple matching.
- Add a basic distance/speed estimate (e.g. scanning a wider strip and finding
  the nearest obstacle edge) so jump timing can adapt to obstacle type instead
  of relying on a single fixed trigger zone.
- Save an actual screen capture on request (not just the mode/obstacle/jump
  counters the current dashboard shows) to make it easier to debug false
  positives/negatives directly against what the bot saw.

## What was verified vs. not

Verified in this environment (a display was available, but no Chrome/live
game):

- The module imports cleanly with no import-time side effects (browser launch
  no longer happens on import; it's gated behind `if __name__ == "__main__":`).
- `check_day` and `check_obstacle` were smoke-tested in isolation by
  monkeypatching `ImageGrab.grab` to return synthetic day/night images built
  in-memory, confirming the color-count and membership logic behaves as
  described above, including the optional `is_day` argument on
  `check_obstacle` used to skip a redundant screen grab.
- `StatusTracker` was driven through a scripted sequence of synthetic
  detection events (day, obstacle, jump, mode flip, log rollover) and its
  snapshot state (mode, obstacle flag, jump count, elapsed time, event log)
  was asserted at every step.
- `TkStatusView` was smoke-tested against a live X display: it constructs
  without exceptions, `update()` refreshes its labels and log from a
  `StatusTracker` across several simulated iterations without blocking, and
  `close()` tears the window down cleanly.
- The headless fallback was verified by unsetting `DISPLAY` and confirming
  `build_status_view()` catches the resulting Tk error and returns a
  `ConsoleStatusView` instead of raising, which then prints throttled status
  lines to stdout.
- `requirements.txt` installs cleanly into a fresh virtual environment.

Not verified here, and needs a real machine with Chrome and a live game
window:

- Actually launching Chrome, navigating to `chrome://dino/`, and confirming the
  game starts and responds to the jump keypress.
- Whether `OBSTACLE_AREA_COORDINATES` and `GAME_AREA_COORDINATES` still match a
  real dino game window; they are very likely wrong for any screen other than
  the one they were originally measured on and need re-measuring before use.
- End-to-end play: whether the bot actually clears obstacles reliably over a
  real run.
- Whether the dashboard update inside the polling loop introduces any visible
  reaction-time lag once it's running against a real, fast-moving game
  (`view.update()` was only measured against synthetic events, not a live
  ~10ms detection loop).
