#!/usr/bin/env python3
"""Freg — a little desktop helper frog that lives in the corner and talks to Claude.

Click Freg to ask something, drag Freg to move, right-click for a menu.
Replies appear as speech bubbles, one paragraph per bubble — click to advance.
Every conversation is a real agent session (Claude Code or pi, picked with FREG_AGENT
or the Agent menu), so "Continue in terminal" reopens it in that agent.
"""
import ctypes
import glob
import json
import math
import os
import random
import re
import shutil
import subprocess
import textwrap
import threading
import time
import uuid

# GNOME on Wayland doesn't let apps position their own windows or stay on top;
# running through XWayland does. Must be set before GTK is imported.
os.environ.setdefault("GDK_BACKEND", "x11")

import cairo  # noqa: E402
import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

WORKDIR = os.path.expanduser(os.environ.get("FREG_WORKDIR", "~"))
AGENT = os.environ.get("FREG_AGENT", "claude")  # "claude" or "pi"; switchable from the menu
MODEL = os.environ.get("FREG_MODEL")  # e.g. "sonnet"; default = the agent's own default
MARGIN = 16
HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
SPRITE_H = 110  # on-screen height of the frog; frog.png/blink.png are scaled to this
MASCOT_W, MASCOT_H = 130, SPRITE_H + 30  # wide enough for napping Freg; room for hops and dots
ANTIC_MINUTES = (2, 5)  # how often Freg considers an idle antic (nap, dance, walk, …)
ANTIC_IDLE_SECONDS = 60  # only once nobody's touched him for this long
NAP_SECONDS = (60, 180)  # how long a nap lasts if nobody wakes him
NAP_FRAME_SECONDS = 0.9
DANCE_SECONDS = (5, 9)  # how long a dance lasts
DANCE_BEAT = 0.4  # seconds per step: swap frog.png/arms.png and lean the other way
DANCE_LEAN = 0.14  # radians (~8°)
WAVE_CYCLES = (3, 4)  # how many times he goes wave_1 → wave_2 in one wave
WAVE_BEAT = 0.28  # seconds on each wave frame
FISH_WAIT_SECONDS = (6, 15)  # how long Freg waits for a bite
FISH_STARE = (1.5, 3.5)  # seconds on fishing1 (eyes open) between blinks while he waits
FISH_BLINK = 0.2  # seconds on fishing2, where his eyes are closed
FISH_FRAMES = ["side_neutral", "fishing1", "fishing2", "fish_caught",
               "fish_pull_frame1", "fish_pull_frame2", "fish_pull_frame3", "fish_celebrate"]
# Spots in the art itself (unscaled pixels): where the line ends, and Freg's raised left hand.
FISH_LINE_ENDS = {"fish_pull_frame2": (11, 127), "fish_pull_frame3": (5, 61)}
FISH_HAND = (153, 90)
# Walking art for each way he can face; the side art faces left and is mirrored for right.
WALK_ART = {
    "left": ["side_walk1", "side_walk2", "side_walk1", "side_walk3"],
    "front": ["walk_front", "walk_front1"],  # toward you, i.e. down the screen
    "back": ["walk_back", "walk_back2"],  # away from you, i.e. up the screen
}
WALK_STAND = {"left": "walk_side_neutral", "front": "frog", "back": "walk_back"}
WALK_FRAME_SECONDS = {"left": 0.16, "right": 0.16, "front": 0.22, "back": 0.22}
WALK_SPEED = 70  # pixels per second
WALK_LEGS = (3, 7)  # how many stretches a walk has; he picks a new direction for each
WALK_MIN_LEG = 60  # shortest stretch, in pixels, so a walk is never just a shuffle
WALK_MAX_LEG = 450  # longest stretch
WALK_PAUSE = (0.3, 1.2)  # seconds he stands and looks around before changing direction
KICKS = (3, 6)  # how many times Freg kicks the ball in one game
KICK_READY = 0.5  # seconds he lines up with the ball before winding up
KICK_WINDUP = 0.35  # seconds on kick1 (leg back)
KICK_STRIKE = 0.35  # seconds on kick2 (leg through); the ball flies at the start of it
BALL_REACH = 30  # pixels from Freg's middle to the ball's centre when it's at his feet
BALL_SPEED = (260, 520)  # how hard he kicks, in pixels per second
BALL_FRICTION = 260  # how quickly the rolling ball slows, in pixels per second squared
BALL_POP = (90, 200)  # how high a kick lifts the ball (initial upward speed, px/s)
BALL_GRAVITY = 900
BALL_HOP_ROOM = 40  # space above the ball in its window for it to bounce into
WATER_STOPS = (3, 6)  # how many spots Freg waters (and flowers he grows) on one gardening walk
WATER_POUR_BEATS = 8  # how long he pours: this many water2/water3 swaps
WATER_REACH = 71  # where the water lands: art pixels in front of his head in water3
FLOWER_GROW = (1.2, 1.4)  # seconds as flower_1, then flower_2, before it's fully grown
FLOWER_SECONDS = 15  # how long a flower stays before it fades away
FLOWER_FADE = 2.0
MAX_FLOWERS = 12  # the oldest fades early when he plants more than this
# Where Freg holds the watering can by its top handle while he walks, in art pixels from his
# head (x) and feet (y); the side art faces left and is mirrored for right. Seen from behind,
# the can's drawn behind him.
CAN_HANDS = {"left": (-20, -122), "front": (-33, -126), "back": (33, -126)}
CAN_GRIP = (52, 2)  # the top of the can's handle, in watering_can.png pixels
BUBBLE_W = 300  # widest a speech bubble gets
SPEECH_H = 270  # tallest a speech bubble gets (long paragraphs are split into pages)
ASK_H = 64  # the ask box plus its tail
WINDOW_H = SPEECH_H + 14 + ASK_H + MASCOT_H  # fixed: room for both bubbles above Freg
PAGE_CHARS = 240  # split paragraphs longer than this into several bubbles
DEBUG = bool(os.environ.get("FREG_DEBUG"))
# Where pi might be installed, tried in order; autostarted apps often lack these on PATH.
PI_LOCATIONS = [
    shutil.which("pi") or "",
    "~/.local/bin/pi",
    "~/.nvm/versions/node/*/bin/pi",
    "~/.npm-global/bin/pi",
    "~/.local/bin/node/pi",
    "/usr/local/bin/pi",
    "/usr/bin/pi",
]


def debug(*args):
    if DEBUG:
        print("[freg]", *args, flush=True)

WAKE_STARTLED = [
    "I wasn't sleeping! I was… debugging with my eyes closed.",
    "Huh?! I'm up! I'm up!",
    "Just resting my eyes. Ribbit.",
    "Five more minutes…",
    "Was I snoring? Frogs don't snore. Ribbit.",
    "I was dreaming about flies. Big ones.",
]
WAKE_NATURAL = [
    "*yaaawn* …where was I?",
    "Ahh, a power nap. Hop to it!",
    "Refreshed and ready to leap!",
    "Mmm, lily pad dreams.",
]

QUIP_MINUTES = (3, 8)  # Freg pipes up at a random interval in this range
QUIPS = [
    "Ribbit!",
    "Ribbit ribbit.",
    "*croak*",
    "Hello there",
    "It looks like you are trying to avoid an existential crisis. Can I help with that?",
    "I see you're trying to focus on something without interruption. Can I help?",
    "Hydration check! Frogs recommend water.",
    "Sit up straight! Your spine will thank you.",
    "Don't froget to take breaks!",
    "Don't you froget about me",
    "What do frog's like to drink? hot croako",
    "Close a tab or two. You've got this.",
    "Progress beats perfection. Ribbit.",
    "Commit early, commit often!",
    "You're doing great. Ribbit!",
    "sorry i've got a little brain frog",
    # frog puns
    "You're toad-ally crushing today.",
    "That code looks ribbiting. Ship it!",
    "Hop around in my pond ;)",
    "Make it a great day",
    "Why are frogs so happy? They eat whatever bugs them.",
    "Debugging is just bug-eating with extra steps.",
    "What goes dot-dot-croak dash-croak-dot? Morse toad",
    "Hocus Croakus. You will now focus"
]

SYSTEM_PROMPT = (
    "You are Freg, a friendly little frog who lives in the corner of the user's "
    "Linux (GNOME) desktop. Your replies are shown as cartoon speech bubbles, one "
    "paragraph per bubble, so keep them short and conversational: usually one to "
    "three short paragraphs of plain text. Don't use markdown (no headings, bold, "
    "bullet lists or code fences). If a task needs long output, code, or lots of "
    "hands-on work, suggest the user right-click you and choose 'Continue in "
    "terminal' to carry on this conversation there."
)

CSS = """
window.freg { background-color: transparent; }
.speech, .ask {
  background-color: #24242E;
  border: 1px solid rgba(255, 255, 255, 0.10);
  border-radius: 16px;
}
.speech { padding: 9px 13px 7px 13px; }
.speech label.text { font-family: "@FREG_FONT@"; font-size: 1.15em; color: #E6E4F0; }
.speech label.text.err { color: #F08A80; }
.speech label.hint { color: #8C8AA6; font-size: 0.85em; }
.ask { padding: 7px; }
.ask entry {
  background-color: #1A1A22; color: #E6E4F0; caret-color: #9C9CD8;
  border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 10px;
  padding: 5px 9px; box-shadow: none;
}
.ask entry:focus { border-color: rgb(74, 74, 123); }
.ask button {
  background-image: none; background-color: rgb(74, 74, 123); color: #FFFFFF;
  border: none; border-radius: 9px; box-shadow: none; text-shadow: none;
  padding: 4px 12px;
}
.ask button:hover { background-color: rgb(92, 92, 150); }
"""

FREG_FONT = "Ldfcomicsans"


