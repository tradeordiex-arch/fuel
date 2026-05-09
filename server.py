"""
FUEL Health Dashboard — Local API Server
Serves static files + proxies to Claude Haiku for:
  - /api/scan   (image → food identification)
  - /api/chat   (health coach conversation)
  - /api/lookup (3-tier ingredient lookup)
"""

import json, os, sys, base64
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import urllib.request
import urllib.parse

# ── Load env ──
# Check local .env first, then NQ Data config as fallback
for env_path in [
    Path(__file__).resolve().parent / ".env",
    Path(__file__).resolve().parent.parent / "config" / ".env",
]:
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
        break

import anthropic

client = anthropic.Anthropic()
MODEL = "claude-haiku-4-5-20251001"

# ── Supabase config ──
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://shcemayremkowwmzuxgd.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


def supabase_query(path: str, method: str = "GET", body: dict = None) -> dict | list | None:
    """Direct Supabase REST API call using service role key."""
    if not SUPABASE_SERVICE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[Supabase] {method} {path} failed: {e}")
        return None


# ═══════════════════════════════════════
# 3-TIER INGREDIENT LOOKUP
# ═══════════════════════════════════════
def lookup_ingredient(query: str) -> dict:
    """
    Tier 1: FUEL verified DB (Supabase ingredients table)
    Tier 2: AI generation (Haiku)
    Result from Tier 2 → written to staging table for review
    """
    normalized = query.strip().lower()
    if not normalized:
        return {"error": "empty query"}

    # ── Tier 1: Supabase verified ingredients ──
    # Try exact match first, then fuzzy
    safe_q = urllib.parse.quote(normalized)
    result = supabase_query(f"ingredients?name_normalized=eq.{safe_q}&limit=1")
    if result and len(result) > 0:
        row = result[0]
        return {
            "name": row["name"], "emoji": row.get("emoji", "🥘"),
            "calories": row["calories"], "protein": row["protein"],
            "carbs": row["carbs"], "fat": row["fat"],
            "fiber": row.get("fiber", 0), "sugar": row.get("sugar", 0),
            "serving": row["serving_desc"],
            "serving_grams": row.get("serving_grams"),
            "category": row.get("category", ""),
            "source": "verified",
        }

    # Try alias match
    result = supabase_query(f"ingredients?aliases=cs.{{{safe_q}}}&limit=1")
    if result and len(result) > 0:
        row = result[0]
        return {
            "name": row["name"], "emoji": row.get("emoji", "🥘"),
            "calories": row["calories"], "protein": row["protein"],
            "carbs": row["carbs"], "fat": row["fat"],
            "fiber": row.get("fiber", 0), "sugar": row.get("sugar", 0),
            "serving": row["serving_desc"],
            "serving_grams": row.get("serving_grams"),
            "category": row.get("category", ""),
            "source": "verified",
        }

    # Try full-text search
    words = normalized.replace("'", "").split()
    ts_query = " & ".join(words)
    safe_ts = urllib.parse.quote(ts_query)
    result = supabase_query(f"ingredients?search_vector=fts.{safe_ts}&limit=1")
    if result and len(result) > 0:
        row = result[0]
        return {
            "name": row["name"], "emoji": row.get("emoji", "🥘"),
            "calories": row["calories"], "protein": row["protein"],
            "carbs": row["carbs"], "fat": row["fat"],
            "fiber": row.get("fiber", 0), "sugar": row.get("sugar", 0),
            "serving": row["serving_desc"],
            "serving_grams": row.get("serving_grams"),
            "category": row.get("category", ""),
            "source": "verified",
        }

    # ── Check staging (maybe we already AI'd this) ──
    result = supabase_query(f"ingredient_staging?name_normalized=eq.{safe_q}&limit=1")
    if result and len(result) > 0:
        row = result[0]
        # Bump lookup count
        supabase_query(
            f"ingredient_staging?id=eq.{row['id']}",
            method="PATCH",
            body={"lookup_count": row.get("lookup_count", 1) + 1, "last_looked_up": "now()"}
        )
        return {
            "name": row["name"], "emoji": row.get("emoji", "🥘"),
            "calories": row["calories"], "protein": row["protein"],
            "carbs": row["carbs"], "fat": row["fat"],
            "fiber": row.get("fiber", 0), "sugar": row.get("sugar", 0),
            "serving": row["serving_desc"],
            "category": row.get("category", ""),
            "source": "ai_cached",
        }

    # ── Tier 2: AI generation ──
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f'What are the nutrition facts for "{query}"? '
                    "Return ONLY valid JSON, no markdown:\n"
                    '{"name":"Proper Name","emoji":"🥘","calories":000,'
                    '"protein":00,"carbs":00,"fat":00,"fiber":00,"sugar":00,'
                    '"sodium":00,"serving":"1 medium","serving_grams":000,'
                    '"category":"protein|vegetable|fruit|grain|dairy|healthy_fat|legume|beverage|condiment",'
                    '"storage":"fridge|freezer|pantry","shelf_days":7}\n'
                    "Use standard serving size. Be accurate — this is for a nutrition tracking app."
                ),
            }],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)

        # Write to staging table
        staging_row = {
            "name": data.get("name", query),
            "name_normalized": normalized,
            "emoji": data.get("emoji", "🥘"),
            "calories": data.get("calories", 0),
            "protein": data.get("protein", 0),
            "carbs": data.get("carbs", 0),
            "fat": data.get("fat", 0),
            "fiber": data.get("fiber", 0),
            "sugar": data.get("sugar", 0),
            "sodium": data.get("sodium", 0),
            "serving_desc": data.get("serving", "1 serving"),
            "serving_grams": data.get("serving_grams"),
            "category": data.get("category", ""),
            "storage": data.get("storage", "fridge"),
            "source": "ai",
        }
        supabase_query("ingredient_staging", method="POST", body=staging_row)

        return {
            "name": data.get("name", query),
            "emoji": data.get("emoji", "🥘"),
            "calories": data.get("calories", 0),
            "protein": data.get("protein", 0),
            "carbs": data.get("carbs", 0),
            "fat": data.get("fat", 0),
            "fiber": data.get("fiber", 0),
            "sugar": data.get("sugar", 0),
            "serving": data.get("serving", "1 serving"),
            "serving_grams": data.get("serving_grams"),
            "category": data.get("category", ""),
            "source": "ai",
        }
    except Exception as e:
        print(f"[Lookup AI] failed: {e}")
        return {"error": str(e)}


