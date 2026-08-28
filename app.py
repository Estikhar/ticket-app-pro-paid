"""
================================================================================
 A FOR AMITABH  ·  The Vvineet Chaudhary Show
 PREMIUM PAID TICKETING  —  2-step async payment verification
================================================================================
 FLOW
   Tab 1  Book        pick a seat -> see the tier price -> pay by UPI ->
                      submit Name / Phone / 12-digit UTR -> Pending_Verification
   Tab 2  Download    enter phone -> if Booked, the JPEG pass is generated,
                      auto-downloaded and shown inline
   Admin              verification queue (approve / reject), live pricing
                      controller, gate check-in scanner, database reset

 No PDF libraries. The pass is drawn with Pillow and saved as JPEG.

 SEATING NOTE
   Rows A,B,C,E,F,G,H,I,J carry seats 1-20; Row D carries 1-10. Total 190.
   The pre-block list includes D11-D20, which do not exist under that matrix.
   Blocking is intersected with the real seat set and skipped entries are
   reported in Admin rather than silently dropped. Both readings of the spec
   (D=10 or D=20) yield the same 157 sellable seats.
================================================================================
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import urllib.request
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import pandas as pd
import qrcode
import streamlit as st
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from streamlit.components.v1 import html as components_html
from streamlit_gsheets import GSheetsConnection

# QR decoding for the gate scanner is optional — the rest of the app must run
# without it rather than refusing to start.
try:
    import cv2
    import numpy as np

    HAS_CV2: Final[bool] = True
except Exception:  # noqa: BLE001
    HAS_CV2: Final[bool] = False


# =============================================================================
# 1. SEATING MATRIX
# =============================================================================

ROW_CAPACITIES: Final[dict[str, int]] = {
    "A": 20, "B": 20, "C": 20, "D": 10, "E": 20,
    "F": 20, "G": 20, "H": 20, "I": 20, "J": 20,
}
SEAT_ORDER: Final[list[str]] = [
    f"{row}{num}" for row, cap in ROW_CAPACITIES.items() for num in range(1, cap + 1)
]
SEAT_RANK: Final[dict[str, int]] = {seat: i for i, seat in enumerate(SEAT_ORDER)}
TOTAL_SEATS: Final[int] = len(SEAT_ORDER)  # 190

# House kills — seeded as Booked so they can never be sold.
PRE_BLOCKED_RANGES: Final[dict[str, range]] = {
    "A": range(6, 15),    # A6  - A14
    "D": range(11, 21),   # D11 - D20  <- does not exist; reported, not silent
    "G": range(1, 15),    # G1  - G14
    "H": range(1, 11),    # H1  - H10
}
_REQUESTED_BLOCKS: Final[set[str]] = {
    f"{row}{num}" for row, rng in PRE_BLOCKED_RANGES.items() for num in rng
}
BLOCKED_SEATS: Final[set[str]] = {s for s in _REQUESTED_BLOCKS if s in SEAT_RANK}
IMPOSSIBLE_BLOCKS: Final[list[str]] = sorted(
    _REQUESTED_BLOCKS - BLOCKED_SEATS, key=lambda s: (s[0], int(s[1:]))
)
SELLABLE_SEATS: Final[int] = TOTAL_SEATS - len(BLOCKED_SEATS)  # 157
BLOCKED_MARK: Final[str] = "-- HOUSE BLOCK --"

# --- Pricing tiers -----------------------------------------------------------
ROW_TIER: Final[dict[str, str]] = {
    "A": "VVIP", "B": "VVIP",
    "C": "VIP", "D": "VIP", "E": "VIP", "F": "VIP", "G": "VIP",
    "H": "PREMIUM", "I": "PREMIUM", "J": "PREMIUM",
}
TIER_ORDER: Final[tuple[str, ...]] = ("VVIP", "VIP", "PREMIUM")
DEFAULT_PRICES: Final[dict[str, int]] = {"VVIP": 5000, "VIP": 2400, "PREMIUM": 1000}


# =============================================================================
# 2. SHEET SCHEMA
# =============================================================================

WORKSHEET: Final[str] = "passes"
SCHEMA: Final[list[str]] = [
    "seat_id", "status", "name", "phone", "utr_number", "booked_at", "checkin_time",
]
SETTINGS_WORKSHEET: Final[str] = "settings"
SETTINGS_SCHEMA: Final[list[str]] = ["tier", "price"]

AVAILABLE: Final[str] = "Available"
PENDING: Final[str] = "Pending_Verification"
BOOKED: Final[str] = "Booked"
VALID_STATUS: Final[tuple[str, ...]] = (AVAILABLE, PENDING, BOOKED)

IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")
STATS_TTL: Final[int] = 8
WRITE_ATTEMPTS: Final[int] = 4
ONE_PASS_PER_PHONE: Final[bool] = True

BASE_DIR: Final[Path] = Path(__file__).parent
HEADER_IMG: Final[Path] = BASE_DIR / "header.png"
FOOTER_IMG: Final[Path] = BASE_DIR / "footer.png"
UPI_QR_IMG: Final[Path] = BASE_DIR / "upi_qr.png"
ASSET_DIR: Final[Path] = BASE_DIR / "assets"


def cfg(key: str, default: Any = "") -> Any:
    return st.secrets.get("app", {}).get(key, default)


EVENT_NAME: Final[str] = cfg("event_name", "A for Amitabh")
EVENT_SUBTITLE: Final[str] = cfg("event_subtitle", "The Vvineet Chaudhary Show")
VENUE: Final[str] = cfg("venue", "Inder Dass Auditorium")
EVENT_DATE: Final[str] = cfg("event_date", "11 Oct 2026")
EVENT_TIME: Final[str] = cfg("event_time", "4:00 PM")
MAPS_URL: Final[str] = cfg("maps_url", "")
UPI_ID: Final[str] = cfg("upi_id", "")
VERIFY_HOURS: Final[str] = str(cfg("verify_hours", "2"))
FETCH_FONTS: Final[bool] = bool(cfg("fetch_fonts", True))

SPLASH_HOLD: Final[float] = 0.85
SPLASH_FADE: Final[float] = 0.65
REVEAL_BASE: Final[float] = 0.72

# --- Palette -----------------------------------------------------------------
GOLD_STOPS: Final[tuple[str, ...]] = (
    "#BF953F", "#FCF6BA", "#B38728", "#FBF5B7", "#AA771C",
)
GOLD_CSS: Final[str] = "linear-gradient(135deg," + ",".join(GOLD_STOPS) + ")"
GOLD: Final[str] = "#D4AF37"
GOLD_SOFT: Final[str] = "#E8CC6B"
OBSIDIAN: Final[str] = "#090B10"
NEON: Final[str] = "#34D07A"
AMBER: Final[str] = "#F0A93B"
# Forms use a luminous green channel on purpose: gold is the brand, green is
# "actionable". Splitting the two ends the ambiguity of gold-on-gold inputs.
LIME: Final[str] = "rgba(144,238,144,1)"
LIME_TEXT: Final[str] = "rgba(200,255,200,1)"

RGB_GOLD_STOPS: Final[tuple[tuple[int, int, int], ...]] = tuple(
    (int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)) for h in GOLD_STOPS
)
RGB_GOLD: Final[tuple[int, int, int]] = (212, 175, 55)
RGB_INK: Final[tuple[int, int, int]] = (15, 15, 15)
RGB_MUTED: Final[tuple[int, int, int]] = (143, 149, 160)
RGB_TEXT: Final[tuple[int, int, int]] = (246, 243, 236)
RGB_SILVER: Final[tuple[int, int, int]] = (226, 231, 240)

# --- Ticket canvas -----------------------------------------------------------
TICKET_W: Final[int] = 1600
TICKET_H: Final[int] = 600
SS: Final[int] = 2
OUT_SCALE: Final[float] = 1.5
STUB_X: Final[int] = 1058
QR_PX: Final[int] = 232
JPEG_QUALITY: Final[int] = 94


def seat_row(seat_id: str) -> str:
    return str(seat_id)[:1].upper()


def seat_tier(seat_id: str) -> str:
    return ROW_TIER.get(seat_row(seat_id), "PREMIUM")


def now_ist() -> str:
    return datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S")


def money_text(value: Any) -> str:
    """A stray sheet value must never crash ticket generation."""
    try:
        return f"{float(str(value).replace(',', '').strip()):,.0f}"
    except (TypeError, ValueError):
        return str(value)


# =============================================================================
# 3. FONT SYSTEM
# =============================================================================
# Pillow's built-in default is a bitmap face that looks like a receipt printer.
# Two further traps, both verified by rendering:
#   * The Rupee sign (U+20B9) is NOT in every TTF. DejaVu has it, Liberation
#     does not — a silent font swap turns the price into a tofu box.
#   * Google's OFL faces ship as VARIABLE fonts. FreeType loads the default
#     instance (Regular); asking for Bold and silently getting Regular is why
#     programmatic tickets look limp. set_variation_by_name() pulls the weight.

FONT_SOURCES: Final[dict[str, str]] = {
    "Inter.ttf":
        "https://raw.githubusercontent.com/google/fonts/main/ofl/inter/"
        "Inter%5Bopsz%2Cwght%5D.ttf",
    "PlayfairDisplay.ttf":
        "https://raw.githubusercontent.com/google/fonts/main/ofl/playfairdisplay/"
        "PlayfairDisplay%5Bwght%5D.ttf",
}
SYSTEM_SANS: Final[tuple[str, ...]] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/local/lib/python3.12/dist-packages/matplotlib/mpl-data/fonts/ttf/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)
SYSTEM_SERIF: Final[tuple[str, ...]] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/local/lib/python3.12/dist-packages/matplotlib/mpl-data/fonts/ttf/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
    "C:/Windows/Fonts/georgiab.ttf",
)


def _fetch_font(name: str) -> Path | None:
    """One-time download into assets/. A ticketing portal must never fail to
    issue a pass because a CDN was slow, so every failure is swallowed."""
    target = ASSET_DIR / name
    if target.exists():
        return target
    if not FETCH_FONTS or name not in FONT_SOURCES:
        return None
    try:
        ASSET_DIR.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(FONT_SOURCES[name], timeout=12) as response:
            payload = response.read()
        if len(payload) < 20_000:
            return None
        tmp = target.with_suffix(".part")
        tmp.write_bytes(payload)
        ImageFont.truetype(str(tmp), 24)      # prove it parses before publishing
        tmp.replace(target)
        return target
    except Exception:                          # noqa: BLE001 — never fatal
        return None


@lru_cache(maxsize=8)
def _face(kind: str) -> tuple[str | None, str | None]:
    if kind == "serif":
        local = _fetch_font("PlayfairDisplay.ttf")
        if local:
            return str(local), "Bold"
        for candidate in SYSTEM_SERIF:
            if Path(candidate).exists():
                return candidate, None
        kind = "sans"
    local = _fetch_font("Inter.ttf")
    if local:
        return str(local), None
    for candidate in SYSTEM_SANS:
        if Path(candidate).exists():
            return candidate, None
    return None, None


ROLES: Final[dict[str, tuple[str, str | None]]] = {
    "title": ("serif", "Black"),
    "subtitle": ("serif", "Medium"),
    "hero": ("sans", "Black"),
    "strong": ("sans", "Bold"),
    "label": ("sans", "Medium"),
}


@lru_cache(maxsize=256)
def font(role: str, size: int) -> Any:
    face, default_weight = ROLES.get(role, ("sans", "Bold"))
    path, forced = _face(face)
    if not path:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()
    fnt = ImageFont.truetype(path, size)
    weight = forced or default_weight
    if weight:
        try:
            fnt.set_variation_by_name(weight)
        except Exception:                      # noqa: BLE001 — static face
            pass
    return fnt


@lru_cache(maxsize=16)
def font_has_glyph(role: str, char: str) -> bool:
    """Render the char beside a guaranteed-unmapped codepoint; identical
    bitmaps mean the font is drawing .notdef, so the glyph is missing."""
    probe = font(role, 48)

    def stamp(text: str) -> bytes:
        img = Image.new("L", (96, 96), 0)
        ImageDraw.Draw(img).text((6, 6), text, font=probe, fill=255)
        return img.tobytes()

    target = stamp(char)
    return target != stamp("\uFFFE") and target != stamp(" ")


def fonts_ready() -> bool:
    return _face("sans")[0] is not None


@lru_cache(maxsize=2)
def rupee() -> str:
    return "\u20b9" if font_has_glyph("strong", "\u20b9") else "Rs. "


@lru_cache(maxsize=2)
def ellipsis() -> str:
    return "\u2026" if font_has_glyph("strong", "\u2026") else "..."


# =============================================================================
# 4. DRAWING PRIMITIVES
# =============================================================================


def text_w(draw: ImageDraw.ImageDraw, text: str, fnt: Any, tracking: float = 0) -> float:
    return draw.textlength(text, font=fnt) + tracking * max(len(text) - 1, 0)


def draw_tracked(
    draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, fnt: Any,
    fill: tuple[int, int, int], tracking: float = 0, anchor: str = "la",
) -> None:
    """
    Pillow has no letter-spacing. Wide tracking on small uppercase labels is the
    single strongest "premium print" signal, so glyphs are advanced by hand.
    """
    x, y = xy
    if tracking <= 0:
        draw.text((x, y), text, font=fnt, fill=fill, anchor=anchor)
        return
    total = text_w(draw, text, fnt, tracking)
    if anchor[0] == "m":
        x -= total / 2
    elif anchor[0] == "r":
        x -= total
    vertical = anchor[1] if len(anchor) > 1 else "a"
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill, anchor="l" + vertical)
        x += draw.textlength(ch, font=fnt) + tracking


def fit_font(draw: ImageDraw.ImageDraw, text: str, role: str, max_w: float,
             start: int, minimum: int) -> Any:
    size = start
    while size > minimum and draw.textlength(text, font=font(role, size)) > max_w:
        size -= 2
    return font(role, size)


def fit_text(draw: ImageDraw.ImageDraw, text: str, role: str, max_w: float,
             start: int, minimum: int) -> tuple[Any, str]:
    """
    Shrink to fit, then TRUNCATE if it still overflows at the size floor.
    Shrinking alone bottoms out and lets a 60-character name run through the
    next column and into the stub.
    """
    fnt = fit_font(draw, text, role, max_w, start, minimum)
    if draw.textlength(text, font=fnt) <= max_w:
        return fnt, text
    clipped = text
    while clipped and draw.textlength(clipped + ellipsis(), font=fnt) > max_w:
        clipped = clipped[:-1]
    return fnt, (clipped.rstrip() + ellipsis()) if clipped else text[:1]


@lru_cache(maxsize=4)
def gold_ramp(size: tuple[int, int]) -> Image.Image:
    """The five-stop metal, as a vertical ramp. Flat gold reads like clip art."""
    w, h = size
    ramp = Image.new("RGB", (1, max(h, 2)))
    px = ramp.load()
    stops = RGB_GOLD_STOPS
    span = len(stops) - 1
    for y in range(ramp.height):
        t = y / max(ramp.height - 1, 1) * span
        i = min(int(t), span - 1)
        k = t - i
        c0, c1 = stops[i], stops[i + 1]
        px[0, y] = tuple(round(c0[j] + (c1[j] - c0[j]) * k) for j in range(3))
    return ramp.resize((max(w, 1), max(h, 1)), Image.BILINEAR)


def _text_mask(size: tuple[int, int], xy: tuple[float, float], text: str,
               fnt: Any, tracking: float, anchor: str) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw_tracked(ImageDraw.Draw(mask), xy, text, fnt, 255,
                 tracking=tracking, anchor=anchor)
    return mask


def draw_embossed(
    canvas: Image.Image, xy: tuple[float, float], text: str, fnt: Any,
    anchor: str = "la", tracking: float = 0, glow: int = 0, emboss: int = 0,
) -> None:
    """
    Foil-stamp in three passes: dark shadow down-right, metallic ramp, pale
    highlight up-left. Separate masks rather than ImageChops.offset, which wraps
    at the canvas edge and would smear a glyph onto the opposite side.
    """
    size = canvas.size
    x, y = xy
    mask = _text_mask(size, (x, y), text, fnt, tracking, anchor)

    if glow:
        halo = mask.filter(ImageFilter.GaussianBlur(glow))
        canvas.paste(Image.new("RGB", size, RGB_GOLD), (0, 0),
                     halo.point(lambda v: int(v * 0.46)))
    if emboss:
        shadow = _text_mask(size, (x + emboss, y + emboss), text, fnt, tracking, anchor)
        canvas.paste(Image.new("RGB", size, (0, 0, 0)), (0, 0),
                     shadow.point(lambda v: int(v * 0.82)))
        hi = _text_mask(size, (x - emboss * 0.5, y - emboss * 0.5), text,
                        fnt, tracking, anchor)
        canvas.paste(Image.new("RGB", size, (255, 250, 226)), (0, 0),
                     hi.point(lambda v: int(v * 0.30)))
    canvas.paste(gold_ramp(size), (0, 0), mask)


def vertical_mask(size: tuple[int, int], stops: list[tuple[float, float]]) -> Image.Image:
    """
    L mask from (position 0..1, alpha 0..1) stops. This is what keeps the header
    artwork OFF the text: alpha drops to exactly zero across the detail block,
    so contrast there is guaranteed rather than hoped for.
    """
    w, h = size
    strip = Image.new("L", (1, max(h, 2)), 0)
    px = strip.load()
    ordered = sorted(stops)
    for y in range(strip.height):
        t = y / max(strip.height - 1, 1)
        val = ordered[-1][1]
        for i in range(len(ordered) - 1):
            t0, a0 = ordered[i]
            t1, a1 = ordered[i + 1]
            if t0 <= t <= t1:
                val = a0 + (a1 - a0) * (t - t0) / max(t1 - t0, 1e-6)
                break
        px[0, y] = max(0, min(255, int(val * 255)))
    return strip.resize((max(w, 1), max(h, 1)), Image.BILINEAR)


def radial_glow(size: tuple[int, int], centre: tuple[float, float],
                radius: float, strength: float) -> Image.Image:
    w, h = size
    small = 96
    mask = Image.new("L", (small, small), 0)
    md = ImageDraw.Draw(mask)
    cx, cy = centre[0] / w * small, centre[1] / h * small
    steps = 26
    for i in range(steps, 0, -1):
        r = radius / max(w, h) * small * (i / steps)
        md.ellipse([cx - r, cy - r, cx + r, cy + r],
                   fill=int(255 * strength * (1 - i / steps) ** 1.7))
    return mask.resize((w, h), Image.BILINEAR)


def cover_fit(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale-and-crop to fill, preserving aspect ratio (never squashed)."""
    tw, th = size
    sw, sh = img.size
    scale = max(tw / sw, th / sh)
    resized = img.resize((max(1, round(sw * scale)), max(1, round(sh * scale))),
                         Image.LANCZOS)
    left, top = (resized.width - tw) // 2, (resized.height - th) // 2
    return resized.crop((left, top, left + tw, top + th))