def load_freg_font():
    """Make Freg's font available to this app without installing it system-wide."""
    try:
        fc = ctypes.CDLL("libfontconfig.so.1")
        fc.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        return bool(fc.FcConfigAppFontAddFile(None, os.path.join(ASSETS, FREG_FONT + ".ttf").encode()))
    except OSError:
        return False


load_freg_font()  # must run before Pango builds its font map


def load_sprite(name):
    return GdkPixbuf.Pixbuf.new_from_file_at_scale(os.path.join(ASSETS, name), -1, SPRITE_H, True)


def load_sprite_like(name, reference="frog.png"):
    """Load at the same scale as frog.png, for art that isn't standing-frog sized."""
    _, _, ref_h = GdkPixbuf.Pixbuf.get_file_info(os.path.join(ASSETS, reference))
    _, w, h = GdkPixbuf.Pixbuf.get_file_info(os.path.join(ASSETS, name))
    scale = SPRITE_H / ref_h
    return GdkPixbuf.Pixbuf.new_from_file_at_scale(
        os.path.join(ASSETS, name), round(w * scale), round(h * scale), True)


def body_center_x(pixbuf, top=0.55, bottom=1.0):
    """Horizontal middle of the opaque art between rows top..bottom (fractions of the
    height); by default the frog's body (lower half of the image), in pixels."""
    w, h = pixbuf.get_width(), pixbuf.get_height()
    rs, n, px = pixbuf.get_rowstride(), pixbuf.get_n_channels(), pixbuf.get_pixels()
    cols = [x for x in range(w)
            if any(px[y * rs + x * n + 3] > 40 for y in range(int(h * top), int(h * bottom), 2))]
    return (cols[0] + cols[-1]) / 2 if cols else w / 2


def body_feet(pixbuf):
    """(middle x, lowest y) of the green body — rods, lines and ripples aren't green."""
    w, h = pixbuf.get_width(), pixbuf.get_height()
    rs, n, px = pixbuf.get_rowstride(), pixbuf.get_n_channels(), pixbuf.get_pixels()
    green = [(x, y) for y in range(int(h * 0.7), h) for x in range(w)
             if px[y * rs + x * n + 3] > 200
             and px[y * rs + x * n + 1] > px[y * rs + x * n] + 30
             and px[y * rs + x * n + 1] > px[y * rs + x * n + 2]]
    if not green:
        return w / 2, h
    xs = [x for x, _ in green]
    return (min(xs) + max(xs)) / 2, max(y for _, y in green)


def natural_key(path):
    """Sort key that puts frame2 before frame10 (and node v9 before v18)."""
    return [int(n) if n.isdigit() else n for n in re.split(r"(\d+)", path)]


def numbered(pattern):
    """Files matching pattern, sorted so frame2 comes before frame10."""
    return sorted(glob.glob(os.path.join(ASSETS, pattern)), key=natural_key)


def find_program(name, *fallbacks):
    """Autostarted GUI apps often lack ~/.local/bin and nvm's bin dir on PATH."""
    for path in (shutil.which(name), *fallbacks):
        if path and os.path.exists(path):
            return path
    return name


def child_env(program=None):
    env = dict(os.environ)
    env.pop("GDK_BACKEND", None)  # don't force terminals onto X11 too
    if program and os.path.dirname(program):
        # pi is a `#!/usr/bin/env node` script; nvm puts node next to it.
        env["PATH"] = os.path.dirname(program) + os.pathsep + env.get("PATH", "")
    return env


class ClaudeBackend:
    """Claude Code: `claude -p` with a stream-json event stream."""

    key, label = "claude", "Claude Code"

    def program(self):
        return find_program("claude", os.path.expanduser("~/.local/bin/claude"))

    def new_session_id(self):
        return None  # Claude picks one and reports it in the stream

    def command(self, text, session_id):
        cmd = [
            self.program(), "-p", text,
            "--output-format", "stream-json", "--verbose",
            "--include-partial-messages",
            "--append-system-prompt", SYSTEM_PROMPT,
        ]
        if MODEL:
            cmd += ["--model", MODEL]
        if session_id:
            cmd += ["--resume", session_id]
        return cmd

    def resume_command(self, session_id):
        return [self.program(), "--resume", session_id]

    def parse(self, msg, wrote_text):
        """Turn one stream line into ("session" | "text" | "tool" | "error", value) events."""
        events = []
        if msg.get("session_id"):
            events.append(("session", msg["session_id"]))
        kind = msg.get("type")
        if kind == "stream_event" and msg.get("parent_tool_use_id") is None:
            ev = msg.get("event", {})
            if ev.get("type") == "content_block_start":
                block = ev.get("content_block", {})
                if block.get("type") == "tool_use":
                    events.append(("tool", block.get("name", "a tool")))
                elif block.get("type") == "text" and wrote_text:
                    events.append(("text", "\n\n"))
            elif ev.get("type") == "content_block_delta":
                delta = ev.get("delta", {})
                if delta.get("type") == "text_delta" and delta.get("text"):
                    events.append(("text", delta["text"]))
        elif kind == "result" and msg.get("is_error"):
            events.append(("error", msg.get("result") or "Claude reported an error."))
        return events


class PiBackend:
    """pi (pi.dev): `pi -p --mode json`, one JSON event per line."""

    key, label = "pi", "pi"

    def program(self):
        for location in PI_LOCATIONS:
            # newest node version first when a pattern matches several installs
            for path in sorted(glob.glob(os.path.expanduser(location)), key=natural_key, reverse=True):
                if os.access(path, os.X_OK):
                    return path
        return "pi"

    def new_session_id(self):
        return str(uuid.uuid4())  # pi creates the session on first use of --session-id

    def command(self, text, session_id):
        cmd = [self.program(), "-p", text, "--mode", "json",
               "--append-system-prompt", SYSTEM_PROMPT, "--session-id", session_id]
        if MODEL:
            cmd += ["--model", MODEL]
        return cmd

    def resume_command(self, session_id):
        return [self.program(), "--session", session_id]

    def parse(self, msg, wrote_text):
        kind = msg.get("type")
        if kind == "message_update":
            ev = msg.get("assistantMessageEvent") or {}
            if ev.get("type") == "text_start" and wrote_text:
                return [("text", "\n\n")]
            if ev.get("type") == "text_delta" and ev.get("delta"):
                return [("text", ev["delta"])]
        elif kind == "tool_execution_start":
            return [("tool", msg.get("toolName", "a tool"))]
        elif kind == "message_end":
            # pi exits 0 even when the request failed; the error is only on the message.
            m = msg.get("message") or {}
            if m.get("role") == "assistant" and m.get("stopReason") == "error":
                return [("error", self.error_text(m.get("errorMessage")))]
        elif kind == "auto_retry_end" and msg.get("success") is False:
            return [("error", self.error_text(msg.get("finalError")))]
        return []

    @staticmethod
    def error_text(raw):
        """Provider errors arrive as e.g. '400 {"error": {"message": ...}}'; keep the message."""
        if not raw:
            return "pi reported an error."
        try:
            body = json.loads(raw[raw.index("{"):])
            return body.get("error", {}).get("message") or raw
        except (ValueError, AttributeError):
            return raw


BACKENDS = {b.key: b for b in (ClaudeBackend(), PiBackend())}


class AgentSession:
    """Runs the agent once per message, resuming the same session each time."""

    def __init__(self, backend):
        self.backend = backend
        self.session_id = None
        self.proc = None

    @property
    def busy(self):
        return self.proc is not None and self.proc.poll() is None

    def reset(self):
        self.cancel()
        self.session_id = None

    def switch(self, backend):
        self.reset()
        self.backend = backend

    def cancel(self):
        if self.busy:
            self.proc.terminate()

    def send(self, text, on_text, on_tool, on_done):
        """Callbacks are invoked on the GTK main thread."""
        if not self.session_id:
            self.session_id = self.backend.new_session_id()
        cmd = self.backend.command(text, self.session_id)
        debug("running", cmd[0], "session", self.session_id)
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=WORKDIR, env=child_env(cmd[0]), text=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except OSError as e:
            GLib.idle_add(on_done, f"Couldn't start {self.backend.label}: {e}")
            return
        threading.Thread(target=self._read,
                         args=(self.proc, self.backend, on_text, on_tool, on_done),
                         daemon=True).start()

    def _read(self, proc, backend, on_text, on_tool, on_done):
        error = None
        wrote_text = False
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            for kind, value in backend.parse(msg, wrote_text):
                if kind == "session":
                    self.session_id = value
                elif kind == "text":
                    wrote_text = True
                    GLib.idle_add(on_text, value)
                elif kind == "tool":
                    GLib.idle_add(on_tool, value)
                elif kind == "error":
                    error = value
        proc.wait()
        if proc.returncode and not error:
            err = proc.stderr.read().strip()
            error = err[-400:] if err else ("Stopped." if proc.returncode < 0
                                            else f"{backend.label} exited with code {proc.returncode}")
        GLib.idle_add(on_done, error)




def split_pages(text):
    """One speech bubble per paragraph; overly long paragraphs become several bubbles."""
    pages = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        page = ""
        for sentence in re.split(r"(?<=[.!?])\s+", para):
            if len(sentence) > PAGE_CHARS:  # one giant sentence: break it at word boundaries
                if page:
                    pages.append(page)
                *full, page = textwrap.wrap(sentence, PAGE_CHARS) or [""]
                pages.extend(full)
            elif page and len(page) + 1 + len(sentence) > PAGE_CHARS:
                pages.append(page)
                page = sentence
            else:
                page = f"{page} {sentence}" if page else sentence
        if page:
            pages.append(page)
    return pages


