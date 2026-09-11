#!/usr/bin/env python3
"""ccdash - coding agent dashboard: chibi + clock, usage limits, session cards."""

import fcntl
import fnmatch
import functools
import glob
import json
import os
import re
import select
import shlex
import shutil
import struct
import subprocess
import sys
import termios
import time
import tty
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

__version__ = "0.1.0"

# The agents' own folders, where they say; ours per XDG, never inside theirs.
CLAUDE = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
CODEX = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
OPENCODE = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser(
    "~/.local/share"), "opencode", "opencode.db")
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                      "ccdash")
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                     "ccdash")
CREDS = os.path.join(CLAUDE, ".credentials.json")
CHIBI_ART = os.environ.get("CCDASH_ART") or os.path.join(CONFIG, "art.gif")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
REFRESH = 5  # transcript rescan: only files whose mtime moved get parsed again
# ponytail: the art's own frame duration. Animating repaints the screen, ~110
# KB/s of truecolor half-blocks - fine locally, chunky over a slow ssh link.
# Point CCDASH_ART at a still image to stop it dead.
FRAME = 0.12
# (cell_w, cell_h) in px once run() finds a sixel terminal: the chibi is then
# real pixels instead of half blocks. CCDASH_SIXEL=0 keeps half blocks.
SIXEL = None
SHOWN = None  # (rows, x) of the chibi the last render() laid out

KEY = (0x12, 0x12, 0x1A)  # keyed_art floods the art's background to this

# ----------------------------------------------------------------- themes

# role -> hex. A theme may leave roles out; they fall back to "ccdash".
ROLES = ("bg", "txt", "mut", "dim", "bor", "acc", "blu", "grn", "yel", "red")
THEMES = {n: dict(zip(ROLES, v.split())) for n, v in {
    "ccdash":           "12121A E2E2F0 8A8AA8 5C5C76 343448 A78BFA 60A5FA 4ADE80 FACC15 F87171",
    "catppuccin-mocha": "1E1E2E CDD6F4 A6ADC8 6C7086 45475A CBA6F7 89B4FA A6E3A1 F9E2AF F38BA8",
    "catppuccin-latte": "EFF1F5 4C4F69 6C6F85 9CA0B0 BCC0CC 8839EF 1E66F5 40A02B DF8E1D D20F39",
    "dracula":          "282A36 F8F8F2 9AA2C8 6272A4 44475A BD93F9 8BE9FD 50FA7B F1FA8C FF5555",
    "nord":             "2E3440 ECEFF4 A3ACBC 616E88 434C5E 88C0D0 81A1C1 A3BE8C EBCB8B BF616A",
    "tokyo-night":      "1A1B26 C0CAF5 A9B1D6 565F89 3B4261 BB9AF7 7AA2F7 9ECE6A E0AF68 F7768E",
    "gruvbox":          "282828 EBDBB2 A89984 7C6F64 504945 FE8019 83A598 B8BB26 FABD2F FB4934",
    "rose-pine":        "191724 E0DEF4 908CAA 6E6A86 403D52 C4A7E7 EBBCBA 9CCFD8 F6C177 EB6F92",
    "solarized":        "002B36 93A1A1 839496 586E75 073642 6C71C4 268BD2 859900 B58900 DC322F",
}.items()}
THEME_FILE = os.path.join(CONFIG, "theme")
USER_THEMES = os.path.join(CONFIG, "themes.json")
THEME = "ccdash"
# set_theme() fills these in: BG and PAL (role -> rgb), and one foreground
# escape per role - TXT MUT DIM BOR ACC BLU GRN YEL RED. R resets everything
# but the theme background, so the whole screen stays painted.


def rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def fg(c):
    return "\033[38;2;%d;%d;%dm" % c


def set_theme(name):
    global THEME, PAL, BG, R, STATES
    THEME = name if name in THEMES else "ccdash"
    PAL = {k: rgb(v) for k, v in {**THEMES["ccdash"], **THEMES[THEME]}.items()}
    globals().update({k.upper(): fg(c) for k, c in PAL.items() if k != "bg"})
    BG = PAL["bg"]
    R = "\033[0;48;2;%d;%d;%dm" % BG
    STATES = (("working", GRN), ("waiting", YEL), ("error", RED), ("idle", DIM))
    _CHIBI_CACHE.clear()  # both hold colours baked in
    _SIXELS.clear()


def load_themes(path=USER_THEMES):
    """Merge {name: {role: "#hex"}} from the user's file; bad roles are dropped."""
    try:
        with open(path) as f:
            user = json.load(f)
    except (OSError, ValueError):
        return
    for name, t in (user.items() if isinstance(user, dict) else ()):
        t = {k: v for k, v in (t.items() if isinstance(t, dict) else ())
             if k in ROLES and isinstance(v, str)
             and re.fullmatch(r"#?[0-9a-fA-F]{6}", v)}
        if t:
            THEMES[name] = t


def pick_theme():
    try:
        with open(THEME_FILE) as f:
            saved = f.read().strip()
    except OSError:
        saved = ""
    return os.environ.get("CCDASH_THEME") or saved


def next_theme():
    names = sorted(THEMES)
    set_theme(names[(names.index(THEME) + 1) % len(names)])
    try:
        os.makedirs(os.path.dirname(THEME_FILE), exist_ok=True)
        with open(THEME_FILE, "w") as f:
            f.write(THEME + "\n")
    except OSError:
        pass


ANSI = re.compile(r"\033\[[0-9;]*m")


def cells(s):
    """Terminal cells a plain string takes; CJK and emoji are double-wide."""
    # ponytail: per-codepoint, so a ZWJ emoji cluster over-counts. Real wcwidth
    # (or the wcwidth package) if that ever shows up in a title.
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


@functools.lru_cache(maxsize=4096)  # every tick re-measures the same art lines
def vlen(s):
    return cells(ANSI.sub("", s))


def pad(s, w):
    return s + " " * max(0, w - vlen(s))


def cut(s, w):
    """Truncate plain text to w cells, ellipsis if it had to bite."""
    s = " ".join(s.split())
    if cells(s) <= w:
        return s
    out, room = "", w - 1
    for c in s:
        room -= cells(c)
        if room < 0:
            break
        out += c
    return out + "…"


# ---------------------------------------------------------------- chibi

PALETTE = {
    " ": None,
    "h": (0x3B, 0x2F, 0x4A), "H": (0x5E, 0x4C, 0x75),
    "s": (0xFF, 0xDC, 0xBC), "e": (0x2B, 0x2B, 0x3A), "E": (0xFF, 0xFF, 0xFF),
    "m": (0xC9, 0x5A, 0x6A), "b": (0xFF, 0x9E, 0x9E), "r": (0xE0, 0x5A, 0x7A),
    "c": (0x3E, 0x5F, 0x8F), "C": (0x6D, 0x94, 0xC9),
}

CHIBI = [
    "         hhhhhhhh        ",
    "       hhhhhhhhhhhh      ",
    "      hhhhhhhhhhhhhh     ",
    "     hhhHHHhhhhhhhhhh    ",
    "     hhHHHhhhhhhhhhhhrr  ",
    "    hhhhhhhhhhhhhhhhrrrr ",
    "    hhsssssssssssshhrrHHH",
    "    hssssssssssssssh HHHH",
    "    hsssssssssssssshHHHHH",
    "   hsseeessssssseeeshHHHH",
    "   hsseEEessssseEEeshHHHH",
    "   hssseeessssseeessh HHH",
    "   hssssssssssssssssh HHH",
    "   hsbbssssssssssbbsh HH ",
    "   hsssssssmmmssssssh HH ",
    "   hhsssssssssssssshh H  ",
    "    hhhssssssssssshh     ",
    "     hhhhsssssshhhh      ",
    "       ccccccccccc       ",
    "     cccCCCCCCCCCccc     ",
    "    ccccCCCCCCCCCcccc    ",
    "    sscCCCCCCCCCCCcss    ",
    "     cccccccccccccc      ",
    "      cc        cc       ",
]


def render_pixels(rows):
    """Pixel rows -> text lines, two pixel rows per line via half blocks.

    A colour is only sent when it changes: pixel art is long runs of a few
    colours, and a full fg+bg escape per cell doubled every frame's bytes.
    """
    out = []
    for y in range(0, len(rows), 2):
        top = rows[y]
        bot = rows[y + 1] if y + 1 < len(rows) else []
        line, cf, cb = [R], None, BG  # R: default fg on the theme bg
        for x in range(max(len(top), len(bot))):
            t = top[x] if x < len(top) else None
            b = bot[x] if x < len(bot) else None
            f, k, ch = ((t, b, "▀") if t and b else (t, BG, "▀") if t
                        else (b, BG, "▄") if b else (cf, BG, " "))
            if k != cb:
                line.append("\033[48;2;%d;%d;%dm" % k)
                cb = k
            if f != cf:
                line.append(fg(f))
                cf = f
            line.append(ch)
        out.append("".join(line) + R)
    return out


def baked_chibi(ph=None):
    """The built-in chibi as pixel rows, `ph` tall by nearest neighbour - the
    layout sizes it like any art, and this path has no Pillow to lean on."""
    w = max(len(r) for r in CHIBI)
    px = [[PALETTE[c] for c in r.ljust(w)] for r in CHIBI]
    if not ph:
        return px
    pw = max(1, round(ph * w / len(px)))
    return [[px[y * len(px) // ph][x * w // pw] for x in range(pw)] for y in range(ph)]


_KEYED = {}


def keyed_art(path):
    """Frames with the checkerboard/flat background keyed to KEY, at work size.

    The costly half, so it is cached per file rather than per row count - a
    resize drag walks several row counts and must not re-key every time.
    """
    if path not in _KEYED:
        from PIL import Image, ImageChops, ImageDraw, ImageSequence

        frames = []
        for frame in ImageSequence.Iterator(Image.open(path)):
            im = frame.convert("RGBA")
            if im.height > 256:  # sprite-sized art keeps its own pixel grid
                im = im.resize((max(1, round(im.width * 256 / im.height)), 256),
                               Image.LANCZOS)
            alpha = im.getchannel("A")
            im = im.convert("RGB")
            if alpha.getextrema()[0] < 128:
                # Real transparency says what the background is: no guessing,
                # and none of the flood fill's pure-Python cost.
                im.paste(KEY, mask=alpha.point(lambda v: 255 * (v < 128)))
                frames.append(im)
                continue
            w, h = im.size
            # Background is what the border connects to, so flood it from the
            # corners. Connectivity, not colour - a white coat on a white
            # backdrop has to survive. floodfill is pure Python and the bulk of
            # load time, so skip corners an earlier fill already reached.
            px = im.load()
            for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
                if px[xy] != KEY:
                    ImageDraw.floodfill(im, xy, KEY, thresh=45)
            ring = ([(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)]
                    + [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)])
            if sum(px[c] == KEY for c in ring) < 0.9 * len(ring):
                # Border still mottled: a checkerboard, whose squares the flood
                # cannot cross. Fall back to keying every pale flat pixel. That
                # bites white in the art, so it only runs when the flood failed.
                r, g, b = im.split()
                lo = ImageChops.darker(ImageChops.darker(r, g), b)
                hi = ImageChops.lighter(ImageChops.lighter(r, g), b)
                pale = lo.point(lambda v: 255 * (v >= 228))
                flat = ImageChops.difference(hi, lo).point(lambda v: 255 * (v <= 5))
                im.paste(KEY, mask=ImageChops.multiply(pale, flat).convert("1"))
            frames.append(im)
        # Trim the keyed-away margin. Every cell we spend on background is a
        # cell not spent on the art, and there are very few of them. One box
        # for all frames, or the character would jitter as it moves.
        blank, box = Image.new("RGB", frames[0].size, KEY), None
        for im in frames:
            b = ImageChops.difference(im, blank).getbbox()
            box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]),
                                         max(box[2], b[2]), max(box[3], b[3]))
        if box:
            frames = [im.crop(box) for im in frames]
        _KEYED[path] = frames
    return _KEYED[path]


_ART = {}


def art_frames():
    """keyed_art with KEY repainted in the theme bg. A theme switch costs a
    few C-speed pastes here instead of the flood fill all over again."""
    if (CHIBI_ART, BG) not in _ART:
        from PIL import Image, ImageChops

        out = []
        for im in keyed_art(CHIBI_ART):
            r, g, b = ImageChops.difference(im, Image.new("RGB", im.size, KEY)).split()
            mask = ImageChops.lighter(ImageChops.lighter(r, g), b).point(
                lambda v: 255 * (v == 0))
            im = im.copy()
            im.paste(BG, mask=mask)
            out.append(im)
        _ART.clear()
        _ART[CHIBI_ART, BG] = out
    return _ART[CHIBI_ART, BG]


_CHIBI_CACHE = {}