@st.cache_data(show_spinner=False)
def load_flat_rgb(path_str: str, _mtime: float) -> bytes | None:
    """
    Flatten any PNG onto the ticket ground as RGB. JPEG has no alpha, and a
    palette PNG carrying transparency composites unpredictably — flattening once
    up front kills a whole class of "why is my banner a black rectangle" bugs.
    """
    try:
        with Image.open(path_str) as src:
            src.load()
            rgba = src.convert("RGBA")
            flat = Image.new("RGB", rgba.size, RGB_INK)
            flat.paste(rgba, (0, 0), rgba)
    except (OSError, ValueError):
        return None
    buf = io.BytesIO()
    flat.save(buf, "PNG")
    return buf.getvalue()


def header_art() -> Image.Image | None:
    if not HEADER_IMG.exists():
        return None
    raw = load_flat_rgb(str(HEADER_IMG), HEADER_IMG.stat().st_mtime)
    return Image.open(io.BytesIO(raw)).convert("RGB") if raw else None




# =============================================================================
# 5. QR + SECURITY BARCODE
# =============================================================================


def gate_payload(row: pd.Series) -> str:
    parts = ["PASS", EVENT_NAME, f"Seat: {row['seat_id']}",
             f"Name: {row['name']}", f"Phone: {row['phone']}"]
    if MAPS_URL:
        parts.append(f"Maps: {MAPS_URL}")
    return " | ".join(parts)


def qr_image(payload: str, edge_px: int) -> Image.Image:
    """
    ERROR_CORRECT_L keeps the grid sparse — higher levels add redundancy
    modules, shrinking each module at a fixed print size, the opposite of what
    a hand-held scan needs. NEAREST resize keeps module edges razor sharp;
    LANCZOS would blur them into grey mush.
    """
    qr = qrcode.QRCode(version=None, box_size=10, border=3,
                       error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return img.resize((edge_px, edge_px), Image.NEAREST)


def qr_px_per_module(payload: str, edge_px: int = round(QR_PX * OUT_SCALE)) -> float:
    probe = qrcode.QRCode(version=None, box_size=1, border=3,
                          error_correction=qrcode.constants.ERROR_CORRECT_L)
    probe.add_data(payload)
    probe.make(fit=True)
    total = probe.modules_count + 6
    return edge_px / total if total else 0.0


def ticket_digest(row: pd.Series) -> str:
    """Stable per pass — the bars MUST look identical every regeneration."""
    seed = f"{EVENT_NAME}|{row['seat_id']}|{row['phone']}|{row['booked_at']}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def draw_security_bars(draw: ImageDraw.ImageDraw, x: float, y: float,
                       w: float, h: float, digest: str, s: int) -> None:
    """
    Abstract security-print bars — the visual language premiere passes use to
    signal "official document".

    NOT machine-readable, by design and by honesty: it encodes nothing and no
    scanner will read it. It IS deterministic (hashed from seat + phone + time)
    so the same pass always renders identically; a random pattern would change
    on every regeneration, which is exactly what a forgery looks like.
    Everything scannable lives in the QR.
    """
    raw = bytes.fromhex(digest)
    cx, i = x, 0
    while cx < x + w and i < 512:
        b = raw[i % len(raw)]
        bar = (1 + (b % 4)) * s
        gap = (1 + ((b >> 3) % 3)) * s
        if cx + bar > x + w:
            break
        tall = h if (b & 0x40) else h * 0.66
        colour = RGB_GOLD if (b & 0x08) else RGB_SILVER
        draw.rectangle([cx, y + (h - tall), cx + bar, y + h], fill=colour)
        cx += bar + gap
        i += 1


# =============================================================================
# 6. THE TICKET
# =============================================================================


def _draw_background(canvas: Image.Image, s: int) -> None:
    """
    Pitch ground. The header artwork bleeds in at the TOP and BOTTOM edges only
    and is masked to zero across the middle band, so attendee details always sit
    on pure black. A full-canvas watermark is precisely what makes the type look
    muddy.
    """
    w, h = canvas.size
    art = header_art()
    if art is not None:
        bed = cover_fit(art, (w, h))
        bed = bed.filter(ImageFilter.GaussianBlur(5 * s)).point(lambda v: int(v * 0.52))
        canvas.paste(bed, (0, 0), vertical_mask(
            (w, h),
            [(0.00, 0.55), (0.13, 0.28), (0.25, 0.00),
             (0.78, 0.00), (0.91, 0.22), (1.00, 0.40)],
        ))

    gold_layer = Image.new("RGB", (w, h), RGB_GOLD)
    canvas.paste(gold_layer, (0, 0),
                 radial_glow((w, h), (w * 0.04, -h * 0.16), w * 0.58, 0.24))
    canvas.paste(gold_layer, (0, 0),
                 radial_glow((w, h), (w * 0.90, h * 1.12), w * 0.40, 0.14))

    vign = Image.new("RGB", (w, h), (0, 0, 0))
    edge = Image.new("L", (w, h), 255)
    ImageDraw.Draw(edge).rounded_rectangle(
        [w * 0.026, h * 0.05, w * 0.974, h * 0.95],
        radius=int(h * 0.10), fill=0)
    canvas.paste(vign, (0, 0),
                 edge.filter(ImageFilter.GaussianBlur(int(h * 0.07)))
                     .point(lambda v: int(v * 0.9)))


def _draw_gold_rule(canvas: Image.Image, box: tuple[float, float, float, float],
                    radius: int, width: int) -> None:
    """A rounded rect stroked with the real metal ramp, not a flat colour."""
    mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius,
                                           outline=255, width=width)
    canvas.paste(gold_ramp(canvas.size), (0, 0), mask)


def _draw_frame(canvas: Image.Image, w: int, h: int, s: int) -> None:
    inset, radius = 15 * s, 26 * s
    _draw_gold_rule(canvas, (inset, inset, w - inset, h - inset), radius, max(1, 2 * s))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        [inset + 7 * s, inset + 7 * s, w - inset - 7 * s, h - inset - 7 * s],
        radius=radius - 5 * s, outline=(122, 100, 24), width=max(1, s))
    arm = 26 * s
    for cx, cy, dx, dy in (
        (inset + 22 * s, inset + 22 * s, 1, 1),
        (w - inset - 22 * s, inset + 22 * s, -1, 1),
        (inset + 22 * s, h - inset - 22 * s, 1, -1),
        (w - inset - 22 * s, h - inset - 22 * s, -1, -1),
    ):
        draw.line([cx, cy, cx + arm * dx, cy], fill=RGB_GOLD, width=max(1, 2 * s))
        draw.line([cx, cy, cx, cy + arm * dy], fill=RGB_GOLD, width=max(1, 2 * s))