class Mascot(Gtk.DrawingArea):
    """Freg the frog: frog.png normally, blink.png while blinking, speaking*.png while
    talking, sleep_frame*.png while napping, side_walk*.png / walk_front*.png /
    walk_back*.png while walking."""

    def __init__(self):
        super().__init__()
        self.set_size_request(MASCOT_W, MASCOT_H)
        self.frog = load_sprite("frog.png")
        self.blink = load_sprite("blink.png")
        self.mouths = [load_sprite(os.path.basename(f)) for f in numbered("speaking*.png")] or [self.frog]
        # sleep_frame1 has the most Zs; play 3 → 2 → 1 so the snore builds up z, zz, zzz.
        self.nap_frames = [load_sprite_like(os.path.basename(f))
                           for f in reversed(numbered("sleep_frame*.png"))]
        # The body sits at the same spot in every frame; extra width is just room for the Zs.
        self.nap_body_w = min(f.get_width() for f in self.nap_frames) if self.nap_frames else 0
        self.napping = False
        self.nap_frame = 0
        self.next_nap_frame = 0.0
        self.jolt_until = 0.0
        arms = os.path.join(ASSETS, "arms.png")
        self.dance_frames = [self.frog, load_sprite("arms.png")] if os.path.exists(arms) else []
        # arms.png is wider than frog.png; line their bodies up so he doesn't slide sideways
        self.dance_centers = [body_center_x(f) for f in self.dance_frames]
        self.dance_until = 0.0
        self.dance_start = 0.0
        waves = [os.path.join(ASSETS, f"wave_{i}.png") for i in (1, 2)]
        self.wave_frames = ([load_sprite(os.path.basename(f)) for f in waves]
                            if all(os.path.exists(f) for f in waves) else [])
        # the raised arm makes the art wider on one side; line the bodies up instead
        self.wave_centers = [body_center_x(f) for f in self.wave_frames]
        self.wave_until = 0.0
        self.wave_start = 0.0
        fish_files = [os.path.join(ASSETS, n + ".png") for n in FISH_FRAMES]
        self.catches = [load_sprite_like(os.path.basename(f)) for f in numbered("fish[0-9]*.png")]
        if self.catches and all(os.path.exists(f) for f in fish_files):
            self.fish_sprites = {n: load_sprite_like(n + ".png") for n in FISH_FRAMES}
        else:
            self.fish_sprites = {}
        # line every frame up on the body's feet so he doesn't slide around as the art changes size
        self.fish_feet = {n: body_feet(f) for n, f in self.fish_sprites.items()}
        self.frog_feet_gap = self.frog.get_height() - body_feet(self.frog)[1]
        _, _, ref_h = GdkPixbuf.Pixbuf.get_file_info(os.path.join(ASSETS, "frog.png"))
        self.fish_scale = SPRITE_H / ref_h
        self.fish_steps = []
        self.fish_start = 0.0
        self.fish_until = 0.0
        self.catch = None
        # facing -> list of (sprite, x of his head), lined up on the head since the legs
        # swing about too much to centre on. Walking needs the side art; front/back are extras.
        self.walk_frames, self.walk_stand = {}, {}
        sprites = {}

        def walk_sprite(name):
            if name not in sprites:
                if name == "frog":
                    sprites[name] = self.frog
                elif os.path.exists(os.path.join(ASSETS, name + ".png")):
                    sprites[name] = load_sprite_like(name + ".png")
                else:
                    sprites[name] = None
            return sprites[name]

        def with_head(sprite):
            return sprite, body_center_x(sprite, 0.0, 0.35)

        for facing, names in WALK_ART.items():
            frames = [walk_sprite(n) for n in names]
            if all(frames):
                self.walk_frames[facing] = [with_head(f) for f in frames]
                stand = walk_sprite(WALK_STAND[facing]) or frames[0]
                self.walk_stand[facing] = with_head(stand)
        if "left" in self.walk_frames:
            self.walk_frames["right"] = [with_head(f.flip(True)) for f, _ in self.walk_frames["left"]]
            self.walk_stand["right"] = with_head(self.walk_stand["left"][0].flip(True))
        else:
            self.walk_frames, self.walk_stand = {}, {}
        # kicking art faces left like the side walk; only usable if he can walk to the ball
        kicks = {n: walk_sprite(n) for n in ("kick1", "kick2")}
        self.kick_frames = {}
        if "left" in self.walk_frames and all(kicks.values()):
            self.kick_frames = {"left": {n: with_head(k) for n, k in kicks.items()},
                                "right": {n: with_head(k.flip(True)) for n, k in kicks.items()}}
        self.kick_pose = None  # "kick1" or "kick2" mid-kick, else None
        # Watering: the can he carries while walking, and the pouring frames (which face left).
        can = walk_sprite("watering_can")
        pour = {n: walk_sprite(n) for n in ("water0", "water1", "water2", "water3")}
        self.water_frames, self.can = {}, {}
        if "left" in self.walk_frames and can and all(pour.values()):
            # these frames are padded differently on the left; line them up on the head alone
            left = {n: (p, body_center_x(p, 0.0, 0.28)) for n, p in pour.items()}
            self.water_frames = {
                "left": left,
                "right": {n: (p.flip(True), p.get_width() - hx) for n, (p, hx) in left.items()}}
            self.can = {"left": can, "right": can.flip(True)}
        self.art_scale = SPRITE_H / GdkPixbuf.Pixbuf.get_file_info(os.path.join(ASSETS, "frog.png"))[2]
        self.carrying_can = False
        self.water_steps = []  # (frame, seconds) while pouring
        self.water_start = 0.0
        self.walk_facing = None  # "left", "right", "front", "back"; None when not walking
        self.walk_moving = False  # False while he stands between stretches
        self.walk_start = 0.0
        self.mouth = self.mouths[0]
        self.next_mouth = 0.0
        self.t = 0.0
        self.thinking = False
        self.talk_until = 0.0
        self.next_blink = time.monotonic() + 2
        self.blink_until = 0.0
        self.connect("draw", self.on_draw)
        GLib.timeout_add(33, self.tick)

    def talk(self, text, at_least=0.6):
        """Move Freg's mouth for about as long as it takes to say `text`."""
        now = time.monotonic()
        start = max(self.talk_until, now)
        self.talk_until = min(start + max(at_least, len(text) / 18), now + 6)

    def start_nap(self):
        self.napping = bool(self.nap_frames)
        self.nap_frame = 0
        self.next_nap_frame = time.monotonic() + NAP_FRAME_SECONDS
        return self.napping

    @property
    def dancing(self):
        return time.monotonic() < self.dance_until

    def dance(self, seconds):
        if self.dance_frames and not self.napping:
            self.dance_start = time.monotonic()
            self.dance_until = self.dance_start + seconds

    def stop_dancing(self):
        self.dance_until = 0.0

    @property
    def waving(self):
        return time.monotonic() < self.wave_until

    def wave(self, cycles):
        if self.wave_frames and not self.napping:
            self.wave_start = time.monotonic()
            self.wave_until = self.wave_start + cycles * len(self.wave_frames) * WAVE_BEAT

    def stop_waving(self):
        self.wave_until = 0.0

    @property
    def fishing(self):
        return time.monotonic() < self.fish_until

    def go_fishing(self):
        """side_neutral, then wait on fishing1 (blinking on fishing2) until a bite, then reel it in and celebrate."""
        if not self.fish_sprites or self.napping:
            return
        wait, waited = random.uniform(*FISH_WAIT_SECONDS), 0.0
        steps = [("side_neutral", 0.25)]
        while waited < wait:  # mostly eyes open on fishing1, with a quick blink on fishing2
            stare = random.uniform(*FISH_STARE)
            steps += [("fishing1", stare), ("fishing2", FISH_BLINK)]
            waited += stare + FISH_BLINK
        self.fish_steps = (steps[:-1]  # don't blink right as he gets the bite
                           + [("fish_caught", 1.2), ("fish_pull_frame1", 0.3),
                              ("fish_pull_frame2", 0.3), ("fish_pull_frame3", 0.5),
                              ("fish_celebrate", 2.5)])
        self.catch = random.choice(self.catches)
        self.fish_start = time.monotonic()
        self.fish_until = self.fish_start + sum(d for _, d in self.fish_steps)

    def stop_fishing(self):
        self.fish_until = 0.0

    @property
    def walking(self):
        return self.walk_facing is not None

    def walk(self, facing):
        """Face `facing` and step along; the window does the actual moving."""
        if facing not in self.walk_frames or self.napping:
            return
        self.walk_start = time.monotonic()
        self.walk_facing, self.walk_moving = facing, True

    def stand(self):
        """Stop for a moment mid-walk, still facing the same way."""
        self.walk_moving = False

    def stop_walking(self):
        self.walk_facing, self.walk_moving = None, False
        self.kick_pose = None
        self.carrying_can = False
        self.water_steps = []

    def start_watering(self):
        """Tip the can and pour; returns (seconds it takes, seconds until water hits the ground)."""
        pour = [("water2", 0.18), ("water3", 0.18)] * WATER_POUR_BEATS
        self.water_steps = [("water0", 0.3), ("water1", 0.25)] + pour + [("water1", 0.2),
                                                                         ("water0", 0.25)]
        self.water_start = time.monotonic()
        return sum(d for _, d in self.water_steps), 0.3 + 0.25 + 0.18

    def water_step(self):
        """The pouring frame to show now, or None once he's done."""
        left = time.monotonic() - self.water_start
        for name, seconds in self.water_steps:
            if left < seconds:
                return name
            left -= seconds
        return None

    def fish_step(self):
        left = time.monotonic() - self.fish_start
        for name, seconds in self.fish_steps:
            if left < seconds:
                return name
            left -= seconds
        return self.fish_steps[-1][0]

    def wake(self, startled):
        self.napping = False
        if startled:
            self.jolt_until = time.monotonic() + 0.45

    def tick(self):
        self.t += 0.033
        now = time.monotonic()
        if self.napping and now > self.next_nap_frame:
            self.nap_frame = (self.nap_frame + 1) % len(self.nap_frames)
            self.next_nap_frame = now + NAP_FRAME_SECONDS
        if now > self.next_blink:
            self.blink_until = now + 0.15
            self.next_blink = now + random.uniform(2.0, 5.5)
        if now < self.talk_until and now > self.next_mouth:
            # a different random mouth shape each time looks more like real speech than a loop
            self.mouth = random.choice([m for m in self.mouths if m is not self.mouth] or self.mouths)
            self.next_mouth = now + random.uniform(0.08, 0.14)
        self.queue_draw()
        return True

    def on_draw(self, _w, cr):
        if self.napping:
            return self.draw_nap(cr)
        if self.dancing:
            return self.draw_dance(cr)
        if self.waving:
            return self.draw_wave(cr)
        if self.fishing:
            return self.draw_fishing(cr)
        if self.walking:
            return self.draw_walk(cr)
        W, H = self.get_allocated_width(), self.get_allocated_height()
        t, now = self.t, time.monotonic()
        talking = now < self.talk_until
        if talking:
            sprite = self.mouth
        else:
            sprite = self.blink if now < self.blink_until else self.frog
        sw, sh = sprite.get_width(), sprite.get_height()

        if talking:
            hop = abs(math.sin(t * 9)) * 2
        else:
            hop = (math.sin(t * 2.2) + 1) * 1.5
        if now < self.jolt_until:  # startled awake: a quick jump
            hop += math.sin((self.jolt_until - now) / 0.45 * math.pi) * 14
        squash = 0.015 * math.sin(t * (9 if talking else 2.2))
        tilt = math.sin(t * 5) * 0.06 if self.thinking else 0.0

        # shadow
        cr.save()
        cr.translate(W / 2, H - 7)
        cr.scale(1, 0.2)
        cr.arc(0, 0, sw * 0.42 - hop, 0, 2 * math.pi)
        cr.restore()
        cr.set_source_rgba(0, 0, 0, 0.16)
        cr.fill()

        # sprite, anchored at its feet so squash/tilt look grounded
        feet_x, feet_y = W / 2, H - 8 - hop
        cr.save()
        cr.translate(feet_x, feet_y)
        cr.rotate(tilt)
        cr.scale(1 - squash, 1 + squash)
        Gdk.cairo_set_source_pixbuf(cr, sprite, -sw / 2, -sh)
        cr.paint()
        cr.restore()

        # thinking dots
        if self.thinking:
            top = feet_y - sh
            for i in range(3):
                lift = max(0.0, math.sin(t * 6 - i * 0.9)) * 5
                cr.arc(W / 2 - 12 + i * 12, top - 8 - lift, 3, 0, 2 * math.pi)
                cr.set_source_rgba(74 / 255, 74 / 255, 123 / 255, 0.9)
                cr.fill()
        return False

    def draw_dance(self, cr):
        W, H = self.get_allocated_width(), self.get_allocated_height()
        beats = (time.monotonic() - self.dance_start) / DANCE_BEAT
        step = int(beats) % 2
        sprite = self.dance_frames[step]
        sh = sprite.get_height()
        swing = math.sin(math.pi * beats)  # changes sign every beat, so each step leans the other way
        lean = DANCE_LEAN * swing
        hop = abs(swing) * 5

        cr.save()
        cr.translate(W / 2 + lean * 20, H - 7)
        cr.scale(1, 0.2)
        cr.arc(0, 0, self.frog.get_width() * 0.42 - hop, 0, 2 * math.pi)
        cr.restore()
        cr.set_source_rgba(0, 0, 0, 0.16)
        cr.fill()

        cr.save()
        cr.translate(W / 2, H - 8 - hop)  # pivot at his feet
        cr.rotate(lean)
        Gdk.cairo_set_source_pixbuf(cr, sprite, -self.dance_centers[step], -sh)
        cr.paint()
        cr.restore()
        return False

    def draw_walk(self, cr):
        W, H = self.get_allocated_width(), self.get_allocated_height()
        facing = self.walk_facing
        pouring = self.water_step() if self.water_steps and facing in self.water_frames else None
        if self.kick_pose:
            (sprite, head_x), hop = self.kick_frames[facing][self.kick_pose], 0.0
        elif pouring:
            (sprite, head_x), hop = self.water_frames[facing][pouring], 0.0
        elif self.walk_moving:
            frames = self.walk_frames[facing]
            steps = (time.monotonic() - self.walk_start) / WALK_FRAME_SECONDS[facing]
            sprite, head_x = frames[int(steps) % len(frames)]
            hop = abs(math.cos(math.pi * steps / 2)) * 2  # up as his legs pass, down mid-stride
        else:
            (sprite, head_x), hop = self.walk_stand[facing], 0.0
        sh = sprite.get_height()

        cr.save()
        cr.translate(W / 2, H - 7)
        cr.scale(1, 0.2)
        cr.arc(0, 0, self.frog.get_width() * 0.38 - hop, 0, 2 * math.pi)
        cr.restore()
        cr.set_source_rgba(0, 0, 0, 0.16)
        cr.fill()

        can = self.carrying_can and not pouring and not self.kick_pose
        if can and facing == "back":  # seen from behind, the can's on his far side
            self.draw_can(cr, facing, W / 2, H - 8 - hop)
        Gdk.cairo_set_source_pixbuf(cr, sprite, W / 2 - head_x, H - 8 - hop - sh)
        cr.paint()
        if can and facing != "back":
            self.draw_can(cr, facing, W / 2, H - 8 - hop)
        return False

    def draw_can(self, cr, facing, head_x, feet_y):
        """The watering can hanging from his hand by its top handle."""
        mirrored = facing in ("right", "back")
        hx, hy = CAN_HANDS["left" if facing == "right" else facing]
        if facing == "right":
            hx = -hx
        can = self.can["right" if mirrored else "left"]
        gx, gy = CAN_GRIP
        if mirrored:
            gx = can.get_width() / self.art_scale - gx
        s = self.art_scale
        Gdk.cairo_set_source_pixbuf(cr, can, head_x + (hx - gx) * s, feet_y + (hy - gy) * s)
        cr.paint()

    def draw_wave(self, cr):
        W, H = self.get_allocated_width(), self.get_allocated_height()
        beats = (time.monotonic() - self.wave_start) / WAVE_BEAT
        step = int(beats) % len(self.wave_frames)
        sprite = self.wave_frames[step]
        hop = abs(math.sin(math.pi * beats / len(self.wave_frames))) * 2  # a little bounce

        cr.save()
        cr.translate(W / 2, H - 7)
        cr.scale(1, 0.2)
        cr.arc(0, 0, self.frog.get_width() * 0.42 - hop, 0, 2 * math.pi)
        cr.restore()
        cr.set_source_rgba(0, 0, 0, 0.16)
        cr.fill()

        Gdk.cairo_set_source_pixbuf(cr, sprite, W / 2 - self.wave_centers[step],
                                    H - 8 - hop - sprite.get_height())
        cr.paint()
        return False

    def draw_fishing(self, cr):
        W, H = self.get_allocated_width(), self.get_allocated_height()
        name = self.fish_step()
        sprite = self.fish_sprites[name]
        sh = sprite.get_height()
        # The rod sticks out to his left, so stand him right of centre if it wouldn't fit.
        body_x = max(W / 2, max(cx for cx, _ in self.fish_feet.values()) + 2)
        feet_x, feet_y = self.fish_feet[name]
        lean = hop = 0.0
        if name == "fish_celebrate":  # a little victory dance, leaning one way then the other
            swing = math.sin(math.pi * self.t / DANCE_BEAT)
            lean, hop = DANCE_LEAN * swing, abs(swing) * 4
        left, top = -feet_x, -feet_y

        cr.save()
        cr.translate(body_x + lean * 20, H - 7)
        cr.scale(1, 0.2)
        cr.arc(0, 0, self.frog.get_width() * 0.42 - hop, 0, 2 * math.pi)
        cr.restore()
        cr.set_source_rgba(0, 0, 0, 0.16)
        cr.fill()

        cr.save()
        cr.translate(body_x, H - 8 - self.frog_feet_gap - hop)  # pivot at his feet
        cr.rotate(lean)
        # the catch goes behind the sprite, so the line's end and his hand overlap it
        fish = self.catch
        fw, fh = fish.get_width(), fish.get_height()
        if name in FISH_LINE_ENDS:  # hooked by the mouth, swinging as he yanks it up
            x, y = FISH_LINE_ENDS[name]
            cr.save()
            cr.translate(left + x * self.fish_scale, top + y * self.fish_scale)
            cr.rotate(0.3 * math.sin(self.t * 9))
            Gdk.cairo_set_source_pixbuf(cr, fish, -fw / 2, -2)
            cr.paint()
            cr.restore()
        elif name == "fish_celebrate":  # held up by the tail
            x, y = FISH_HAND
            Gdk.cairo_set_source_pixbuf(cr, fish, left + x * self.fish_scale - fw / 2,
                                        top + y * self.fish_scale - fh * 0.8)
            cr.paint()

        Gdk.cairo_set_source_pixbuf(cr, sprite, left, top)
        cr.paint()
        cr.restore()
        return False

    def draw_nap(self, cr):
        W, H = self.get_allocated_width(), self.get_allocated_height()
        sprite = self.nap_frames[self.nap_frame]
        sh = sprite.get_height()
        breathe = 0.025 * math.sin(self.t * 1.6)

        cr.save()
        cr.translate(W / 2, H - 7)
        cr.scale(1, 0.2)
        cr.arc(0, 0, self.nap_body_w * 0.45, 0, 2 * math.pi)
        cr.restore()
        cr.set_source_rgba(0, 0, 0, 0.16)
        cr.fill()

        cr.save()
        cr.translate(W / 2 - self.nap_body_w / 2, H - 8)
        cr.scale(1, 1 + breathe)
        Gdk.cairo_set_source_pixbuf(cr, sprite, 0, -sh)
        cr.paint()
        cr.restore()
        return False