def _parse_json_response(text: str):
    """Strip markdown fences and parse JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    return json.loads(text)


def _classify_image(image_b64: str, media_type: str) -> dict:
    """Quick call: is this a packaged product or homemade food?"""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": image_b64},
                },
                {
                    "type": "text",
                    "text": (
                        "Is this a packaged/branded commercial product (with packaging, label, brand name visible) "
                        "or is it homemade/restaurant/unpackaged food?\n\n"
                        "Return ONLY valid JSON: {\"packaged\": true, \"product\": \"Brand Product Name\"} "
                        "or {\"packaged\": false, \"product\": \"\"}\n\n"
                        "Packaged = you can see a wrapper, box, bag, label, brand logo, nutrition facts, or "
                        "it's clearly a commercial product still in/on its packaging.\n"
                        "NOT packaged = plated food, food on a cutting board, loose fruit, restaurant meal, "
                        "homemade cooking, food without any visible commercial packaging."
                    ),
                },
            ],
        }],
    )
    return _parse_json_response(resp.content[0].text)


def _scan_packaged(image_b64: str, media_type: str, product_name: str) -> dict:
    """Scan a packaged product — return as single item with label nutrition."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": image_b64},
                },
                {
                    "type": "text",
                    "text": (
                        f"This is a packaged product: \"{product_name}\"\n\n"
                        "Read the nutrition label if visible, or use your knowledge of this product.\n"
                        "Return ONLY valid JSON, no markdown:\n"
                        '{"name": "Brand + Product Name", "description": "brief description", '
                        '"category": "savory|sweet", "packaged": true, '
                        '"tags": ["HIGH PROTEIN", ...], '
                        '"health_note": "one-sentence nutritional insight", '
                        '"calories": 000, "protein": 00, "carbs": 00, "fat": 00, "fiber": 00}\n\n'
                        "Macros are per serving from the label. "
                        "Tags: HIGH PROTEIN, LOW CARB, HIGH FIBER, BALANCED, LIGHT, WHOLE GRAIN, HEALTHY FATS."
                    ),
                },
            ],
        }],
    )
    result = _parse_json_response(resp.content[0].text)
    result["packaged"] = True
    # Wrap as single ingredient for frontend consistency
    result["ingredients"] = [{
        "name": result["name"],
        "emoji": "📦",
        "calories": result.get("calories", 0),
        "protein": result.get("protein", 0),
        "carbs": result.get("carbs", 0),
        "fat": result.get("fat", 0),
        "fiber": result.get("fiber", 0),
    }]
    return result