def _draw_perforation(draw: ImageDraw.ImageDraw, x: int, h: int, s: int) -> None:
    dash, gap = 13 * s, 11 * s
    y = 44 * s
    while y < h - 44 * s:
        draw.line([x, y, x, min(y + dash, h - 44 * s)],
                  fill=RGB_GOLD, width=max(1, 2 * s))
        y += dash + gap
    r = 15 * s
    for cy in (15 * s, h - 15 * s):
        draw.ellipse([x - r, cy - r, x + r, cy + r],
                     fill=RGB_INK, outline=RGB_GOLD, width=max(1, 2 * s))


def build_ticket_jpeg(row: pd.Series) -> bytes:
    s = SS
    w, h = TICKET_W * s, TICKET_H * s
    canvas = Image.new("RGB", (w, h), RGB_INK)
    _draw_background(canvas, s)
    _draw_frame(canvas, w, h, s)
    draw = ImageDraw.Draw(canvas)

    stub_x = STUB_X * s
    _draw_perforation(draw, stub_x, h, s)
    digest = ticket_digest(row)

    # ---------------------------------------------------- LEFT: the marquee
    lx = 60 * s
    right_edge = stub_x - 52 * s
    avail = right_edge - lx

    draw_tracked(draw, (lx, 50 * s), "OFFICIAL ADMISSION PASS",
                 font("label", 16 * s), RGB_GOLD, tracking=5.4 * s)

    title_font, title_text = fit_text(draw, EVENT_NAME, "title", avail,
                                      74 * s, 34 * s)
    draw_embossed(canvas, (lx, 82 * s), title_text, title_font,
                  glow=9 * s, emboss=3 * s)
    draw = ImageDraw.Draw(canvas)

    sub_font, sub_text = fit_text(draw, EVENT_SUBTITLE, "subtitle", avail,
                                  27 * s, 14 * s)
    draw_tracked(draw, (lx, 168 * s), sub_text, sub_font,
                 (206, 178, 106), tracking=1.6 * s)

    meta = f"{VENUE.upper()}   ·   {EVENT_DATE.upper()}   ·   {EVENT_TIME.upper()}"
    meta_font, meta_text = fit_text(draw, meta, "label", avail, 19 * s, 11 * s)
    draw_tracked(draw, (lx, 214 * s), meta_text, meta_font, RGB_MUTED,
                 tracking=2.3 * s)

    draw.line([lx, 250 * s, right_edge, 250 * s], fill=(122, 100, 24),
              width=max(1, s))

    col_w = (avail - 40 * s) / 2
    col2 = lx + col_w + 40 * s

    def field(x: float, y: float, label: str, value: str, max_w: float) -> None:
        draw_tracked(draw, (x, y), label, font("label", 13 * s),
                     RGB_MUTED, tracking=3.4 * s)
        vf, shown = fit_text(draw, value, "strong", max_w, 36 * s, 16 * s)
        draw.text((x, y + 25 * s), shown, font=vf, fill=RGB_TEXT)

    field(lx, 280 * s, "ATTENDEE NAME", row["name"], col_w)
    field(col2, 280 * s, "CATEGORY", f"{seat_tier(row['seat_id'])}", col_w)
    field(lx, 366 * s, "WHATSAPP", row["phone"], col_w)
    field(col2, 366 * s, "TXN / UTR", str(row.get("utr_number", "") or "—"), col_w)

    # ---------------------------------------------------- value badge
    tier = seat_tier(row["seat_id"])
    paid = money_text(row.get("_price", tier_price(tier)))
    badge = f"{tier}  ·  {rupee()}{paid} PAID  ·  VERIFIED"
    bf = font("strong", 18 * s)
    bw = text_w(draw, badge, bf, 3.0 * s) + 52 * s
    by, bh = 456 * s, 52 * s
    _draw_gold_rule(canvas, (lx, by, lx + bw, by + bh), bh // 2, max(1, 2 * s))
    draw = ImageDraw.Draw(canvas)
    draw_tracked(draw, (lx + bw / 2, by + bh / 2), badge, bf, (252, 246, 186),
                 tracking=3.0 * s, anchor="mm")

    serial = f"REF {digest[:12].upper()}"
    draw_tracked(draw, (lx + bw + 30 * s, by + bh / 2), serial,
                 font("label", 13 * s), RGB_MUTED, tracking=3.0 * s, anchor="lm")

    # ---------------------------------------------------- RIGHT: the stub
    sx = stub_x + (w - 15 * s - stub_x) / 2
    stub_w = w - 15 * s - stub_x

    draw_tracked(draw, (sx, 50 * s), "SEAT", font("label", 17 * s),
                 RGB_MUTED, tracking=8 * s, anchor="ma")

    seat = str(row["seat_id"])
    seat_font = fit_font(draw, seat, "hero", stub_w - 74 * s, 110 * s, 50 * s)
    draw_embossed(canvas, (sx, 72 * s), seat, seat_font, anchor="ma",
                  glow=13 * s, emboss=4 * s)
    draw = ImageDraw.Draw(canvas)

    plate = 252 * s
    py = 206 * s
    _draw_gold_rule(canvas, (sx - plate / 2, py, sx + plate / 2, py + plate),
                    14 * s, max(1, 2 * s))
    draw = ImageDraw.Draw(canvas)
    pad = 8 * s
    draw.rounded_rectangle(
        [sx - plate / 2 + pad, py + pad, sx + plate / 2 - pad, py + plate - pad],
        radius=9 * s, fill=(255, 255, 255))

    bar_w = plate - 16 * s
    draw_security_bars(draw, sx - bar_w / 2, 476 * s, bar_w, 28 * s, digest, s)

    draw_tracked(draw, (sx, 516 * s), "SCAN FOR ENTRY", font("label", 15 * s),
                 RGB_GOLD, tracking=5.4 * s, anchor="ma")
    draw_tracked(draw, (sx, 540 * s), f"{seat}  ·  NON-TRANSFERABLE",
                 font("label", 11 * s), (120, 126, 136), tracking=2.2 * s,
                 anchor="ma")

    # ---------------------------------------------------- render out
    out_w, out_h = round(TICKET_W * OUT_SCALE), round(TICKET_H * OUT_SCALE)
    final = canvas.resize((out_w, out_h), Image.LANCZOS)

    # QR goes on AFTER the downsample at its exact output size. Resampling a QR
    # is what turns crisp modules into grey mush.
    qr_out = round(QR_PX * OUT_SCALE)
    qr = qr_image(gate_payload(row), qr_out)
    cx = sx / s * OUT_SCALE
    cy = (py / s + plate / s / 2) * OUT_SCALE
    final.paste(qr, (round(cx - qr_out / 2), round(cy - qr_out / 2)))

    buf = io.BytesIO()
    # subsampling=0 (4:4:4). JPEG's default 4:2:0 chroma subsampling visibly
    # smears fine gold type and QR edges against a dark ground.
    final.save(buf, "JPEG", quality=JPEG_QUALITY, subsampling=0, optimize=True)
    return buf.getvalue()


def cached_ticket(row: pd.Series) -> bytes:
    store: dict[str, bytes] = st.session_state.setdefault("tickets", {})
    seat = str(row["seat_id"])
    if seat not in store:
        store[seat] = build_ticket_jpeg(row)
    return store[seat]




# =============================================================================
# 7. DATA LAYER  (two worksheets: passes + settings)
# =============================================================================


def get_conn() -> GSheetsConnection:
    return st.connection("gsheets", type=GSheetsConnection)


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Sheets round-trips are lossy: blanks -> NaN, phone/UTR -> '98765.0'."""
    df = df.reindex(columns=SCHEMA)
    df = df.astype(object).where(pd.notna(df), "")
    for col in SCHEMA:
        df[col] = (df[col].astype(str).str.strip()
                   .replace({"nan": "", "None": "", "NaT": "", "<NA>": ""}))
    for col in ("phone", "utr_number"):
        df[col] = df[col].str.replace(r"\.0$", "", regex=True)
    df["seat_id"] = df["seat_id"].str.upper()
    df = df[df["seat_id"] != ""]
    # Any unrecognised status is treated as Available — never as sold.
    df["status"] = df["status"].where(df["status"].isin(VALID_STATUS), AVAILABLE)
    return df.reset_index(drop=True)


def load_seats(*, fresh: bool = False) -> pd.DataFrame:
    raw = get_conn().read(worksheet=WORKSHEET, ttl=0 if fresh else STATS_TTL)
    return _normalise(pd.DataFrame(raw))


def save_seats(df: pd.DataFrame) -> None:
    get_conn().update(worksheet=WORKSHEET, data=df[SCHEMA])
    st.cache_data.clear()


def blank_layout() -> pd.DataFrame:
    """
    Fresh matrix. Pre-blocked seats are seeded as Booked with a house marker
    and no phone, so they can never be sold, never match a phone lookup, and
    never inflate the attendee count.
    """
    rows = []
    for seat in SEAT_ORDER:
        blocked = seat in BLOCKED_SEATS
        rows.append({
            "seat_id": seat,
            "status": BOOKED if blocked else AVAILABLE,
            "name": BLOCKED_MARK if blocked else "",
            "phone": "",
            "utr_number": "",
            "booked_at": now_ist() if blocked else "",
            "checkin_time": "",
        })
    return pd.DataFrame(rows)


def is_house_block(row: pd.Series) -> bool:
    """
    A house block is a seat the venue killed, not merely an unsold one.

    Testing "has no phone" is wrong — every Available seat has no phone, which
    made the gate report free seats as house blocks. Identify by the seeded
    marker, or by membership of the block set for sheets seeded before the
    marker existed.
    """
    return (str(row.get("name", "")).strip() == BLOCKED_MARK
            or str(row.get("seat_id", "")).upper() in BLOCKED_SEATS)


# --- settings / pricing -------------------------------------------------------


def load_prices(*, fresh: bool = False) -> dict[str, int]:
    """
    Live tier pricing from the `settings` worksheet, with defaults as a floor.
    A missing or malformed sheet must never stop the box office, so this falls
    back rather than raising — the Admin panel reports which source was used.
    """
    prices = dict(DEFAULT_PRICES)
    try:
        raw = get_conn().read(worksheet=SETTINGS_WORKSHEET,
                              ttl=0 if fresh else STATS_TTL)
        df = pd.DataFrame(raw).reindex(columns=SETTINGS_SCHEMA)
        df = df.astype(object).where(pd.notna(df), "")
        for _, entry in df.iterrows():
            tier = str(entry["tier"]).strip().upper()
            if tier in DEFAULT_PRICES:
                try:
                    value = int(float(str(entry["price"]).replace(",", "").strip()))
                    if value >= 0:
                        prices[tier] = value
                except (TypeError, ValueError):
                    continue
    except Exception:  # noqa: BLE001 — settings are advisory, not critical
        st.session_state["_prices_fallback"] = True
        return prices
    st.session_state["_prices_fallback"] = False
    return prices


def save_prices(prices: dict[str, int]) -> None:
    frame = pd.DataFrame(
        [{"tier": tier, "price": int(prices[tier])} for tier in TIER_ORDER]
    )
    get_conn().update(worksheet=SETTINGS_WORKSHEET, data=frame[SETTINGS_SCHEMA])
    st.cache_data.clear()


def tier_price(tier: str) -> int:
    return int(st.session_state.get("_prices", DEFAULT_PRICES).get(
        tier, DEFAULT_PRICES.get(tier, 0)))


def seat_price(seat_id: str) -> int:
    return tier_price(seat_tier(seat_id))


# =============================================================================
# 8. BOOKING STATE MACHINE
# =============================================================================
# Sheets has no transactions and conn.update() rewrites the whole worksheet, so
# every mutation is: read(ttl=0) -> mutate -> write -> read back and confirm the
# row carries OUR marker -> retry if someone landed on it first.
# Compare-and-verify, not compare-and-swap. It shrinks the window; it cannot
# close it. With money involved, the read-back is the important half.

PHONE_RE: Final[re.Pattern[str]] = re.compile(r"^[6-9]\d{9}$")
UTR_RE: Final[re.Pattern[str]] = re.compile(r"^\d{12}$")


def available_seats(df: pd.DataFrame, row_letter: str | None = None) -> list[str]:
    free = df[df["status"] == AVAILABLE]["seat_id"].tolist()
    if row_letter:
        free = [s for s in free if seat_row(s) == row_letter]
    return sorted(free, key=lambda s: SEAT_RANK.get(s, 10**6))


def find_by_phone(df: pd.DataFrame, phone: str) -> pd.DataFrame:
    hit = df[(df["phone"] == phone) & (df["status"].isin((PENDING, BOOKED)))]
    return hit.sort_values("seat_id", key=lambda c: c.map(SEAT_RANK))


def reserve_seat(seat_id: str, name: str, phone: str, utr: str
                 ) -> tuple[bool, pd.Series | None, str]:
    """Seat -> Pending_Verification. No ticket is generated at this stage."""
    for _ in range(WRITE_ATTEMPTS):
        df = load_seats(fresh=True)
        if df.empty:
            return False, None, "Seat database is empty. Ask the organiser to initialise it."

        if ONE_PASS_PER_PHONE:
            existing = find_by_phone(df, phone)
            if not existing.empty:
                held = existing.iloc[0]
                return False, held, (
                    f"This number already holds seat {held['seat_id']} "
                    f"({held['status'].replace('_', ' ').lower()}). "
                    "Use the Download Ticket tab.")

        clash = df[(df["utr_number"] == utr) & (df["utr_number"] != "")]
        if not clash.empty:
            return False, None, (
                "That UTR has already been submitted against another seat. "
                "Each transaction can only be used once.")

        match = df.index[df["seat_id"] == seat_id]
        if match.empty:
            return False, None, f"Seat {seat_id} is not in the seating plan."
        idx = match[0]
        if df.at[idx, "status"] != AVAILABLE:
            return False, None, f"Seat {seat_id} was just taken. Please pick another."

        df.loc[idx, SCHEMA[1:]] = [PENDING, name, phone, utr, now_ist(), ""]
        save_seats(df)

        confirm = load_seats(fresh=True)
        back = confirm[confirm["seat_id"] == seat_id]
        if not back.empty and back.iloc[0]["utr_number"] == utr:
            return True, back.iloc[0], f"Seat {seat_id} held for verification."

    return False, None, (
        "High demand right now — that seat was taken mid-booking. Please try again.")


def set_status(seat_id: str, new_status: str) -> tuple[bool, str]:
    """Approve (-> Booked) or reject (-> Available, row wiped)."""
    for _ in range(WRITE_ATTEMPTS):
        df = load_seats(fresh=True)
        match = df.index[df["seat_id"] == seat_id]
        if match.empty:
            return False, f"Seat {seat_id} not found."
        idx = match[0]

        if new_status == BOOKED:
            df.at[idx, "status"] = BOOKED
            if not str(df.at[idx, "booked_at"]).strip():
                df.at[idx, "booked_at"] = now_ist()
        elif new_status == AVAILABLE:
            df.loc[idx, SCHEMA[1:]] = [AVAILABLE, "", "", "", "", ""]
        else:
            return False, f"Unsupported status {new_status}."

        save_seats(df)
        back = load_seats(fresh=True)
        row = back[back["seat_id"] == seat_id]
        if not row.empty and row.iloc[0]["status"] == new_status:
            return True, f"{seat_id} -> {new_status.replace('_', ' ')}."
    return False, "Write did not stick — please retry."


def check_in(seat_id: str) -> tuple[bool, str, pd.Series | None]:
    """
    Gate admission. Only Booked passes admit, and a second scan is refused —
    duplicate entry is the failure mode that actually costs money at the door.
    """
    df = load_seats(fresh=True)
    match = df[df["seat_id"] == seat_id.upper()]
    if match.empty:
        return False, f"{seat_id} is not in the seating plan.", None
    row = match.iloc[0]

    if is_house_block(row):
        return False, f"{seat_id} is a house block, not a sold seat.", row
    if row["status"] == AVAILABLE:
        return False, f"{seat_id} has not been sold — no valid pass exists.", row
    if row["status"] == PENDING:
        return False, f"{seat_id} is still awaiting payment verification.", row
    if row["status"] != BOOKED:
        return False, f"{seat_id} is not a valid pass ({row['status']}).", row
    if str(row["checkin_time"]).strip():
        return False, f"ALREADY CHECKED IN at {row['checkin_time']}.", row

    idx = df.index[df["seat_id"] == seat_id.upper()][0]
    df.at[idx, "checkin_time"] = now_ist()
    save_seats(df)
    return True, f"ADMIT — {row['name']} · {seat_id}", df.loc[idx]


def seat_from_payload(text: str) -> str | None:
    """Pull the seat id out of a scanned QR payload."""
    hit = re.search(r"Seat:\s*([A-J]\d{1,2})", text or "", re.IGNORECASE)
    return hit.group(1).upper() if hit else None


# =============================================================================
# 9. HTML / CSS INJECTION
# =============================================================================


def _html(markup: str) -> None:
    """
    The ONLY sanctioned injection path.

    Not st.html(): it sanitizes its input and, depending on the Streamlit build,
    drops a <style> block entirely. Nothing errors — the page just renders with
    stock styling, and the #FF4B4B primary button is the tell.

    Three markdown traps this also defuses:
      * a line indented 4+ spaces becomes a CODE BLOCK, printing the stylesheet
        onto the page as literal text;
      * a blank line inside HTML becomes a paragraph break, injecting <p> tags
        into the middle of the markup;
      * a line starting '#' or '-' can parse as a heading or list item.
    Joining with SPACES into one physical line makes all three impossible.
    """
    cleaned = " ".join(line.strip() for line in markup.splitlines() if line.strip())
    st.markdown(cleaned, unsafe_allow_html=True)


def inject_theme(intro: bool = False) -> None:
    """
    `intro=True` only on the FIRST script run of a session.

    Streamlit re-executes the whole script on every interaction, so
    unconditional entrance CSS would replay the 2s blackout and the slide-up on
    every keystroke rerun and every form submit. The app would feel broken.
    """
    hold, fade = SPLASH_HOLD, SPLASH_FADE

    if intro:
        stagger = "".join(
            f'[data-testid="stVerticalBlock"] > *:nth-child({i}) '
            f"{{ animation-delay:{REVEAL_BASE + 0.05 * (i - 1):.2f}s; }} "
            for i in range(1, 11)
        )
        reveal = (
            '[data-testid="stVerticalBlock"] > [data-testid="stElementContainer"],'
            '[data-testid="stVerticalBlock"] > .stElementContainer {'
            "animation:vipRise .55s cubic-bezier(.22,1,.36,1) both;"
            f"animation-delay:{REVEAL_BASE:.2f}s; }} " + stagger
        )
    else:
        reveal = ""

    _html(f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;800;900&family=Playfair+Display:wght@500;700;900&display=swap');

    /* ============ GROUND ============ */
    .stApp, [data-testid="stAppViewContainer"], section.main,
    [data-testid="stMain"] {{ background-color:{OBSIDIAN} !important; }}
    .stApp {{
        background-image:
          radial-gradient(1250px 700px at 6% -14%, rgba(212,175,55,.18), transparent 60%),
          radial-gradient(950px 600px at 96% 2%, rgba(52,208,122,.055), transparent 62%) !important;
        color:#ECE7DA !important;
    }}
    [data-testid="stHeader"], header[data-testid="stHeader"] {{
        background:transparent !important; }}
    .block-container {{ padding-top:1.2rem; padding-bottom:3.5rem; max-width:840px; }}
    html, body, [class*="css"] {{ font-family:Inter, system-ui, sans-serif; }}
    ::selection {{ background:rgba(212,175,55,.32); }}

    /* ============ GLASS CARDS ============ */
    /* The metal edge is a masked ::before ring, not a border-image. A gradient
       painted via background-clip:border-box bleeds through a translucent
       fill and tints the whole card gold. */
    .glass {{
        position:relative;
        background:rgba(255,255,255,0.03);
        border-radius:22px; padding:1.7rem 1.9rem; margin-bottom:1rem;
        box-shadow:0 24px 60px rgba(0,0,0,.66),
                   inset 0 1px 0 rgba(255,255,255,.07);
        backdrop-filter:blur(18px) saturate(150%);
        -webkit-backdrop-filter:blur(18px) saturate(150%);
    }}
    .glass::before {{
        content:""; position:absolute; inset:0; border-radius:inherit;
        padding:1px; background:{GOLD_CSS}; opacity:.55;
        -webkit-mask:linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
        -webkit-mask-composite:xor; mask:linear-gradient(#000 0 0) content-box,
                                          linear-gradient(#000 0 0);
        mask-composite:exclude; pointer-events:none;
    }}
    .glass--hero::before {{ opacity:.9; animation:vipBreathe 5.6s ease-in-out infinite; }}
    .glass--hero {{ box-shadow:0 24px 60px rgba(0,0,0,.66),
                    inset 0 1px 0 rgba(255,255,255,.07),
                    0 0 44px rgba(212,175,55,.10); }}

    /* ============ TYPE ============ */
    .pill {{
        display:inline-block; padding:.46rem 1.2rem; border-radius:999px;
        font-size:.63rem; font-weight:900; letter-spacing:.3em; color:#12141A;
        background:{GOLD_CSS}; background-size:200% 200%;
        animation:pillsheen 6s ease-in-out infinite, pillglow 3.2s ease-in-out infinite;
    }}
    @keyframes pillsheen {{ 0%,100%{{background-position:0% 50%}}
                            50%{{background-position:100% 50%}} }}
    @keyframes pillglow {{
        0%,100%{{box-shadow:0 0 14px rgba(212,175,55,.38),0 0 32px rgba(212,175,55,.14)}}
        50%    {{box-shadow:0 0 28px rgba(212,175,55,.70),0 0 62px rgba(212,175,55,.28)}}
    }}
    .show-title {{
        font-family:'Playfair Display', Georgia, serif;
        font-size:clamp(2.1rem, 7vw, 3.1rem); font-weight:900; line-height:1.02;
        margin:1rem 0 .3rem; letter-spacing:-.005em;
        background:{GOLD_CSS};
        -webkit-background-clip:text; background-clip:text;
        -webkit-text-fill-color:transparent;
        filter:drop-shadow(0 6px 26px rgba(212,175,55,.26));
    }}
    .show-sub {{
        font-family:'Playfair Display', Georgia, serif;
        font-size:1.02rem; font-weight:500; font-style:italic;
        letter-spacing:.04em; color:rgba(232,204,107,.9); margin-bottom:.2rem;
    }}
    .micro {{ font-size:.74rem; font-weight:300; letter-spacing:.13em;
              line-height:1.75; color:rgba(236,231,218,.46); }}
    .eyebrow {{ font-size:.58rem; font-weight:800; letter-spacing:.32em;
                text-transform:uppercase; color:rgba(236,231,218,.4); }}

    /* ============ META CHIPS ============ */
    .chips {{ display:flex; flex-wrap:wrap; gap:.5rem; margin-top:1.1rem; }}
    .chip {{
        display:inline-flex; align-items:baseline; gap:.55rem;
        padding:.5rem 1rem; border-radius:12px; font-size:.76rem;
        font-weight:500; letter-spacing:.02em; color:#EFE8D8;
        border:1px solid rgba(212,175,55,.24); background:rgba(212,175,55,.06);
        transition:border-color .2s ease, background .2s ease, transform .12s ease;
    }}
    .chip:hover {{ border-color:rgba(212,175,55,.55);
                   background:rgba(212,175,55,.12); transform:translateY(-1px); }}
    .chip b {{ font-size:.58rem; font-weight:900; letter-spacing:.2em;
               color:{GOLD_SOFT}; text-transform:uppercase; }}
    .chip--map {{ border-color:rgba(212,175,55,.5);
                  background:rgba(212,175,55,.11); }}
    a.chip, a.chip:link, a.chip:visited, a.chip:hover {{
        text-decoration:none !important; color:#EFE8D8 !important; }}

    /* ============ PRICE ============ */
    .price {{ display:flex; flex-wrap:wrap; gap:2.2rem; align-items:flex-end; }}
    .price-old {{ font-size:1.4rem; font-weight:700; color:{GOLD_SOFT};
                  text-decoration:line-through; opacity:.6;
                  font-variant-numeric:tabular-nums; }}
    .price-new {{ font-size:2.35rem; font-weight:900; color:{NEON};
                  text-shadow:0 0 28px rgba(52,208,122,.34);
                  font-variant-numeric:tabular-nums; line-height:1; }}
    .price-tag {{ font-size:.6rem; font-weight:900; letter-spacing:.2em;
                  color:{NEON}; }}

    /* ============ TRACKER ============ */
    .trk-rail {{ height:10px; border-radius:999px; overflow:hidden;
                 background:rgba(255,255,255,.05);
                 border:1px solid rgba(212,175,55,.16); }}
    .trk-fill {{ height:100%; border-radius:999px; background:{GOLD_CSS};
                 background-size:220% 100%;
                 box-shadow:0 0 18px rgba(212,175,55,.62); }}
    @keyframes shimmer {{0%{{background-position:0 0}}100%{{background-position:220% 0}}}}
    .num {{ color:{GOLD_SOFT}; font-weight:900;
            font-variant-numeric:tabular-nums; }}

    /* ============ TIER CARDS ============ */
    .tier-grid {{ display:flex; flex-wrap:wrap; gap:.7rem; margin-top:.85rem; }}
    .tier {{ flex:1 1 150px; padding:.9rem 1.1rem; border-radius:14px;
             border:1px solid rgba(212,175,55,.28);
             background:rgba(255,255,255,.03); }}
    .tier-price {{ font-size:1.65rem; font-weight:900; color:{GOLD_SOFT};
                   font-variant-numeric:tabular-nums; line-height:1.15; }}
    .tier-rows {{ font-size:.68rem; letter-spacing:.14em; text-transform:uppercase;
                  color:rgba(236,231,218,.45); }}
    /* ============ RECEIPT ============ */
    /* Reads as a torn-off counterfoil: warm gradient, gold hairline, a notched
       perforation down the right edge and tabular numerals so the amount lines
       up like a printed receipt rather than a status div. */
    .receipt {{ position:relative; overflow:hidden; margin:.3rem 0 1.2rem;
                padding:1.35rem 1.6rem; border-radius:18px;
                background:linear-gradient(135deg, rgba(212,175,55,.16) 0%,
                           rgba(212,175,55,.05) 42%, rgba(255,255,255,.02) 100%);
                border:1px solid rgba(212,175,55,.55);
                box-shadow:0 18px 46px rgba(0,0,0,.55),
                           inset 0 1px 0 rgba(255,255,255,.10),
                           0 0 34px rgba(212,175,55,.12); }}
    .receipt::before {{ content:""; position:absolute; top:0; left:0; right:0;
                        height:2px; background:{GOLD_CSS}; opacity:.9; }}
    .receipt::after {{ content:""; position:absolute; top:12%; bottom:12%;
                       right:23%; width:1px;
                       background:repeating-linear-gradient(to bottom,
                           rgba(212,175,55,.75) 0 6px, transparent 6px 13px); }}
    .r-grid {{ display:flex; flex-wrap:wrap; gap:1.9rem; align-items:flex-end; }}
    .r-cell {{ min-width:96px; }}
    .r-key {{ display:block; font-size:.56rem; font-weight:900;
              letter-spacing:.3em; text-transform:uppercase;
              color:rgba(236,231,218,.5); margin-bottom:.3rem; }}
    .r-seat {{ font-family:'Playfair Display', Georgia, serif;
               font-size:3rem; font-weight:900; line-height:1;
               background:{GOLD_CSS}; -webkit-background-clip:text;
               background-clip:text; -webkit-text-fill-color:transparent;
               filter:drop-shadow(0 4px 18px rgba(212,175,55,.35)); }}
    .r-tier {{ font-size:1.3rem; font-weight:900; color:{GOLD_SOFT};
               letter-spacing:.1em; }}
    .r-amt {{ font-size:2.1rem; font-weight:900; color:{NEON}; line-height:1;
              font-variant-numeric:tabular-nums;
              text-shadow:0 0 24px rgba(52,208,122,.4); }}
    .r-note {{ font-size:.68rem; letter-spacing:.1em; margin-top:.9rem;
               color:rgba(236,231,218,.42); }}

    /* ============ STEPPER ============ */
    .stepper {{ display:flex; align-items:center; gap:.55rem; margin:.2rem 0 1.1rem; }}
    .stp {{ display:flex; align-items:center; gap:.5rem; }}
    .stp-dot {{ width:30px; height:30px; border-radius:50%; display:flex;
                align-items:center; justify-content:center; font-size:.76rem;
                font-weight:900; border:1px solid rgba(212,175,55,.35);
                color:rgba(236,231,218,.45); background:rgba(255,255,255,.03); }}
    .stp-txt {{ font-size:.6rem; font-weight:800; letter-spacing:.2em;
                text-transform:uppercase; color:rgba(236,231,218,.4); }}
    .stp-bar {{ flex:1; height:1px; background:rgba(212,175,55,.22); }}
    .stp.on .stp-dot {{ background:{GOLD_CSS}; color:#12100C;
                        border-color:transparent;
                        box-shadow:0 0 18px rgba(212,175,55,.6); }}
    .stp.on .stp-txt {{ color:{GOLD_SOFT}; }}
    .stp.done .stp-dot {{ border-color:{GOLD}; color:{GOLD_SOFT};
                          background:rgba(212,175,55,.12); }}
    .stp.done .stp-txt {{ color:rgba(232,204,107,.7); }}
    @media (max-width:640px) {{ .stp-txt {{ display:none; }}
                                .receipt::after {{ display:none; }}
                                .r-grid {{ gap:1.2rem; }} }}
    .notice {{ border-radius:18px; padding:1.5rem 1.7rem; margin:.4rem 0 1rem;
               border:1px solid rgba(240,169,59,.55);
               background:linear-gradient(160deg, rgba(240,169,59,.16),
                          rgba(240,169,59,.04));
               box-shadow:0 0 40px rgba(240,169,59,.18); }}
    .notice h3 {{ font-size:1.4rem; font-weight:900; color:{AMBER};
                  margin:.7rem 0 .5rem; letter-spacing:.01em; }}
    .notice p {{ font-size:.95rem; line-height:1.7;
                 color:rgba(245,238,224,.86); margin:0; }}

    /* ============ FORM CONTROLS ============ */
    /* Two deliberate lanes, because the brief pulls in two directions:
         SELECTBOXES (step 2, choosing a seat)  -> GOLD. It is a brand moment.
         TEXT INPUTS (step 3, entering data)    -> NEON. It is an action.
       Labels are gold/white in BOTH lanes — neon-on-neon labels are what made
       the old form read as a terminal rather than a ticketing portal. */

    .stTextInput label, .stTextInput label p,
    .stNumberInput label, .stNumberInput label p,
    .stSelectbox label, .stSelectbox label p {{
        font-size:0.9rem !important; font-weight:800 !important;
        letter-spacing:.16em !important; text-transform:uppercase !important;
        color:#FFF4D6 !important; opacity:1 !important;
        margin-bottom:.55rem !important;
        text-shadow:0 0 16px rgba(212,175,55,.45) !important; }}

    /* ---------- NEON lane: text + number inputs ---------- */
    .stTextInput > div > div,
    .stTextInput [data-baseweb="input"],
    .stTextInput [data-baseweb="base-input"],
    [data-testid="stTextInputRootElement"],
    .stNumberInput [data-baseweb="input"],
    [data-testid="stNumberInputContainer"] {{
        background-color:rgba(57,255,20,0.12) !important;
        background-image:none !important;
        border:2px solid #39FF14 !important;
        border-radius:14px !important;
        box-shadow:0 0 15px rgba(57,255,20,0.4) !important;
        transition:box-shadow .22s ease, background-color .22s ease,
                   border-color .22s ease; }}
    .stTextInput > div > div:hover,
    .stTextInput [data-baseweb="input"]:hover,
    .stNumberInput [data-baseweb="input"]:hover {{
        background-color:rgba(57,255,20,0.17) !important;
        box-shadow:0 0 20px rgba(57,255,20,0.6) !important; }}
    .stTextInput > div > div:focus-within,
    .stTextInput [data-baseweb="input"]:focus-within,
    [data-testid="stTextInputRootElement"]:focus-within,
    .stNumberInput [data-baseweb="input"]:focus-within {{
        background-color:rgba(57,255,20,0.18) !important;
        border-color:#7CFF5A !important;
        box-shadow:0 0 25px rgba(57,255,20,0.8) !important; }}

    .stTextInput input, .stTextInput input[type="text"],
    .stTextInput input[type="password"], .stNumberInput input {{
        height:72px !important; font-size:1.32rem !important;
        font-weight:700 !important; letter-spacing:.03em !important;
        padding:0 1.3rem !important;
        background:transparent !important; background-color:transparent !important;
        border:none !important; outline:none !important; box-shadow:none !important;
        color:#EAFFE4 !important; caret-color:#39FF14 !important;
        -webkit-text-fill-color:#EAFFE4 !important;
        text-shadow:0 0 12px rgba(57,255,20,.35) !important; }}
    /* At 1.32rem a faint placeholder reads as a rendering fault. */
    .stTextInput input::placeholder, .stNumberInput input::placeholder {{
        color:rgba(215,255,205,.45) !important; font-weight:400 !important;
        font-size:.98rem !important; letter-spacing:.04em !important;
        text-shadow:none !important;
        -webkit-text-fill-color:rgba(215,255,205,.45) !important; }}
    .stNumberInput button {{
        background:rgba(57,255,20,.14) !important;
        border:1px solid rgba(57,255,20,.5) !important;
        color:#EAFFE4 !important; }}

    /* ---------- GOLD lane: seat selection ---------- */
    .stSelectbox [data-baseweb="select"] > div,
    .stSelectbox [data-baseweb="select"] div[role="button"] {{
        background-color:rgba(212,175,55,0.08) !important;
        background-image:linear-gradient(180deg, rgba(255,255,255,.05),
                                         rgba(255,255,255,.01)) !important;
        border:2px solid rgba(212,175,55,.62) !important;
        border-radius:14px !important;
        box-shadow:0 0 15px rgba(212,175,55,.28) !important;
        min-height:72px !important; padding:0 1.25rem !important;
        color:#FFF4D6 !important; font-weight:800 !important;
        font-size:1.18rem !important; letter-spacing:.02em !important;
        transition:box-shadow .22s ease, border-color .22s ease,
                   background-color .22s ease; }}
    .stSelectbox [data-baseweb="select"] > div:hover {{
        border-color:{GOLD} !important;
        box-shadow:0 0 22px rgba(212,175,55,.5) !important; }}
    .stSelectbox [data-baseweb="select"] > div:focus-within {{
        border-color:#FCF6BA !important;
        background-color:rgba(212,175,55,.14) !important;
        box-shadow:0 0 28px rgba(212,175,55,.78) !important; }}
    .stSelectbox [data-baseweb="select"] svg {{ fill:{GOLD_SOFT} !important; }}
    [data-baseweb="popover"] [role="listbox"] {{
        background:#12100A !important;
        border:1px solid rgba(212,175,55,.5) !important;
        border-radius:14px !important;
        box-shadow:0 20px 50px rgba(0,0,0,.7) !important; }}
    [data-baseweb="popover"] [role="option"] {{
        color:#F3E9CE !important; font-size:1.02rem !important;
        font-weight:600 !important; }}
    [data-baseweb="popover"] [role="option"]:hover,
    [data-baseweb="popover"] [aria-selected="true"] {{
        background:rgba(212,175,55,.18) !important; color:#FFF8E4 !important; }}

    /* ============ BUTTONS ============ */
    /* Streamlit renames its button hooks between releases and its default
       primary is #FF4B4B, so every generation of selector is covered and
       background-color is nuked explicitly — the `background` shorthand alone
       loses to a later background-color rule in some builds. */
    .stButton > button, .stButton button,
    .stFormSubmitButton > button, .stFormSubmitButton button,
    .stDownloadButton > button, .stDownloadButton button,
    button[kind="primary"], button[kind="secondary"],
    button[kind="primaryFormSubmit"], button[kind="secondaryFormSubmit"],
    [data-testid^="stBaseButton"] {{
        border:1px solid rgba(212,175,55,.42) !important; border-radius:14px !important;
        background-color:rgba(212,175,55,.09) !important; background-image:none !important;
        color:#F2EBD9 !important; font-weight:800 !important;
        letter-spacing:.1em !important; min-height:54px !important;
        box-shadow:none !important;
        transition:transform .1s ease, box-shadow .24s ease,
                   background-color .24s ease; }}

    .stFormSubmitButton > button, .stFormSubmitButton button,
    .stFormSubmitButton button[kind="primaryFormSubmit"],
    .stFormSubmitButton [data-testid^="stBaseButton"] {{
        width:100% !important; min-height:88px !important;
        font-size:1.14rem !important; font-weight:900 !important;
        letter-spacing:.22em !important; border-radius:20px !important;
        border:none !important; color:#12100C !important;
        background-color:{GOLD} !important;
        background-image:{GOLD_CSS} !important; background-size:200% 200%;
        position:relative; overflow:hidden;
        animation:ctapulse 2.6s ease-in-out infinite,
                  ctashift 7s ease-in-out infinite; }}
    .stFormSubmitButton button * {{ color:#12100C !important;
        font-weight:900 !important; letter-spacing:.22em !important; }}
    @keyframes ctashift {{ 0%,100%{{background-position:0% 50%}}
                           50%{{background-position:100% 50%}} }}
    @keyframes ctapulse {{
        0%,100% {{ box-shadow:0 16px 40px -10px rgba(212,175,55,.6),
                              0 0 0 0 rgba(212,175,55,.44),
                              inset 0 1px 0 rgba(255,255,255,.7); }}
        50%     {{ box-shadow:0 20px 54px -8px rgba(212,175,55,.85),
                              0 0 0 18px rgba(212,175,55,0),
                              inset 0 1px 0 rgba(255,255,255,.7); }} }}
    .stFormSubmitButton button::after {{
        content:""; position:absolute; top:0; left:-60%; width:36%; height:100%;
        background:linear-gradient(105deg, transparent,
                                   rgba(255,255,255,.6), transparent);
        transform:skewX(-18deg); animation:sheen 4.4s ease-in-out infinite; }}
    @keyframes sheen {{ 0%{{left:-60%}} 55%{{left:132%}} 100%{{left:132%}} }}
    .stFormSubmitButton button:hover {{ transform:translateY(-2px);
        box-shadow:0 26px 62px -10px rgba(212,175,55,.95) !important; }}
    .stFormSubmitButton button:active {{ transform:translateY(0); }}

    .stDownloadButton > button, .stDownloadButton button {{
        width:100% !important; min-height:74px !important;
        font-size:1rem !important; font-weight:900 !important;
        letter-spacing:.18em !important; border-radius:18px !important;
        border:1px solid rgba(212,175,55,.5) !important; color:#F7F0DE !important;
        background-color:rgba(212,175,55,.12) !important;
        background-image:linear-gradient(135deg, rgba(212,175,55,.2),
                                         rgba(212,175,55,.04)) !important; }}
    .stDownloadButton button:hover {{ background-image:{GOLD_CSS} !important;
        color:#12100C !important; border-color:transparent !important;
        box-shadow:0 20px 50px -12px rgba(212,175,55,.85) !important; }}
    .stDownloadButton button:hover * {{ color:#12100C !important; }}
    .st-key-reset_db button {{ background-color:#C7222A !important;
        background-image:linear-gradient(135deg,#F0616B,#B91C1C) !important;
        color:#FFF !important; border:none !important; }}

    /* ============ CHROME ============ */
    [data-baseweb="tab-list"] {{ gap:.45rem;
        border-bottom:1px solid rgba(212,175,55,.14) !important; }}
    [data-baseweb="tab"] {{ font-weight:900 !important;
        letter-spacing:.16em !important; font-size:.72rem !important; }}
    [data-baseweb="tab-highlight"] {{ background:{GOLD} !important; height:2px; }}
    [data-testid="stImage"] img {{ border-radius:18px;
        box-shadow:0 30px 80px rgba(0,0,0,.75), 0 0 46px rgba(212,175,55,.14); }}
    [data-testid="stMetricValue"] {{ color:{GOLD_SOFT} !important;
        font-weight:900 !important; font-variant-numeric:tabular-nums; }}
    [data-testid="stMetricLabel"] {{ letter-spacing:.18em !important;
        text-transform:uppercase !important; font-size:.6rem !important; }}
    [data-testid="stDataFrame"] {{ border:1px solid rgba(212,175,55,.22);
        border-radius:14px; overflow:hidden; }}
    hr {{ border-color:rgba(212,175,55,.14) !important; }}

    /* ============ CINEMATIC INTRO ============ */
    /* Simple two-stop keyframes. The hold is expressed as an animation-DELAY
       rather than as percentage stops inside the keyframe — same result, far
       less for the compositor to interpolate, and no mid-timeline jumps. */
    @keyframes vipVeil {{ from {{ opacity:1; }}
                          to   {{ opacity:0; visibility:hidden; }} }}
    @keyframes vipMarkIn {{ from {{ opacity:0; transform:translateY(14px) scale(.972); }}
                            to   {{ opacity:1; transform:none; }} }}
    @keyframes vipRuleIn {{ from {{ width:0; opacity:0; }}
                            to   {{ width:210px; opacity:1; }} }}
    @keyframes vipRise {{ from {{ opacity:0; transform:translateY(16px); }}
                          to   {{ opacity:1; transform:none; }} }}
    @keyframes vipBreathe {{ 0%,100%{{opacity:.5}} 50%{{opacity:1}} }}

    /* THE GLITCH FIX.
       A `position:fixed` element is trapped by ANY ancestor carrying a
       transform, filter or backdrop-filter — that ancestor becomes its
       containing block. The veil is injected inside a Streamlit element
       container, and the reveal animation puts translateY() on those very
       containers, so the veil was being dragged around mid-fade and could sit
       over live controls. Its own container is therefore pinned static. */
    [data-testid="stElementContainer"]:has(.vip-veil),
    .stElementContainer:has(.vip-veil) {{
        animation:none !important; transform:none !important;
        filter:none !important; opacity:1 !important;
        position:static !important; contain:none !important; }}

    /* pointer-events is none on the veil AND every descendant, from the first
       frame — not switched at the end of the animation. An opaque full-screen
       layer that failed to fade would otherwise lock every click in the app. */
    .vip-veil, .vip-veil * {{ pointer-events:none !important; }}
    .vip-veil {{
        position:fixed !important; inset:0; z-index:2147483000;
        display:flex; flex-direction:column; align-items:center;
        justify-content:center; will-change:opacity;
        background:radial-gradient(1000px 640px at 50% 44%,
                   #14161D 0%, #05060B 62%, #000 100%);
        animation:vipVeil {fade:.2f}s cubic-bezier(.4,0,.2,1) {hold:.2f}s forwards; }}
    .vip-veil__mark {{
        font-family:'Playfair Display', Georgia, serif;
        font-size:clamp(1.5rem, 6vw, 3rem); font-weight:900;
        letter-spacing:.16em; text-align:center; padding:0 1.4rem;
        background:{GOLD_CSS};
        -webkit-background-clip:text; background-clip:text;
        -webkit-text-fill-color:transparent;
        filter:drop-shadow(0 0 30px rgba(212,175,55,.6));
        animation:vipMarkIn .5s cubic-bezier(.22,1,.36,1) both; }}
    .vip-veil__rule {{ height:1px; margin:1.25rem 0 .95rem;
        background:linear-gradient(90deg,transparent,{GOLD},transparent);
        animation:vipRuleIn .6s cubic-bezier(.22,1,.36,1) .08s both; }}
    .vip-veil__sub {{
        font-family:Inter, system-ui, sans-serif; font-size:.58rem;
        font-weight:800; letter-spacing:.44em; color:rgba(232,204,107,.72);
        text-align:center; text-transform:uppercase;
        animation:vipMarkIn .5s cubic-bezier(.22,1,.36,1) .12s both; }}
    {reveal}
    /* Accessibility + failsafe. If motion is suppressed nothing may be left at
       opacity:0 — that would render the app permanently blank. */
    @media (prefers-reduced-motion: reduce) {{
        .vip-veil {{ display:none !important; }}
        [data-testid="stVerticalBlock"] > [data-testid="stElementContainer"],
        [data-testid="stVerticalBlock"] > .stElementContainer,
        .glass, .glass--hero, .glass--hero::before, .pill, .trk-live,
        .stFormSubmitButton button, .stFormSubmitButton button::after {{
            animation:none !important; opacity:1 !important;
            transform:none !important; }} }}
    @media (max-width: 640px) {{
        .block-container {{ padding-left:.9rem; padding-right:.9rem; }}
        .glass {{ padding:1.25rem 1.1rem; border-radius:18px; }}
        .price {{ gap:1.3rem; }}
    }}
    </style>
    """)


