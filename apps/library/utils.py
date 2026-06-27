import os
import base64
import json
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

def scan_image_for_existing_elements(image_file):
    """
    Scans an uploaded garden reference photo to identify existing elements
    (e.g., trees, bushes, rocks) and estimate their 3D coordinates and scale.

    Args:
        image_file: Django uploaded file (or file-like object/bytes) representing the image.

    Returns:
        list of dicts matching the Vision AI schema, or [] on failure.
    """
    if not image_file:
        logger.warning("No image file provided to scan_image_for_existing_elements.")
        return []

    # Reference Gemini API key configured in environment
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        from django.conf import settings
        api_key = getattr(settings, "GEMINI_API_KEY", None)

    if not api_key:
        logger.error("GEMINI_API_KEY is not configured in environment or django settings.")
        return []

    try:
        # Convert incoming image_file to base64 so it can be attached to the API payload
        if hasattr(image_file, 'read'):
            image_file.seek(0)
            image_bytes = image_file.read()
            image_file.seek(0)  # Reset stream pointer
            mime_type = getattr(image_file, 'content_type', 'image/jpeg') or 'image/jpeg'
        elif isinstance(image_file, bytes):
            image_bytes = image_file
            mime_type = 'image/jpeg'
        else:
            logger.error("Invalid image_file type provided. Must be a file-like object or bytes.")
            return []

        # base64 representation of the image
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
    except Exception as e:
        logger.exception("Failed to read and base64-encode image file.")
        return []

    try:
        # Initialize the GenAI client with key
        client = genai.Client(api_key=api_key)

        # Attach image to the API payload (re-decoding the base64 string to bytes as required by the SDK)
        image_part = types.Part.from_bytes(
            data=base64.b64decode(image_base64),
            mime_type=mime_type
        )

        system_prompt = (
            "Analyze this garden/backyard photo. Assume a 3D ground grid where the bottom-center of the image is (x: 0, z: 0). \n"
            "Identify existing objects and classify them strictly into one of two categories:\n"
            "1. 'ground_obstacle': Items physically on the lawn/dirt where things cannot be planted (e.g., pools, patio floors, pathways, existing trees).\n"
            "2. 'structural_wall': Vertical backgrounds where planting is impossible and that act as a visual boundary (e.g., the house facade, fences, retaining walls).\n\n"
            "Estimate their approximate center (x, z) coordinates from -10 to +10, and their real-world bounding size (width/diameter and height in meters).\n\n"
            "Return ONLY a raw JSON array matching this exact schema:\n"
            "[\n"
            "  { \"type\": \"ground_obstacle\", \"label\": \"swimming_pool\", \"x\": 2.0, \"z\": 5.0, \"width\": 4.0, \"height\": 0.2, \"is_existing\": true },\n"
            "  { \"type\": \"structural_wall\", \"label\": \"house_wall\", \"x\": 0.0, \"z\": 8.5, \"width\": 12.0, \"height\": 6.0, \"is_existing\": true }\n"
            "]"
        )

        # Make the Vision API call synchronously
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                image_part,
                "Scan the image and list all existing physical elements as JSON."
            ],
            config={
                "system_instruction": system_prompt,
                "response_mime_type": "application/json",
                "temperature": 0.1
            }
        )

        response_text = response.text
        if not response_text:
            logger.warning("Empty response from Vision AI model.")
            return []

        # Clean potential markdown block formatting from model response
        cleaned_text = response_text.strip()
        if cleaned_text.startswith("```json"):
            cleaned_text = cleaned_text[7:]
        if cleaned_text.endswith("```"):
            cleaned_text = cleaned_text[:-3]
        cleaned_text = cleaned_text.strip()

        # Parse JSON output into list of dicts
        parsed_data = json.loads(cleaned_text)
        if isinstance(parsed_data, list):
            return parsed_data
        else:
            logger.warning(f"Vision AI returned non-list JSON payload: {type(parsed_data)}")
            return []

    except json.JSONDecodeError as jde:
        logger.error(f"JSON decode failed for Vision AI response: {jde}. Raw response: {response_text}")
        return []
    except Exception as e:
        logger.exception("Failed during Vision AI garden scan pre-pass.")
        return []