def chibi_lines(rows, frame=0):
    """Art frame `frame` (it wraps) at `rows` text rows, drawn on first show:
    a resize drag walks dozens of row counts and needs one frame of each,
    not all of them. A still image has one frame."""
    key = rows, frame % nframes()
    if key not in _CHIBI_CACHE:
        if len(_CHIBI_CACHE) > 3 * nframes():
            _CHIBI_CACHE.clear()
        try:
            im = art_frames()[key[1]]
            if SIXEL:  # blank cells to lay out around; chibi_at() fills them
                w = -(-round(rows * SIXEL[1] * im.width / im.height) // SIXEL[0])
                lines = [" " * w] * rows
            else:
                small = sharp(im, rows * 2)  # native-size sprites grow, not blur
                px = small.load()
                lines = render_pixels([[px[x, y] for x in range(small.width)]
                                       for y in range(rows * 2)])
        except Exception:
            lines = render_pixels(baked_chibi(rows * 2))  # no Pillow, or no art file
        _CHIBI_CACHE[key] = lines
    return _CHIBI_CACHE[key]


def sharp(im, ph):
    """Pixel art to ph rows. Growing, nearest: crisp, and it keeps the art's few
    colours - smoothing would mint hundreds and triple every frame's bytes."""
    from PIL import Image

    # ponytail: a fractional zoom makes some art pixels 1px wider than others,
    # invisible at screen density. Snap ph to a multiple of im.height if not.
    # Shrinking, box: a plain average. LANCZOS rings, and round bright hair on
    # a dark bg that ringing draws as a black outline the art doesn't have.
    size = (max(1, round(ph * im.width / im.height)), ph)
    return im.resize(size, Image.NEAREST if ph >= im.height else Image.BOX)


def sixel(im):
    """RGB image -> sixel: one colour register each, six pixel rows per band."""
    p = im.quantize(255)  # sprite art has a few dozen colours - lossless
    w, h = p.size
    px, pal = p.tobytes(), p.getpalette()
    out = ['\033P0;1q"1;1;%d;%d' % (w, h)]
    out += ["#%d;2;%d;%d;%d" % (c, *(round(v * 100 / 255) for v in pal[3 * c:3 * c + 3]))
            for c in sorted(set(px))]
    char = bytes(range(63, 127)) + bytes(192)  # six bits -> "?".."~"
    for y in range(0, h, 6):
        rows = [px[r * w:(r + 1) * w] for r in range(y, min(y + 6, h))]
        band = []
        for c in sorted(set(b"".join(rows))):
            # bytes.translate lights this colour's bit per row at C speed; the
            # rows' bits never overlap, so OR-ing them as big ints builds the band
            bits = 0
            for dy, row in enumerate(rows):
                t = bytearray(256)
                t[c] = 1 << dy
                bits |= int.from_bytes(row.translate(t), "big")
            s = bits.to_bytes(w, "big").translate(char).decode().rstrip("?")
            band.append("#%d" % c + re.sub(r"(.)\1{3,}",
                                           lambda m: "!%d%s" % (len(m[0]), m[1]), s))
        out.append("$".join(band) + "-")
    return "".join(out) + "\033\\"


_SIXELS = {}


def chibi_sixel(rows, frame):
    """One frame as sixel, encoded on first show - a resize walks many sizes."""
    if (rows, frame) not in _SIXELS:
        if len(_SIXELS) > 64:
            _SIXELS.clear()
        frames = art_frames()
        _SIXELS[rows, frame] = sixel(sharp(frames[frame % len(frames)],
                                           rows * SIXEL[1]))
    return _SIXELS[rows, frame]


def probe(buf):
    """Terminal replies -> cell size in px if it speaks sixel, else None.

    DA1 lists 4 when sixel is on; CSI 16 t gives the cell size. No size reply
    means the VT340 cell sixel was designed on, which Windows Terminal emulates.
    """
    da = re.search(r"\033\[\?([\d;]*)c", buf)
    if not da or "4" not in da[1].split(";"):
        return None
    cell = re.search(r"\033\[6;(\d+);(\d+)t", buf)
    return (int(cell[2]), int(cell[1])) if cell else (10, 20)


def ask_terminal():
    """Send the two queries, collect replies until DA1 lands or 0.5s pass."""
    if os.environ.get("CCDASH_SIXEL") == "0":
        return None
    sys.stdout.write("\033[16t\033[c")  # DA1 last: everyone answers it
    sys.stdout.flush()
    buf, end = "", time.time() + 0.5
    while not re.search(r"\033\[\?[\d;]*c", buf):
        left = end - time.time()
        if left <= 0 or not select.select([sys.stdin], [], [], left)[0]:
            break
        buf += os.read(sys.stdin.fileno(), 64).decode("ascii", "replace")
    return probe(buf)


def nframes():
    """Art frames there are; 1 means there is nothing to animate."""
    try:
        return len(art_frames())
    except Exception:
        return 1  # no Pillow, or no art file: the baked chibi


def chibi_at(frame, off=0, height=1 << 16):
    """Just the chibi, cursor-placed where the last render() put it: a frame
    tick redraws the art, not the screen around it. Scrolled `off` rows, the
    half blocks clip to the `height` rows on screen; sixel can't clip, so
    scrolled at all it just blanks."""
    if not SHOWN:
        return ""
    rows, x = SHOWN
    col = PAD_X + x + 1
    out = R + "".join("\033[%d;%dH" % (PAD_Y + 1 + i - off, col) + l
                      for i, l in enumerate(chibi_lines(rows, frame))
                      if 0 <= i - off < height)
    if SIXEL and not off:
        out += "\033[%d;%dH" % (PAD_Y + 1, col) + chibi_sixel(rows, frame)
    return out


# ----------------------------------------------------------------- clock

DIGITS = {
    "0": ("███", "█ █", "█ █", "█ █", "███"),
    "1": ("  █", "  █", "  █", "  █", "  █"),
    "2": ("███", "  █", "███", "█  ", "███"),
    "3": ("███", "  █", "███", "  █", "███"),
    "4": ("█ █", "█ █", "███", "  █", "  █"),
    "5": ("███", "█  ", "███", "  █", "███"),
    "6": ("███", "█  ", "███", "█ █", "███"),
    "7": ("███", "  █", "  █", "  █", "  █"),
    "8": ("███", "█ █", "███", "█ █", "███"),
    "9": ("███", "█ █", "███", "  █", "███"),
    ":": (" ", "█", " ", "█", " "),
}


def big(text):
    return [" ".join(DIGITS[c][r] for c in text) for r in range(5)]


# --------------------------------------------------------------- limits

WINDOW = {"session": timedelta(hours=5), "weekly_all": timedelta(days=7),
          "weekly_scoped": timedelta(days=7), "monthly": timedelta(days=30)}


# The usage endpoint has a small per-account budget that Claude Code itself is
# already spending. Poll it rarely, cache across runs, and back off hard on 429
# - retrying through a rate limit is what keeps you rate limited.
LIMITS_TTL = 300
LIMITS_MAX_WAIT = 3600
LIMITS_CACHE = os.path.join(CACHE, "limits.json")


def claude_creds():
    """Claude Code's login: its credentials file, or on macOS the Keychain."""
    try:
        with open(CREDS) as f:
            return json.load(f)
    except FileNotFoundError:
        if sys.platform != "darwin":
            raise
    got = subprocess.run(["security", "find-generic-password", "-s",
                          "Claude Code-credentials", "-w"],
                         capture_output=True, text=True, timeout=5)
    if got.returncode:
        raise FileNotFoundError("no Claude Code login in the Keychain")
    return json.loads(got.stdout)


def why(exc):
    """What went wrong fetching limits, in words the panel can show."""
    code = getattr(exc, "code", None)
    return ("not logged in" if isinstance(exc, FileNotFoundError)
            else "API key login: no plan limits" if isinstance(exc, KeyError)
            else "token expired: open claude" if code == 401
            else "rate limited" if code == 429
            else "offline" if isinstance(exc, (urllib.error.URLError, OSError))
            else type(exc).__name__)


def fetch_limits():
    tok = claude_creds()["claudeAiOauth"]["accessToken"]  # KeyError: an API key login
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + tok,
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def load_limits():
    try:
        with open(LIMITS_CACHE) as f:
            return json.load(f)
    except Exception:
        return {"at": 0.0, "ok_at": 0.0, "data": None, "err": None, "wait": 0}


def limits():
    """Limits with a disk-backed cache, so restarts don't cost a request."""
    st = load_limits()
    if time.time() - st["at"] >= st["wait"]:
        try:
            st.update(at=time.time(), ok_at=time.time(), data=fetch_limits(),
                      err=None, wait=LIMITS_TTL)
        except Exception as exc:
            st.update(at=time.time(), err=why(exc),
                      wait=min(LIMITS_MAX_WAIT, st["wait"] * 2 or LIMITS_TTL))
        try:
            os.makedirs(os.path.dirname(LIMITS_CACHE), exist_ok=True)
            with open(LIMITS_CACHE, "w") as f:
                json.dump(st, f)
        except OSError:
            pass  # a read-only home is not worth crashing the dashboard over
    return (st["data"] or {}).get("limits") or [], st["err"], st.get("ok_at", 0)


def codex_limits(rl, now=None):
    """Codex's logged rate_limits -> limit dicts shaped like the usage
    endpoint's. A window that has reset since it was logged reads 0%."""
    now = now or time.time()
    out = []
    for w in ((rl or {}).get("primary"), (rl or {}).get("secondary")):
        kind = {300: "session", 10080: "weekly_all", 43200: "monthly"}.get(
            (w or {}).get("window_minutes"))
        if not kind:
            continue
        at = w.get("resets_at")
        out.append({"agent": "codex", "kind": kind,
                    "percent": 0 if at and at <= now else round(w.get("used_percent") or 0),
                    "resets_at": at and datetime.fromtimestamp(at, timezone.utc).isoformat()})
    return out


def limit_label(lim, agent=False):
    kind = lim.get("kind")
    model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
    label = ("5-HOUR" if kind == "session" else "MONTHLY" if kind == "monthly"
             else ("%s WEEKLY" % model).upper() if model else "WEEKLY")
    return (lim.get("agent", "claude").upper() + " " + label) if agent else label


def iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def until(s):
    if not s:
        return "?"
    secs = (iso(s) - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return "now"
    h, m = divmod(int(secs) // 60, 60)
    if h >= 24:
        return "%dd %dh" % divmod(h, 24)
    return "%dh %02dm" % (h, m) if h else "%dm" % m


def window_frac(lim):
    """How far through its window a limit is, 0..1, or None if unknown."""
    at, win = lim.get("resets_at"), WINDOW.get(lim.get("kind"))
    if not at or not win:
        return None
    left = (iso(at) - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, min(1.0, 1 - left / win.total_seconds()))


def full_by(lim):
    """Clock time this limit hits 100% at the pace so far, or '' if never."""
    pct, f = lim.get("percent") or 0, window_frac(lim)
    if not f or pct <= 0:
        return ""
    win = WINDOW[lim["kind"]]
    reset = iso(lim["resets_at"])
    hits = reset - win + win * (f * 100.0 / pct)
    if hits >= reset:
        return ""
    hits = hits.astimezone()  # a weekly or monthly window runs out days away
    days = (hits.date() - datetime.now().date()).days
    return hits.strftime("%H:%M" if days < 1 else "%a %H:%M" if days < 7 else "%-d %b")


def ramp(t):
    """green -> yellow -> red for how full something is, t in 0..1."""
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        return mix(PAL["grn"], PAL["yel"], t * 2)
    return mix(PAL["yel"], PAL["red"], t * 2 - 1)


EIGHTHS = " ▏▎▍▌▋▊▉"


def bar(pct, width, marker=None):
    """Eighth-block gauge, each cell coloured by how far along the bar it sits.

    `marker` (0..1) draws a thin bright line there - how much of the limit's
    window has gone, so a fill past it means ahead of pace.
    """
    full, part = divmod(int(round(max(0.0, min(100.0, pct)) / 100.0 * width * 8)), 8)
    track = mix(BG, PAL["bor"], 0.6)
    at = min(width - 1, int(marker * width)) if marker is not None else -1
    out = []
    for i in range(width):
        col = ramp((i + 0.5) / width)
        back, ch = (col, " ") if i < full else (track, EIGHTHS[part] if i == full else " ")
        if i == at:
            ch, col = "▏", PAL["txt"]
        out.append("\033[48;2;%d;%d;%dm" % back + fg(col) + ch)
    return "".join(out) + R


# ------------------------------------------------------------- sessions

TOK = ("input_tokens", "output_tokens",
       "cache_creation_input_tokens", "cache_read_input_tokens")
# what the hourly chart can show; `g` cycles CHART through them
METRICS = (("cost", "$ / HOUR"), ("output", "OUTPUT TOKENS / HOUR"),
           ("tokens", "ALL TOKENS / HOUR"))
CHART = 0

# $ per 1M tokens (input, output); cache write 1.25x in, cache read 0.10x in.
# OpenAI's cached input is also 0.10x. Their rates as of the 2026-07-30 cut.
PRICE = {"claude-fable": (10.0, 50.0), "claude-mythos": (10.0, 50.0),
         "claude-opus": (5.0, 25.0), "claude-sonnet-4-6": (3.0, 15.0),
         "claude-sonnet": (2.0, 10.0), "claude-haiku": (1.0, 5.0),
         "gpt-6-astra": (10.0, 50.0), "gpt-5.6-sol": (5.0, 30.0),
         "gpt-5.6-terra": (2.0, 12.0), "gpt-5.6-luna": (0.2, 1.2)}


def price(model):
    # ponytail: assumes a codex tune bills as its base model; own rows if not
    model = (model or "").replace("-codex-", "-")
    hit = [k for k in PRICE if model.startswith(k)]
    return PRICE[max(hit, key=len)] if hit else (5.0, 25.0)


def cost_of(model, u):
    pin, pout = price(model)
    return (((u.get("input_tokens") or 0)
             + (u.get("cache_creation_input_tokens") or 0) * 1.25
             + (u.get("cache_read_input_tokens") or 0) * 0.10) * pin
            + (u.get("output_tokens") or 0) * pout) / 1e6


def model_name(m):
    if not m:
        return "?"
    if m.startswith("gpt-"):  # gpt-5.6-codex-terra -> GPT-5.6 Codex Terra
        v, *rest = m[4:].split("-")
        return ("GPT-" + v + " " + " ".join(r.title() for r in rest)).strip()
    p = m.replace("claude-", "").split("-")
    return (p[0].title() + " " + ".".join(p[1:])).strip()


def text_of(msg):
    c = (msg or {}).get("content")
    if isinstance(c, str):
        return c
    for b in reversed(c or []):
        if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
            return b["text"]
    return ""


# Parsed transcripts persist across runs, so a restart only re-reads files
# that changed. The first run of a day still re-reads them all: "today" moved.
SCAN_CACHE = os.path.join(CACHE, "scan.json")
SCAN_VERSION = 3  # bump when a reader's output changes shape
_SCAN = None  # path -> [key, session]; collect() loads it from SCAN_CACHE
_SCAN_DIRTY = False


def scan(path, day_start):
    """The agent's reader, cached by mtime + size. Age is the one field that
    moves while the file sits still, so it is set fresh on every call."""
    global _SCAN_DIRTY
    newest, size = stamp(path)
    key = [newest, size, day_start]  # a list: it round-trips JSON
    hit = _SCAN.get(path)
    if not hit or hit[0] != key:
        _SCAN[path] = hit = [key, AGENTS[agent_of(path)]["read"](path, day_start)]
        _SCAN_DIRTY = True
    if hit[1].get("phase") == "tool":  # a running subagent writes its own file
        newest = max([newest] + [os.path.getmtime(p) for p in
                                 glob.glob(path[:-6] + "/subagents/*.jsonl")])
    hit[1]["age"] = time.time() - newest
    # the hook's word, when it's installed: asked since the transcript last moved
    hooked = None
    if hit[1].get("agent") == "claude" and os.path.isdir(WAITING):
        try:
            hooked = os.path.getmtime(os.path.join(WAITING, hit[1]["id"])) >= newest
        except OSError:
            hooked = False
    hit[1]["hooked"] = hooked
    return hit[1]


WAITING = os.path.join(CACHE, "waiting")  # one file per session the hook flagged


def hook(stdin=sys.stdin):
    """--hook, as Claude Code's Notification hook (matcher permission_prompt):
    a session stopped to ask, so mark it waiting.
    Never fails loudly - a hook that errors shows up in every session."""
    try:
        sid = json.load(stdin).get("session_id") or ""
        if UUID.fullmatch(sid):  # it becomes a file name: nothing but an id
            os.makedirs(WAITING, exist_ok=True)
            with open(os.path.join(WAITING, sid), "w"):
                pass
    except (OSError, ValueError, AttributeError):
        pass


def load_scans():
    try:
        with open(SCAN_CACHE) as f:
            got = json.load(f)
        if got.get("v") == SCAN_VERSION:
            return got["files"]
    except (OSError, ValueError, AttributeError, KeyError):
        pass  # missing or mangled: just parse again
    return {}


def save_scans(scans):
    try:  # write-then-rename, so a second ccdash never reads half a file
        os.makedirs(os.path.dirname(SCAN_CACHE), exist_ok=True)
        with open(SCAN_CACHE + ".tmp", "w") as f:
            json.dump({"v": SCAN_VERSION, "files": scans}, f)
        os.replace(SCAN_CACHE + ".tmp", SCAN_CACHE)
    except OSError:
        pass


def blank(agent):
    # phase is where the turn stands: busy, tool (call out), done (your turn)
    # or error. state() turns it plus the file's age into what a card says.
    return {"agent": agent, "title": None, "prompt": "", "asked": "", "reply": "",
            "cwd": None, "branch": None, "model": None, "effort": None, "ctx": 0,
            "cap": None, "msgs": 0, "today": dict.fromkeys(TOK, 0), "today_msgs": 0,
            "cost": 0.0, "hours": {k: [0] * 24 for k, _ in METRICS}, "days": {},
            "by_model": {}, "tool": "", "phase": None}


def tally(s, model, usage, when, day_start, cost=None):
    """One turn's usage into the day, hour and model buckets. `usage` is in
    Claude's terms: TOK keys, cache reads apart from fresh input. `cost` is
    the agent's own figure, when it keeps one; else PRICE's."""
    cost = cost_of(model, usage) if cost is None else cost
    if when and cost:
        day = when.date().isoformat()
        s["days"][day] = s["days"].get(day, 0.0) + cost
    if when is None or when.timestamp() < day_start:
        return
    for k in TOK:
        s["today"][k] += usage.get(k) or 0
    s["cost"] += cost
    m = model or "?"
    s["by_model"][m] = s["by_model"].get(m, 0.0) + cost
    h = s["hours"]
    h["cost"][when.hour] += cost
    h["output"][when.hour] += usage.get("output_tokens") or 0
    h["tokens"][when.hour] += sum(usage.get(k) or 0 for k in TOK)


def finish(s, sid):
    # a card shows well under 300 chars of these; the rest only bloats the cache
    asked = s.pop("asked")
    s["prompt"], s["reply"] = (s["prompt"] or asked)[:300], s["reply"][:300]
    s["id"] = sid
    s["title"] = s["title"] or sid[:8]
    s["project"] = os.path.basename(s["cwd"]) if s["cwd"] else "?"
    return s


def read_session(path, day_start):
    s = blank("claude")
    seen = set()  # message ids whose usage is already counted
    with open(path, errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            kind = d.get("type")
            if kind == "ai-title":
                s["title"] = d.get("aiTitle") or s["title"]
                continue
            if kind == "last-prompt":
                s["prompt"] = d.get("lastPrompt") or s["prompt"]
                continue
            if kind == "system" and d.get("subtype") == "turn_duration":
                s["phase"] = "error" if s["phase"] == "error" else "done"
                continue
            if kind not in ("user", "assistant"):
                continue
            s["msgs"] += 1
            s["cwd"] = s["cwd"] or d.get("cwd")
            s["branch"] = s["branch"] or d.get("gitBranch")
            msg = d.get("message") or {}
            usage = msg.get("usage") or {}
            when = iso(d["timestamp"]).astimezone() if d.get("timestamp") else None
            today = when is not None and when.timestamp() >= day_start
            s["today_msgs"] += today
            if kind == "user":
                said = text_of(msg)
                if said.startswith("[Request interrupted"):
                    s["phase"] = "done"
                elif not d.get("isMeta") and not said.startswith("<"):
                    s["phase"] = "busy"  # a prompt, or a tool result back
                    if said:
                        s["asked"] = said
                continue
            said = text_of(msg)
            tools = [b.get("name") or "?" for b in msg.get("content") or []
                     if isinstance(b, dict) and b.get("type") == "tool_use"]
            # a tool call is what it's doing now; a text turn means it stopped
            s["tool"] = tools[-1] if tools else "" if said else s["tool"]
            s["reply"] = said or s["reply"]
            if d.get("isApiErrorMessage"):
                s["phase"] = "error"
            elif msg.get("model") != "<synthetic>":  # Claude Code's own filler
                s["phase"] = "tool" if tools else "busy"
                s["model"] = msg.get("model") or s["model"]
            s["effort"] = d.get("effort") or s["effort"]
            ctx = sum(usage.get(k) or 0 for k in TOK if k != "output_tokens")
            s["ctx"] = ctx or s["ctx"]
            # one line per content block, each repeating the message's usage:
            # count a message id once or tokens and cost come out ~2x
            mid = msg.get("id")
            if mid in seen:
                continue
            if mid:
                seen.add(mid)
            tally(s, msg.get("model"), usage, when, day_start)
    return finish(s, os.path.basename(path)[:-6])  # the file is <session id>.jsonl


def read_codex(path, day_start):
    """A Codex rollout -> the dict read_session makes, plus the rate limits
    Codex logs with every turn.

    Usage comes as a running total that is now and then re-emitted unchanged,
    so a turn counts only when the total moves. Its input_tokens include the
    cached ones: split those out, or the cache is billed as fresh input.
    """
    s, total = blank("codex"), None
    with open(path, errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            p = d.get("payload") or {}
            kind = p.get("type") if d.get("type") in ("event_msg", "response_item") \
                else d.get("type")
            when = iso(d["timestamp"]).astimezone() if d.get("timestamp") else None
            if kind == "session_meta":
                s["cwd"] = p.get("cwd")
                s["branch"] = (p.get("git") or {}).get("branch")
            elif kind == "turn_context":
                s["model"] = p.get("model") or s["model"]
                s["effort"] = p.get("effort") or s["effort"]
            elif kind == "message" and p.get("role") in ("user", "assistant"):
                # the one form every Codex version writes (0.151 dropped
                # user_message/agent_message); user turns that open with a
                # tag are Codex's own injected context, not something typed
                said = next((c.get("text") for c in reversed(p.get("content") or [])
                             if isinstance(c, dict) and c.get("text")), "")
                if p["role"] == "user" and said.startswith("<"):
                    continue
                s["msgs"] += 1
                s["today_msgs"] += when is not None and when.timestamp() >= day_start
                if p["role"] == "user":
                    s["phase"], s["asked"] = "busy", said or s["asked"]
                    s["title"] = s["title"] or cut(said, 80) or None
                else:
                    s["tool"], s["reply"] = "", said or s["reply"]
            elif kind in ("function_call", "custom_tool_call"):
                s["phase"], s["tool"] = "tool", p.get("name") or "?"
            elif kind in ("function_call_output", "custom_tool_call_output", "task_started"):
                s["phase"] = "busy"
                s["cap"] = p.get("model_context_window") or s["cap"]
            elif kind in ("task_complete", "turn_aborted"):
                s["phase"] = "done"
            elif kind == "error":
                s["phase"] = "error"
            elif kind == "token_count":
                if p.get("rate_limits") and when:
                    s["rl"] = [when.timestamp(), p["rate_limits"]]
                info = p.get("info") or {}
                s["cap"] = info.get("model_context_window") or s["cap"]
                if not info.get("total_token_usage") or info["total_token_usage"] == total:
                    continue
                total, u = info["total_token_usage"], info.get("last_token_usage") or {}
                cached = u.get("cached_input_tokens") or 0
                s["ctx"] = u.get("input_tokens") or s["ctx"]
                tally(s, s["model"], {"input_tokens": (u.get("input_tokens") or 0) - cached,
                                      "cache_read_input_tokens": cached,
                                      "output_tokens": u.get("output_tokens") or 0},
                      when, day_start)
    return finish(s, os.path.basename(path)[-42:-6])  # rollout-<time>-<uuid>.jsonl


def opencode_db(path):
    import sqlite3  # here: a Python built without it just has no opencode

    return sqlite3.connect("file:%s?mode=ro" % urllib.request.pathname2url(path),
                           uri=True, timeout=2)


def read_opencode(path, day_start):
    """One opencode session - a row in its database, `path` being db#id.

    opencode prices each reply itself, whatever the provider (free models
    come out at 0), so its figure stands over PRICE's. Its input already
    leaves the cache out, as Claude's does.
    """
    db_path, sid = path.rsplit("#", 1)
    s = blank("opencode")
    try:
        db = opencode_db(db_path)
        try:
            row = db.execute("select directory, title from session where id = ?",
                             (sid,)).fetchone()
            msgs = db.execute("select id, data from message where session_id = ? "
                              "order by time_created", (sid,)).fetchall()
            parts = {}
            for mid, data in db.execute("select message_id, data from part where "
                                        "session_id = ? order by time_created", (sid,)):
                parts.setdefault(mid, []).append(json.loads(data))
        finally:
            db.close()
    except Exception:  # locked, or a schema this doesn't know: no card, no crash
        return finish(s, sid)
    if row:
        s["cwd"] = row[0]
        s["title"] = None if row[1].startswith("New session - ") else row[1]
    for mid, data in msgs:
        d = json.loads(data)
        t = d.get("time") or {}
        when = datetime.fromtimestamp(t["created"] / 1000).astimezone() \
            if t.get("created") else None
        mine = parts.get(mid, [])
        said = next((p["text"] for p in reversed(mine) if p.get("type") == "text"
                     and p.get("text") and not p.get("synthetic")), "")
        tools = [p for p in mine if p.get("type") == "tool"]
        s["msgs"] += 1
        s["today_msgs"] += when is not None and when.timestamp() >= day_start
        if d.get("role") == "user":
            s["phase"], s["asked"] = "busy", said or s["asked"]
            s["title"] = s["title"] or cut(said, 80) or None
            continue
        s["model"] = d.get("modelID") or s["model"]
        s["tool"] = tools[-1].get("tool") or "?" if tools else "" if said else s["tool"]
        s["reply"] = said or s["reply"]
        running = tools and (tools[-1].get("state") or {}).get("status") in ("pending", "running")
        s["phase"] = ("error" if d.get("error") else "tool" if running
                      else "busy" if not t.get("completed") or d.get("finish") == "tool-calls"
                      else "done")
        tok = d.get("tokens") or {}
        cache = tok.get("cache") or {}
        usage = {"input_tokens": tok.get("input") or 0,
                 "output_tokens": (tok.get("output") or 0) + (tok.get("reasoning") or 0),
                 "cache_read_input_tokens": cache.get("read") or 0,
                 "cache_creation_input_tokens": cache.get("write") or 0}
        s["ctx"] = sum(usage[k] for k in TOK if k != "output_tokens") or s["ctx"]
        tally(s, s["model"], usage, when, day_start, d.get("cost"))
    return finish(s, sid)


_ROWS = {}  # db#id -> update time: stamp()'s mtime for a session with no file


def opencode_rows(sid="*"):
    """opencode keeps every session in one database: each top-level one
    (a subagent's has a parent) as db#id, its time_updated its mtime."""
    if not os.path.isfile(OPENCODE):
        return []
    try:
        db = opencode_db(OPENCODE)
        try:
            rows = db.execute("select id, time_updated from session where parent_id is null"
                              + ("" if sid == "*" else " and id = ?"),
                              () if sid == "*" else (sid,)).fetchall()
        finally:
            db.close()
    except Exception:  # no sqlite3, locked, a schema this doesn't know
        return []
    for i, t in rows:
        _ROWS[OPENCODE + "#" + i] = t / 1000.0
    return [OPENCODE + "#" + i for i, _ in rows]


def stamp(path):
    """(mtime, size) of a transcript - for a database row, its update time."""
    if path in _ROWS:
        return _ROWS[path], 0
    st = os.stat(path)
    return st.st_mtime, st.st_size


# Every agent ccdash reads. Adding one is an entry here and a reader that
# returns what blank() holds: `glob` finds its transcripts (%s is the session
# id, or * for all of them) - or `list`, given the id, returns them when they
# aren't files - and `resume` + [id] reopens one in its own folder.
AGENTS = {
    "claude": {"glob": os.path.join(CLAUDE, "projects", "*", "%s.jsonl"),
               "read": read_session, "resume": ["claude", "--resume"]},
    "codex": {"glob": os.path.join(CODEX, "sessions", "*", "*", "*", "rollout-*%s.jsonl"),
              "read": read_codex, "resume": ["codex", "resume"]},
    "opencode": {"glob": OPENCODE + "#%s", "list": opencode_rows,
                 "read": read_opencode, "resume": ["opencode", "--session"]},
}


def found(agent, sid="*"):
    """One agent's transcripts, or just session `sid`'s."""
    a = AGENTS[agent]
    if "list" in a:
        return a["list"](sid)
    return glob.glob(a["glob"] % (sid if sid == "*" else glob.escape(sid)))


def transcripts(sid="*"):
    """Transcript paths of every agent, or of the one session `sid`."""
    return [p for name in AGENTS for p in found(name, sid)]


def agent_of(path):
    return next((n for n, a in AGENTS.items() if fnmatch.fnmatch(path, a["glob"] % "*")),
                "claude")


def si(n):
    for unit in ("", "k", "M", "B", "T"):
        if n < 1000 or unit == "T":
            if not unit:
                return "%d" % n
            return ("%.1f" % n).replace(".0", "") + unit
        n /= 1000.0


def ago(secs):
    for limit, unit, div in ((60, "s", 1), (3600, "m", 60), (86400, "h", 3600)):
        if secs < limit:
            return "%d%s" % (secs // div, unit)
    d, h = divmod(int(secs) // 3600, 24)
    return "%dd %dh" % (d, h)


# STATES (name, colour) lives in set_theme(); the glyph keeps state readable
# without telling green from grey.
GLYPH = {"working": "●", "waiting": "◐", "error": "✕", "idle": "○"}
ASKS = ("AskUserQuestion", "ExitPlanMode")  # tools that block on you by design


def state(s):
    """(name, colour): working, waiting on you, error, or idle."""
    age, phase, hooked = s["age"], s.get("phase"), s.get("hooked")
    # A transcript can't see a permission prompt. With the --hook installed,
    # `hooked` says; without it (None), a tool call quiet for 60s reads as
    # one - and so does a slow Bash.
    name = ("idle" if age >= 1800 else "error" if phase == "error"
            else "waiting" if hooked or phase == "done" or phase == "tool" and (
                s.get("tool") in ASKS or hooked is None and age >= 60)
            else "working" if age < 600 else "idle")  # 600: killed mid-turn
    return name, dict(STATES)[name]


# ---------------------------------------------------------------- panels

def panel_clock(rows, frame=0):
    """Art over the clock."""
    global SHOWN
    SHOWN = (rows, 0)
    art = chibi_lines(rows, frame)
    w = max(vlen(l) for l in art)
    now = datetime.now()
    body = [fg(mix(PAL["acc"], PAL["blu"], i / 4.0)) + l.center(w) + R
            for i, l in enumerate(big(now.strftime("%H:%M")))]
    date = now.strftime("%a %-d %b").upper()
    return art + ["", ""] + body + ["", MUT + date.center(w) + R]


def rule(title, w):
    """Section header: the title, then a hairline out to the column edge."""
    title = cut(title, w - 2)
    return MUT + title + " " + BOR + "─" * (w - cells(title) - 1) + R


def money(v):
    return "$%.2f" % v if v < 10 else "$%.1f" % v if v < 100 else "$%d" % v


SHORT = {"session": "5h", "weekly_all": "week", "weekly_scoped": "week", "monthly": "month"}


def advice(lims):
    """One line when an agent is running dry and another has room in the
    same kind of window, e.g. 'Claude 5h full by 15:40 · Codex 70% left'.
    Advice only: which agent to open next is yours to pick."""
    for hot in lims:
        pct, by = hot.get("percent") or 0, full_by(hot)
        kind, who = SHORT.get(hot.get("kind")), hot.get("agent", "claude")
        if not kind or pct < 85 and not by:
            continue
        room = [l for l in lims if l.get("agent", "claude") != who
                and SHORT.get(l.get("kind")) == kind and (l.get("percent") or 0) < min(pct, 85)
                and not full_by(l)]
        if room:
            best = min(room, key=lambda l: l.get("percent") or 0)
            return "%s %s %s · %s %d%% left" % (
                who.title(), kind, "full by " + by if by else "at %d%%" % pct,
                best.get("agent", "claude").title(), 100 - (best.get("percent") or 0))
    return ""


def panel_limits(w, extra=()):
    """The plan limits: Claude's from the usage endpoint, plus `extra` from
    agents that log their own (Codex)."""
    lims, err, at = limits()
    if extra and err in ("not logged in", "API key login: no plan limits"):
        err = None  # someone on Codex alone needn't hear Claude has no plan
    lims = list(lims) + list(extra)
    tag =" · %s, %s old" % (err, ago(time.time() - at)) if err and at else \
          " · " + err if err else ""
    out = [rule("LIMITS" + tag, w), ""]
    if not lims:
        return out + [DIM + "unavailable" + R, ""]
    multi = any(l.get("agent", "claude") != "claude" for l in lims)  # say whose
    tip = advice(lims)
    if tip:  # the other agent's room goes on a line of its own if need be
        out += [ACC + "→ " + TXT + cut(l, w - 2) + R
                for l in ([tip] if cells(tip) <= w - 2 else tip.split(" · "))] + [""]
    for lim in lims:
        pct = lim.get("percent") or 0
        col = GRN if pct < 60 else YEL if pct < 85 else RED
        head = TXT + limit_label(lim, multi)[:w - 5]
        out.append(pad(head, w - 4) + col + "%3d%%" % pct + R)
        f = window_frac(lim)
        out.append(bar(pct, w, f))
        by = full_by(lim)
        pace = " · full by " + by if by else " · under pace" if f else ""
        out.append(DIM + cut("resets in " + until(lim.get("resets_at")) + pace, w) + R)
        out.append("")
    return out


# model family -> theme role, so a model keeps its colour across themes
MODEL_COL = {"opus": "acc", "fable": "blu", "mythos": "red", "sonnet": "grn",
             "haiku": "yel", "gpt": "txt"}


def model_col(m):
    return PAL[MODEL_COL.get((m or "").replace("claude-", "").split("-")[0], "mut")]


def panel_today(w, data):
    t, cost = data["totals"], data["cost"]
    tiles = [(si(t["output_tokens"]), "OUTPUT"),
             (si(t["input_tokens"]), "INPUT"),
             (si(t["cache_read_input_tokens"]), "CACHE"),
             (si(t["msgs"]), "MSGS")]
    cw = w // 4
    hit = 100.0 * t["cache_read_input_tokens"] / (
        (t["input_tokens"] + t["cache_read_input_tokens"]) or 1)
    head = money(cost)
    # ponytail: "last hour" is this hour's bucket plus the unexpired share of
    # the one before - a linear guess. Per-minute buckets if it needs to be exact.
    now = datetime.now()
    hc = data["hours"]["cost"]
    burn = hc[now.hour] + (hc[now.hour - 1] * (1 - now.minute / 60.0) if now.hour else 0)
    # a usual day is an active one: weekends off shouldn't halve the baseline
    since = (now.date() - timedelta(days=13)).isoformat()
    prev = [v for d, v in data["days"].items()
            if since <= d < now.date().isoformat() and v > 0]
    pace = MUT + "burn " + TXT + money(burn) + "/h"
    if prev:
        x = cost / (sum(prev) / len(prev))
        pace += MUT + " · " + fg(ramp(x / 2)) + "%.1f×" % x + MUT + " a usual day"
    # subscription plans don't bill per token: this is what the API would charge
    out = [rule("TODAY · AT API PRICES", w), "",
           "\033[1m" + TXT + head + R + DIM + "  " + cut(
               "%d sessions · cache hit %d%%" % (data["nsess"], hit),
               w - len(head) - 2) + R,
           pace + R if vlen(pace) <= w else pace.split(MUT + " · ")[0] + R, "",
           "".join(TXT + pad(v, cw) for v, _ in tiles) + R,
           "".join(DIM + pad(k, cw) for _, k in tiles) + R]
    models = sorted(((m, c) for m, c in data["by_model"].items() if c > 0),
                    key=lambda mc: -mc[1])
    if models:
        # stacked by cost; cumulative rounding so the segments sum to w exactly
        strip, run = "", 0.0
        for m, c in models:
            n = round((run + c) / cost * w) - round(run / cost * w)
            run += c
            strip += fg(model_col(m)) + "▆" * n
        out += ["", strip + R]
        line, used = "", 0
        for m, c in models:
            item = cut("%s %s" % (model_name(m), money(c)), w - 2)
            if used and used + 2 + len(item) + 2 > w:
                out.append(line + R)
                line, used = "", 0
            line += ("  " if used else "") + fg(model_col(m)) + "● " + MUT + item
            used += (2 if used else 0) + 2 + len(item)
        out.append(line + R)
    return out + [""]


BLOCKS = " ▁▂▃▄▅▆▇█"


def vbars(vals, width, height, hi=None, upto=None):
    """Bar chart: eighth-block tops, rows shading BLU at the floor to ACC at
    the top, `hi` picked out in TXT. Empty slots up to `upto` get a baseline
    dot so the time axis stays legible. `height` rows of exactly `width` cells."""
    drop = max(0, len(vals) - width)  # too narrow for every slot: keep the newest
    vals = vals[drop:]
    hi = None if hi is None else hi - drop
    upto = None if upto is None else upto - drop
    n = max(1, len(vals))
    bw = max(1, width // n)
    top = max(vals) or 1
    out = []
    for row in range(height):  # row 0 is the top
        up = height - 1 - row
        col = fg(mix(PAL["blu"], PAL["acc"], up / max(1, height - 1)))
        line = ""
        for i, v in enumerate(vals):
            lvl = int(round(v / top * height * 8))
            ch = BLOCKS[max(0, min(8, max(lvl, v > 0) - up * 8))]  # >0 never vanishes
            c = TXT if i == hi else col
            if not v and not up and (upto is None or i <= upto):
                ch, c = "·", BOR
            line += c + ch * (bw - (bw > 1)) + " " * (bw > 1)
        out.append(pad(line + R, width))
    return out


def panel_chart(w, hours, height=4):
    key, title = METRICS[CHART]
    vals, fmt, lab = hours[key], money if key == "cost" else si, 6
    now = datetime.now().hour
    top = max(range(24), key=vals.__getitem__)
    bw = max(1, (w - lab) // 24)
    out = [rule(title + " · TODAY", w)]
    for i, row in enumerate(vbars(vals, w - lab, height, now, now)):
        out.append(DIM + pad(cut(fmt(vals[top]), lab - 1) if i == 0 else "", lab) + row)
    ticks = "".join(("%-*d" % (6 * bw, h))[:6 * bw] for h in range(0, 24, 6))
    ticks = ticks[max(0, 24 - (w - lab)) * bw:]  # vbars drops the oldest hours
    out.append(DIM + " " * lab + ticks[:w - lab] + R)  # ticks track the bars
    stats = "peak %s @%dh · now %s" % (fmt(vals[top]), top, fmt(vals[now]))
    return out + [DIM + cut(stats if vals[top] else "nothing yet today", w) + R, ""]


def panel_history(w, days):
    today = datetime.now().date()
    dates = [today - timedelta(days=d) for d in range(13, -1, -1)]
    vals = [days.get(d.isoformat(), 0.0) for d in dates]
    lab = 6
    bw = max(1, (w - lab) // 14)
    out = [rule("LAST 14 DAYS · %s" % money(sum(vals)), w)]
    for i, row in enumerate(vbars(vals, w - lab, 4, 13)):
        out.append(DIM + pad(cut(money(max(vals)), lab - 1) if i == 0 else "", lab) + row)
    names = "".join(d.strftime("%a")[0].ljust(bw) for d in dates)
    out.append(DIM + " " * lab + names[:w - lab] + R)
    return out + [DIM + cut("avg %s/day" % money(sum(vals) / 14), w) + R, ""]


def middle(data, w, height):
    """Stats column. The hourly chart grows into spare rows; when even its
    smallest size won't fit, the 14-day history goes first."""
    base = panel_limits(w, data.get("limits", ())) + panel_today(w, data)
    hist = panel_history(w, data["days"])
    room = height - len(base) - len(hist) - 4  # chart = header+ticks+stats+gap
    if room < 4:
        hist, room = [], room + len(hist)
    return base + panel_chart(w, data["hours"], max(4, min(8, room))) + hist


def card(s, w, picked=False):
    label, col = state(s)
    inner = w - 4
    # ponytail: Claude transcripts don't say which context window a session
    # has, so non-Haiku is assumed 1M. Codex logs its own; so could a hook.
    cap = s.get("cap") or (200000 if "haiku" in (s["model"] or "") else 1000000)
    pct = 100.0 * s["ctx"] / cap
    ctx = "%s/%s %d%%" % (si(s["ctx"]), si(cap), pct)
    age = ago(s["age"])
    agent = s.get("agent", "claude")
    badge = ("" if agent == "claude" else agent + " · ") + "%s %s" % (
        model_name(s["model"]), s["effort"] or "")
    said = inner - len(agent) - 2
    doing = ""
    if s.get("tool"):  # mid tool call: say which, before the last words
        tool = cut(s["tool"], max(1, said // 2))
        doing, said = ACC + "› " + tool + " ", said - cells(tool) - 3
    rows = [
        pad(col + GLYPH[label] + " " + label.upper() + R, inner - len(age)) + DIM + age + R,
        (MUT if label == "idle" else TXT) + cut(s["title"], inner) + R,
        DIM + cut(s["project"] + ("@" + s["branch"] if s["branch"] else ""),
                  inner) + R,
        MUT + "you: " + DIM + cut(s["prompt"], inner - 5) + R,
        MUT + agent + ": " + doing + DIM + (cut(s["reply"], said) if said > 0 else "") + R,
        DIM + cut("%s today · %d msgs" % (money(s.get("cost", 0.0)), s["msgs"]), inner) + R,
        bar(pct, inner - len(ctx) - 2) + DIM + "  " + ctx + R,
        DIM + cut(badge.strip(), inner) + R,
    ]
    rim = {"working": mix(PAL["grn"], BG, 0.45), "waiting": mix(PAL["yel"], BG, 0.45),
           "error": mix(PAL["red"], BG, 0.45)}
    edge = ACC if picked else fg(rim[label]) if label in rim else BOR
    tl, tr, bl, br, h, v = "┏┓┗┛━┃" if picked else "╭╮╰╯─│"  # run() finds ┏
    body = [edge + v + " " + R + pad(r, inner) + edge + " " + v + R for r in rows]
    return ([edge + tl + h * (w - 2) + tr + R] + body
            + [edge + bl + h * (w - 2) + br + R])


CARD_H = 10  # 8 rows + 2 borders; keep in step with card()


def side_by_side(panels, gap):
    h = max(len(p) for p in panels)
    widths = [max([vlen(l) for l in p] or [0]) for p in panels]
    out = []
    for i in range(h):
        row = [pad(p[i] if i < len(p) else "", widths[j])
               for j, p in enumerate(panels)]
        out.append((" " * gap).join(row).rstrip())
    return out


# ------------------------------------------------------------------ draw

GAP, MIN_CW, MAX_CW, MIN_MW, MAX_MW = 2, 30, 60, 28, 40
MAX_CARDS = 24
SEL = None  # id of the session run() has picked out
GRID = (1, 0)  # (columns, cards) the last grid_of() laid out, for moving SEL


def fit_clock(budget, height, frame=0):
    """Biggest art that fits both the height and `budget` cells of width.

    No fixed row ceiling: the source is far denser than any terminal, so on a
    big window the only sane limits are the two the layout actually has.
    Width only grows with rows, so bisect on the real rendered width - an
    estimate from a small render rounds up and leaves the art rows short.
    """
    lo, hi = 6, max(6, height - 9)  # panel_clock adds 9 rows under the art
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if max(vlen(l) for l in panel_clock(mid, frame)) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return panel_clock(lo, frame)


def grid_of(sessions, width, rows):
    """Card grid sized to fill `width`, under a tally of session states."""
    global GRID
    cols = max(1, (width + GAP) // (MIN_CW + GAP))
    cw, wide = divmod(width - (cols - 1) * GAP, cols)  # `wide` cards get +1 cell
    if cw >= MAX_CW:
        cw, wide = MAX_CW, 0
    picked = sessions[:cols * rows]
    GRID = (cols, len(picked))
    shown = [(n, c, v) for n, c in STATES
             for v in [sum(state(s) == (n, c) for s in picked)] if v]
    tally = "  ".join("%s %d %s" % (GLYPH[n], v, n) for n, _, v in shown)
    head = MUT + "SESSIONS  " + "  ".join("%s%s %d %s" % (c, GLYPH[n], v, n)
                                          for n, c, v in shown)
    if 10 + cells(tally) + 2 <= width:
        head += " " + BOR + "─" * (width - 10 - cells(tally) - 1) + R
    else:
        head = rule("SESSIONS  " + tally, width)
    out = [head, ""] + ([] if picked else [DIM + "no sessions" + R])
    for r in range(rows):
        band = [card(s, cw + (i < wide), SEL is not None and s.get("id") == SEL)
                for i, s in enumerate(picked[r * cols:(r + 1) * cols])]
        if band:
            out += side_by_side(band, GAP) + [""]
    return out


def collect():
    """Everything that touches disk. Kept apart so a reflow costs nothing."""
    global _SCAN, _SCAN_DIRTY
    if _SCAN is None:
        _SCAN = load_scans()
    day_start = datetime.now().replace(hour=0, minute=0, second=0,
                                       microsecond=0).timestamp()
    when = {f: stamp(f)[0] for f in transcripts()}
    files = sorted(when, key=when.get, reverse=True)
    # ponytail: the 14-day history parses ~270MB (~1.5s) whenever SCAN_CACHE
    # is cold - first run of each day - then only files whose mtime moved.
    recent = [f for f in files if when[f] >= day_start - 13 * 86400]
    # the grid shows what fits; the rest is what the / filter searches.
    # sidechain/subagent logs have no assistant turn - nothing to show on a card
    pool = set(files[:MAX_CARDS * 3]) | set(recent)
    picked = [s for s in (scan(f, day_start) for f in files if f in pool)
              if s["model"]]

    totals = dict.fromkeys(TOK, 0)
    totals["msgs"] = 0
    hours = {k: [0] * 24 for k, _ in METRICS}
    cost, days, by_model = 0.0, {}, {}
    for f in recent:
        s = scan(f, day_start)
        for k in TOK:
            totals[k] += s["today"][k]
        totals["msgs"] += s["today_msgs"]
        cost += s["cost"]
        for k in hours:
            hours[k] = [a + b for a, b in zip(hours[k], s["hours"][k])]
        for src, dst in ((s["days"], days), (s["by_model"], by_model)):
            for k, v in src.items():
                dst[k] = dst.get(k, 0.0) + v
    nsess = sum(when[f] >= day_start for f in recent)
    # agents that log their own limits (Codex): the newest snapshot is current
    rl = max((s["rl"] for s in picked if s.get("rl")), key=lambda r: r[0], default=None)
    # keep only what this pass looked at, so neither copy grows forever
    _SCAN = {f: _SCAN[f] for f in pool}
    if _SCAN_DIRTY:
        save_scans(_SCAN)
        _SCAN_DIRTY = False
    return {"sessions": picked, "totals": totals, "cost": cost, "hours": hours,
            "days": days, "by_model": by_model, "nsess": nsess, "at": time.time(),
            "limits": codex_limits(rl[1]) if rl else []}


def find_session(sid):
    """One session by id, wherever its project folder is; None if no transcript."""
    global _SCAN
    if _SCAN is None:
        _SCAN = load_scans()  # read only: the dashboard is the one that saves
    day_start = datetime.now().replace(hour=0, minute=0, second=0,
                                       microsecond=0).timestamp()
    hit = transcripts(sid)
    return scan(hit[0], day_start) if hit else None


def render(data, width, height, frame=0):
    """Pure layout: same data, any terminal. May overflow height - we scroll."""
    mw = max(MIN_MW, min(MAX_MW, width // 4))

    if width < 60:  # no room for the art alongside anything
        mid = middle(data, mw, height - CARD_H - 4)
        rows = max(1, (height - len(mid) - 4) // (CARD_H + 1))
        return mid + grid_of(data["sessions"], width, rows)

    left = fit_clock(max(20, width // 3), height, frame)
    if width < 100:  # art and stats share the top, cards get the full width
        mid = middle(data, mw, height - CARD_H - 4)
        top = side_by_side([left, mid], GAP)
        rows = max(1, (height - len(top) - 4) // (CARD_H + 1))
        return top + [""] + grid_of(data["sessions"], width, rows)

    mid = middle(data, mw, height)
    lw = max(vlen(l) for l in left)
    rows = max(1, (height - 2) // (CARD_H + 1))
    grid = grid_of(data["sessions"], width - lw - mw - 2 * GAP, rows)
    return side_by_side([left, mid, grid], GAP)


SIDE_W = 40  # cells tmux gives the column beside a resumed claude, padding in


def panel_open(w, wins):
    """Every session open in a tmux window, so another one finishing or
    stopping to ask shows up from inside this one."""
    out = [rule("OPEN", w)]
    for i, sid, active in wins:
        o = find_session(sid)
        name, col = state(o) if o else ("idle", DIM)
        out.append(DIM + "%d " % i + col + GLYPH[name] + " " + (TXT if active else MUT)
                   + cut(o["title"] if o else sid[:8], w - len(str(i)) - 3) + R)
    if OURS:
        out.append(DIM + cut("alt+0 ccdash · alt+number jumps", w) + R)
    return out + [""]


def render_side(s, width, height, frame=0, wins=()):
    """The column beside a resumed claude: art and clock, limits, the open
    sessions, then this one's card and hourly chart - the chart, the card,
    then the open list go first when the art (20 rows with its clock) would
    not fit over them."""
    w = max(20, width)
    box = card(s, w) if s else []
    # state and title, then the numbers: the you/claude rows are right beside it
    own = codex_limits(s["rl"][1]) if s and s.get("rl") else ()
    parts = ([panel_limits(w, own)] + ([panel_open(w, wins)] if wins else [])
             + ([box[:3] + box[6:] + [""], panel_chart(w, s["hours"], 3)]
                if s else [[DIM + "no transcript yet" + R]]))
    while len(parts) > 1 and sum(map(len, parts)) + 1 + 20 > height:
        parts.pop()
    info = [""] + sum(parts, [])
    clock = fit_clock(w, height - len(info), frame)
    global SHOWN  # chibi_at() lands where the centred art does
    SHOWN = SHOWN[0], (w - vlen(clock[0])) // 2
    return [" " * ((w - vlen(l)) // 2) + l for l in clock] + info


# -------------------------------------------------------------- selftest

def selftest():
    import tempfile

    global SCAN_CACHE, _SCAN  # swapped out by the scan-cache checks

    assert vlen(TXT + "abc" + R) == 3
    assert vlen(pad(ACC + "hi" + R, 6)) == 6
    assert cut("a  b", 9) == "a b" and cut("abcdef", 4) == "abc…"

    set_theme("ccdash")
    track = "\033[48;2;%d;%d;%dm" % mix(BG, PAL["bor"], 0.6)
    assert bar(0, 4).count(track) == 4 and vlen(bar(0, 4)) == 4
    assert bar(50, 4).count(track) == 2
    assert bar(150, 4).count(track) == 0, "must not overflow"
    assert ANSI.sub("", bar(100 / 32.0, 4)) == "▏   ", "one eighth of a cell"
    assert ANSI.sub("", bar(0, 10, 0.55)).index("▏") == 5, "pace marker cell"
    assert ramp(0) == PAL["grn"] and ramp(1) == PAL["red"] and ramp(0.5) == PAL["yel"]

    now = datetime.now(timezone.utc)
    assert until(None) == "?"
    assert until(now.replace(year=now.year - 1).isoformat()) == "now"
    assert until(_plus(now, 90 * 60)) == "1h 30m"
    assert until(_plus(now, 50 * 3600)) == "2d 2h"

    # 5h window, 1h elapsed at 10% -> 100% ten hours out, past the reset
    lim = {"kind": "session", "percent": 10,
           "resets_at": _plus(now, 4 * 3600)}
    assert full_by(lim) == "", full_by(lim)
    lim["percent"] = 50  # 1h in at 50% -> full one hour from now
    assert full_by(lim).endswith((now + timedelta(hours=1)).astimezone().strftime("%H:%M"))
    week = {"kind": "weekly_all", "percent": 50, "resets_at": _plus(now, 4 * 86400)}
    hits = iso(week["resets_at"]) - timedelta(days=1)
    assert full_by(week) == hits.astimezone().strftime("%a %H:%M"), \
        "3 days in at 50%: full 3 days from now, and it says which day"
    assert abs(window_frac(lim) - 0.2) < 0.01, "1h into a 5h window"
    assert window_frac({"kind": "session"}) is None

    assert si(999) == "999" and si(1500) == "1.5k" and si(2_500_000) == "2.5M"
    assert si(1_000_000) == "1M" and si(412_300) == "412.3k"
    assert ago(30) == "30s" and ago(3600) == "1h" and ago(90000) == "1d 1h"
    st = lambda age, phase=None, tool="": state({"age": age, "phase": phase, "tool": tool})[0]
    assert st(10) == st(10, "busy") == st(10, "tool") == "working"
    assert st(1e6) == st(1e6, "done") == st(900, "busy") == "idle"
    assert st(10, "done") == "waiting", "turn over: your move"
    assert st(90, "tool") == "waiting", "tool quiet a while: a permission prompt"
    assert st(5, "tool", "AskUserQuestion") == "waiting", "asks block at once"
    assert st(5, "error") == "error"
    hs = lambda age, hooked: state({"age": age, "phase": "tool", "tool": "Bash",
                                    "hooked": hooked})[0]
    assert hs(90, False) == "working", "hook installed, nothing asked: a slow Bash"
    assert hs(5, True) == "waiting", "the hook says asked: waiting at once"

    import io
    global WAITING
    real_waiting, WAITING = WAITING, os.path.join(tempfile.mkdtemp(), "waiting")
    sid = "01a07d31-cefd-7730-b892-fdd9dc9166fc"
    for junk in ('{"session_id": "../../etc/x"}', "not json", "[1]", "{}"):
        hook(io.StringIO(junk))
    assert not os.path.exists(WAITING), "only a session id becomes a file"
    path = os.path.join(os.path.dirname(WAITING), sid + ".jsonl")
    with open(path, "w") as f:
        f.write(json.dumps({"type": "assistant", "message": {"model": "claude-opus-5"}}) + "\n")
    day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    _SCAN = {}
    assert scan(path, day)["hooked"] is None, "no hook dir: the 60s guess stands"
    hook(io.StringIO(json.dumps({"session_id": sid, "message": "needs permission"})))
    assert scan(path, day)["hooked"] is True
    os.utime(path, (time.time() + 5, time.time() + 5))  # the session moved on
    assert scan(path, day)["hooked"] is False
    os.unlink(path)
    WAITING, _SCAN = real_waiting, None
    run_ = [{"id": "a", "age": 5, "phase": "done"}, {"id": "b", "age": 5, "phase": "busy"}]
    assert stopped({"a": "working"}, run_) and not stopped({"a": "waiting"}, run_)
    assert not stopped({"b": "working"}, run_), "still working: no bell"
    assert not stopped({}, run_), "a session seen for the first time: no bell"
    assert SEQ.findall("jk\033[A/\033[5~\033\033OP") == [
        "j", "k", "\033[A", "/", "\033[5~", "\033", "\033OP"], "one read, many keys"

    assert limit_label({"kind": "session"}) == "5-HOUR"
    assert limit_label({"kind": "weekly_all"}) == "WEEKLY"
    assert limit_label({"kind": "weekly_scoped",
                        "scope": {"model": {"display_name": "Fable"}}}) == "FABLE WEEKLY"

    assert model_name("claude-opus-5") == "Opus 5"
    assert model_name("claude-fable-5-1") == "Fable 5.1"
    assert price("claude-sonnet-4-6") == (3.0, 15.0), "longest prefix wins"
    assert price("claude-sonnet-5") == (2.0, 10.0)
    assert abs(cost_of("claude-opus-5", {"output_tokens": 1_000_000}) - 25) < 1e-9
    assert abs(cost_of("claude-opus-5", {"cache_read_input_tokens": 1_000_000})
               - 0.5) < 1e-9

    global LIMITS_CACHE, fetch_limits
    LIMITS_CACHE = tempfile.mktemp(suffix=".json")
    real, calls = fetch_limits, []

    def boom():
        calls.append(1)
        raise urllib.error.HTTPError(USAGE_URL, 429, "rate", None, None)

    fetch_limits = lambda: {"limits": [{"kind": "session", "percent": 4}]}
    assert any("5-HOUR" in l for l in panel_limits(32))
    fetch_limits = boom
    assert any("5-HOUR" in l for l in panel_limits(32)), "fresh cache, no refetch"
    assert not calls, "must not refetch inside the TTL"

    st = load_limits()
    st["at"] = st["ok_at"] = time.time() - LIMITS_TTL - 1
    json.dump(st, open(LIMITS_CACHE, "w"))
    panel = panel_limits(32)
    assert calls and "rate limited" in panel[0], panel[0]
    assert any("5-HOUR" in l for l in panel), "stale numbers must survive a 429"
    assert load_limits()["wait"] == LIMITS_TTL * 2, "429 must back off"

    os.unlink(LIMITS_CACHE)
    fetch_limits = boom
    assert "unavailable" in "".join(panel_limits(32)), "no cache, no numbers"
    os.unlink(LIMITS_CACHE)

    http = lambda code: urllib.error.HTTPError(USAGE_URL, code, "", None, None)
    assert why(FileNotFoundError()) == "not logged in"
    assert why(KeyError("claudeAiOauth")) == "API key login: no plan limits"
    assert why(http(401)) == "token expired: open claude" and why(http(429)) == "rate limited"
    assert why(urllib.error.URLError("dns")) == "offline" and why(ValueError()) == "ValueError"
    fetch_limits = lambda: open(os.path.join(tempfile.mkdtemp(), "none"))
    panel = panel_limits(32, [{"agent": "codex", "kind": "session", "percent": 5}])
    assert "not logged in" not in panel[0] and any("CODEX 5-HOUR" in l for l in panel), \
        "Codex alone: no nagging about a Claude login"
    assert "not logged in" in panel_limits(32)[0], "Claude alone: say why it's empty"
    os.unlink(LIMITS_CACHE)
    global CREDS
    real_creds, CREDS = CREDS, tempfile.mktemp()
    with open(CREDS, "w") as f:
        json.dump({"claudeAiOauth": {"accessToken": "tok"}}, f)
    assert claude_creds()["claudeAiOauth"]["accessToken"] == "tok"
    os.unlink(CREDS)
    CREDS = real_creds
    fetch_limits = real

    assert big("1")[0] == "  █" and len(big("12:34")) == 5
    chart = panel_chart(32, {k: [0] * 23 + [10] for k, _ in METRICS})
    assert len(chart) == 4 + 4 and "█" in chart[1], chart[1]
    assert all(vlen(l) <= 32 for l in chart), "chart must fit its column"
    rows = vbars([0, 5, 10, 0], 8, 3, hi=2, upto=2)
    assert len(rows) == 3 and {vlen(r) for r in rows} == {8}, "height x width"
    assert ANSI.sub("", rows[-1]) == "· █ █   ", "dot past zero, nothing after upto"
    assert TXT + "█" in rows[0] and ANSI.sub("", rows[0])[:4] == "    ", "hi in TXT"
    assert ANSI.sub("", vbars([1, 1000], 2, 2)[1])[0] == "▁", "tiny must not vanish"
    hist = panel_history(40, {datetime.now().date().isoformat(): 12.5})
    assert "$12.5" in ANSI.sub("", hist[0]) and all(vlen(l) <= 40 for l in hist)

    for name in THEMES:
        t = THEMES[name]
        assert set(t) == set(ROLES), name
        assert all(re.fullmatch(r"[0-9A-F]{6}", v) for v in t.values()), name
        set_theme(name)
        assert THEME == name and R.endswith("48;2;%d;%d;%dm" % BG)
    set_theme("no-such-theme")
    assert THEME == "ccdash", "unknown name falls back"
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"half": {"acc": "#ff0000", "r": "#000000"}, "junk": {"acc": "red"},
                   "list": [1]}, f)
    load_themes(f.name)
    os.unlink(f.name)
    assert THEMES["half"] == {"acc": "#ff0000"}, "unknown roles are dropped"
    assert "junk" not in THEMES and "list" not in THEMES
    set_theme("half")
    assert ACC == fg((255, 0, 0)) and TXT == fg(rgb(THEMES["ccdash"]["txt"])), \
        "missing roles come from ccdash"
    del THEMES["half"]
    global THEME_FILE
    real_file, THEME_FILE = THEME_FILE, tempfile.mktemp()
    set_theme("ccdash")
    next_theme()
    assert THEME == sorted(THEMES)[(sorted(THEMES).index("ccdash") + 1) % len(THEMES)]
    assert open(THEME_FILE).read().strip() == THEME, "t must persist"
    os.unlink(THEME_FILE)
    THEME_FILE = real_file
    set_theme("ccdash")

    px = [[(1, 2, 3), None], [None, (4, 5, 6)]]
    assert "▀" in render_pixels(px)[0] and "▄" in render_pixels(px)[0]
    assert render_pixels([[(1, 2, 3)] * 4] * 2)[0].count("2;1;2;3m") == 2, \
        "a run of one colour sends its fg and bg once, not per cell"
    assert len(render_pixels(baked_chibi())) == len(CHIBI) // 2

    assert probe("\033[6;18;9t\033[?62;4;22c") == (9, 18)
    assert probe("\033[?62;4c") == (10, 20), "no size reply: VT340 cell"
    assert probe("\033[?1;2c") is None and probe("") is None, "no 4, no sixel"
    global SIXEL  # tmux moved its cell from 16x32 to 9x19 px under a 100x40 pane
    real_ioctl, fcntl.ioctl = fcntl.ioctl, lambda *a: struct.pack("4H", 40, 100, 900, 760)
    SIXEL, _SIXELS[1, 0] = (16, 32), "stale"
    assert tuple(winsize()) == (100, 40) and SIXEL == (9, 19) and not _SIXELS, \
        "the sixel must follow the cell size a resize leaves"
    SIXEL = None
    assert tuple(winsize()) == (100, 40) and SIXEL is None, "no sixel: nothing to follow"
    fcntl.ioctl = real_ioctl
    try:
        from PIL import Image
    except ImportError:
        Image = None
    if Image:
        im = Image.new("RGB", (5, 7), (255, 0, 0))
        im.putpixel((1, 6), (0, 0, 255))
        six = sixel(im)
        assert six.startswith('\033P0;1q"1;1;5;7') and six.endswith("\033\\")
        assert ";2;100;0;0" in six and ";2;0;0;100" in six, "one register each"
        assert "!5~-" in six, "full red band, run-length coded"
        assert "@?@@@" in six and six.count("-") == 2, "row 7: red with a hole"

        # two red dots on white: the white keys away, and the gap left between
        # them after the trim must come back in the theme bg, not KEY
        global CHIBI_ART
        dots = Image.new("RGB", (8, 8), (255, 255, 255))
        dots.putpixel((1, 1), (255, 0, 0))
        dots.putpixel((5, 5), (255, 0, 0))
        real_art, CHIBI_ART = CHIBI_ART, tempfile.mktemp(suffix=".png")
        dots.save(CHIBI_ART)
        set_theme("catppuccin-latte")
        got = art_frames()[0]
        assert got.size == (5, 5) and got.getpixel((0, 0)) == (255, 0, 0)
        assert got.getpixel((4, 0)) == BG, got.getpixel((4, 0))
        set_theme("ccdash")
        assert art_frames()[0].getpixel((4, 0)) == BG, "re-themed without re-keying"
        os.unlink(CHIBI_ART)
        _KEYED.pop(CHIBI_ART)

        # real transparency wins over the flood: a white coat touching the
        # edge on a clear background survives, the clear margin is trimmed
        coat = Image.new("RGBA", (6, 6), (255, 255, 255, 0))
        for xy in ((0, 2), (1, 2), (0, 3), (1, 3)):
            coat.putpixel(xy, (255, 255, 255, 255))
        CHIBI_ART = tempfile.mktemp(suffix=".png")
        coat.save(CHIBI_ART)
        got = keyed_art(CHIBI_ART)[0]
        assert got.size == (2, 2) and got.getpixel((0, 0)) == (255, 255, 255), got.size
        os.unlink(CHIBI_ART)
        _KEYED.pop(CHIBI_ART)
        CHIBI_ART = real_art
        _CHIBI_CACHE.clear()

    art = [chibi_lines(6, f) for f in range(nframes())]
    assert len({len(f) for f in art}) == 1, "frames must be the same height"
    assert all(len({vlen(l) for l in f}) == 1 for f in art), "ragged frame"
    assert panel_clock(6, nframes() * 3 + 1)[:6] == panel_clock(6, 1)[:6], \
        "frame index must wrap, not run off the end"
    # a frame tick redraws the art alone, at the rows and column it was laid out
    global SHOWN
    SHOWN = (6, 4)
    at = lambda s: re.findall(r"\033\[(\d+);(\d+)H", s)
    assert at(chibi_at(1)) == [(str(PAD_Y + 1 + i), str(PAD_X + 5)) for i in range(6)]
    assert chibi_lines(6, 1)[5] in chibi_at(1)
    assert at(chibi_at(1, off=2, height=3)) == [(str(PAD_Y + 1 + i), str(PAD_X + 5))
                                                for i in range(3)], \
        "scrolled, the art clips to what is on screen"
    SHOWN = None
    assert chibi_at(1) == "", "no art laid out (help, narrow): nothing to draw"

    assert side_by_side([["ab"], ["c", "d"]], 1) == ["ab c", "   d"]
    box = card(_stub(), 34)
    assert len(box) == CARD_H, "CARD_H out of step with card()"
    assert {vlen(l) for l in box} == {34}, [vlen(l) for l in box]
    box = card(_stub(), 34, True)
    assert "┏" in box[0] and {vlen(l) for l in box} == {34}, "picked: heavy, same size"

    day = datetime.now().replace(hour=0, minute=0, second=0,
                                 microsecond=0).timestamp()
    ts = datetime.now(timezone.utc).isoformat()
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"type": "ai-title", "aiTitle": "hello"}) + "\n")
        f.write("{ not json\n")
        f.write(json.dumps({"type": "last-prompt", "lastPrompt": "do it"}) + "\n")
        f.write(json.dumps({"type": "user", "cwd": "/home/x/proj",
                            "gitBranch": "main", "message": {}}) + "\n")
        turn = {"type": "assistant", "timestamp": ts, "effort": "high", "message": {
            "id": "msg_1", "model": "claude-opus-5",
            "content": [{"type": "text", "text": "done"}],
            "usage": {"input_tokens": 5, "output_tokens": 10,
                      "cache_read_input_tokens": None}}}
        f.write(json.dumps(turn) + "\n")
        # the same message's next block repeats its usage - must not count twice
        turn["message"]["content"] = [{"type": "tool_use", "name": "Bash"}]
        f.write(json.dumps(turn) + "\n")
        path = f.name
    s = read_session(path, day)
    assert (s["title"], s["project"], s["branch"]) == ("hello", "proj", "main"), s
    assert (s["prompt"], s["reply"], s["msgs"]) == ("do it", "done", 3), s
    assert s["ctx"] == 5 and s["effort"] == "high" and s["tool"] == "Bash"
    assert s["phase"] == "tool" and s["id"] == os.path.basename(path)[:-6], s["phase"]
    for tail, phase in (({"type": "system", "subtype": "turn_duration"}, "done"),
                        ({"type": "user", "message": {"content": "[Request interrupted by user]"}},
                         "done"),
                        ({"type": "assistant", "isApiErrorMessage": True, "message": {
                            "model": "<synthetic>", "content": "API Error"}}, "error")):
        with open(path) as f:
            body = f.read()
        with open(path + ".2.jsonl", "w") as f:
            f.write(body + json.dumps(tail) + "\n")
        got = read_session(path + ".2.jsonl", day)
        assert got["phase"] == phase and got["model"] == "claude-opus-5", (phase, got)
        os.unlink(path + ".2.jsonl")
    assert s["today"]["output_tokens"] == 10 and s["cost"] > 0
    assert sum(s["hours"]["tokens"]) == 15 and s["today_msgs"] == 2, s["today_msgs"]
    assert sum(s["hours"]["output"]) == 10 and abs(sum(s["hours"]["cost"]) - s["cost"]) < 1e-12
    assert s["by_model"] == {"claude-opus-5": s["cost"]}
    assert s["days"] == {datetime.now().date().isoformat(): s["cost"]}

    # scan cache: survives a restart, never re-parses a still file, but the
    # age keeps moving - a cached age froze "working" on a quiet session
    real_cache, real_time = SCAN_CACHE, time.time
    SCAN_CACHE, _SCAN = tempfile.mktemp(suffix=".json"), {}
    assert agent_of(path) == "claude", "a path no agent claims reads as claude"
    first = scan(path, day)
    save_scans(_SCAN)
    _SCAN = load_scans()  # as a fresh process would
    AGENTS["claude"]["read"] = lambda *a: 1 / 0
    assert scan(path, day)["title"] == first["title"] == "hello", "from disk"
    time.time = lambda: real_time() + 3600
    assert scan(path, day)["age"] >= 3600, "age must move while the file sits"
    time.time = real_time
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 5))
    try:
        scan(path, day)
        assert False, "a touched file must be parsed again"
    except ZeroDivisionError:
        pass
    with open(SCAN_CACHE, "w") as f:
        f.write("{ nope")
    assert load_scans() == {}, "a mangled cache is just a cold one"
    with open(SCAN_CACHE, "w") as f:
        json.dump({"v": SCAN_VERSION + 1, "files": {path: first}}, f)
    assert load_scans() == {}, "an old format must not be trusted"
    os.unlink(SCAN_CACHE)
    AGENTS["claude"]["read"], SCAN_CACHE, _SCAN = read_session, real_cache, None
    os.unlink(path)

    # a Codex rollout: injected context skipped, a re-emitted usage total
    # counted once, cached input split out of input_tokens before pricing
    sid, soon = "01a07d31-cefd-7730-b892-fdd9dc9166fc", int(time.time()) + 3600
    usage = {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 100}
    tok = {"type": "event_msg", "timestamp": ts, "payload": {
        "type": "token_count",
        "info": {"total_token_usage": usage, "last_token_usage": usage,
                 "model_context_window": 258400},
        "rate_limits": {"primary": {"used_percent": 40.4, "window_minutes": 300,
                                    "resets_at": soon},
                        "secondary": {"used_percent": 10, "window_minutes": 10080,
                                      "resets_at": soon + 86400}}}}
    msg = lambda role, text: {"type": "response_item", "timestamp": ts, "payload": {
        "type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}}
    path = os.path.join(tempfile.mkdtemp(), "rollout-2026-09-07T15-46-46-%s.jsonl" % sid)
    with open(path, "w") as f:
        for d in ({"type": "session_meta", "payload": {"id": sid, "cwd": "/home/x/cx",
                                                        "git": {"branch": "dev"}}},
                  {"type": "turn_context", "payload": {"model": "gpt-5.6-terra",
                                                        "effort": "medium"}},
                  msg("user", "<environment_context>cwd</environment_context>"),
                  msg("user", "fix it"),
                  {"type": "event_msg", "payload": {"type": "task_started"}},
                  {"type": "response_item", "payload": {"type": "custom_tool_call",
                                                         "name": "exec"}},
                  tok, tok, msg("assistant", "done"),
                  {"type": "event_msg", "payload": {"type": "task_complete"}}):
            f.write(json.dumps(d) + "\n")
    s = read_codex(path, day)
    assert (s["id"], s["title"], s["project"], s["branch"]) == (sid, "fix it", "cx", "dev"), s
    assert (s["msgs"], s["phase"], s["tool"], s["reply"]) == (2, "done", "", "done"), s
    assert (s["ctx"], s["cap"], s["effort"]) == (1000, 258400, "medium"), s
    assert s["today"]["output_tokens"] == 100, "a repeated total counts once"
    assert (s["today"]["input_tokens"], s["today"]["cache_read_input_tokens"]) == (200, 800)
    assert abs(s["cost"] - (200 * 2 + 800 * 0.2 + 100 * 12) / 1e6) < 1e-12, s["cost"]
    got = codex_limits(s["rl"][1])
    assert [(l["kind"], l["percent"]) for l in got] == [("session", 40), ("weekly_all", 10)]
    assert iso(got[0]["resets_at"]).timestamp() == soon
    assert codex_limits(s["rl"][1], soon + 1)[0]["percent"] == 0, "reset since it was logged"
    assert codex_limits({"limit_id": "premium"}) == [] and codex_limits(None) == []
    assert agent_of(AGENTS["codex"]["glob"] % sid) == "codex"
    os.unlink(path)

    # opencode: sessions are rows, a subagent's has a parent and no card, a
    # reply that called tools is followed by one that answers, its cost stands
    try:
        import sqlite3
    except ImportError:
        sqlite3 = None
    if sqlite3:
        global OPENCODE
        real_oc, OPENCODE = OPENCODE, os.path.join(tempfile.mkdtemp(), "opencode.db")
        AGENTS["opencode"]["glob"] = OPENCODE + "#%s"
        db = sqlite3.connect(OPENCODE)
        db.executescript(
            "create table session (id text, parent_id text, directory text, title text,"
            " time_updated integer); create table message (id text, session_id text,"
            " time_created integer, data text); create table part (id text, message_id"
            " text, session_id text, time_created integer, data text);")
        ms = int(time.time() * 1000)
        oid = "ses_f6e7274a6ffeC3Qf81Rc5my7Fx"
        db.executemany("insert into session values (?, ?, '/home/x/oc', ?, ?)", [
            (oid, None, "New session - 2026-09-11T17:39:27.705Z", ms),
            ("ses_child0000000000000000000", oid, "subagent", ms)])
        tokens = lambda i, o, r: {"input": i, "output": o, "reasoning": 0,
                                  "cache": {"read": r, "write": 0}}
        for n, (role, extra, part) in enumerate((
                ("user", {}, {"type": "text", "text": "read note.txt"}),
                ("assistant", {"finish": "tool-calls", "tokens": tokens(12960, 99, 0),
                               "cost": 0.01},
                 {"type": "tool", "tool": "read", "state": {"status": "completed"}}),
                ("assistant", {"finish": "stop", "tokens": tokens(234, 20, 12928), "cost": 0.02},
                 {"type": "text", "text": "It says hello."}))):
            d = dict(extra, role=role, modelID="big-pickle", time={"created": ms, "completed": ms})
            db.execute("insert into message values (?, ?, ?, ?)",
                       ("m%d" % n, oid, ms + n, json.dumps(d)))
            db.execute("insert into part values (?, ?, ?, ?, ?)",
                       ("p%d" % n, "m%d" % n, oid, ms + n, json.dumps(part)))
        db.commit()
        db.close()
        rows = found("opencode")
        assert rows == [OPENCODE + "#" + oid], "the subagent's session gets no card"
        assert stamp(rows[0]) == (ms / 1000.0, 0) and agent_of(rows[0]) == "opencode"
        s = read_opencode(rows[0], day)
        assert (s["id"], s["title"], s["project"], s["model"]) == (
            oid, "read note.txt", "oc", "big-pickle"), s
        assert (s["phase"], s["tool"], s["reply"], s["msgs"]) == ("done", "", "It says hello.", 3)
        assert s["ctx"] == 234 + 12928 and s["today"]["cache_read_input_tokens"] == 12928
        assert abs(s["cost"] - 0.03) < 1e-12, "opencode's own cost, not PRICE's"
        assert UUID.fullmatch(oid), "an opencode window is a session window too"
        os.unlink(OPENCODE)
        OPENCODE, AGENTS["opencode"]["glob"] = real_oc, real_oc + "#%s"
        gone = os.path.join(tempfile.mkdtemp(), "none.db") + "#" + oid
        assert read_opencode(gone, day)["model"] is None, "no database: no card, no crash"
    assert model_name("gpt-5.6-codex-terra") == "GPT-5.6 Codex Terra"
    assert price("gpt-5.6-codex-terra") == price("gpt-5.6-terra") == (2.0, 12.0)

    # the advisor: one agent running dry while another has room
    # 20 minutes to the reset: 90% then is on pace to last, so no "full by"
    cl = {"kind": "session", "percent": 90, "resets_at": _plus(now, 1200)}
    cx = {"agent": "codex", "kind": "session", "percent": 40, "resets_at": _plus(now, 1200)}
    assert advice([cl, cx]) == "Claude 5h at 90% · Codex 60% left", advice([cl, cx])
    assert advice([cl]) == "", "one agent: nowhere else to go"
    assert advice([dict(cl, percent=20), cx]) == "", "nobody is short"
    assert advice([cl, dict(cx, kind="weekly_all")]) == "", "different windows don't compare"
    pace = dict(cl, percent=50, resets_at=_plus(now, 4 * 3600))  # full an hour from now
    assert advice([pace, cx]).startswith("Claude 5h full by "), advice([pace, cx])

    assert cells("你好") == 4 and cut("你好世界", 5) == "你好…"
    assert cut("abcdef", 4) == "abc…", "ascii truncation unchanged"

    assert grid_of([_stub()] * 6, 30, 2)[2].count("╭") == 1, "one narrow column"
    wide = grid_of([_stub()] * 6, 120, 2)
    assert wide[2].count("╭") == 3, "cards must multiply on a wide grid"
    assert vlen(wide[2]) == 120, "cards must fill the row, no ragged gutter"

    LIMITS_CACHE = tempfile.mktemp(suffix=".json")
    fetch_limits = lambda: {"limits": [{"kind": "session", "percent": 4}]}
    fake = demo_data()
    out = render(fake, 200, 60)
    assert any("CLAUDE 5-HOUR" in l for l in out) and any("CODEX 5-HOUR" in l for l in out), \
        "two agents' limits: each says whose"
    assert {s["agent"] for s in fake["sessions"]} == {"claude", "codex"}
    assert sum(any(t in ANSI.sub("", l) for l in out) for t in ("WORKING", "WAITING", "ERROR")) == 3
    for w, h in ((40, 20), (60, 24), (80, 24), (100, 30), (120, 34), (200, 60)):
        out = render(fake, w, h, w // 20)  # a different art frame at each size
        assert all(vlen(l) <= w for l in out), (w, h, max(vlen(l) for l in out))
        assert any("╭" in l for l in out), "every size must still show a card"
    assert any("LAST 14 DAYS" in l for l in render(fake, 200, 60))
    assert not any("LAST 14 DAYS" in l for l in render(fake, 120, 30)), \
        "a short screen drops the history before the hourly chart"
    for name in sorted(THEMES):  # every theme lays out the same
        set_theme(name)
        out = render(fake, 120, 34)
        assert all(vlen(l) <= 120 for l in out), name
    set_theme("ccdash")
    global SEL
    for w in (20, 60, 120):
        assert vlen(footer(w, time.time(), True)) == w, w
        assert vlen(footer(w, time.time(), True, "x" * 200, True)) == w, w
        SEL = "t"
        assert vlen(footer(w, time.time(), False, "你好")) == w, w
        SEL = None
    SEL = fake["sessions"][0]["id"]
    assert any("┏" in l for l in render(fake, 120, 34)), "the picked card stands out"
    SEL = None
    assert any("no sessions" in l for l in render(dict(fake, sessions=[]), 120, 34))
    assert all(vlen(l) <= 80 for l in help_box(80, 30))
    one = dict(_stub(), hours=fake["hours"])  # read_session always fills hours
    for w, h in ((SIDE_W, 50), (SIDE_W, 30), (24, 20), (60, 70)):
        out = render_side(one, w, h, 1)
        assert all(vlen(l) <= max(20, w) for l in out), (w, h)
        assert any("5-HOUR" in l for l in out), "limits always make the column"
    tall, short, tiny = (render_side(one, SIDE_W, h) for h in (60, 35, 28))
    assert any("╭" in l for l in tall) and any("$ / HOUR" in l for l in tall)
    assert any("╭" in l for l in short) and not any("$ / HOUR" in l for l in short), \
        "a short pane drops the chart first"
    assert not any("╭" in l for l in tiny), "then the card"
    assert len(short) <= 35 and sum("│" in l for l in tall) == 5, "state, title, $, ctx, model"
    assert any("no transcript" in l for l in render_side(None, SIDE_W, 50))
    for budget, h in ((40, 60), (60, 40), (80, 60), (40, 30)):  # biggest that fits
        got = fit_clock(budget, h)
        assert len(got) <= h and max(vlen(l) for l in got) <= budget, (budget, h)
        more = panel_clock(SHOWN[0] + 1)
        assert len(more) > h or max(vlen(l) for l in more) > budget, (budget, h)
    out = render_side(one, 60, 50)  # wide enough that the art has a margin
    assert SHOWN[1] > 0 and abs(SHOWN[1] - (60 - vlen(out[0]))) <= 1, "art centred"
    wins = [(1, "0" * 8 + "-0000" * 3 + "-" + "0" * 12, True), (12, "f" * 36, False)]
    assert UUID.fullmatch(wins[0][1]) and not UUID.fullmatch("ccdash")
    assert all(vlen(l) <= 24 for l in panel_open(24, wins)), "open list fits"
    out = render_side(one, SIDE_W, 42, 0, wins)
    assert any("OPEN" in l for l in out) and any("00000000" in l for l in out)
    assert any("╭" in l for l in out) and not any("$ / HOUR" in l for l in out), \
        "the chart gives way first"
    kid = subprocess.Popen(["true"])
    kid.wait()
    assert alive(os.getpid()) and not alive(kid.pid), "claude gone: the column goes"

    # statusline: one line, from the caches alone
    SCAN_CACHE = tempfile.mktemp(suffix=".json")
    rl = {"primary": {"used_percent": 40, "window_minutes": 300,
                      "resets_at": int(time.time()) + 3600}}
    save_scans({"a": [[0, 0, day], {"cost": 3.2}], "b": [[0, 0, day - 86400], {"cost": 99}],
                "c": [[0, 0, day], {"cost": 0, "rl": [time.time(), rl]}]})
    line = statusline(io.StringIO(""))
    assert line == "5h 4% · codex 5h 40% · $3.20 today", line
    fetch_limits = boom
    piped = {"rate_limits": {"five_hour": {"used_percentage": 23.5,
                                           "resets_at": int(time.time()) + 3600},
                             "seven_day": {"used_percentage": 41.2}}}
    line = statusline(io.StringIO(json.dumps(piped)))
    assert line.startswith("5h 24% · week 41% · codex 5h 40%"), line
    assert given_limits({"rate_limits": {"five_hour": None}}) == given_limits([]) == []
    os.unlink(SCAN_CACHE)
    SCAN_CACHE = real_cache

    # doctor on a machine with no agents, no login, no tty: says so, doesn't die
    real_agents = {n: dict(a) for n, a in AGENTS.items()}
    for a in AGENTS.values():
        a["glob"] = os.path.join(tempfile.mkdtemp(), "%s.jsonl")
    real_creds, CREDS = CREDS, tempfile.mktemp()
    real_stdin, sys.stdin = sys.stdin, io.StringIO()
    got = doctor()
    sys.stdin, CREDS = real_stdin, real_creds
    AGENTS.update(real_agents)
    assert "claude: 0 transcripts" in got and "codex: 0 transcripts" in got, got
    assert "✗ claude login: not logged in" in got, got
    os.unlink(LIMITS_CACHE)
    fetch_limits = real

    print("ok")


def _plus(now, secs):
    return datetime.fromtimestamp(now.timestamp() + secs + 1, timezone.utc).isoformat()


def _stub():
    return {"id": "s1", "title": "t", "project": "p", "branch": "main", "prompt": "u",
            "reply": "a", "model": "claude-opus-5", "effort": "high", "phase": "tool",
            "ctx": 1000, "msgs": 2, "age": 5, "cost": 1.25, "tool": "Bash"}


# ------------------------------------------------------------------ demo

DEMO = False  # --demo: made-up data, nothing read, fetched or resumed

DEMO_ROWS = (  # title, project, branch, model, effort, phase, age, tool, prompt, reply
    ("Add retry to the upload queue", "api", "main", "claude-opus-5", "high", "tool",
     5, "Bash", "retry failed uploads with exponential backoff", "Running the tests now."),
    ("Dark mode for the settings page", "web", "feat/dark", "claude-fable-5-1", "xhigh",
     "done", 40, "", "make settings respect prefers-color-scheme",
     "Done - all three themes pass the contrast check."),
    ("Migrate cron jobs to systemd timers", "infra", "main", "gpt-5.6-terra", "medium",
     "busy", 12, "", "move the nightly jobs off cron", "Writing the backup.timer unit."),
    ("Flaky test in auth middleware", "api", "fix/flaky", "claude-opus-5", "high", "tool",
     20, "AskUserQuestion", "why does test_refresh fail one run in ten?",
     "Two ways to fix the clock skew - which do you prefer?"),
    ("Profile the image resizer", "media", "perf", "gpt-5.6-sol", "high", "error", 30, "",
     "find where resize spends its time", "API Error: overloaded"),
    ("Parse the CSV exports", "tools", "main", "claude-opus-5", "high", "busy", 8, "",
     "read the bank CSVs into one table", "Handling the two date formats."),
    ("Release notes for v2.3", "docs", "main", "claude-sonnet-5", "medium", "done", 3000, "",
     "draft release notes from the merged PRs", "Drafted in CHANGELOG.md."),
    ("Bump dependencies", "web", "chore/deps", "claude-haiku-4-5", "", "done", 7200, "",
     "update everything that isn't a major bump", "Updated 14 packages, tests pass."),
)


def demo_data():
    """What collect() returns, made up: every panel has something in it and
    no real transcript is anywhere near. --demo, screenshots, the selftest."""
    now = datetime.now()
    shape = [0, 0, 0, 0, 0, 0, 0, .4, 1.2, 2.1, 2.8, 1.9, .6, 1.4, 2.6, 3.2, 2.2, 1.1,
             .9, 1.6, 1.2, .5, .2, 0]
    cost = [v if h <= now.hour else 0 for h, v in enumerate(shape)]
    today = sum(cost)
    sessions = [dict(zip(("title", "project", "branch", "model", "effort", "phase", "age",
                          "tool", "prompt", "reply"), r),
                     id="demo-%d" % i, agent="codex" if r[3].startswith("gpt") else "claude",
                     cap=258400 if r[3].startswith("gpt") else None,
                     ctx=(i * 37 % 9 + 2) * 21000, msgs=14 + i * 9,
                     cost=round(today * (8 - i) / 36, 2))
                for i, r in enumerate(DEMO_ROWS)]
    days = [18.2, 22.5, 9.1, 0, 0, 25.3, 30.1, 27.8, 19.4, 24.6, 0, 12.3, 28.9, today]
    at = lambda s: (datetime.now(timezone.utc) + timedelta(seconds=s)).isoformat()
    return {"sessions": sessions, "cost": today, "nsess": len(sessions), "at": time.time(),
            "hours": {"cost": cost, "output": [int(v * 42000) for v in cost],
                      "tokens": [int(v * 1.6e6) for v in cost]},
            "days": {(now.date() - timedelta(days=13 - i)).isoformat(): v
                     for i, v in enumerate(days)},
            "by_model": {"claude-opus-5": today * .6, "claude-fable-5-1": today * .2,
                         "gpt-5.6-terra": today * .12, "claude-sonnet-5": today * .08},
            "totals": {"output_tokens": int(today * 42000), "input_tokens": 380000,
                       "cache_creation_input_tokens": 1200000,
                       "cache_read_input_tokens": 42000000, "msgs": 412},
            "limits": [{"agent": "codex", "kind": "session", "percent": 22,
                        "resets_at": at(3 * 3600)},
                       {"agent": "codex", "kind": "weekly_all", "percent": 35,
                        "resets_at": at(4 * 86400)}]}


def demo_limits():
    """What limits() returns, made up: a 5-hour window on pace to run out."""
    at = lambda s: (datetime.now(timezone.utc) + timedelta(seconds=s)).isoformat()
    return ([{"kind": "session", "percent": 72, "resets_at": at(3 * 3600)},
             {"kind": "weekly_all", "percent": 41, "resets_at": at(3 * 86400)},
             {"kind": "weekly_scoped", "percent": 28, "resets_at": at(3 * 86400),
              "scope": {"model": {"display_name": "Fable"}}}], None, time.time())


# ------------------------------------------------------------------- tui

PAD_Y, PAD_X = 1, 2  # blank rows over everything, blank cells either side


def sync(s):
    """Write s as one synchronized update: the terminal shows all or none."""
    sys.stdout.write("\033[?2026h" + s + "\033[?2026l")
    sys.stdout.flush()


def paint(lines, height, image=""):
    """Erase as we go - clearing first is what makes a repaint flicker.

    `lines` land PAD_Y rows down and PAD_X cells in; callers lay out for
    2 * PAD_X fewer columns. `image` (the sixel chibi, from chibi_at()) goes
    on last, so the terminal never shows the text-only half.
    """
    rows = [""] * PAD_Y + [" " * PAD_X + l for l in lines]
    # R before each erase: \033[K and \033[J fill with the current bg, which
    # is how the theme background reaches every cell
    sync("\033[H" + "\r\n".join(R + l + R + "\033[K" for l in rows[:height])
         + "\033[J" + image)


def footer(width, at, more, find="", typing=False):
    if typing:  # the whole line is the filter prompt
        hint = "  ⏎ keep · esc clear"
        q = cut(find, max(1, width - 2 - cells(hint))) if find else ""
        return pad(ACC + "/" + TXT + q + ACC + "▏" + DIM
                   + hint[:max(0, width - 2 - cells(q))], width) + R
    stamp = cut("%s · updated %s" % (THEME, datetime.fromtimestamp(at).strftime("%H:%M")),
                width)
    parts = ([("q", "quit")] + ([("⏎", "resume")] if SEL else [("hjkl", "select")])
             + [("/", cut(find, 16) if find else "filter"), ("?", "help"),
                ("t", "theme"), ("g", "chart"), ("r", "refresh")]
             + ([("J/K", "scroll")] if more else []))
    # least useful hints drop off the end until the stamp fits
    while parts and sum(cells(k) + cells(a) + 3 for k, a in parts) - 1 > width - cells(stamp):
        parts.pop()
    keys = "  ".join(ACC + k + DIM + " " + a for k, a in parts)
    return DIM + keys + " " * (width - vlen(keys) - cells(stamp)) + stamp + R


KEYS = (("q", "quit"), ("hjkl ←→↑↓", "pick a card"),
        ("⏎", "open the picked session in its own tmux window"),
        ("alt+0", "in a session: back here; alt+1-9 jump to one"),
        ("/", "filter by title, project, branch or prompt"),
        ("esc", "drop the pick and filter; again to quit"),
        ("r", "refresh now"), ("t", "next theme"),
        ("g", "hourly chart: $ / output / all tokens"),
        ("J K", "scroll (PgUp PgDn too)"), ("?", "this help"))


def help_box(width, height):
    """The key list, themes and knobs, boxed and centred on an empty screen."""
    inner = max(10, min(60, width - 6))
    rows = [ACC + "KEYS" + R] + [TXT + k.ljust(10) + MUT + cut(a, inner - 10) + R
                                 for k, a in KEYS]
    rows += ["", ACC + "THEMES" + R + DIM + "  (t cycles, remembered)" + R]
    rows += [(TXT + "▸ " if n == THEME else MUT + "  ") + cut(n, inner - 2) + R
             for n in sorted(THEMES)]
    rows += ["", ACC + "CUSTOM" + R]
    rows += [DIM + l[:inner] + R for l in (  # cut() would squash the indent
        "~/.config/ccdash/themes.json:",
        '  {"mine": {"bg": "#101010", "acc": "#ff8800"}}',
        "roles: " + " ".join(ROLES),
        "CCDASH_THEME=name  CCDASH_ART=path  CCDASH_SIXEL=0")]
    rows += ["", DIM + "any key closes" + R]
    box = ([BOR + "╭" + "─" * (inner + 2) + "╮" + R]
           + [BOR + "│ " + R + pad(r, inner) + BOR + " │" + R for r in rows]
           + [BOR + "╰" + "─" * (inner + 2) + "╯" + R])
    left = " " * max(0, (width - inner - 4) // 2)
    return [""] * max(0, (height - len(box)) // 2) + [left + l for l in box]


SEQ = re.compile(r"\033\[[\d;]*[A-Za-z~]|\033O[A-Za-z]|.", re.S)  # one key
NAMED = {"\033[A": "up", "\033[B": "down", "\033[C": "right", "\033[D": "left",
         "\033[5~": "pgup", "\033[6~": "pgdn"}
_TYPED = ""  # keys read but not handed out yet


def getkey(timeout):
    """One keypress, or None on timeout. A read holding several (a paste, key
    repeat) queues them; arrows arrive as escape sequences, and any other
    sequence (F-keys, Home) is None, not an Esc."""
    global _TYPED
    if not _TYPED:
        if not select.select([sys.stdin], [], [], timeout)[0]:
            return None
        _TYPED = os.read(sys.stdin.fileno(), 64).decode("utf-8", "replace") or "q"
    key = SEQ.match(_TYPED).group()
    _TYPED = _TYPED[len(key):]
    return NAMED.get(key) if len(key) > 1 else key


def stopped(was, sessions):
    """Did a session that was working stop to wait on you, or fail?"""
    return any(was.get(s["id"]) == "working" and state(s)[0] in ("waiting", "error")
               for s in sessions)


def winsize():
    """Ask the tty, not $COLUMNS - bash only refreshes that between commands.

    The cell's size in px comes along. tmux reads our sixel at the size it
    puts in the pane's winsize, and a resize can move it: sixel still drawn
    at the startup probe's size then spills over the text around the art.
    """
    global SIXEL
    try:
        rows, cols, xp, yp = struct.unpack("4H", fcntl.ioctl(
            sys.stdout.fileno(), termios.TIOCGWINSZ, bytes(8)))
    except OSError:
        return shutil.get_terminal_size((120, 34))
    cell = (xp // cols, yp // rows) if xp and yp and cols and rows else None
    if SIXEL and cell and cell != SIXEL:
        SIXEL = cell
        _CHIBI_CACHE.clear()  # the art's blank cells and sixel follow the cell
        _SIXELS.clear()
    return os.terminal_size((cols, rows))


def run():
    """The live dashboard. Returns the session to resume, if Enter picked one."""
    global SIXEL, SHOWN, CHART, SEL
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    # decoding the art and scanning transcripts costs a second or two; say so
    sys.stdout.write("\033[?1049h\033[?25l" + R + "\033[2J" + DIM + "loading…" + R)
    sys.stdout.flush()
    data, off, painted, drawn, helping = collect(), 0, None, None, False
    find, typing, follow, resume = "", False, False, None
    try:
        tty.setcbreak(fd)
        try:
            keyed_art(CHIBI_ART)  # sixel needs Pillow and real art, not baked
            SIXEL = ask_terminal()
        except Exception:
            pass
        seen = True
        while True:
            vis = on_screen()
            if vis and not seen:
                data["at"] = 0  # back from a session: fresh numbers first
            seen = vis
            if time.time() - data["at"] >= REFRESH:
                # a snapshot, not the old dicts: scan() ages those in place
                was = {s["id"]: state(s)[0] for s in data["sessions"]}
                data = collect()
                if stopped(was, data["sessions"]):
                    sys.stdout.write("\a")
            q = find.lower()
            view = [s for s in data["sessions"] if q in " ".join(filter(None, (
                s["title"], s["project"], s["branch"], s["prompt"]))).lower()]
            size = winsize()
            width = size.columns - 2 * PAD_X
            body = max(1, size.lines - 1 - PAD_Y)
            frame = int(time.time() / FRAME) % nframes()
            now = (size, SIXEL, off, data["at"], THEME, CHART, helping, SEL, find,
                   typing, datetime.now().strftime("%H:%M"))
            if vis and now == painted and frame != drawn:
                sync(chibi_at(frame, off, body))  # the art is all that moved
                drawn = frame
            elif vis and now != painted:
                painted, drawn = now, frame
                SHOWN = None
                lines = (help_box(width, body) if helping else
                         render(dict(data, sessions=view), width, body, frame))
                if follow:  # scroll the picked card (the one drawn ┏━┓) into view
                    top = next((i for i, l in enumerate(lines) if "┏" in l), None)
                    if top is not None:
                        off = min(max(off, top + CARD_H - body), top)
                    follow = False
                off = max(0, min(off, len(lines) - body))
                # ponytail: scrolled at all, the sixel chibi just blanks - sixel
                # has no clipping. Crop the frame by off rows if scrolling gets common.
                paint(lines[off:off + body]
                      + [footer(width, data["at"], len(lines) > body, find, typing)],
                      size.lines, chibi_at(frame, off, body) if SIXEL else "")
            # doubles as the resize poll - render() is pure, so reflow is free.
            # Wake on the frame boundary: a flat FRAME sleep drifts by the
            # draw time and every few ticks skips a frame - a visible hitch.
            key = getkey(FRAME - time.time() % FRAME if vis and nframes() > 1 else 0.25)
            if helping and key:
                helping = False
                continue
            if typing:
                if key in ("\n", "\r"):
                    typing = False
                elif key == "\033":
                    typing, find = False, ""
                elif key in ("\x7f", "\b"):
                    find = find[:-1]
                elif key and len(key) == 1 and key.isprintable():
                    find += key
                continue
            ids = [s["id"] for s in view[:GRID[1]]]
            step = {"h": -1, "left": -1, "l": 1, "right": 1, "j": GRID[0],
                    "down": GRID[0], "k": -GRID[0], "up": -GRID[0]}.get(key)
            if step and ids:
                i = ids.index(SEL) + step if SEL in ids else 0
                SEL, follow = ids[max(0, min(len(ids) - 1, i))], True
            elif key in ("\n", "\r") and SEL in ids and not DEMO:
                if not os.environ.get("TMUX"):
                    resume = view[ids.index(SEL)]
                    break
                open_window(SEL)  # the dashboard stays on, in its own window
            elif key == "\033" and (SEL or find):
                SEL, find = None, ""
            elif key in ("q", "\033"):
                break
            elif key == "/":
                typing = True
            elif key == "r":
                data["at"] = 0  # the refresh at the top of the loop, bell and all
            elif key == "t":
                next_theme()
            elif key == "g":
                CHART = (CHART + 1) % len(METRICS)
            elif key == "?":
                helping = True
            half = size.lines // 2
            off = max(0, off + {"J": 1, "K": -1, "pgdn": half, "pgup": -half}.get(key, 0))
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\033[?25h\033[0m\033[?1049l")
        sys.stdout.flush()
    return resume


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # someone else's process, but it is there
    return True


def side(sid, pid):
    """render_side until claude (pid) exits or q; the pane closes with us."""
    global SIXEL, SHOWN
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    sys.stdout.write("\033[?1049h\033[?25l" + R + "\033[2J")
    s, wins, at, painted, seen = find_session(sid), open_windows(), time.time(), None, True
    drawn = None
    try:
        tty.setcbreak(fd)
        try:
            keyed_art(CHIBI_ART)
            SIXEL = ask_terminal()
        except Exception:
            pass
        while alive(pid):
            vis = on_screen()
            if time.time() - at >= REFRESH or vis and not seen:
                s, wins, at = find_session(sid), open_windows(), time.time()
            seen = vis
            size = winsize()
            frame = int(time.time() / FRAME) % nframes()
            now = (size, SIXEL, at, datetime.now().strftime("%H:%M"))
            if vis and now == painted and frame != drawn:
                sync(chibi_at(frame))
                drawn = frame
            elif vis and now != painted:
                painted, drawn, SHOWN = now, frame, None
                lines = render_side(s, size.columns - 2 * PAD_X,
                                    size.lines - 2 * PAD_Y, frame, wins)
                paint(lines, size.lines, chibi_at(frame) if SIXEL else "")
            if getkey(FRAME - time.time() % FRAME if vis and nframes() > 1 else 0.25) == "q":
                break  # also what a closed stdin reads as
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\033[?25h\033[0m\033[?1049l")
        sys.stdout.flush()


# a tmux server of our own, so these options never touch the user's tmux.
# ccdash already paints 24-bit colour, so claiming RGB costs nothing new.
# The interpreter spelled out: a pip install leaves this file without its exec bit.
ME = [sys.executable, os.path.realpath(__file__)]
TMUX = ["tmux", "-L", "ccdash"]
OURS = os.path.basename(os.environ.get("TMUX", "").split(",")[0]) == TMUX[2]
TMUX_SETUP = [["set", "-g", "status", "off"], ["set", "-g", "mouse", "on"],
              ["set", "-as", "terminal-features", ",*:RGB"],
              # no prefix: alt+0 is the dashboard (made again if it was quit),
              # alt+1-9 the windows sessions opened in. Claude Code binds neither.
              ["bind", "-n", "M-0", "new-window", "-S", "-n", "ccdash", shlex.join(ME)]
              ] + [["bind", "-n", "M-%d" % i, "select-window", "-t", ":%d" % i]
                   for i in range(1, 10)]


def tmux_out(*args):
    """Lines a tmux command prints - to the server $TMUX names - or [] if it fails."""
    try:
        return subprocess.run(["tmux"] + list(args), capture_output=True,
                              text=True, timeout=2).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []


_ON = [0.0, True]  # when on_screen() last asked tmux, and what it said


def on_screen():
    """Is this pane's window the one being looked at? Asked of tmux at most
    once a second; outside tmux, always. Hidden, the loops stop painting: a
    sidebar nobody sees still costs tmux every sixel frame sent to it."""
    pane = os.environ.get("TMUX_PANE")
    if pane and time.time() - _ON[0] >= 1:
        _ON[:] = [time.time(), tmux_out("display", "-p", "-t", pane,
                  "#{&&:#{window_active},#{session_attached}}") != ["0"]]
    return _ON[1]


# a session id, as Claude and Codex (uuid) or opencode (ses_...) write one
UUID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}|ses_[0-9A-Za-z]{20,40}")


def open_windows():
    """(index, session id, active) for the windows open_window() made in
    this tmux session - they are named by session id."""
    out = []
    for line in tmux_out("list-windows", "-F",
                         "#{window_index} #{window_active} #{window_name}"):
        i, act, name = line.split(" ", 2)
        if UUID.fullmatch(name):
            out.append((int(i), name, act == "1"))
    return out


def window_cmd(sid):
    # -S: an open session is just switched to - two claudes on one
    # transcript would trample each other
    return ["new-window", "-S", "-n", sid, shlex.join(ME + ["--here", sid])]


def open_window(sid):
    tmux_out(*window_cmd(sid))


def enter_tmux(sid=None):
    """Outside tmux: become a client of our server's "ccdash" session - made
    with the dashboard as window 0, or just reattached - opening sid there.
    Returns only if there is no tmux to run."""
    # a running server keeps the env it started with: hand ours over
    env = [a for k, v in os.environ.items() if k.startswith("CCDASH_")
           for a in ("-e", k + "=" + v)]
    cmds = [["start-server"]] + TMUX_SETUP + [
        ["new-session", "-A", "-s", "ccdash", "-n", "ccdash"] + env + [shlex.join(ME)]]
    if sid:
        cmds.append(window_cmd(sid))
    try:
        os.execvp("tmux", TMUX + [a for c in cmds for a in c + [";"]][:-1])
    except OSError:
        pass


def resume(sid, wait=False):
    """Reopen session sid with its own agent, in the session's folder. In
    tmux we become the agent, render_side split off beside it watching this
    pid; `wait` runs it as a child instead, so the dashboard comes back."""
    s = find_session(sid)
    argv = AGENTS[s["agent"] if s else "claude"]["resume"] + [sid]
    try:
        os.chdir(s and s["cwd"] or ".")
    except OSError:
        pass  # folder gone: the agent will say it can't find the session
    try:
        if wait:
            subprocess.call(argv)
            return
        if os.environ.get("TMUX"):
            # -b: the column goes left of the agent; -d: the agent keeps the focus
            tmux_out("split-window", "-hbd", "-l", str(SIDE_W),
                     shlex.join(ME + ["--side", sid, str(os.getpid())]))
        os.execvp(argv[0], argv)
    except OSError as exc:
        sys.exit("ccdash: can't start %s: %s" % (argv[0], exc))


def given_limits(d):
    """The rate_limits Claude Code hands a statusLine command on stdin (plans
    only, from a session's first reply on) -> limit dicts."""
    rl = (d.get("rate_limits") if isinstance(d, dict) else None) or {}
    return [{"kind": kind, "percent": round(w.get("used_percentage") or 0),
             "resets_at": w.get("resets_at") and datetime.fromtimestamp(
                 w["resets_at"], timezone.utc).isoformat()}
            for key, kind in (("five_hour", "session"), ("seven_day", "weekly_all"))
            for w in [rl.get(key)] if isinstance(w, dict)]


def statusline(stdin=sys.stdin):
    """--statusline, for Claude Code's statusLine: '5h 42% · week 18% · $3.20
    today'. Claude's limits come from what Claude Code pipes in, else from the
    dashboard's cache (fetched only past its TTL); cost and Codex's limits
    from the scan cache, read, never saved."""
    # ponytail: with no dashboard running the cost stops moving. A scan() here
    # would fix that, at a transcript parse per statusline refresh.
    try:
        lims = [] if stdin.isatty() else given_limits(json.load(stdin))
    except (ValueError, OSError):
        lims = []
    lims = lims or limits()[0]
    day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    scans = list(load_scans().values())
    rl = max((s["rl"] for _, s in scans if s.get("rl")), key=lambda r: r[0], default=None)
    lims = list(lims) + (codex_limits(rl[1]) if rl else [])
    parts = ["%s%s %d%%" % ("" if l.get("agent", "claude") == "claude" else l["agent"] + " ",
                            SHORT[l["kind"]], l.get("percent") or 0)
             for l in lims if l.get("kind") in SHORT and l["kind"] != "weekly_scoped"]
    parts.append(money(sum(s.get("cost", 0) for key, s in scans if key[2] == day)) + " today")
    tip = advice(lims)
    return " · ".join(parts) + (" · → " + tip if tip else "")


def doctor():
    """--doctor: what ccdash can and can't see, one line each, with the fix."""
    out = []

    def check(ok, what, fix=""):
        out.append(("✓ " if ok else "· " if ok is None else "✗ ") + what
                   + ("  → " + fix if fix and not ok else ""))

    check(sys.version_info >= (3, 8), "python %d.%d" % sys.version_info[:2], "needs 3.8+")
    try:
        import PIL
        check(True, "Pillow %s: your art, animated, sixel" % PIL.__version__)
    except ImportError:
        check(None, "no Pillow: the built-in chibi only",
              "pipx install 'ccdash[art]', or pip install Pillow")
    check(os.path.isfile(CHIBI_ART) or None, "art: " + CHIBI_ART,
          "optional: put a gif or png there, or set CCDASH_ART")
    check(bool(shutil.which("tmux")) or None, "tmux: ⏎ opens sessions beside the dashboard",
          "optional: without it ⏎ runs the agent in place")
    for name, a in AGENTS.items():
        n = len(found(name))
        check(bool(n) or None, "%s: %d transcripts under %s" % (
            name, n, a["glob"].split("%s")[0].split("*")[0].rstrip("/#")),
            "none yet - fine if you don't use %s" % name)
    try:
        oauth = claude_creds().get("claudeAiOauth")
        check(bool(oauth), "claude login: " + ("plan (limits available)" if oauth
                                               else "API key"),
              "an API key login has no plan limits to show")
    except Exception as exc:
        check(False, "claude login: " + why(exc), "run claude and /login")
    st = load_limits()
    check(not st.get("err"), "limits cache: " + (
        "fetched %s ago" % ago(time.time() - st["ok_at"]) if st.get("ok_at") else "empty")
        + (", last try: " + st["err"] if st.get("err") else ""),
        "retries on its own, backing off to %dm" % (LIMITS_MAX_WAIT // 60))
    try:
        with open(os.path.join(CLAUDE, "settings.json")) as f:
            text = f.read()
        hooked = "ccdash" in text and "--hook" in text  # a pipx name or a full path
    except OSError:
        hooked = False
    check(hooked or None, "waiting hook: " + ("installed" if hooked else "not installed"),
          "optional: exact 'waiting' - see the README's Notification hook")
    if sys.stdin.isatty() and sys.stdout.isatty():
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            cell = ask_terminal()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        check(bool(cell) or None, "sixel: " + ("%dx%d px cells" % cell if cell else "no"),
              "optional: half blocks work anywhere")
    out += ["", "config " + CONFIG, "cache  " + CACHE]
    return "\n".join(out)


# ------------------------------------------------------------------ main

set_theme("ccdash")


USAGE = """usage: ccdash [--demo] [--once] [--resume SESSION_ID]
       ccdash --doctor | --statusline | --hook | --selftest | --version

  --demo        made-up sessions: try it out, or take a screenshot
  --once        print one frame and exit (also what a pipe gets)
  --resume ID   open that session (Claude or Codex) straight away
  --doctor      what ccdash can see, and how to fix what it can't
  --statusline  one line for Claude Code's statusLine setting
  --hook        Claude Code Notification hook: exact 'waiting' cards
  --selftest    run the built-in tests"""


def main():
    global DEMO, collect, limits
    args = sys.argv[1:]
    if "--hook" in args:  # first: it runs on every notification
        return hook()
    if "--selftest" in args:
        return selftest()
    if "--version" in args:
        return print("ccdash " + __version__)
    if "--statusline" in args:
        return print(statusline())
    if "--doctor" in args:
        return print(doctor())
    load_themes()
    set_theme(pick_theme())
    if "-h" in args or "--help" in args:
        return print(
            __doc__.strip() + "\n\n" + USAGE +
            "\n\nIn tmux (a server of its own, -L ccdash) the dashboard is window 0;"
            "\n⏎ on a card, or --resume, opens that session in a window of its own"
            "\nwith a column of art, limits and stats beside it (q closes it). alt+0"
            "\ncomes back here, alt+1-9 jump between sessions; run ccdash again to"
            "\nreattach. Without tmux, ⏎ runs the agent in place and quitting it"
            "\ncomes back to the dashboard."
            "\n\nkeys:\n"
            + "\n".join("  %-10s %s" % ka for ka in KEYS)
            + "\n\nthemes: %s\n  `t` cycles and remembers (%s); CCDASH_THEME=name"
            "\n  overrides. Your own go in %s as"
            '\n  {"name": {"bg": "#101010", "acc": "#ff8800", ...}}; roles:'
            "\n  %s - missing ones come from ccdash."
            % (", ".join(sorted(THEMES)), THEME_FILE, USER_THEMES, " ".join(ROLES))
            + "\n\nart: %s, or CCDASH_ART=path; an animated gif plays, a still"
            "\n  image sits there, no file means the built-in chibi. Terminals with"
            "\n  sixel get real pixels; CCDASH_SIXEL=0 forces half blocks."
            "\n\nCosts are what the API would charge; a subscription doesn't bill per"
            "\ntoken. Limits are cached in %s (delete it to refetch); the"
            "\nusage endpoint rate-limits, so ccdash backs off to %dm on error."
            % (CHIBI_ART, LIMITS_CACHE, LIMITS_MAX_WAIT // 60))

    if "--demo" in args:  # nothing read, fetched or resumed
        DEMO, collect, limits = True, demo_data, demo_limits

    if args[:1] in (["--resume"], ["--here"], ["--side"]):
        if len(args) != (3 if args[0] == "--side" else 2):
            sys.exit("usage: ccdash --resume SESSION_ID")
        if args[0] == "--side":
            return side(args[1], int(args[2]))
        if args[0] == "--here":  # what a window open_window() made runs
            return resume(args[1])

    if "--once" in args or not sys.stdout.isatty():
        size = shutil.get_terminal_size((120, 34))
        return print("\n".join(render(collect(), size.columns, size.lines)))

    if DEMO:
        return run()
    sid = args[1] if args[:1] == ["--resume"] else None
    if not os.environ.get("TMUX"):
        enter_tmux(sid)  # back only when there is no tmux: the plain way, then
        if sid:
            return resume(sid)
    elif sid:
        return open_window(sid)
    while True:  # no tmux: ⏎ runs the agent here, and quitting it comes back
        s = run()
        if not s:
            break
        resume(s["id"], wait=True)


if __name__ == "__main__":
    main()