def splash_overlay() -> None:
    """Cinematic veil. Injected ONCE per session — see inject_theme()."""
    _html(f"""
    <div class="vip-veil">
      <div class="vip-veil__mark">{EVENT_NAME.upper()}</div>
      <div class="vip-veil__rule"></div>
      <div class="vip-veil__sub">{EVENT_SUBTITLE}</div>
    </div>
    """)


def banner(path: Path, fallback: str) -> None:
    if path.exists():
        st.image(str(path), width="stretch")
    else:
        _html(f"<div class='glass' style='text-align:center;letter-spacing:.3em;"
              f"font-weight:900;color:{GOLD_SOFT};'>{fallback}</div>")


def hero() -> None:
    # A real anchor, target=_blank, rel=noopener — it must actually open Maps.
    maps_chip = (
        f'<a class="chip chip--map" href="{MAPS_URL}" target="_blank" '
        f'rel="noopener noreferrer"><b>Map</b><span>Directions &#8599;</span></a>'
        if MAPS_URL else ""
    )
    _html(f"""
    <div class="glass glass--hero">
      <span class="pill">OFFICIAL TICKETING</span>
      <div class="show-title">{EVENT_NAME}</div>
      <div class="show-sub">{EVENT_SUBTITLE}</div>
      <div class="micro">An evening of tribute &middot; strictly by invitation</div>
      <div class="chips">
        <span class="chip"><b>Venue</b><span>{VENUE}</span></span>
        <span class="chip"><b>Date</b><span>{EVENT_DATE}</span></span>
        <span class="chip"><b>Time</b><span>{EVENT_TIME}</span></span>
        {maps_chip}
      </div>
    </div>
    """)


