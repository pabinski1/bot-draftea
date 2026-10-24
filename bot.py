import os
import sys
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

# ───────────── Configuración ─────────────
API_KEY = os.environ["ODDS_API_KEY"]
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

SPORTS = [
    "soccer_mexico_ligamx",
    "soccer_epl",
    "soccer_spain_la_liga",
    "basketball_nba",
    "americanfootball_nfl",
]
REGIONS = os.getenv("ODDS_REGIONS", "eu")
MIN_ODD, MAX_ODD = 1.45, 1.80
HOURS_AHEAD = 36
MIN_BOOKMAKERS = 3
STAKE = os.getenv("STAKE_TEXT", "1 unidad (≈1.5% del bankroll)")

BASE_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds/"


def fetch_events(sport):
    now = datetime.now(timezone.utc)
    params = {
        "apiKey": API_KEY,
        "regions": REGIONS,
        "markets": "h2h",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
        "commenceTimeFrom": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commenceTimeTo": (now + timedelta(hours=HOURS_AHEAD)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        r = requests.get(BASE_URL.format(sport=sport), params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"[WARN] {sport}: {e}", file=sys.stderr)
        return []


def event_candidates(event):
    prices = defaultdict(list)
    for bm in event.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for o in market.get("outcomes", []):
                prices[o["name"]].append(o["price"])

    if not prices or any(len(p) < MIN_BOOKMAKERS for p in prices.values()):
        return []

    avg = {n: statistics.mean(p) for n, p in prices.items()}
    implied = {n: 1 / a for n, a in avg.items()}
    total = sum(implied.values())

    candidates = []
    for name, odd in avg.items():
        if not (MIN_ODD <= odd <= MAX_ODD):
            continue
        fair_p = implied[name] / total
        best = max(prices[name])
        ev = fair_p * best - 1
        sd = statistics.pstdev(prices[name])
        candidates.append({
            "event_id": event["id"],
            "pick": "Empate" if name == "Draw" else name,
            "match": f"{event['home_team']} vs {event['away_team']}",
            "start": datetime.fromisoformat(event["commence_time"]),
            "odd": odd,
            "score": ev - sd,  # valor esperado penalizado por inestabilidad
        })
    return candidates


def build_parlay():
    candidates = []
    for sport in SPORTS:
        for event in fetch_events(sport):
            candidates.extend(event_candidates(event))

    candidates.sort(key=lambda c: c["score"], reverse=True)

    picks, used = [], set()
    for c in candidates:
        if c["event_id"] in used:
            continue
        picks.append(c)
        used.add(c["event_id"])
        if len(picks) == 2:
            return picks
    return None


def fmt_pick(p):
    ts = int(p["start"].timestamp())
    return (
        f"**{p['pick']}** (Ganador)\n"
        f"🏟️ {p['match']}\n"
        f"🕒 <t:{ts}:f>\n"
        f"📊 Cuota: **{p['odd']:.2f}**"
    )


def send_discord(picks):
    if picks:
        multiplier = picks[0]["odd"] * picks[1]["odd"]
        embed = {
            "title": "⚡ DUPLA RECOMENDADA PARA DRAFTEA",
            "description": "Selecciones de cuota estable y alto valor para hoy.",
            "color": 0x00E676,
            "fields": [
                {"name": "🎯 Selección 1", "value": fmt_pick(picks[0]), "inline": False},
                {"name": "🎯 Selección 2", "value": fmt_pick(picks[1]), "inline": False},
                {"name": "✖️ Multiplicador Estimado", "value": f"**{multiplier:.2f}x**", "inline": True},
                {"name": "💰 Stake", "value": f"**{STAKE}**", "inline": True},
            ],
            "footer": {"text": "Verifica las cuotas en Draftea antes de jugar. Juega con responsabilidad."},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    else:
        embed = {
            "title": "⚡ DUPLA RECOMENDADA PARA DRAFTEA",
            "description": "Hoy no hay selecciones que cumplan los filtros (cuotas 1.45–1.80). Mejor no forzar.",
            "color": 0xFFC107,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    r = requests.post(WEBHOOK_URL, json={"embeds": [embed]}, timeout=20)
    r.raise_for_status()


if __name__ == "__main__":
    send_discord(build_parlay())