class Tail(Gtk.DrawingArea):
    """Speech-bubble tail pointing down at Freg."""

    def __init__(self):
        super().__init__()
        self.set_size_request(MASCOT_W, 14)
        self.connect("draw", self.on_draw)

    def on_draw(self, _w, cr):
        x = self.get_allocated_width() / 2
        cr.move_to(x - 12, -1)
        cr.line_to(x + 8, -1)
        cr.line_to(x - 2, 13)
        cr.close_path()
        cr.set_source_rgb(0x24 / 255, 0x24 / 255, 0x2E / 255)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.10)
        cr.set_line_width(1)
        cr.stroke()
        cr.rectangle(x - 11.5, -1, 19, 1.5)  # hide the stroke where it meets the bubble
        cr.set_source_rgb(0x24 / 255, 0x24 / 255, 0x2E / 255)
        cr.fill()
        return False


class FlowerWindow(Gtk.Window):
    """A flower Freg grew: a tiny click-through window that sprouts, blooms, then fades."""

    def __init__(self, stages, x, ground):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.stages = stages  # flower_1, flower_2, flower_3
        self.w = max(p.get_width() for p in stages) + 6
        self.h = max(p.get_height() for p in stages) + 4
        self.set_size_request(self.w, self.h)
        screen = self.get_screen()
        if screen.get_rgba_visual() and screen.is_composited():
            self.set_visual(screen.get_rgba_visual())
        self.set_app_paintable(True)
        self.set_keep_above(True)
        self.born = time.monotonic()
        self.stage, self.stage_since, self.alpha = 0, self.born, 1.0
        self.connect("draw", self.on_draw)
        self.connect("realize", lambda *_: self.input_shape_combine_region(cairo.Region()))
        self.move(round(x - self.w / 2), round(ground - self.h + 2))
        self.show()

    def wilt(self):
        """Start fading now."""
        self.born = min(self.born, time.monotonic() - FLOWER_SECONDS)

    def update(self, now):
        """Grow, then fade; returns False once it's gone."""
        age = now - self.born
        stage = 0 if age < FLOWER_GROW[0] else 1 if age < sum(FLOWER_GROW) else 2
        if stage != self.stage:
            self.stage, self.stage_since = stage, now
        if age > FLOWER_SECONDS:
            self.alpha = 1 - (age - FLOWER_SECONDS) / FLOWER_FADE
            if self.alpha <= 0:
                self.destroy()
                return False
        if now - self.stage_since < 0.4 or age > FLOWER_SECONDS:
            self.queue_draw()
        return True

    def on_draw(self, _w, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        sprite = self.stages[self.stage]
        # pop up a little each time it grows
        t = min(1.0, (time.monotonic() - self.stage_since) / 0.3)
        grow = 0.6 + 0.4 * math.sin(t * math.pi / 2) + 0.08 * math.sin(t * math.pi)
        cr.translate(self.w / 2, self.h - 2)
        cr.scale(grow, grow)
        Gdk.cairo_set_source_pixbuf(cr, sprite, -sprite.get_width() / 2, -sprite.get_height())
        cr.paint_with_alpha(max(0.0, self.alpha))
        return False


class BallWindow(Gtk.Window):
    """Freg's ball: a tiny click-through window of its own, so it can roll anywhere."""

    def __init__(self, sprite):
        super().__init__(type=Gtk.WindowType.POPUP)  # unmanaged, so nothing stops it moving
        self.sprite = sprite
        self.radius = max(sprite.get_width(), sprite.get_height()) / 2
        self.w = int(self.radius * 2) + 10  # room for the corners as it spins
        self.h = self.w + BALL_HOP_ROOM
        self.set_size_request(self.w, self.h)
        screen = self.get_screen()
        if screen.get_rgba_visual() and screen.is_composited():
            self.set_visual(screen.get_rgba_visual())
        self.set_app_paintable(True)
        self.set_keep_above(True)
        # where it is: centre x and ground line on screen, height above the ground, speeds
        self.x = self.ground = self.height = 0.0
        self.vx = self.vh = 0.0
        self.angle = 0.0
        self.alpha = 1.0
        self.connect("draw", self.on_draw)
        self.connect("realize", lambda *_: self.input_shape_combine_region(cairo.Region()))

    @property
    def rolling(self):
        return abs(self.vx) > 0 or self.height > 0 or self.vh > 0

    def kick(self, direction):
        self.vx = direction * random.uniform(*BALL_SPEED)
        self.vh = random.uniform(*BALL_POP)

    def step(self, dt, lo, hi):
        """Roll, bounce and slow down; bounce off lo/hi, the furthest it may go each way."""
        if not self.rolling:
            return  # sitting still: don't keep moving and redrawing its window for nothing
        if self.vx:
            slow = BALL_FRICTION * dt
            self.vx = 0.0 if abs(self.vx) <= slow else self.vx - math.copysign(slow, self.vx)
            self.x += self.vx * dt
            if not lo <= self.x <= hi:  # off the edge of the screen: bounce back
                self.x = min(max(self.x, lo), hi)
                self.vx = -self.vx * 0.6
            self.angle += self.vx * dt / self.radius
        if self.height > 0 or self.vh > 0:
            self.vh -= BALL_GRAVITY * dt
            self.height += self.vh * dt
            if self.height <= 0:
                self.height = 0.0
                self.vh = -self.vh * 0.45 if -self.vh > 60 else 0.0
        self.place()

    def place(self):
        self.move(round(self.x - self.w / 2), round(self.ground - self.h + 4))
        self.queue_draw()

    def on_draw(self, _w, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        ground = self.h - 4
        cr.save()  # shadow, shrinking as the ball goes up
        cr.translate(self.w / 2, ground)
        cr.scale(1, 0.25)
        cr.arc(0, 0, max(2.0, self.radius * (0.9 - self.height / 80)), 0, 2 * math.pi)
        cr.restore()
        cr.set_source_rgba(0, 0, 0, 0.16 * self.alpha)
        cr.fill()
        cr.translate(self.w / 2, ground - self.radius - self.height)
        cr.rotate(self.angle)
        Gdk.cairo_set_source_pixbuf(cr, self.sprite, -self.sprite.get_width() / 2,
                                    -self.sprite.get_height() / 2)
        cr.paint_with_alpha(self.alpha)
        return False


def with_tail(child):
    """Stack a bubble over a tail pointing at Freg, right-aligned above him."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.set_halign(Gtk.Align.END)
    box.pack_start(child, False, False, 0)
    tail = Tail()
    tail.set_halign(Gtk.Align.END)
    box.pack_start(tail, False, False, 0)
    return box


class FregWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Freg")
        self.session = AgentSession(BACKENDS.get(AGENT, BACKENDS["claude"]))
        self.press = None
        self.dragging = False
        self.quiet = False
        self.antics = True  # naps, dancing, fishing and walks when idle
        self.nap_end_id = None
        self.walk_id = None
        self.walk_legs_left = 0  # stretches still to go on this walk
        self.walk_target = (0.0, 0.0)  # window position the current stretch ends at
        self.walk_pause_until = 0.0
        self.ball = None  # BallWindow, made the first time he plays
        self.watering = False  # on a gardening walk: water at the end of each stretch
        self.water_gen = 0  # bumps each time watering stops, so late flowers aren't planted
        self.flowers = []  # FlowerWindows he's grown
        self.flower_id = None
        self.ball_state = None  # what Freg's doing in a game of kickabout; None when not playing
        self.ball_id = None
        # The window can't go above the top of the screen, but the empty bubble space above
        # Freg can: past that point he's raised inside the window by this many pixels instead.
        self.lift = 0
        self.last_interaction = time.monotonic()
        self.speech_hide_id = None
        self.speech_mode = None  # "quip" (auto-hides) or "reply" (click through pages)
        self.reply = ""
        self.pages = []
        self.page = 0
        self.reply_done = True
        self.reply_error = None
        self.generation = 0  # bumps when a reply is abandoned, so its late callbacks are ignored

        self.get_style_context().add_class("freg")
        screen = self.get_screen()
        if screen.get_rgba_visual() and screen.is_composited():
            self.set_visual(screen.get_rgba_visual())
        self.set_app_paintable(True)
        self.set_decorated(False)
        # The window never resizes (GNOME/XWayland is unreliable about it); empty
        # space is transparent and click-through.
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.stick()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_size_request(BUBBLE_W, WINDOW_H)
        self.add(outer)
        root = self.root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.set_valign(Gtk.Align.END)
        outer.pack_start(root, True, True, 0)

        # Speech bubble: Freg's replies (one page at a time) and quips.
        self.speech_text = Gtk.Label(wrap=True, max_width_chars=28, xalign=0)
        self.speech_text.get_style_context().add_class("text")
        self.speech_hint = Gtk.Label(xalign=1)
        self.speech_hint.get_style_context().add_class("hint")
        speech_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        speech_box.get_style_context().add_class("speech")
        speech_box.pack_start(self.speech_text, False, False, 0)
        speech_box.pack_start(self.speech_hint, False, False, 0)
        speech_click = Gtk.EventBox(visible_window=False, above_child=True)
        speech_click.add(speech_box)
        speech_click.connect("button-release-event", self.on_speech_click)
        self.speech_area = with_tail(speech_click)
        root.pack_start(self.speech_area, False, False, 0)

        # Ask box: just a prompt and a Send button.
        self.entry = Gtk.Entry(placeholder_text="Ask Freg something…", width_chars=22)
        self.entry.connect("activate", self.on_send)
        self.entry.connect("key-press-event", self.on_entry_key)
        self.send_btn = Gtk.Button(label="Send")
        self.send_btn.connect("clicked", self.on_send)
        ask_box = Gtk.Box(spacing=6)
        ask_box.get_style_context().add_class("ask")
        ask_box.pack_start(self.entry, True, True, 0)
        ask_box.pack_start(self.send_btn, False, False, 0)
        self.ask_area = with_tail(ask_box)
        root.pack_start(self.ask_area, False, False, 0)

        self.mascot = Mascot()
        self.mascot.set_halign(Gtk.Align.END)
        self.mascot.set_tooltip_text("Click to ask · drag to move · right-click for menu")
        box = Gtk.EventBox()
        box.set_visible_window(False)
        box.set_above_child(True)  # DrawingArea has its own GdkWindow that would swallow clicks
        box.set_halign(Gtk.Align.END)
        box.add(self.mascot)
        box.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                       | Gdk.EventMask.POINTER_MOTION_MASK)
        box.connect("button-press-event", self.on_press)
        box.connect("button-release-event", self.on_release)
        box.connect("motion-notify-event", self.on_motion)
        root.pack_start(box, False, False, 0)
        self.mascot_box = box
        self.lift_box = Gtk.Box()  # empty space under Freg that raises him; see self.lift
        root.pack_start(self.lift_box, False, False, 0)

        outer.show_all()
        self.speech_area.hide()
        self.ask_area.hide()
        self.lift_box.hide()
        self.speech_area.connect("show", self.lower_for_bubbles)
        self.ask_area.connect("show", self.lower_for_bubbles)
        self.schedule_quip()
        self.schedule_nap()
        GLib.timeout_add_seconds(4, lambda: self.show_quip("Ribbit!") and False)
        root.connect("size-allocate", lambda *_: GLib.idle_add(self.update_input_shape))
        root.connect("size-allocate", lambda *_: GLib.idle_add(self.keep_bubbles_onscreen))
        self.connect("realize", lambda *_: self.reposition())

    # ---------- positioning ----------

    def workarea(self):
        display = Gdk.Display.get_default()
        mon = display.get_primary_monitor() or display.get_monitor(0)
        return mon.get_workarea()

    def reposition(self):
        wa = self.workarea()
        _, nat = self.get_preferred_size()
        self.move(wa.x + wa.width - MARGIN - nat.width, wa.y + wa.height - MARGIN - nat.height)

    def reset_corner(self, *_):
        self.stop_walk()
        self.set_lift(0)
        self.reposition()

    def set_lift(self, lift):
        lift = max(0, round(lift))
        if lift != self.lift:
            self.lift = lift
            self.lift_box.set_size_request(-1, lift)
            self.lift_box.set_visible(lift > 0)

    def lower_for_bubbles(self, *_):
        """A bubble just opened above Freg; if he's raised too high for it to fit under the
        top of the screen, bring him down just far enough."""
        if self.lift > self.room_to_raise():
            self.stop_walk()
            self.set_lift(self.room_to_raise())
        return False

    def room_to_raise(self):
        """How far Freg can be raised inside the window: all the empty space above him, or
        with a bubble open above him, just the space above the bubble."""
        if not (self.speech_area.get_visible() or self.ask_area.get_visible()):
            return WINDOW_H - MASCOT_H
        _, content_h = self.root.get_preferred_height()
        return max(0, WINDOW_H - (content_h - self.lift))

    def keep_bubbles_onscreen(self):
        """Bubbles hang above Freg and off to his left; if he's wandered near the top or
        left edge, step back in far enough for them to fit."""
        shown = [w for w in (self.speech_area, self.ask_area) if w.get_visible()]
        if not self.get_realized() or not shown:
            return False
        self.lower_for_bubbles()  # e.g. a reply grew while he's raised
        x, y = self.get_position()
        wa = self.workarea()
        top = min(w.translate_coordinates(self, 0, 0)[1] for w in shown)
        nx, ny = max(x, wa.x), max(y, wa.y - top)
        if (nx, ny) != (x, y):
            self.stop_walk()
            self.move(nx, ny)
        return False

    def update_input_shape(self):
        """Only Freg and visible bubbles catch clicks; the rest passes through."""
        if not self.get_realized():
            return False
        region = cairo.Region()
        for widget in (self.mascot_box, self.speech_area, self.ask_area):
            if widget.get_visible():
                x, y = widget.translate_coordinates(self, 0, 0)
                region.union(cairo.RectangleInt(x, y, widget.get_allocated_width(),
                                                widget.get_allocated_height()))
        self.input_shape_combine_region(region)
        return False

    # ---------- mouse ----------

    def on_press(self, _w, ev):
        debug("press", ev.type, ev.button, ev.x_root, ev.y_root)
        if ev.type != Gdk.EventType.BUTTON_PRESS:
            return True
        if ev.button == 3:
            self.show_menu(ev)
            return True
        if ev.button == 1:
            self.stop_walk()
            x, y = self.get_position()
            self.press = (ev.x_root, ev.y_root, x, y - self.lift)
            self.dragging = False
        return True

    def on_motion(self, _w, ev):
        # We move the window ourselves rather than with begin_move_drag: the window manager
        # won't take it above the top of the screen, but place_at can raise Freg inside it.
        if not self.press:
            return True
        px, py, wx, wy = self.press
        dx, dy = ev.x_root - px, ev.y_root - py
        if not self.dragging and abs(dx) + abs(dy) > 5:
            self.dragging = True
        if self.dragging:
            self.place_at(wx + dx, wy + dy)
        return True

    def place_at(self, x, y):
        """Put the window at (x, y), or as close as the screen allows. Past the top of the
        screen the window stops and Freg is raised inside it instead, up to the screen's edge."""
        wa = self.workarea()
        x, y = round(x), max(round(y), wa.y - self.room_to_raise())
        self.set_lift(wa.y - y)
        self.move(x, max(y, wa.y))

    def on_release(self, _w, ev):
        debug("release", ev.button, "press:", self.press, "dragging:", self.dragging)
        if ev.button == 1 and self.press and not self.dragging:
            if self.mascot.napping:
                self.wake_up(startled=True)  # this click just wakes him
            else:
                self.mascot.stop_dancing()
                self.mascot.stop_waving()
                self.mascot.stop_fishing()
                self.toggle_ask()
        self.last_interaction = time.monotonic()
        self.press = None
        self.dragging = False
        return True

    def show_menu(self, ev):
        menu = Gtk.Menu()
        for label, cb in (("Continue in terminal", self.open_terminal),
                          ("New conversation", self.new_chat)):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", cb)
            menu.append(item)
        agents = Gtk.Menu()
        group = None
        for backend in BACKENDS.values():
            item = Gtk.RadioMenuItem(label=backend.label, group=group,
                                     active=backend is self.session.backend)
            group = group or item
            item.connect("toggled", self.on_agent_toggled, backend)
            agents.append(item)
        agent = Gtk.MenuItem(label="Agent")
        agent.set_submenu(agents)
        menu.append(agent)
        menu.append(Gtk.SeparatorMenuItem())
        quiet = Gtk.CheckMenuItem(label="Quiet mode (no ribbits)", active=self.quiet)
        quiet.connect("toggled", self.on_quiet_toggled)
        menu.append(quiet)
        # naps, dancing, waving, fishing, walks, ball and gardening now only happen on their own
        antics = Gtk.CheckMenuItem(label="Idle antics", active=self.antics)
        antics.connect("toggled", self.on_antics_toggled)
        menu.append(antics)
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit Freg")
        quit_item.connect("activate", lambda *_: self.get_application().quit())
        menu.append(quit_item)
        menu.show_all()
        menu.attach_to_widget(self, None)
        menu.popup_at_pointer(ev)

    # ---------- ask box ----------

    def toggle_ask(self, show=None):
        if show is None:
            show = not self.ask_area.get_visible()
        debug("toggle ask ->", show)
        if show and self.mascot.napping:
            self.wake_up(startled=False, quiet=True)
        if show and self.speech_mode == "quip":
            self.hide_speech()  # first, so the ask box only needs room for itself
        self.ask_area.set_visible(show)
        GLib.idle_add(self.update_input_shape)
        if show:
            self.present()
            self.entry.grab_focus()

    def on_entry_key(self, _w, ev):
        if ev.keyval == Gdk.KEY_Escape:
            self.toggle_ask(False)
            return True
        return False

    def set_busy(self, busy):
        self.mascot.thinking = busy
        self.send_btn.set_label("Stop" if busy else "Send")
        self.entry.set_placeholder_text("Freg is thinking…" if busy else "Ask Freg something…")

    def on_send(self, *_):
        if self.session.busy:
            self.session.cancel()
            return
        text = self.entry.get_text().strip()
        if not text:
            return
        self.entry.set_text("")
        self.last_interaction = time.monotonic()
        self.mascot.stop_dancing()
        self.mascot.stop_waving()
        self.mascot.stop_fishing()
        self.stop_walk()
        self.toggle_ask(False)
        self.hide_speech()
        self.reply, self.pages, self.page = "", [], 0
        self.reply_done, self.reply_error = False, None
        self.set_busy(True)
        self.generation += 1
        gen = self.generation

        def current(fn):
            return lambda *a: fn(*a) if gen == self.generation else None
        self.session.send(text, current(self.on_text), current(self.on_tool), current(self.on_done))

    # ---------- Freg talking ----------

    def on_text(self, chunk):
        self.mascot.thinking = False
        self.reply += chunk
        self.pages = split_pages(self.reply)
        if not self.pages:
            return
        if self.speech_mode != "reply":
            self.page = 0
            self.speech_mode = "reply"
            self.cancel_speech_timer()
            self.speech_area.show()
        if self.page == len(self.pages) - 1:
            self.mascot.talk(chunk)  # he's saying the page you're looking at
        self.render_page()

    def on_tool(self, _name):
        self.mascot.thinking = True

    def on_done(self, error):
        self.set_busy(False)
        self.reply_done = True
        if error:
            self.reply_error = error
            self.pages.append(f"Uh oh… {error}")
        elif not self.pages:
            self.pages = ["…ribbit?"]
        if (error or self.pages == ["…ribbit?"]) and self.page >= len(self.pages) - 2:
            self.mascot.talk(self.pages[-1])
        if self.speech_mode != "reply":
            self.page = 0
            self.speech_mode = "reply"
            self.cancel_speech_timer()
            self.speech_area.show()
        self.render_page()

    def render_page(self):
        text = self.pages[self.page]
        self.speech_text.set_text(text)
        ctx = self.speech_text.get_style_context()
        if self.reply_error and self.page == len(self.pages) - 1:
            ctx.add_class("err")
        else:
            ctx.remove_class("err")
        n = len(self.pages)
        if self.page < n - 1:
            hint = f"{self.page + 1}/{n}  ▸"
        elif not self.reply_done:
            hint = "…"
        else:
            hint = f"{n}/{n}" if n > 1 else ""
        self.speech_hint.set_text(hint)
        self.speech_hint.set_visible(bool(hint))

    def on_speech_click(self, *_):
        if self.speech_mode == "reply" and self.page < len(self.pages) - 1:
            self.page += 1
            self.render_page()
            self.mascot.talk_until = 0.0
            self.mascot.talk(self.pages[self.page])
        elif self.speech_mode != "reply" or self.reply_done:
            self.hide_speech()
        return True

    def show_quip(self, text, seconds=7, aside=False):
        """aside: a reply to something the user just did (e.g. a menu choice), which may
        show while the ask box is open; other quips wait until it's closed."""
        if self.speech_mode == "reply" or self.mascot.napping:
            return  # don't talk over a reply, or in his sleep
        if self.ask_area.get_visible() and not aside:
            return  # don't chatter while they're typing
        self.speech_mode = "quip"
        self.speech_text.set_text(text)
        self.speech_text.get_style_context().remove_class("err")
        self.speech_hint.hide()
        self.speech_area.show()
        self.mascot.talk_until = 0.0
        self.mascot.talk(text, at_least=1.5)
        self.cancel_speech_timer()
        self.speech_hide_id = GLib.timeout_add_seconds(seconds, self._speech_timed_out)

    def _speech_timed_out(self):
        self.speech_hide_id = None
        self.hide_speech()
        return False

    def cancel_speech_timer(self):
        if self.speech_hide_id:
            GLib.source_remove(self.speech_hide_id)
            self.speech_hide_id = None

    def hide_speech(self):
        self.cancel_speech_timer()
        self.speech_mode = None
        self.speech_area.hide()
        GLib.idle_add(self.update_input_shape)
        return False

    # ---------- quips ----------

    def schedule_quip(self):
        delay = random.uniform(*QUIP_MINUTES) * 60
        GLib.timeout_add_seconds(int(delay), self.maybe_quip)

    def maybe_quip(self):
        if not (self.quiet or self.speech_area.get_visible() or self.ask_area.get_visible()
                or self.session.busy or self.mascot.dancing or self.mascot.fishing
                or self.mascot.waving
                or self.mascot.walking):
            self.show_quip(random.choice(QUIPS))
            GLib.timeout_add_seconds(4, lambda: self.show_quip("Ribbit!") and False)
        self.schedule_quip()
        return False

    # ---------- idle antics: naps, dancing, fishing and walks ----------

    def schedule_nap(self):
        GLib.timeout_add_seconds(int(random.uniform(*ANTIC_MINUTES) * 60), self.maybe_antic)

    def maybe_antic(self):
        idle = (time.monotonic() - self.last_interaction > ANTIC_IDLE_SECONDS
                and not (self.speech_area.get_visible() or self.ask_area.get_visible()
                         or self.session.busy or self.mascot.napping or self.mascot.dancing
                         or self.mascot.waving
                         or self.mascot.fishing or self.mascot.walking))
        if self.antics and idle:
            options = [f for f, ok in ((self.start_nap, self.mascot.nap_frames),
                                       (self.start_dance, self.mascot.dance_frames),
                                       (self.start_wave, self.mascot.wave_frames),
                                       (self.start_fishing, self.mascot.fish_sprites),
                                       (self.start_walk, self.mascot.walk_frames),
                                       (self.start_ball_game,
                                        self.mascot.kick_frames and self.ball_sprite()),
                                       (self.start_watering,
                                        self.mascot.water_frames and self.flower_sprites())) if ok]
            if options:
                random.choice(options)()
        self.schedule_nap()
        return False

    def start_wave(self):
        if self.mascot.napping or self.session.busy:
            return
        if self.speech_mode == "quip":
            self.hide_speech()
        self.mascot.stop_dancing()
        self.mascot.stop_fishing()
        self.stop_walk()
        self.mascot.wave(random.randint(*WAVE_CYCLES))

    def start_dance(self):
        if self.mascot.napping or self.session.busy:
            return
        if self.speech_mode == "quip":
            self.hide_speech()
        self.mascot.stop_fishing()
        self.mascot.stop_waving()
        self.stop_walk()
        self.mascot.dance(random.uniform(*DANCE_SECONDS))

    def start_fishing(self):
        if self.mascot.napping or self.session.busy:
            return
        if self.speech_mode == "quip":
            self.hide_speech()
        self.mascot.stop_dancing()
        self.mascot.stop_waving()
        self.stop_walk()
        self.mascot.go_fishing()

    def start_nap(self):
        if self.mascot.napping or self.session.busy:
            return
        self.mascot.stop_dancing()
        self.mascot.stop_waving()
        self.mascot.stop_fishing()
        self.stop_walk()
        self.toggle_ask(False)
        if self.speech_mode == "quip":
            self.hide_speech()
        if self.mascot.start_nap():
            self.nap_end_id = GLib.timeout_add_seconds(int(random.uniform(*NAP_SECONDS)),
                                                       self._nap_over)

    def start_walk(self):
        """Wander around the screen: a few stretches up, down, left or right, pausing to
        change direction between them, then stop wherever he ends up."""
        if not self.mascot.walk_frames or self.mascot.napping or self.session.busy:
            return
        if self.speech_mode == "quip":
            self.hide_speech()
        self.mascot.stop_dancing()
        self.mascot.stop_waving()
        self.mascot.stop_fishing()
        self.toggle_ask(False)
        self.stop_walk()  # including any game of kickabout
        x, y = self.get_position()
        # walk_x/walk_y are where the window would be if it could go above the screen
        self.walk_x, self.walk_y = float(x), float(y - self.lift)
        self.walk_legs_left = random.randint(*WALK_LEGS)
        self.walk_pause_until = 0.0
        if not self.next_walk_leg():
            return
        self.walk_last = time.monotonic()
        if not self.walk_id:
            self.walk_id = GLib.timeout_add(33, self.walk_tick)

    def walk_bounds(self):
        """(left, right, top, bottom) window positions that keep Freg himself on screen."""
        wa = self.workarea()
        win_w, win_h = self.get_allocated_width(), self.get_allocated_height()
        # Freg sits in the bottom-right corner of the window, below where the bubbles go.
        return (wa.x + MARGIN + MASCOT_W // 2 - (win_w - MASCOT_W // 2),
                wa.x + wa.width - MARGIN - win_w,
                wa.y + MARGIN - (win_h - MASCOT_H),
                wa.y + wa.height - MARGIN - win_h)

    def next_walk_leg(self):
        """Pick a direction with room to walk (preferably a new one) and a distance."""
        if self.walk_legs_left <= 0:
            self.stop_walk()
            return False
        self.walk_legs_left -= 1
        left, right, top, bottom = self.walk_bounds()
        x, y = self.walk_x, self.walk_y
        room = {"left": x - left, "right": right - x, "back": y - top, "front": bottom - y}
        options = [f for f, r in room.items() if f in self.mascot.walk_frames and r >= WALK_MIN_LEG]
        if len(options) > 1 and self.mascot.walk_facing in options:
            options.remove(self.mascot.walk_facing)
        if not options:
            self.stop_walk()
            return False
        facing = random.choice(options)
        dist = random.uniform(WALK_MIN_LEG, min(room[facing], WALK_MAX_LEG))
        dx, dy = {"left": (-1, 0), "right": (1, 0), "back": (0, -1), "front": (0, 1)}[facing]
        self.walk_target = (x + dx * dist, y + dy * dist)
        self.mascot.walk(facing)
        return True

    def walk_tick(self):
        if not self.mascot.walking or self.ball_state:
            self.walk_id = None
            return False
        now = time.monotonic()
        step = WALK_SPEED * (now - self.walk_last)
        self.walk_last = now
        if self.walk_pause_until:  # standing still between stretches
            if now >= self.walk_pause_until:
                self.walk_pause_until = 0.0
                if not self.next_walk_leg():
                    self.walk_id = None
                    return False
            return True
        tx, ty = self.walk_target
        dx, dy = tx - self.walk_x, ty - self.walk_y
        dist = math.hypot(dx, dy)
        if dist <= step:
            self.walk_x, self.walk_y = tx, ty
            self.mascot.stand()
            if self.watering:
                self.walk_pause_until = now + self.water_here() + 0.4
            else:
                self.walk_pause_until = now + random.uniform(*WALK_PAUSE)
        else:
            self.walk_x += dx / dist * step
            self.walk_y += dy / dist * step
        self.place_at(self.walk_x, self.walk_y)
        return True

    def stop_walk(self):
        self.mascot.stop_walking()
        self.walk_legs_left = 0
        self.walk_pause_until = 0.0
        if self.watering:
            self.watering = False
            self.water_gen += 1
        self.stop_ball_game()

    # ---------- gardening: walk about with the watering can; flowers grow where he waters ----------

    def flower_sprites(self):
        if not hasattr(self, "_flower_sprites"):
            names = [f"flower_{i}.png" for i in (1, 2, 3)]
            ok = all(os.path.exists(os.path.join(ASSETS, n)) for n in names)
            self._flower_sprites = [load_sprite_like(n) for n in names] if ok else None
        return self._flower_sprites

    def start_watering(self):
        m = self.mascot
        if not m.water_frames or not self.flower_sprites():
            return
        self.start_walk()
        if not m.walking:
            return
        self.watering = True
        m.carrying_can = True
        self.walk_legs_left = random.randint(*WATER_STOPS) - 1  # start_walk began the first

    def water_here(self):
        """Turn side-on and pour; a flower comes up where the water lands. Returns how long
        the pouring takes."""
        m = self.mascot
        side = m.walk_facing if m.walk_facing in ("left", "right") else random.choice(("left", "right"))
        m.walk(side)
        m.stand()
        seconds, splash = m.start_watering()
        reach = WATER_REACH * m.art_scale * (-1 if side == "left" else 1)
        spot = (self.feet_x() + reach, self.walk_y + WINDOW_H - 8)
        gen = self.water_gen
        # give the water a moment to soak in before anything comes up
        GLib.timeout_add(int((splash + 0.6) * 1000),
                         lambda: self.plant_flower(*spot) if gen == self.water_gen else False)
        return seconds

    def plant_flower(self, x, ground):
        self.flowers.append(FlowerWindow(self.flower_sprites(), x, ground))
        growing = [f for f in self.flowers if f.alpha == 1.0]
        for old in growing[:-MAX_FLOWERS]:
            old.wilt()
        if not self.flower_id:
            self.flower_id = GLib.timeout_add(50, self.flower_tick)
        return False

    def flower_tick(self):
        now = time.monotonic()
        self.flowers = [f for f in self.flowers if f.update(now)]
        if not self.flowers:
            self.flower_id = None
            return False
        return True

    # ---------- kickabout: kick the ball, walk to where it stopped, kick it again ----------

    def ball_sprite(self):
        if not hasattr(self, "_ball_sprite"):
            path = os.path.join(ASSETS, "ball.png")
            self._ball_sprite = load_sprite_like("ball.png") if os.path.exists(path) else None
        return self._ball_sprite

    def feet_x(self):
        """Freg's middle on screen, from the window position he's walking with."""
        return self.walk_x + self.get_allocated_width() - MASCOT_W / 2

    def ball_range(self):
        """How far the ball may roll each way: never so close to an edge that Freg
        couldn't get round behind it."""
        left, right, _, _ = self.walk_bounds()
        offset = self.get_allocated_width() - MASCOT_W / 2
        return left + offset + BALL_REACH, right + offset - BALL_REACH

    def pick_kick_direction(self):
        lo, hi = self.ball_range()
        room_left, room_right = self.ball.x - lo, hi - self.ball.x
        if min(room_left, room_right) > 250:
            return random.choice((-1, 1))
        return -1 if room_left > room_right else 1

    def start_ball_game(self):
        m = self.mascot
        if not m.kick_frames or not self.ball_sprite() or m.napping or self.session.busy:
            return
        if self.speech_mode == "quip":
            self.hide_speech()
        m.stop_dancing()
        m.stop_waving()
        m.stop_fishing()
        self.toggle_ask(False)
        self.stop_walk()
        x, y = self.get_position()
        self.walk_x, self.walk_y = float(x), float(y - self.lift)
        lo, hi = self.ball_range()
        if hi - lo < 100:
            return  # no room to play
        if not self.ball:
            self.ball = BallWindow(self.ball_sprite())
        ball = self.ball
        # drop the ball at his feet on whichever side has more room
        feet = self.feet_x()
        self.kick_dir = -1 if feet - lo > hi - feet else 1
        ball.x = min(max(feet + self.kick_dir * BALL_REACH, lo), hi)
        ball.ground = self.walk_y + WINDOW_H - 8
        ball.vx = ball.vh = ball.height = ball.angle = 0.0
        ball.alpha = 1.0
        ball.place()
        ball.show()
        self.kicks_left = random.randint(*KICKS)
        self.set_ball_state("fetch")  # usually already in place; this just lines him up
        self.ball_last = time.monotonic()
        if not self.ball_id:
            self.ball_id = GLib.timeout_add(33, self.ball_tick)

    def set_ball_state(self, state):
        self.ball_state = state
        self.ball_state_since = time.monotonic()

    def face(self, direction, moving):
        """Face left (-1) or right (1), walking or standing."""
        facing = "left" if direction < 0 else "right"
        m = self.mascot
        if m.walk_facing != facing or m.walk_moving != moving or m.kick_pose:
            m.kick_pose = None
            m.walk(facing)
            if not moving:
                m.stand()

    def ball_tick(self):
        if not self.ball_state:
            self.ball_id = None
            return False
        now = time.monotonic()
        dt = min(now - self.ball_last, 0.1)
        self.ball_last = now
        self.ball.step(dt, *self.ball_range())
        state, waited = self.ball_state, now - self.ball_state_since
        m = self.mascot
        if state == "fetch":  # walk round to the ball, so it's at his feet facing the way he'll kick
            target = self.ball.x - self.kick_dir * BALL_REACH
            gap = target - self.feet_x()
            step = WALK_SPEED * dt
            if abs(gap) <= step:
                self.walk_x += gap
                self.face(self.kick_dir, moving=False)
                self.set_ball_state("ready")
            else:
                self.face(1 if gap > 0 else -1, moving=True)
                self.walk_x += math.copysign(step, gap)
            self.place_at(self.walk_x, self.walk_y)
        elif state == "ready" and waited > KICK_READY:
            m.kick_pose = "kick1"
            self.set_ball_state("windup")
        elif state == "windup" and waited > KICK_WINDUP:
            m.kick_pose = "kick2"
            self.ball.kick(self.kick_dir)
            self.kicks_left -= 1
            self.set_ball_state("kick")
        elif state == "kick" and waited > KICK_STRIKE:
            self.face(self.kick_dir, moving=False)  # watch it go
            self.set_ball_state("watch")
        elif state == "watch" and not self.ball.rolling and waited > 0.6:
            if self.kicks_left > 0:
                self.kick_dir = self.pick_kick_direction()
                self.set_ball_state("fetch")
            else:
                self.set_ball_state("done")
        elif state == "done":  # the ball fades away and he goes back to standing about
            self.ball.alpha = max(0.0, 1 - waited / 0.8)
            self.ball.queue_draw()
            if waited > 1.2:
                self.stop_walk()
                self.ball_id = None
                return False
        return True

    def stop_ball_game(self):
        if self.ball_state:
            self.ball_state = None
            self.mascot.kick_pose = None
        if self.ball:
            self.ball.hide()

    def _nap_over(self):
        self.nap_end_id = None
        self.wake_up(startled=False)
        return False

    def wake_up(self, startled, quiet=False):
        if not self.mascot.napping:
            return
        if self.nap_end_id:
            GLib.source_remove(self.nap_end_id)
            self.nap_end_id = None
        self.mascot.wake(startled)
        self.last_interaction = time.monotonic()
        if not quiet:
            self.show_quip(random.choice(WAKE_STARTLED if startled else WAKE_NATURAL))

    def on_antics_toggled(self, item):
        self.antics = item.get_active()

    def on_quiet_toggled(self, item):
        self.quiet = item.get_active()
        if self.quiet and self.speech_mode == "quip":
            self.hide_speech()

    # ---------- conversation ----------

    def say_aside(self, text):
        """A short remark that replaces whatever bubble is up."""
        self.hide_speech()
        self.show_quip(text, aside=True)

    def on_agent_toggled(self, item, backend):
        if not item.get_active() or backend is self.session.backend:
            return
        self.generation += 1
        self.session.switch(backend)
        self.set_busy(False)
        self.say_aside(f"Now talking through {backend.label}. Fresh start!")

    def new_chat(self, *_):
        self.generation += 1
        self.session.reset()
        self.set_busy(False)
        self.say_aside("Fresh start! Ribbit.")

    def open_terminal(self, *_):
        if self.session.busy:
            self.say_aside("Give me a sec to finish talking, then try again!")
            return
        backend = self.session.backend
        cmd = (backend.resume_command(self.session.session_id) if self.session.session_id
               else [backend.program()])
        for term in (["ptyxis", "--new-window", "-d", WORKDIR, "--"],
                     ["gnome-terminal", f"--working-directory={WORKDIR}", "--"],
                     ["kgx", f"--working-directory={WORKDIR}", "--"],
                     ["konsole", "--workdir", WORKDIR, "-e"],
                     ["x-terminal-emulator", "-e"],
                     ["xterm", "-e"]):
            if shutil.which(term[0]):
                env = child_env(cmd[0])
                # Terminals like ptyxis and gnome-terminal start the command from a background
                # server with its own environment, so pass PATH (for pi's node) on the command line.
                subprocess.Popen(term + ["env", "PATH=" + env["PATH"]] + cmd, cwd=WORKDIR, env=env,
                                 start_new_session=True)
                # The terminal now owns the conversation; start fresh here.
                if self.session.session_id:
                    self.session.reset()
                    self.say_aside("Hopped over to the terminal! Anything new here starts fresh.")
                return
        self.say_aside("I couldn't find a terminal to open. Ribbit…")


class FregApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.local.FregHelper")
        self.window = None

    def do_activate(self):
        if self.window:
            self.window.toggle_ask(True)
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.replace("@FREG_FONT@", FREG_FONT).encode())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.window = FregWindow(self)
        self.window.show()


if __name__ == "__main__":
    FregApp().run(None)