def _scan_homemade(image_b64: str, media_type: str) -> dict:
    """Scan homemade/restaurant food — decompose into ingredients."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": image_b64},
                },
                {
                    "type": "text",
                    "text": (
                        "Identify the food in this image.\n\n"
                        "CRITICAL: You MUST break it down into individual SUBSTANTIAL ingredients. "
                        "Do NOT return the meal as a single item. Always decompose into parts.\n\n"
                        "Return ONLY valid JSON, no markdown:\n"
                        '{"name": "Meal Name", "description": "brief description", '
                        '"category": "savory|sweet", "packaged": false, '
                        '"tags": ["HIGH PROTEIN", ...], '
                        '"health_note": "one-sentence nutritional insight", '
                        '"ingredients": [\n'
                        '  {"name": "Ingredient", "emoji": "🥩", '
                        '"calories": 000, "protein": 00, "carbs": 00, "fat": 00, "fiber": 00}\n'
                        ']}\n\n'
                        "Rules:\n"
                        "- The 'ingredients' array is MANDATORY. Never omit it.\n"
                        "- List each substantial ingredient separately (meat, grain, vegetables, cheese, sauce, toppings)\n"
                        "- ONLY include ingredients with 10+ calories in the visible portion. Skip salt, pepper, garlic, herbs, spices.\n"
                        "- Macros are for the estimated portion visible in the image\n"
                        "- Use accurate per-ingredient macros, not rough splits of a total\n"
                        "- emoji should be a single food emoji that represents the ingredient\n"
                        "- Aim for 3-7 ingredients per meal. Don't over-decompose.\n\n"
                        "Example for a burger: [{\"name\": \"Beef patty\", \"emoji\": \"🥩\", \"calories\": 250, \"protein\": 20, \"carbs\": 0, \"fat\": 18, \"fiber\": 0}, "
                        "{\"name\": \"Brioche bun\", \"emoji\": \"🍞\", \"calories\": 200, \"protein\": 5, \"carbs\": 36, \"fat\": 4, \"fiber\": 1}, "
                        "{\"name\": \"Cheddar cheese\", \"emoji\": \"🧀\", \"calories\": 110, \"protein\": 7, \"carbs\": 0, \"fat\": 9, \"fiber\": 0}]\n\n"
                        "Tags: HIGH PROTEIN, LOW CARB, HIGH FIBER, RICH IN GREENS, "
                        "BALANCED, LIGHT, WHOLE GRAIN, HEALTHY FATS."
                    ),
                },
            ],
        }],
    )
    result = _parse_json_response(resp.content[0].text)
    result["packaged"] = False

    # If AI didn't return ingredients, decompose via a follow-up call
    if "ingredients" not in result or not result["ingredients"]:
        try:
            decomp = client.messages.create(
                model=MODEL,
                max_tokens=800,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Break down \"{result.get('name', 'this meal')}\" ({result.get('description', '')}) "
                        f"into its substantial ingredients (10+ calories each). "
                        f"Total macros: {result.get('calories', 0)} cal, {result.get('protein', 0)}g protein, "
                        f"{result.get('carbs', 0)}g carbs, {result.get('fat', 0)}g fat.\n"
                        "Return ONLY a JSON array, no markdown:\n"
                        '[{"name": "Ingredient", "emoji": "🥩", '
                        '"calories": 000, "protein": 00, "carbs": 00, "fat": 00, "fiber": 00}]\n'
                        "Split the total macros accurately across ingredients. 3-7 items."
                    ),
                }],
            )
            result["ingredients"] = _parse_json_response(decomp.content[0].text)
        except Exception:
            pass

    # Compute totals from ingredients
    if "ingredients" in result and result["ingredients"]:
        result["calories"] = sum(i.get("calories", 0) for i in result["ingredients"])
        result["protein"] = sum(i.get("protein", 0) for i in result["ingredients"])
        result["carbs"] = sum(i.get("carbs", 0) for i in result["ingredients"])
        result["fat"] = sum(i.get("fat", 0) for i in result["ingredients"])
        result["fiber"] = sum(i.get("fiber", 0) for i in result["ingredients"])

    return result


def scan_image(image_b64: str, media_type: str = "image/jpeg") -> dict:
    """Two-step scan: classify packaged vs homemade, then branch."""
    # Step 1: Quick classify
    try:
        classify = _classify_image(image_b64, media_type)
    except Exception:
        classify = {"packaged": False}

    # Step 2: Branch based on classification
    if classify.get("packaged"):
        return _scan_packaged(image_b64, media_type, classify.get("product", ""))
    else:
        return _scan_homemade(image_b64, media_type)


def chat_coach(messages: list, daily_state: dict) -> str:
    """Health coach conversation with context. Can return actions."""
    system = (
        "You are FUEL AI, a premium health coach inside a calorie-tracking app.\n\n"
        "COMMUNICATION STYLE — follow these rules exactly:\n"
        "1. Put a BLANK LINE between every paragraph. This is mandatory.\n"
        "2. Each paragraph is 1-2 sentences MAX. Never more.\n"
        "3. Lead with the key number or answer. Not the reasoning.\n"
        "4. Use **bold** for important numbers and key takeaways.\n"
        "5. Never write more than 5 paragraphs total.\n"
        "6. For lists, use bullet points with line breaks between them.\n"
        "7. Be warm but direct. Sharp friend, not a doctor. Never preachy.\n\n"
        "EXAMPLE of correct format:\n"
        "You've got **489 cal** and **71g protein** left today.\n\n"
        "Protein is your biggest gap right now. Lean into it at dinner.\n\n"
        "A grilled chicken breast with veggies would cover it — about **350 cal, 40g protein**.\n\n"
        "Go easy on carbs and fat, you're nearly maxed on both.\n\n"
        f"User's full state: {json.dumps(daily_state)}\n\n"
        "You have FULL ACCESS to:\n"
        "- Profile: name, age, sex, weight, height, goal, dietary restrictions, activity level\n"
        "- Today: every meal logged (with full macros), calories/protein/carbs/fat consumed and remaining, water intake\n"
        "- Pantry: what ingredients the user has at home RIGHT NOW — suggest meals from these when relevant\n"
        "- Recent history: last 7 days of meals, scores, and water — use this to spot patterns, praise streaks, and give context-aware advice\n"
        "- Streak: how many consecutive days they've logged\n\n"
        "USE the pantry and history actively. If someone asks 'what should I eat?', check their pantry and suggest something they can actually make.\n"
        "If you notice patterns in their history (e.g., low protein every day, skipping breakfast), mention it.\n\n"
        "IMPORTANT — ACTION COMMANDS:\n"
        "You can take actions by including a hidden JSON block at the END of your response.\n"
        "Format: <!--ACTION:{...}-->\n"
        "NEVER show the ACTION tag in your visible text. Put it at the very end.\n\n"
        "ACTION TYPE 1 — Update Profile:\n"
        "<!--ACTION:{\"type\":\"update_profile\",\"changes\":{\"field\":\"value\"}}-->\n"
        "Fields: calories, protein, carbs, fat (numbers), weight (lbs), goal (lose/maintain/build), name (string), water_goal (number)\n"
        "Example: 'Set my protein to 160g' → '...<!--ACTION:{\"type\":\"update_profile\",\"changes\":{\"protein\":160}}-->'\n\n"
        "ACTION TYPE 2 — Add to Cart (grocery shopping list):\n"
        "When the user asks to add a meal or recipe to their cart, add all its ingredients.\n"
        "<!--ACTION:{\"type\":\"add_to_cart\",\"meal\":\"Meal Name\",\"items\":[{\"name\":\"Chicken Breast\",\"emoji\":\"🍗\",\"qty\":\"1 lb\",\"category\":\"protein\"},{\"name\":\"Broccoli\",\"emoji\":\"🥦\",\"qty\":\"2 cups\",\"category\":\"produce\"}]}-->\n"
        "Include EVERY ingredient needed to make the meal from scratch. Use realistic grocery quantities.\n"
        "Categories: protein, produce, dairy, grain, pantry, spice, oil, sauce\n"
        "Example: 'Add broccoli cheese casserole to cart' → 'Added to your cart! ...<!--ACTION:{\"type\":\"add_to_cart\",\"meal\":\"Broccoli Cheese Casserole\",\"items\":[...]}-->'\n\n"
        "ACTION TYPE 3 — Add to Pantry:\n"
        "When the user says they bought something or have something at home.\n"
        "<!--ACTION:{\"type\":\"add_to_pantry\",\"items\":[\"chicken\",\"rice\",\"broccoli\"]}-->\n"
        "Example: 'I just bought eggs and milk' → 'Nice, added to your pantry.<!--ACTION:{\"type\":\"add_to_pantry\",\"items\":[\"eggs\",\"milk\"]}-->'\n\n"
        "If the user is NOT asking to change anything or add anything, just respond normally with no ACTION tag.\n\n"
        "If they ask about their stats, goals, or profile, reference the data above directly.\n"
        "If they ask about meals, suggest specific foods with calorie/macro estimates.\n"
        "Refer to their remaining calories/macros when relevant."
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=system,
        messages=messages,
    )
    return resp.content[0].text.strip()


def generate_briefing(snapshot: dict) -> dict:
    """Generate a personalized daily briefing — headline + detail."""
    system = (
        "You are a sharp, warm nutrition coach writing a daily briefing for a health app.\n\n"
        "RULES:\n"
        "- Return ONLY valid JSON: {\"headline\": \"...\", \"detail\": \"...\"}\n"
        "- headline: ONE sentence, max 120 chars. Truncatable with ellipsis. Actionable and specific.\n"
        "- detail: 1-2 follow-up sentences with concrete advice. Max 200 chars.\n"
        "- Reference specific numbers, meal names, and patterns from the data.\n"
        "- Tone: direct, encouraging, never preachy. Like a friend who happens to know nutrition.\n"
        "- If the user closed a gap (e.g. hit protein after being short), celebrate it.\n"
        "- If there's a gap, give a specific fix — name a food, not a category.\n"
        "- Never say 'consider' or 'you might want to'. Just say what to do.\n"
        "- Time-aware: morning = plan ahead, noon = adjust course, evening = reflect + tomorrow.\n"
        "- No emojis. No exclamation marks. Confident and calm.\n"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": json.dumps(snapshot)}],
    )
    try:
        raw = resp.content[0].text.strip()
        # Handle potential markdown code block wrapping
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except (json.JSONDecodeError, IndexError):
        return {"headline": "", "detail": ""}


def suggest_meals(remaining_cal: int, remaining_protein: int, flavor: str = "all",
                   goal_focus: str = "", diets: list = None,
                   remaining_carbs: int = 0, remaining_fat: int = 0,
                   eaten_today: list = None, time_of_day: str = "",
                   pantry: list = None) -> list:
    """Generate meal suggestions based on remaining goals + context."""
    focus_instruction = ""
    if goal_focus == "protein":
        focus_instruction = "IMPORTANT: Focus heavily on HIGH PROTEIN options (30g+ protein per serving). Every suggestion must be protein-rich. "
    elif goal_focus == "greens":
        focus_instruction = "IMPORTANT: Focus heavily on GREENS and vegetables. Every suggestion must feature leafy greens, vegetables, or plant-based ingredients prominently. "
    elif goal_focus == "fiber":
        focus_instruction = "IMPORTANT: Focus heavily on HIGH FIBER options (8g+ fiber per serving). Feature whole grains, legumes, beans, vegetables, fruits with skin. "

    diet_instruction = ""
    if diets and len(diets) > 0:
        diet_instruction = f"DIETARY RESTRICTIONS: All suggestions MUST be {', '.join(diets)}. Do not include any ingredients that violate these restrictions. "

    # Context-aware instructions
    context_parts = []

    # Macro balance
    if remaining_fat and remaining_fat < 10:
        context_parts.append("User is nearly maxed on fat — suggest LOW FAT options, avoid fried/oily/cheesy dishes.")
    if remaining_carbs and remaining_carbs < 30:
        context_parts.append("User is nearly maxed on carbs — suggest LOW CARB options, avoid rice/bread/pasta-heavy dishes.")

    # Avoid repetition
    if eaten_today and len(eaten_today) > 0:
        context_parts.append(f"Already eaten today: {', '.join(eaten_today)}. Do NOT suggest similar meals — offer variety in protein source, cuisine, and cooking style.")

    # Time-appropriate
    time_hints = {
        "late_night": "It's late — suggest LIGHT options only: herbal tea, small snacks, yogurt, a handful of nuts. Nothing heavy. Keep under 200 cal each.",
        "breakfast": "Suggest BREAKFAST-appropriate meals: eggs, oatmeal, smoothies, yogurt, toast. No heavy dinners.",
        "late_morning": "Suggest late morning snacks or light brunch items.",
        "lunch": "Suggest LUNCH-appropriate meals: salads, wraps, bowls, sandwiches, soups.",
        "afternoon_snack": "Suggest SNACK-sized options: protein bars, fruit, nuts, yogurt, small bites. Keep portions small (100-300 cal).",
        "dinner": "Suggest DINNER-appropriate meals: full entrees, proteins with sides, stir-fry, pasta, grilled dishes.",
    }
    if time_of_day and time_of_day in time_hints:
        context_parts.append(time_hints[time_of_day])

    # Pantry awareness
    if pantry and len(pantry) > 0:
        context_parts.append(f"User has these ingredients at home: {', '.join(pantry[:15])}. When possible, favor meals they can make from what they have. Mark those with a '🏠' at the start of the one_liner.")

    context_block = "\n".join(context_parts)

    resp = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        messages=[{
            "role": "user",
            "content": (
                f"Suggest 8 meals/snacks that fit within {remaining_cal} remaining calories "
                f"and help hit {remaining_protein}g remaining protein goal. "
                f"{'Only ' + flavor + ' options.' if flavor != 'all' else 'Mix of sweet and savory.'}\n"
                f"{focus_instruction}"
                f"{diet_instruction}"
                f"{context_block}\n" if context_block else ""
                "Return ONLY a JSON array, no markdown:\n"
                '[{"name": "Meal Name", "calories": 000, "protein": 00, "carbs": 00, '
                '"fat": 00, "fiber": 00, "category": "savory|sweet", '
                '"tags": ["HIGH PROTEIN"], "one_liner": "brief appetizing description"}]\n'
                "Make them diverse, realistic, and appetizing. Use real dishes, not generic. "
                "Include exact macro numbers. Tags: HIGH PROTEIN, LOW CARB, HIGH FIBER, "
                "RICH IN GREENS, BALANCED, LIGHT, WHOLE GRAIN, HEALTHY FATS."
            ),
        }],
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    return json.loads(text)


def describe_meal(text: str) -> dict:
    """Text description → structured meal data. Detects packaged products vs homemade."""
    # Step 1: Quick classify — is this a brand/packaged product?
    try:
        classify = client.messages.create(
            model=MODEL,
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": (
                    f"Is \"{text}\" a specific brand-name or commercial packaged product "
                    "(frozen meal, snack bar, candy, chips, canned food, fast food chain item, "
                    "protein bar, bottled drink, etc.)?\n\n"
                    "Return ONLY: {\"packaged\": true, \"product\": \"Brand Product Name\"} "
                    "or {\"packaged\": false, \"product\": \"\"}"
                ),
            }],
        )
        cls = _parse_json_response(classify.content[0].text)
    except Exception:
        cls = {"packaged": False}

    # Step 2: Branch
    if cls.get("packaged"):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": (
                    f"The user ate: \"{text}\"\n"
                    f"This is a packaged product: \"{cls.get('product', text)}\"\n\n"
                    "Return ONLY valid JSON with the product's label nutrition per serving:\n"
                    '{"name": "Brand + Product Name", "description": "brief description", '
                    '"category": "savory|sweet", "packaged": true, '
                    '"tags": ["HIGH PROTEIN", ...], '
                    '"health_note": "one-sentence nutritional insight", '
                    '"calories": 000, "protein": 00, "carbs": 00, "fat": 00, "fiber": 00}\n\n'
                    "Tags: HIGH PROTEIN, LOW CARB, HIGH FIBER, BALANCED, LIGHT, WHOLE GRAIN, HEALTHY FATS."
                ),
            }],
        )
        result = _parse_json_response(resp.content[0].text)
        result["packaged"] = True
        result["ingredients"] = [{
            "name": result["name"],
            "emoji": "📦",
            "calories": result.get("calories", 0),
            "protein": result.get("protein", 0),
            "carbs": result.get("carbs", 0),
            "fat": result.get("fat", 0),
            "fiber": result.get("fiber", 0),
        }]
    else:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1200,
            messages=[{
                "role": "user",
                "content": (
                    f"The user described a meal they ate: \"{text}\"\n\n"
                    "Break it down into individual SUBSTANTIAL ingredients.\n"
                    "Return ONLY valid JSON, no markdown:\n"
                    '{"name": "Meal Name", "description": "brief description", '
                    '"category": "savory|sweet", "packaged": false, '
                    '"tags": ["HIGH PROTEIN", ...], '
                    '"health_note": "one-sentence nutritional insight", '
                    '"ingredients": [\n'
                    '  {"name": "Ingredient", "emoji": "🥩", '
                    '"calories": 000, "protein": 00, "carbs": 00, "fat": 00, "fiber": 00}\n'
                    ']}\n\n'
                    "Rules:\n"
                    "- The 'ingredients' array is MANDATORY.\n"
                    "- Only include ingredients with 10+ calories. Skip salt, pepper, herbs, spices.\n"
                    "- Aim for 3-7 ingredients. Don't over-decompose.\n"
                    "- Be accurate with calorie estimates.\n"
                    "- If the description is vague, make reasonable assumptions.\n"
                    "Tags: HIGH PROTEIN, LOW CARB, HIGH FIBER, "
                    "RICH IN GREENS, BALANCED, LIGHT, WHOLE GRAIN, HEALTHY FATS."
                ),
            }],
        )
        result = _parse_json_response(resp.content[0].text)
        result["packaged"] = False

        # Compute totals from ingredients
        if "ingredients" in result and result["ingredients"]:
            result["calories"] = sum(i.get("calories", 0) for i in result["ingredients"])
            result["protein"] = sum(i.get("protein", 0) for i in result["ingredients"])
            result["carbs"] = sum(i.get("carbs", 0) for i in result["ingredients"])
            result["fat"] = sum(i.get("fat", 0) for i in result["ingredients"])
            result["fiber"] = sum(i.get("fiber", 0) for i in result["ingredients"])

    return result


def classify_foods(food_list: list) -> dict:
    """Classify a list of food names into food groups."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": (
                f"Classify these foods into food groups. Return ONLY valid JSON, no markdown.\n"
                f"Foods: {json.dumps(food_list)}\n\n"
                'Return: {{"food_name": "protein|vegetable|fruit|grain|dairy|healthy_fat|legume|herb|condiment|beverage|snack|other", ...}}\n\n'
                "Rules:\n"
                "- protein: meat, fish, poultry, eggs, tofu, tempeh, protein powder\n"
                "- vegetable: all vegetables, leafy greens, root vegetables\n"
                "- fruit: all fruits including exotic (guava, dragonfruit, lychee, etc.)\n"
                "- grain: rice, bread, pasta, oats, quinoa, couscous, barley, cereal, tortilla\n"
                "- dairy: milk, cheese, yogurt, butter, cream\n"
                "- healthy_fat: avocado, nuts, seeds, olive oil, nut butters\n"
                "- legume: beans, lentils, chickpeas, hummus\n"
                "- herb: fresh herbs, dried spices, seasonings\n"
                "- condiment: sauces, dressings, mustard, vinegar, honey\n"
                "- beverage: juice, coffee, tea, kombucha, drinks\n"
                "- snack: chips, crackers, bars, popcorn\n"
                "- other: anything that doesn't fit above"
            ),
        }],
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    return json.loads(text)


