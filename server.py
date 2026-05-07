"""
FUEL Health Dashboard — Local API Server
Serves static files + proxies to Claude Haiku for:
  - /api/scan   (image → food identification)
  - /api/chat   (health coach conversation)
"""

import json, os, sys, base64
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

# ── Load API key ──
# Check local .env first, then NQ Data config as fallback
for env_path in [
    Path(__file__).resolve().parent / ".env",
    Path(__file__).resolve().parent.parent / "config" / ".env",
]:
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip()
        break

import anthropic

client = anthropic.Anthropic()
MODEL = "claude-haiku-4-5-20251001"


def scan_image(image_b64: str, media_type: str = "image/jpeg") -> dict:
    """Send image to Haiku for food identification."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_b64,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "Identify the food in this image. Return ONLY valid JSON, no markdown:\n"
                        '{"name": "Meal Name", "description": "brief ingredient list", '
                        '"calories": 000, "protein": 00, "carbs": 00, "fat": 00, "fiber": 00, '
                        '"category": "savory|sweet", "tags": ["HIGH PROTEIN", ...], '
                        '"health_note": "one-sentence nutritional insight"}\n'
                        "Be accurate with calorie estimates. Use typical serving sizes. "
                        "Tags can be: HIGH PROTEIN, LOW CARB, HIGH FIBER, RICH IN GREENS, "
                        "BALANCED, LIGHT, WHOLE GRAIN, HEALTHY FATS."
                    ),
                },
            ],
        }],
    )
    text = resp.content[0].text.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    return json.loads(text)


def chat_coach(messages: list, daily_state: dict) -> str:
    """Health coach conversation with context. Can return actions."""
    system = (
        "You are FUEL AI, a premium health coach inside a calorie-tracking app. "
        "You are concise, warm, and evidence-based. Never preachy. "
        "Speak like a knowledgeable friend, not a doctor. Keep responses under 3 sentences "
        "unless the user asks for detail.\n\n"
        f"User's full state: {json.dumps(daily_state)}\n\n"
        "You have FULL ACCESS to the user's profile, goals, stats, and intake history.\n\n"
        "IMPORTANT — ACTION COMMANDS:\n"
        "If the user asks you to change their goals, targets, weight, name, or any profile setting, "
        "include a JSON action block at the END of your response in this exact format:\n"
        "<!--ACTION:{\"type\":\"update_profile\",\"changes\":{\"field\":\"value\"}}-->\n\n"
        "Valid fields you can change:\n"
        "- calories (daily calorie target, number)\n"
        "- protein (daily protein target in grams, number)\n"
        "- carbs (daily carbs target in grams, number)\n"
        "- fat (daily fat target in grams, number)\n"
        "- weight (user weight in lbs, number)\n"
        "- goal (\"lose\", \"maintain\", or \"build\")\n"
        "- name (user's name, string)\n"
        "- water_goal (daily water glasses target, number)\n\n"
        "Examples:\n"
        "User: 'Set my protein to 160g' → 'Done — protein target updated to 160g.<!--ACTION:{\"type\":\"update_profile\",\"changes\":{\"protein\":160}}-->'\n"
        "User: 'I want to cut to 1800 calories' → 'Got it — daily target set to 1,800 cal.<!--ACTION:{\"type\":\"update_profile\",\"changes\":{\"calories\":1800}}-->'\n"
        "User: 'Change my goal to bulking' → 'Switched to build mode.<!--ACTION:{\"type\":\"update_profile\",\"changes\":{\"goal\":\"build\"}}-->'\n"
        "User: 'I weigh 180 now' → 'Updated — 180 lbs logged.<!--ACTION:{\"type\":\"update_profile\",\"changes\":{\"weight\":180}}-->'\n\n"
        "You can change multiple fields at once: {\"calories\":2200,\"protein\":180,\"goal\":\"build\"}\n"
        "NEVER show the ACTION tag in your visible text. Put it at the very end.\n"
        "If the user is NOT asking to change anything, just respond normally with no ACTION tag.\n\n"
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


def suggest_meals(remaining_cal: int, remaining_protein: int, flavor: str = "all", goal_focus: str = "", diets: list = None) -> list:
    """Generate meal suggestions based on remaining goals."""
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
                "Return ONLY a JSON array, no markdown:\n"
                '[{"name": "Meal Name", "calories": 000, "protein": 00, "carbs": 00, '
                '"fat": 00, "category": "savory|sweet", '
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
    """Text description → structured meal data via Haiku."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": (
                f"The user described a meal they ate: \"{text}\"\n\n"
                "Identify the food and estimate nutrition for a standard serving. "
                "Return ONLY valid JSON, no markdown:\n"
                '{"name": "Meal Name", "description": "brief ingredient list", '
                '"calories": 000, "protein": 00, "carbs": 00, "fat": 00, "fiber": 00, '
                '"category": "savory|sweet", "tags": ["HIGH PROTEIN", ...], '
                '"health_note": "one-sentence nutritional insight"}\n'
                "Be accurate with calorie estimates. Use typical serving sizes. "
                "If the description is vague, make reasonable assumptions and note them "
                "in the description. Tags: HIGH PROTEIN, LOW CARB, HIGH FIBER, "
                "RICH IN GREENS, BALANCED, LIGHT, WHOLE GRAIN, HEALTHY FATS."
            ),
        }],
    )
    text_out = resp.content[0].text.strip()
    if text_out.startswith("```"):
        text_out = text_out.split("\n", 1)[1] if "\n" in text_out else text_out[3:]
        if text_out.endswith("```"):
            text_out = text_out[:-3]
        text_out = text_out.strip()
    return json.loads(text_out)


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

            elif self.path == "/api/suggestions":
                meals = suggest_meals(
                    body.get("remaining_cal", 800),
                    body.get("remaining_protein", 60),
                    body.get("flavor", "all"),
                    body.get("goal_focus", ""),
                    body.get("diets", None),
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
