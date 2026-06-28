import os
import base64
import json
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# ── Zone-grid mapping (Vision-then-Math pipeline) ───────────────
# The Vision AI outputs only a zone label (< 100 tokens).
# resolve_coordinates() converts that label into exact grid cells —
# all spatial math stays in Python, not in the AI generation step.

ZONE_GRID: dict = {
    'background_perimeter': {'gx': (0,  19), 'gz': (15, 19)},
    'midground_left':       {'gx': (0,   6), 'gz': (8,  14)},
    'midground_center':     {'gx': (7,  12), 'gz': (8,  14)},
    'midground_right':      {'gx': (13, 19), 'gz': (8,  14)},
    'foreground_left':      {'gx': (0,   6), 'gz': (2,   7)},
    'foreground_center':    {'gx': (7,  12), 'gz': (2,   7)},
    'foreground_right':     {'gx': (13, 19), 'gz': (2,   7)},
    'blocked':              {'gx': (6,  13), 'gz': (7,  12)},
}

_VALID_ZONES = set(ZONE_GRID.keys())
_GRID_COLS = 20
_GRID_ROWS = 20


def resolve_coordinates(
    zone_id: str,
    lot_width: float = 10.0,
    lot_length: float = 10.0,
    grid_cols: int = _GRID_COLS,
    grid_rows: int = _GRID_ROWS,
) -> dict:
    """
    Map a Vision-AI zone label to grid-center coordinates and excluded cells.

    The Vision AI only identifies *which zone* an obstacle occupies.
    This function does all the spatial math, keeping AI output tiny.

    Returns:
        {
          'gx': int, 'gz': int,              # zone centre in grid coords
          'x': float, 'z': float,            # real-world metres
          'excluded_cells': [(gx, gz), ...]  # every cell inside the zone
        }
    """
    zone = ZONE_GRID.get(zone_id, ZONE_GRID['midground_center'])
    gx_min, gx_max = zone['gx']
    gz_min, gz_max = zone['gz']

    gx_center = (gx_min + gx_max) // 2
    gz_center = (gz_min + gz_max) // 2

    cell_w = lot_width / grid_cols
    cell_l = lot_length / grid_rows

    excluded = [
        (gx, gz)
        for gx in range(gx_min, gx_max + 1)
        for gz in range(gz_min, gz_max + 1)
    ]

    return {
        'gx': gx_center,
        'gz': gz_center,
        'x': round(gx_center * cell_w, 3),
        'z': round(gz_center * cell_l, 3),
        'excluded_cells': excluded,
    }


def scan_image_for_existing_elements(image_file):
    """
    Vision pre-pass: scans a garden photo and returns high-level
    placement-zone annotations only — no coordinates, no sizes.

    The prompt asks the model for zone labels exclusively (< 100-token
    response). resolve_coordinates() in views_api.py then handles all
    the spatial math from those labels.

    Returns:
        list of dicts [{zone, label, blocks_planting, is_existing}] or []
    """
    if not image_file:
        logger.warning("No image file provided to scan_image_for_existing_elements.")
        return []

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        from django.conf import settings
        api_key = getattr(settings, "GEMINI_API_KEY", None)

    if not api_key:
        logger.error("GEMINI_API_KEY is not configured.")
        return []

    try:
        if hasattr(image_file, 'read'):
            image_file.seek(0)
            image_bytes = image_file.read()
            image_file.seek(0)
            mime_type = getattr(image_file, 'content_type', 'image/jpeg') or 'image/jpeg'
        elif isinstance(image_file, bytes):
            image_bytes = image_file
            mime_type = 'image/jpeg'
        else:
            logger.error("Invalid image_file type: must be file-like or bytes.")
            return []

        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
    except Exception:
        logger.exception("Failed to encode image file.")
        return []

    try:
        client = genai.Client(api_key=api_key)
        image_part = types.Part.from_bytes(
            data=base64.b64decode(image_base64),
            mime_type=mime_type,
        )

        system_prompt = (
            "You are analyzing a garden or backyard photo to identify placement zones ONLY.\n\n"
            "Divide the visible ground into these eight named zones:\n"
            "  background_perimeter – far edge: fence line, house wall, back of garden\n"
            "  midground_left       – mid-distance, left third of the space\n"
            "  midground_center     – mid-distance, centre third\n"
            "  midground_right      – mid-distance, right third\n"
            "  foreground_left      – near viewer, left third\n"
            "  foreground_center    – near viewer, centre third\n"
            "  foreground_right     – near viewer, right third\n"
            "  blocked              – any area already occupied and unplantable "
            "(pool, patio, path, existing tree canopy, shed)\n\n"
            "For each distinct feature you see, output exactly ONE JSON object:\n"
            "  zone            – one of the eight zone names above\n"
            "  label           – a short snake_case label (e.g. swimming_pool, house_wall)\n"
            "  blocks_planting – true if this prevents new planting, false if visual backdrop only\n"
            "  is_existing     – always true\n\n"
            "CRITICAL: Return ONLY a raw JSON array. "
            "No coordinates. No sizes. No explanations.\n"
            'Example: [{"zone":"blocked","label":"patio","blocks_planting":true,"is_existing":true}]'
        )

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                image_part,
                "List each existing garden feature with its placement zone as JSON.",
            ],
            config={
                "system_instruction": system_prompt,
                "response_mime_type": "application/json",
                "temperature": 0.1,
            },
        )

        response_text = response.text
        if not response_text:
            logger.warning("Empty response from Vision AI zone scan.")
            return []

        cleaned = response_text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        parsed = json.loads(cleaned)
        if not isinstance(parsed, list):
            logger.warning("Vision AI returned non-list payload: %s", type(parsed))
            return []

        result = []
        for item in parsed:
            zone = item.get('zone', '')
            if zone not in _VALID_ZONES:
                logger.debug("Ignoring unrecognised zone '%s' from Vision AI.", zone)
                continue
            result.append({
                'zone': zone,
                'label': item.get('label', 'unknown'),
                'blocks_planting': bool(item.get('blocks_planting', True)),
                'is_existing': True,
            })
        return result

    except json.JSONDecodeError as e:
        logger.error("JSON decode failed for Vision AI zone scan: %s", e)
        return []
    except Exception:
        logger.exception("Failed during Vision AI zone scan.")
        return []