def scan_receipt(image_b64: str, media_type: str = "image/jpeg") -> list:
    """Extract grocery items from a receipt photo."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": { "type": "base64", "media_type": media_type, "data": image_b64 },
                },
                {
                    "type": "text",
                    "text": (
                        "This is a grocery receipt. Extract every food item purchased.\n"
                        "Return ONLY a JSON array, no markdown:\n"
                        '[{"name": "Chicken breast", "qty": 1.5, "unit": "lb", '
                        '"price": 7.99, "category": "protein|produce|dairy|grain|pantry|spice", '
                        '"location": "fridge|freezer|pantry", '
                        '"days_until_expiry": 4, "emoji": "🍗"}]\n\n'
                        "Rules:\n"
                        "- Use common grocery names (not brand-specific codes)\n"
                        "- Estimate quantity from the receipt line if possible\n"
                        "- 'location' = where this item goes: fridge (fresh meat, dairy, produce), "
                        "freezer (frozen items), pantry (canned, dried, shelf-stable)\n"
                        "- 'days_until_expiry' = typical shelf life from purchase date: "
                        "fresh meat 3-5, produce 5-10, dairy 7-14, frozen 90-180, pantry 180-365\n"
                        "- Skip non-food items (bags, cleaning products, etc.)"
                    ),
                },
            ],
        }],
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    return json.loads(text)


def suggest_from_pantry(ingredients: str, remaining_cal: int = 800, remaining_protein: int = 60) -> list:
    """Suggest meals the user can make from ingredients they have."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        messages=[{
            "role": "user",
            "content": (
                f"The user has these ingredients at home: {ingredients}\n"
                f"They have {remaining_cal} calories and {remaining_protein}g protein remaining today.\n\n"
                "Suggest 6 meals they can make using PRIMARILY these ingredients. "
                "They may need 1-2 small additional items (spices, oil, etc.) but the core should come from what they have.\n\n"
                "For each meal, note if any small additions are needed.\n\n"
                "Return ONLY a JSON array, no markdown:\n"
                '[{"name": "Meal Name", "calories": 000, "protein": 00, "carbs": 00, '
                '"fat": 00, "category": "savory|sweet", '
                '"tags": ["HIGH PROTEIN"], "one_liner": "brief description", '
                '"uses": ["chicken", "broccoli", "rice"], '
                '"needs": ["soy sauce", "garlic"]}]\n'
                "Make them diverse, realistic, and appetizing. Include exact macro numbers."
            ),
        }],
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    return json.loads(text)


