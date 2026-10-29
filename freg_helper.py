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
NAP_MINUTES = (6, 14)  # how often Freg considers a nap while nobody's bothering him
NAP_SECONDS = (60, 180)  # how long a nap lasts if nobody wakes him
NAP_FRAME_SECONDS = 0.9
DANCE_SECONDS = (5, 9)  # how long a dance lasts
DANCE_BEAT = 0.4  # seconds per step: swap frog.png/arms.png and lean the other way
DANCE_LEAN = 0.14  # radians (~8°)
BUBBLE_W = 300  # widest a speech bubble gets
SPEECH_H = 270  # tallest a speech bubble gets (long paragraphs are split into pages)
ASK_H = 64  # the ask box plus its tail
PAGE_CHARS = 240  # split paragraphs longer than this into several bubbles
DEBUG = bool(os.environ.get("FREG_DEBUG"))


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
    "Hop around in my pod ;)",
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


def body_center_x(pixbuf):
    """Horizontal middle of the frog's body (lower half of the image), in pixels."""
    w, h = pixbuf.get_width(), pixbuf.get_height()
    rs, n, px = pixbuf.get_rowstride(), pixbuf.get_n_channels(), pixbuf.get_pixels()
    cols = [x for x in range(w)
            if any(px[y * rs + x * n + 3] > 40 for y in range(int(h * 0.55), h, 2))]
    return (cols[0] + cols[-1]) / 2 if cols else w / 2


def numbered(pattern):
    """Files matching pattern, sorted so frame2 comes before frame10."""
    return sorted(glob.glob(os.path.join(ASSETS, pattern)),
                  key=lambda f: [int(n) if n.isdigit() else n for n in re.split(r"(\d+)", f)])


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
        nvm = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/pi")))
        return find_program("pi", os.path.expanduser("~/.local/bin/pi"), *reversed(nvm))

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
    talking, sleep_frame*.png while napping."""

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
        self.antics = True  # naps and dancing when idle
        self.nap_end_id = None
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
        outer.set_size_request(BUBBLE_W, SPEECH_H + 14 + ASK_H + MASCOT_H)
        self.add(outer)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
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

        outer.show_all()
        self.speech_area.hide()
        self.ask_area.hide()
        self.schedule_quip()
        self.schedule_nap()
        GLib.timeout_add_seconds(4, lambda: self.show_quip("Ribbit!") and False)
        root.connect("size-allocate", lambda *_: GLib.idle_add(self.update_input_shape))
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
        self.reposition()

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
            self.press = (ev.x_root, ev.y_root, ev.time)
            self.dragging = False
        return True

    def on_motion(self, _w, ev):
        if self.press and not self.dragging:
            if abs(ev.x_root - self.press[0]) + abs(ev.y_root - self.press[1]) > 5:
                self.dragging = True
                self.begin_move_drag(1, int(self.press[0]), int(self.press[1]), ev.time)
                self.press = None
        return True

    def on_release(self, _w, ev):
        debug("release", ev.button, "press:", self.press, "dragging:", self.dragging)
        if ev.button == 1 and self.press and not self.dragging:
            if self.mascot.napping:
                self.wake_up(startled=True)  # this click just wakes him
            else:
                self.mascot.stop_dancing()
                self.toggle_ask()
        self.last_interaction = time.monotonic()
        self.press = None
        self.dragging = False
        return True

    def show_menu(self, ev):
        menu = Gtk.Menu()
        for label, cb in (("Ask Freg…", lambda *_: self.toggle_ask(True)),
                          ("Continue in terminal", self.open_terminal),
                          ("New conversation", self.new_chat),
                          (None, None),
                          ("Move back to corner", self.reset_corner),
                          ("Quit Freg", lambda *_: self.get_application().quit())):
            item = Gtk.SeparatorMenuItem() if label is None else Gtk.MenuItem(label=label)
            if cb:
                item.connect("activate", cb)
            menu.append(item)
        quiet = Gtk.CheckMenuItem(label="Quiet mode (no ribbits)", active=self.quiet)
        quiet.connect("toggled", self.on_quiet_toggled)
        menu.insert(quiet, 4)
        antics = Gtk.CheckMenuItem(label="Idle antics (naps, dancing)", active=self.antics)
        antics.connect("toggled", self.on_antics_toggled)
        menu.insert(antics, 5)
        pos = 6
        if self.mascot.nap_frames:
            nap_now = Gtk.MenuItem(label="Wake up!" if self.mascot.napping else "Nap time")
            nap_now.connect("activate", lambda *_: self.wake_up(True) if self.mascot.napping
                            else self.start_nap())
            menu.insert(nap_now, pos)
            pos += 1
        if self.mascot.dance_frames:
            dance = Gtk.MenuItem(label="Dance!")
            dance.connect("activate", lambda *_: self.start_dance())
            menu.insert(dance, pos)
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
        menu.insert(agent, 3)
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
        self.ask_area.set_visible(show)
        GLib.idle_add(self.update_input_shape)
        if show:
            if self.speech_mode == "quip":
                self.hide_speech()
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

    def show_quip(self, text, seconds=7):
        if self.speech_mode == "reply" or self.mascot.napping:
            return  # don't talk over a reply, or in his sleep
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
                or self.session.busy or self.mascot.dancing):
            self.show_quip(random.choice(QUIPS))
            GLib.timeout_add_seconds(4, lambda: self.show_quip("Ribbit!") and False)
        self.schedule_quip()
        return False

    # ---------- idle antics: naps and dancing ----------

    def schedule_nap(self):
        GLib.timeout_add_seconds(int(random.uniform(*NAP_MINUTES) * 60), self.maybe_antic)

    def maybe_antic(self):
        idle = (time.monotonic() - self.last_interaction > 120
                and not (self.speech_area.get_visible() or self.ask_area.get_visible()
                         or self.session.busy or self.mascot.napping or self.mascot.dancing))
        if self.antics and idle:
            options = [f for f, ok in ((self.start_nap, self.mascot.nap_frames),
                                       (self.start_dance, self.mascot.dance_frames)) if ok]
            if options:
                random.choice(options)()
        self.schedule_nap()
        return False

    def start_dance(self):
        if self.mascot.napping or self.session.busy:
            return
        if self.speech_mode == "quip":
            self.hide_speech()
        self.mascot.dance(random.uniform(*DANCE_SECONDS))

    def start_nap(self):
        if self.mascot.napping or self.session.busy:
            return
        self.mascot.stop_dancing()
        self.toggle_ask(False)
        if self.speech_mode == "quip":
            self.hide_speech()
        if self.mascot.start_nap():
            self.nap_end_id = GLib.timeout_add_seconds(int(random.uniform(*NAP_SECONDS)),
                                                       self._nap_over)

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
        self.show_quip(text)

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
                subprocess.Popen(term + cmd, cwd=WORKDIR, env=child_env(cmd[0]),
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
