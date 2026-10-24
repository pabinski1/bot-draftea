import os
import requests
import discord_bot as bot

url = os.getenv("DISCORD_WEBHOOK_URL")
if not url or not bot.ODDS_API_KEY:
    raise SystemExit("Faltan DISCORD_WEBHOOK_URL y/o ODDS_API_KEY")

bot.init_db()
day = bot.target_day("hoy")
embeds, _ = bot.build_tiers("all", day, "auto", True)
if not embeds:
    embeds = [bot.empty_embed(day)]

r = requests.post(url, json={"embeds": [e.to_dict() for e in embeds]}, timeout=15)
print("Discord status:", r.status_code, r.text)
r.raise_for_status()