def get_full_recipe(name: str, description: str = "") -> dict:
    """Get complete recipe: ingredients (with dual pricing) + steps + prep time."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": (
                f"Give me the complete recipe for this meal:\n"
                f"Meal: {name}\n"
                f"{'Description: ' + description if description else ''}\n\n"
                "Return ONLY valid JSON, no markdown:\n"
                '{"prep_time": "20 min", "cook_time": "15 min", '
                '"ingredients": [{"name": "Chicken breast", '
                '"recipe_qty": "6 oz", "purchase_qty": "1 lb", '
                '"recipe_cost": 2.10, "store_cost": 5.99, '
                '"servings_from_purchase": 2.6, '
                '"category": "protein", "staple": false, "emoji": "🍗"}], '
                '"steps": ["Step 1 text", "Step 2 text", ...], '
                '"health_note": "One sentence nutritional insight"}\n\n'
                "Rules for ingredients:\n"
                "- Include EVERY ingredient needed to make from scratch\n"
                "- 'recipe_qty' = exact amount used in this 1-serving recipe (e.g. '6 oz', '½ lemon', '1 tbsp')\n"
                "- 'purchase_qty' = the minimum package/unit you'd actually buy at a grocery store (e.g. '1 lb', '1 whole', '1 bunch', '1 bottle')\n"
                "- 'recipe_cost' = cost of just the portion used in the recipe\n"
                "- 'store_cost' = actual retail price for the purchase_qty at a US grocery store\n"
                "- 'servings_from_purchase' = how many times this recipe could be made from purchase_qty (e.g. buy 1 lb chicken, use 6 oz per serving = 2.6)\n"
                "- 'staple' = true for items most kitchens already have (salt, pepper, olive oil, butter, garlic, basic spices)\n"
                "- Categories: protein, produce, dairy, grain, pantry, spice, oil, sauce\n"
                "- Be accurate with real US grocery prices\n\n"
                "Rules for steps:\n"
                "- 3-6 clear, concise steps\n"
                "- Include temperatures, times, and visual cues\n"
                "- Written for a home cook, not a chef"
            ),
        }],
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    recipe = json.loads(text)

    # Calculate min servings from bottleneck ingredient (server-side validation)
    non_staple_ings = [i for i in recipe.get("ingredients", []) if not i.get("staple")]
    if non_staple_ings:
        servings_list = [i.get("servings_from_purchase", 1) for i in non_staple_ings]
        min_servings = min(servings_list) if servings_list else 1
        # Floor to nearest 0.5
        min_servings = max(1, int(min_servings * 2) / 2)
        total_store = sum(i.get("store_cost", 0) for i in recipe["ingredients"] if not i.get("staple"))
        total_recipe = sum(i.get("recipe_cost", 0) for i in recipe["ingredients"] if not i.get("staple"))
        recipe["min_servings"] = min_servings
        recipe["total_store_cost"] = round(total_store, 2)
        recipe["total_recipe_cost"] = round(total_recipe, 2)
        recipe["cost_per_serving"] = round(total_store / min_servings, 2) if min_servings > 0 else total_store
    else:
        recipe["min_servings"] = 1
        recipe["total_store_cost"] = 0
        recipe["total_recipe_cost"] = 0
        recipe["cost_per_serving"] = 0

    return recipe


def suggest_replacements(ingredient_name: str, meal_context: str = "") -> list:
    """Suggest alternative ingredients for a scanned item."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": (
                f"The user scanned a meal and wants to replace \"{ingredient_name}\".\n"
                f"{'Meal context: ' + meal_context if meal_context else ''}\n"
                "Suggest 4 likely alternatives — common substitutions or things that look similar.\n"
                "Return ONLY a JSON array, no markdown:\n"
                '[{"name": "Greek Yogurt", "emoji": "🥛", '
                '"calories": 00, "protein": 00, "carbs": 00, "fat": 00, "fiber": 00, '
                '"portion_sizes": {"pinch": "1 tbsp", "light": "1.5 tbsp", "regular": "2 tbsp", "generous": "3 tbsp", "loaded": "4 tbsp"}}]\n'
                "Macros are for the 'regular' portion size. Be accurate."
            ),
        }],
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    return json.loads(text)