def tier_banner(prices: dict[str, int]) -> None:
    rows = {
        "VVIP": "Rows A – B",
        "VIP": "Rows C – G",
        "PREMIUM": "Rows H – J",
    }
    cards = "".join(
        f'<div class="tier"><span class="eyebrow">{t}</span>'
        f'<div class="tier-price">&#8377;{prices[t]:,}</div>'
        f'<div class="tier-rows">{rows[t]}</div></div>'
        for t in TIER_ORDER
    )
    _html(f"""
    <div class="glass" style="padding:1.3rem 1.6rem;">
      <span class="eyebrow">Seat categories</span>
      <div class="tier-grid">{cards}</div>
    </div>
    """)


def tracker(sold: int, pending: int, sellable: int) -> None:
    done = sold + pending
    pct = (done / sellable * 100) if sellable else 0.0
    sold_pct = (sold / sellable * 100) if sellable else 0.0
    _html(f"""
    <style>
    @keyframes fillbar {{ from {{width:0%}} to {{width:{sold_pct:.2f}%}} }}
    @keyframes fillbar2 {{ from {{width:0%}} to {{width:{pct:.2f}%}} }}
    .trk-live {{ animation:fillbar 1.1s cubic-bezier(.22,1,.36,1) both,
                            shimmer 2.8s linear infinite; }}
    .trk-pend {{ animation:fillbar2 1.1s cubic-bezier(.22,1,.36,1) both; }}
    </style>
    <div class="glass" style="padding:1.3rem 1.6rem;">
      <div style="display:flex;justify-content:space-between;margin-bottom:.75rem;">
        <span class="eyebrow">Live seating tracker</span>
        <span class="eyebrow">{pct:.0f}% taken</span>
      </div>
      <div class="trk-rail" style="position:relative;">
        <div class="trk-pend" style="position:absolute;inset:0;width:{pct:.2f}%;
             background:rgba(240,169,59,.45);border-radius:999px;"></div>
        <div class="trk-fill trk-live" style="position:absolute;inset:0;
             width:{sold_pct:.2f}%;"></div>
      </div>
      <div class="micro" style="margin-top:.75rem;">
        <span class="num">{sold}</span> confirmed &nbsp;&middot;&nbsp;
        <span style="color:{AMBER};font-weight:900;">{pending}</span> awaiting
        verification &nbsp;&middot;&nbsp;
        <span class="num">{max(sellable - done, 0)}</span> still open
        &nbsp;&middot;&nbsp; <span class="micro">{len(BLOCKED_SEATS)} house-blocked</span>
      </div>
    </div>
    """)