# ── Recipe cache (in-memory, persists for server lifetime) ──
_recipe_cache = {}


class FuelHandler(SimpleHTTPRequestHandler):
    """Serve static files + API endpoints."""

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}

            if self.path == "/api/scan":
                result = scan_image(
                    body["image"],
                    body.get("media_type", "image/jpeg"),
                )
                self._json_response(result)

            elif self.path == "/api/describe":
                result = describe_meal(body["text"])
                self._json_response(result)

            elif self.path == "/api/chat":
                reply = chat_coach(body["messages"], body.get("daily_state", {}))
                self._json_response({"reply": reply})

            elif self.path in ("/api/recipe", "/api/decompose"):
                meal_name = body.get("name", "")
                cache_key = meal_name.lower().strip()
                if cache_key in _recipe_cache:
                    self._json_response({**_recipe_cache[cache_key], "cached": True})
                else:
                    recipe = get_full_recipe(meal_name, body.get("description", ""))
                    _recipe_cache[cache_key] = recipe
                    self._json_response({**recipe, "cached": False})

            elif self.path == "/api/replace-ingredient":
                alts = suggest_replacements(
                    body.get("ingredient", ""),
                    body.get("meal_context", ""),
                )
                self._json_response({"alternatives": alts})

            elif self.path == "/api/classify":
                result = classify_foods(body.get("foods", []))
                self._json_response({"classifications": result})

            elif self.path == "/api/scan-receipt":
                items = scan_receipt(body["image"], body.get("media_type", "image/jpeg"))
                self._json_response({"items": items})

            elif self.path == "/api/pantry-meals":
                meals = suggest_from_pantry(
                    body.get("ingredients", ""),
                    body.get("remaining_cal", 800),
                    body.get("remaining_protein", 60),
                )
                self._json_response({"meals": meals})

            elif self.path == "/api/lookup":
                result = lookup_ingredient(body.get("query", ""))
                self._json_response(result)

            elif self.path == "/api/briefing":
                result = generate_briefing(body.get("snapshot", {}))
                self._json_response(result)

            elif self.path == "/api/suggestions":
                meals = suggest_meals(
                    body.get("remaining_cal", 800),
                    body.get("remaining_protein", 60),
                    body.get("flavor", "all"),
                    body.get("goal_focus", ""),
                    body.get("diets", None),
                    remaining_carbs=body.get("remaining_carbs", 0),
                    remaining_fat=body.get("remaining_fat", 0),
                    eaten_today=body.get("eaten_today", None),
                    time_of_day=body.get("time_of_day", ""),
                    pantry=body.get("pantry", None),
                )
                self._json_response({"meals": meals})

            else:
                self._json_response({"error": "not found"}, 404)

        except json.JSONDecodeError as e:
            self._json_response({"error": f"Invalid JSON from AI: {str(e)}"}, 422)
        except Exception as e:
            print(f"[API ERROR] {e}")
            self._json_response({"error": str(e)}, 500)

    def _json_response(self, data, code=200):
        payload = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(payload))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        if "/api/" in (args[0] if args else ""):
            print(f"[FUEL] {args[0]}")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8888
    server = HTTPServer(("0.0.0.0", port), FuelHandler)
    print(f"[FUEL] Server running on http://0.0.0.0:{port}")
    print(f"[FUEL] Phone access: http://192.168.86.228:{port}")
    server.serve_forever()