# =============================================================================
# 10. AUTO-DOWNLOAD
# =============================================================================


def auto_download(jpeg: bytes, filename: str, fire_key: str) -> None:
    """
    Best-effort automatic save. Fired ONCE per issued pass — an ungated version
    would re-trigger on every Streamlit rerun and spam the user's downloads.

    Honest about the limits. Programmatic downloads are actively restricted:
      * st.markdown strips <script>, so this MUST go through components.html,
        which renders in a sandboxed iframe;
      * Chrome blocks downloads from sandboxed iframes lacking allow-downloads,
        so the script first tries window.parent.document to step outside it;
      * browsers require a recent user gesture, and the form submit's gesture
        has usually expired by the time this component mounts;
      * iOS Safari largely ignores the `download` attribute and will open the
        image in a tab instead.
    Net effect: reliable on desktop Chrome/Edge/Firefox, unreliable on mobile
    Safari. The inline image and the download button below it are therefore the
    real delivery path on phones, not a fallback.
    """
    if st.session_state.get("_auto_dl") == fire_key:
        return
    st.session_state["_auto_dl"] = fire_key
    payload = base64.b64encode(jpeg).decode("ascii")
    components_html(
        f"""<script>
(function(){{
  var b64 = "{payload}";
  function bytes() {{
    var s = atob(b64), a = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) a[i] = s.charCodeAt(i);
    return a;
  }}
  function save(doc, win) {{
    try {{
      var url = win.URL.createObjectURL(new Blob([bytes()], {{type: "image/jpeg"}}));
      var a = doc.createElement("a");
      a.href = url; a.download = "{filename}"; a.rel = "noopener";
      a.style.display = "none";
      doc.body.appendChild(a); a.click();
      setTimeout(function(){{ doc.body.removeChild(a); win.URL.revokeObjectURL(url); }}, 5000);
      return true;
    }} catch (e) {{ return false; }}
  }}
  var ok = false;
  try {{ ok = save(window.parent.document, window.parent); }} catch (e) {{ ok = false; }}
  if (!ok) save(document, window);
}})();
</script>""",
        height=0,
    )



# =============================================================================
# 11. TAB 1 — BOOK TICKET
# =============================================================================

NAME_MIN: Final[int] = 3


def validate_booking(name: str, phone: str, utr: str) -> list[str]:
    errors: list[str] = []
    if len(name) < NAME_MIN:
        errors.append(f"Full name must be at least {NAME_MIN} characters.")
    if not PHONE_RE.match(phone):
        errors.append("Phone must be exactly 10 digits starting with 6-9.")
    if not UTR_RE.match(utr):
        errors.append("UTR / Transaction ID must be exactly 12 digits.")
    return errors


BOOK_STEP: Final[str] = "_book_step"
SEL_ROW: Final[str] = "_sel_row_letter"
SEL_SEAT: Final[str] = "_sel_seat_id"


def goto_step(step: int) -> None:
    st.session_state[BOOK_STEP] = step
    st.rerun()


def render_stepper(active: int) -> None:
    labels = ("Choose", "Seat", "Pay")
    parts = []
    for index, label in enumerate(labels, start=1):
        state = "on" if index == active else ("done" if index < active else "")
        mark = "&#10003;" if index < active else str(index)
        parts.append(
            f'<div class="stp {state}"><div class="stp-dot">{mark}</div>'
            f'<span class="stp-txt">{label}</span></div>')
        if index < len(labels):
            parts.append('<div class="stp-bar"></div>')
    _html(f'<div class="stepper">{"".join(parts)}</div>')


def render_receipt(seat_id: str, amount: int, locked: bool) -> None:
    tier = seat_tier(seat_id)
    note = ("Seat confirmed for this session. It is released only if someone "
            "else completes payment first — you will be told immediately."
            if locked else "Confirm to move on to payment.")
    _html(f"""
    <div class="receipt">
      <div class="r-grid">
        <div class="r-cell"><span class="r-key">Your seat</span>
             <div class="r-seat">{seat_id}</div></div>
        <div class="r-cell"><span class="r-key">Category</span>
             <div class="r-tier">{tier}</div></div>
        <div class="r-cell"><span class="r-key">Amount payable</span>
             <div class="r-amt">&#8377;{amount:,}</div></div>
      </div>
      <div class="r-note">{note}</div>
    </div>
    """)


# --- STEP 1 -------------------------------------------------------------------


def step_choose(df: pd.DataFrame, prices: dict[str, int]) -> None:
    tier_banner(prices)
    sold = int(((df["status"] == BOOKED) & (~df.apply(is_house_block, axis=1))).sum())
    pending = int((df["status"] == PENDING).sum())
    tracker(sold, pending, SELLABLE_SEATS)

    if not available_seats(df):
        st.error("Every seat has been taken or is awaiting verification.", icon="🎭")
        return

    _html(f"""
    <div class="glass" style="padding-bottom:.8rem;">
      <span class="pill">HOW IT WORKS</span>
      <div class="micro" style="margin-top:1.05rem;line-height:2;">
        <b style="color:{GOLD_SOFT};">1.</b> Pick your row and seat &mdash; the
        price updates live.<br>
        <b style="color:{GOLD_SOFT};">2.</b> Pay the exact amount by UPI and
        enter your 12-digit transaction ID.<br>
        <b style="color:{GOLD_SOFT};">3.</b> We verify within {VERIFY_HOURS}
        hours, then your pass unlocks in the
        <b style="color:{GOLD_SOFT};">Download Ticket</b> tab.
      </div>
    </div>
    """)
    if st.button("START BOOKING", type="primary", width="stretch",
                 key="start_booking"):
        goto_step(2)


# --- STEP 2 -------------------------------------------------------------------


def step_seat(df: pd.DataFrame, prices: dict[str, int]) -> None:
    open_rows = sorted({seat_row(s) for s in available_seats(df)})
    if not open_rows:
        st.error("Every seat has been taken or is awaiting verification.", icon="🎭")
        return

    _html("""
    <div class="glass" style="padding-bottom:.8rem;">
      <span class="pill">SELECT YOUR SEAT</span>
      <div class="micro" style="margin-top:1.05rem;">
        Rows are priced by category. Only seats still open are listed.
      </div>
    </div>
    """)

    left, right = st.columns(2, gap="medium")
    with left:
        row_letter = st.selectbox(
            "Row", open_rows,
            format_func=lambda r: f"Row {r}  ·  {ROW_TIER[r]}  ·  ₹{prices[ROW_TIER[r]]:,}",
            key=SEL_ROW)
    seats_here = available_seats(df, row_letter)
    with right:
        seat_id = st.selectbox("Seat", seats_here,
                               format_func=lambda s: f"Seat {s}", key=SEL_SEAT)

    if not seat_id:
        st.warning("No seats left in that row — pick another.", icon="⚠️")
        return

    st.caption(f"{len(seats_here)} seat(s) still open in row {row_letter}.")
    render_receipt(seat_id, prices[seat_tier(seat_id)], locked=False)

    proceed, back = st.columns([2, 1], gap="small")
    with proceed:
        if st.button("CONFIRM SEAT & PROCEED TO PAY", type="primary",
                     width="stretch", key="lock_seat"):
            st.session_state["_locked_seat"] = seat_id
            goto_step(3)
    with back:
        if st.button("Back", width="stretch", key="back_to_1"):
            goto_step(1)


# --- STEP 3 -------------------------------------------------------------------


def step_pay(df: pd.DataFrame, prices: dict[str, int]) -> None:
    seat_id = st.session_state.get("_locked_seat")
    if not seat_id:
        goto_step(2)
        return

    # The seat is held in this session, not in the sheet. Re-checking here turns
    # "your payment failed for no reason" into a clear, early message.
    current = df[df["seat_id"] == seat_id]
    if current.empty or current.iloc[0]["status"] != AVAILABLE:
        st.error(f"Seat {seat_id} was taken while you were deciding. "
                 "Please choose another — do not pay yet.", icon="🚫")
        if st.button("PICK ANOTHER SEAT", type="primary", width="stretch",
                     key="reselect"):
            goto_step(2)
        return

    amount = prices[seat_tier(seat_id)]
    render_receipt(seat_id, amount, locked=True)

    _html(f"""
    <div class="glass" style="padding-bottom:.9rem;">
      <span class="pill">STEP 1 &middot; PAY BY UPI</span>
      <div class="micro" style="margin-top:1rem;">
        Scan and pay <b style="color:{GOLD_SOFT};">&#8377;{amount:,}</b> exactly.
        A different amount will delay verification.
      </div>
    </div>
    """)

    qr_col, info_col = st.columns([1, 1.25], gap="medium")
    with qr_col:
        if UPI_QR_IMG.exists():
            st.image(str(UPI_QR_IMG), width=250)
        else:
            _html('<div class="glass" style="text-align:center;padding:2rem 1rem;">'
                  '<span class="eyebrow">UPI QR missing</span>'
                  '<div class="micro" style="margin-top:.6rem;">Place '
                  '<code>upi_qr.png</code> next to app.py.</div></div>')
    with info_col:
        if UPI_ID:
            st.markdown("**UPI ID**")
            st.code(UPI_ID, language=None)
        _html(f"""
        <div class="micro" style="line-height:1.95;">
          After paying, copy the <b style="color:#8CFF7A;">12-digit UTR /
          Transaction ID</b> from your UPI app receipt.<br>
          Keep that receipt until you are inside the venue.
        </div>
        """)

    _html("""
    <div class="glass" style="padding-bottom:.9rem;margin-top:.4rem;">
      <span class="pill">STEP 2 &middot; CONFIRM PAYMENT</span>
    </div>
    """)

    with st.form("book_seat", border=False):
        name = st.text_input("Full Name", max_chars=60,
                             placeholder="Exactly as printed on your ID")
        phone = st.text_input("Phone Number", max_chars=10,
                              placeholder="10 digits, starting 6-9")
        utr = st.text_input("12-Digit UTR / Transaction ID", max_chars=12,
                            placeholder="From your UPI payment receipt")
        submitted = st.form_submit_button("SUBMIT FOR VERIFICATION",
                                          type="primary")

    if st.button("Change seat", width="content", key="back_to_2"):
        goto_step(2)

    if not submitted:
        return

    name, phone, utr = name.strip(), phone.strip(), utr.strip()
    errors = validate_booking(name, phone, utr)
    if errors:
        for message in errors:
            st.error(message, icon="⚠️")
        return

    with st.spinner("Holding your seat…"):
        ok, row, message = reserve_seat(seat_id, name, phone, utr)

    if not ok:
        st.error(message, icon="🚫")
        return

    st.session_state["_booked_phone"] = phone
    st.session_state["_booked_seat"] = str(row["seat_id"]) if row is not None else seat_id
    st.balloons()
    st.rerun()


def render_booking(df: pd.DataFrame, prices: dict[str, int]) -> None:
    hero()
    step = int(st.session_state.get(BOOK_STEP, 1))
    render_stepper(step)
    if step >= 3:
        step_pay(df, prices)
    elif step == 2:
        step_seat(df, prices)
    else:
        step_choose(df, prices)


def render_pending_notice() -> None:
    seat = st.session_state.get("_booked_seat", "")
    phone = st.session_state.get("_booked_phone", "")
    _html(f"""
    <div class="notice">
      <span class="pill" style="background:linear-gradient(135deg,#FFD9A0,{AMBER});">
        PAYMENT UNDER VERIFICATION</span>
      <h3>Thank you — seat {seat} is held in your name.</h3>
      <p>
        Your payment is being verified manually. This takes a maximum of
        <b>{VERIFY_HOURS} hours</b>.<br><br>
        <b>No ticket is issued yet.</b> Once your transaction is confirmed,
        open the <b>DOWNLOAD TICKET</b> tab, enter
        <b style="color:{LIME_TEXT};">{phone}</b>, and your pass will be
        generated and downloaded instantly.<br><br>
        Keep your UPI receipt until you are inside the venue.
      </p>
    </div>
    """)
    if st.button("Book another seat (different phone number)", width="stretch"):
        for key in ("_booked_phone", "_booked_seat", "_locked_seat",
                    BOOK_STEP, SEL_ROW, SEL_SEAT):
            st.session_state.pop(key, None)
        st.rerun()


# =============================================================================
# 12. TAB 2 — DOWNLOAD TICKET
# =============================================================================


def issue_pass(row: pd.Series, prices: dict[str, int]) -> None:
    """Generate, auto-save and display the JPEG for a verified booking."""
    enriched = row.copy()
    enriched["_price"] = prices[seat_tier(str(row["seat_id"]))]
    store: dict[str, bytes] = st.session_state.setdefault("tickets", {})
    seat = str(row["seat_id"])
    if seat not in store:
        with st.spinner("Pressing your pass…"):
            store[seat] = build_ticket_jpeg(enriched)
    jpeg = store[seat]
    filename = f"VIP_Pass_{seat}.jpeg"

    auto_download(jpeg, filename, fire_key=seat)

    _html(f"""
    <div class="glass glass--hero" style="text-align:center;">
      <span class="pill">PAYMENT VERIFIED</span>
      <div class="show-title" style="font-size:clamp(3rem,14vw,4.6rem);
           margin:1.1rem 0 .2rem;">{seat}</div>
      <div class="show-sub">{EVENT_SUBTITLE}</div>
      <div class="micro">{row['name']} &nbsp;&middot;&nbsp;
           {seat_tier(seat)} &nbsp;&middot;&nbsp; &#8377;{enriched['_price']:,} paid</div>
    </div>
    """)
    st.image(jpeg, width="stretch")
    _html("""
    <div class="micro" style="text-align:center;margin:-.1rem 0 1rem;">
      Saving automatically. If nothing downloaded, long-press the pass to save
      it to your gallery, or use the button below.
    </div>
    """)
    st.download_button(
        f"DOWNLOAD PASS  ·  SEAT {seat}",
        data=jpeg, file_name=filename, mime="image/jpeg",
        width="stretch", key=f"dl_{seat}")

    if str(row["checkin_time"]).strip():
        st.info(f"This pass was already scanned in at {row['checkin_time']}.",
                icon="✅")
    if not fonts_ready():
        st.warning("No TrueType font available — the pass used a bitmap face. "
                   "Drop a .ttf into `assets/`.", icon="🔤")
    ppm = qr_px_per_module(gate_payload(row))
    if ppm < 4.0:
        st.warning(f"QR is dense ({ppm:.1f}px per module) and may not scan. "
                   "Shorten `maps_url` to a maps.app.goo.gl link.", icon="📷")


def render_download_tab(prices: dict[str, int]) -> None:
    _html(f"""
    <div class="glass" style="padding-bottom:.7rem;">
      <span class="pill">DOWNLOAD YOUR TICKET</span>
      <div class="micro" style="margin-top:1.05rem;">
        Enter the phone number you booked with. Passes are released as soon as
        the organiser verifies your payment &mdash; within {VERIFY_HOURS} hours
        of booking.
      </div>
    </div>
    """)

    with st.form("fetch_pass", border=False):
        phone = st.text_input("Phone Number", max_chars=10,
                              placeholder="The number you booked with")
        looked_up = st.form_submit_button("GET MY TICKET", type="primary")

    if looked_up:
        phone = phone.strip()
        if not PHONE_RE.match(phone):
            st.session_state["_lookup"] = {
                "error": "Enter a valid 10-digit number starting 6-9."}
        else:
            with st.spinner("Checking your booking…"):
                hits = find_by_phone(load_seats(fresh=True), phone)
            st.session_state["_lookup"] = (
                {"rows": hits.to_dict("records")} if not hits.empty
                else {"error": "No booking found for that number. If you have "
                               "just paid, please allow a few minutes."})

    result = st.session_state.get("_lookup")
    if not result:
        return
    if "error" in result:
        st.error(result["error"], icon="🔍")
        return

    for record in result["rows"]:
        row = pd.Series(record)
        seat = str(row["seat_id"])
        if row["status"] == BOOKED:
            issue_pass(row, prices)
        elif row["status"] == PENDING:
            _html(f"""
            <div class="notice">
              <span class="pill" style="background:linear-gradient(135deg,#FFD9A0,{AMBER});">
                VERIFICATION IN PROGRESS</span>
              <h3>Seat {seat} is held for you.</h3>
              <p>We have received your transaction ID
                 <b style="color:{LIME_TEXT};">{row['utr_number']}</b> and are
                 confirming it with the bank. This takes up to
                 <b>{VERIFY_HOURS} hours</b>. Check back here — nothing else is
                 needed from you.</p>
            </div>
            """)
        else:
            st.info(f"Seat {seat} is not currently held against this number.",
                    icon="ℹ️")


# =============================================================================
# 13. ADMIN
# =============================================================================


def admin_login() -> bool:
    if st.session_state.get("is_admin"):
        return True
    _html("""
    <div class="glass"><span class="pill">RESTRICTED</span>
      <div class="micro" style="margin-top:1.05rem;">Organiser access only.</div>
    </div>
    """)
    with st.form("admin_login", border=False):
        pwd = st.text_input("Admin password", type="password")
        if st.form_submit_button("UNLOCK", type="primary"):
            if pwd and pwd == cfg("admin_password"):
                st.session_state["is_admin"] = True
                st.rerun()
            else:
                st.error("Incorrect password.", icon="🔒")
    return False


def render_verification_queue(df: pd.DataFrame, prices: dict[str, int]) -> None:
    queue = df[df["status"] == PENDING].copy()
    if queue.empty:
        st.success("Nothing awaiting verification.", icon="✅")
        return
    queue["rank"] = queue["seat_id"].map(SEAT_RANK)
    queue = queue.sort_values("booked_at")

    st.caption(f"{len(queue)} payment(s) to verify · oldest first. "
               "Match the UTR against your bank statement before approving.")

    for _, row in queue.iterrows():
        seat = str(row["seat_id"])
        tier = seat_tier(seat)
        expected = prices[tier]
        with st.expander(
            f"{seat}  ·  {tier}  ·  ₹{expected:,}  ·  {row['name']}  "
            f"·  UTR {row['utr_number']}",
            expanded=False,
        ):
            st.write({
                "Seat": seat,
                "Category": tier,
                "Expected amount": f"₹{expected:,}",
                "Name": row["name"],
                "Phone": row["phone"],
                "UTR / Txn ID": row["utr_number"],
                "Submitted": row["booked_at"],
            })
            st.caption("Confirm this exact amount landed against this UTR "
                       "before approving. Approval issues a real ticket.")
            approve, reject = st.columns(2)
            if approve.button("APPROVE", key=f"ok_{seat}", type="primary",
                              width="stretch"):
                ok, message = set_status(seat, BOOKED)
                st.session_state.get("tickets", {}).pop(seat, None)
                (st.success if ok else st.error)(message)
                if ok:
                    st.rerun()
            if reject.button("REJECT & RELEASE", key=f"no_{seat}",
                             width="stretch"):
                ok, message = set_status(seat, AVAILABLE)
                st.session_state.get("tickets", {}).pop(seat, None)
                (st.success if ok else st.error)(message)
                if ok:
                    st.rerun()


def render_pricing_controller(prices: dict[str, int]) -> None:
    st.caption("Prices are read live from the `settings` worksheet on every "
               "page load. Changing them here does NOT alter what past buyers "
               "already paid — the amount is stamped onto each pass at issue.")
    if st.session_state.get("_prices_fallback"):
        st.warning("The `settings` worksheet could not be read — the defaults "
                   "below are in use. Saving will create/repair the sheet.",
                   icon="⚠️")

    with st.form("pricing", border=False):
        columns = st.columns(3)
        new_prices: dict[str, int] = {}
        for column, tier in zip(columns, TIER_ORDER):
            with column:
                new_prices[tier] = int(st.number_input(
                    f"{tier} (₹)", min_value=0, max_value=1_000_000,
                    value=int(prices[tier]), step=50, key=f"price_{tier}"))
        if st.form_submit_button("SAVE PRICING", type="primary"):
            if new_prices == prices:
                st.info("No change.", icon="ℹ️")
            else:
                try:
                    save_prices(new_prices)
                    st.success(
                        " · ".join(f"{t} ₹{new_prices[t]:,}" for t in TIER_ORDER),
                        icon="✅")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not write settings: {exc}", icon="🔌")


def render_gate_scanner(df: pd.DataFrame) -> None:
    st.caption("Only `Booked` passes admit. A second scan of the same seat is "
               "refused — duplicate entry is the failure that costs money at "
               "the door.")

    if not HAS_CV2:
        st.info("Camera decoding needs `opencv-python-headless`. Add it to "
                "requirements.txt to enable QR scanning. Manual entry below "
                "works regardless.", icon="📷")
    else:
        shot = st.camera_input("Scan the pass QR")
        if shot is not None:
            buf = np.frombuffer(shot.getvalue(), np.uint8)
            frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            payload, _, _ = cv2.QRCodeDetector().detectAndDecode(frame)
            seat = seat_from_payload(payload)
            if not seat:
                st.error("No readable pass QR in that frame. Move closer, "
                         "raise screen brightness, and hold steady.", icon="📷")
            else:
                ok, message, row = check_in(seat)
                (st.success if ok else st.error)(
                    message, icon="✅" if ok else "⛔")
                if row is not None:
                    st.write({"Seat": row["seat_id"], "Name": row["name"],
                              "Category": seat_tier(str(row["seat_id"])),
                              "Status": row["status"]})

    st.divider()
    with st.form("manual_checkin", border=False):
        manual = st.text_input("Manual check-in — seat number",
                               max_chars=4, placeholder="e.g. C12")
        if st.form_submit_button("CHECK IN", type="primary"):
            candidate = manual.strip().upper()
            if not re.fullmatch(r"[A-J]\d{1,2}", candidate):
                st.error("Enter a seat like C12.", icon="⚠️")
            else:
                ok, message, _ = check_in(candidate)
                (st.success if ok else st.error)(
                    message, icon="✅" if ok else "⛔")

    admitted = df[df["checkin_time"].astype(str).str.strip() != ""]
    st.metric("Checked in so far", len(admitted))


def attendee_table(df: pd.DataFrame, prices: dict[str, int]) -> pd.DataFrame:
    sold = df[(df["status"] == BOOKED) & (~df.apply(is_house_block, axis=1))].copy()
    if sold.empty:
        return sold
    sold["rank"] = sold["seat_id"].map(SEAT_RANK)
    sold = sold.sort_values("rank", na_position="last")
    sold["Category"] = sold["seat_id"].map(seat_tier)
    sold["Paid"] = sold["Category"].map(lambda t: f"₹{prices[t]:,}")
    return sold[["seat_id", "Category", "Paid", "name", "phone",
                 "utr_number", "booked_at", "checkin_time"]].rename(
        columns={"seat_id": "Seat", "name": "Name", "phone": "Phone",
                 "utr_number": "UTR", "booked_at": "Booked At",
                 "checkin_time": "Checked In"})


def render_admin(df: pd.DataFrame, prices: dict[str, int]) -> None:
    sold = int(((df["status"] == BOOKED) & (~df.apply(is_house_block, axis=1))).sum())
    pending = int((df["status"] == PENDING).sum())
    revenue = sum(prices[seat_tier(s)] for s in
                  df[(df["status"] == BOOKED)]["seat_id"]
                  if s not in BLOCKED_SEATS)

    a, b, c, d = st.columns(4)
    a.metric("Sold", sold)
    b.metric("Pending", pending)
    c.metric("Open", max(SELLABLE_SEATS - sold - pending, 0))
    d.metric("Revenue", f"₹{revenue:,}")

    queue_tab, price_tab, gate_tab, roster_tab, danger_tab = st.tabs(
        ["VERIFY", "PRICING", "GATE", "ROSTER", "DANGER"])

    with queue_tab:
        render_verification_queue(df, prices)
    with price_tab:
        render_pricing_controller(prices)
    with gate_tab:
        render_gate_scanner(df)
    with roster_tab:
        table = attendee_table(df, prices)
        if table.empty:
            st.info("No confirmed attendees yet.", icon="🎫")
        else:
            st.caption(f"{len(table)} confirmed · physical seat order. "
                       "Use the table toolbar to search or export CSV.")
            st.dataframe(table, hide_index=True, width="stretch")
    with danger_tab:
        rows = " · ".join(f"{r}:{n}" for r, n in ROW_CAPACITIES.items())
        st.warning(
            f"Reset wipes every booking and re-seeds {TOTAL_SEATS} seats, "
            f"re-applying {len(BLOCKED_SEATS)} house blocks "
            f"({SELLABLE_SEATS} sellable).\n\nLayout — {rows}\n\nNot reversible.",
            icon="⚠️")
        if IMPOSSIBLE_BLOCKS:
            st.info(
                "These pre-block entries were skipped because those seats do "
                f"not exist in the matrix: {', '.join(IMPOSSIBLE_BLOCKS)}. "
                "Row D holds seats 1-10 only. Sellable capacity is 157 either "
                "way — if Row D physically has 20 seats, set its capacity to 20 "
                "in ROW_CAPACITIES and the blocks will apply.", icon="ℹ️")
        confirmed = st.checkbox("I understand this deletes all bookings")
        if st.button("RESET DATABASE", key="reset_db",
                     disabled=not confirmed, width="stretch"):
            with st.spinner("Re-seeding…"):
                save_seats(blank_layout())
            for key in ("tickets", "_lookup", "_auto_dl", "_booked_phone",
                        "_booked_seat", "_locked_seat", BOOK_STEP,
                        SEL_ROW, SEL_SEAT):
                st.session_state.pop(key, None)
            st.success(f"Reset. {TOTAL_SEATS} seats seeded, "
                       f"{len(BLOCKED_SEATS)} blocked.", icon="✅")
            st.rerun()


# =============================================================================
# 14. ENTRY POINT
# =============================================================================


def main() -> None:
    st.set_page_config(page_title=f"{EVENT_NAME} — Tickets",
                       page_icon="🎭", layout="centered")

    # First script run of this browser session only. Streamlit re-executes the
    # whole script on every interaction, so an ungated intro would black the
    # screen out again on every form submit and every st.rerun().
    intro = not st.session_state.get("_intro_played", False)
    st.session_state["_intro_played"] = True

    inject_theme(intro=intro)
    if intro:
        splash_overlay()

    banner(HEADER_IMG, EVENT_NAME.upper())

    try:
        df = load_seats()
        st.session_state["_prices"] = load_prices()
    except Exception as exc:  # noqa: BLE001 — surface config errors
        st.error(f"Could not read the Google Sheet: {exc}", icon="🔌")
        banner(FOOTER_IMG, VENUE.upper())
        st.stop()
        return

    prices = st.session_state["_prices"]

    if df.empty:
        st.warning("Seat database is empty. Open Admin → Danger and reset it.",
                   icon="🗄️")

    book_tab, download_tab, admin_tab = st.tabs(
        ["BOOK TICKET", "DOWNLOAD TICKET", "ADMIN"])

    with book_tab:
        if st.session_state.get("_booked_seat"):
            render_pending_notice()
        else:
            render_booking(df, prices)
    with download_tab:
        render_download_tab(prices)
    with admin_tab:
        if admin_login():
            render_admin(df, prices)

    st.divider()
    banner(FOOTER_IMG, VENUE.upper())


if __name__ == "__main__":
    main()
