import os
import math
import time
import asyncio
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo

import discord
import requests
from discord import app_commands
from discord.ext import tasks

# ── Variables de entorno ─────────────────────────────────────────────
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")        # Opcional: tu servidor (comandos al instante)
CHANNEL_ID = os.getenv("CHANNEL_ID")    # Opcional: canal para el parlay diario automático
DB_PATH = os.getenv("DB_PATH", "picks.db")
CACHE_TTL = int(os.getenv("CACHE_TTL_MIN", "60")) * 60

TZ = ZoneInfo("America/Mexico_City")

# ── Ligas (las que Draftea ofrece según su información pública) ──────
# Cada liga cuesta (mercados x regiones) créditos de The Odds API por consulta; por defecto 2.
# Para agregar más: soccer_uefa_europa_league, basketball_nba, etc.
SPORTS = {
    "⚽ Fútbol": ["soccer_mexico_liga_mx", "soccer_epl", "soccer_uefa_champions_league"],
    "🏈 NFL": ["americanfootball_nfl"],
}
GROUPS = {
    "futbol": ["⚽ Fútbol"],
    "nfl": ["🏈 NFL"],
    "all": list(SPORTS.keys()),
}

MARKETS = tuple(os.getenv("ODDS_MARKETS", "h2h,totals").split(","))  # cada mercado cuesta créditos
REGIONS = os.getenv("ODDS_REGIONS", "eu")                              # cada región también
PRICE_RANGE = (1.40, 2.30)   # cuota decimal aceptada por selección
MIN_BOOKS = 3                # mínimo de casas para confiar en el consenso
MIN_EV = 0.01                # edge mínimo (1%)
MAX_EV = 0.15                # edge mayor suele ser línea vieja o error: se descarta

# ── API de cuotas con caché ──────────────────────────────────────────
_cache = {}
API_REMAINING = None


def get_odds(sport_key):
    global API_REMAINING
    now = time.time()
    hit = _cache.get(sport_key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]

    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": ",".join(MARKETS),
        "oddsFormat": "decimal",
    }
    try:
        res = requests.get(url, params=params, timeout=15)
        API_REMAINING = res.headers.get("x-requests-remaining", API_REMAINING)
        if res.status_code == 200:
            data = res.json()
            _cache[sport_key] = (now, data)
            return data
        print(f"{sport_key}: status {res.status_code}")
    except Exception as e:
        print(f"Error consultando {sport_key}: {e}")
    return []


def is_cached(sport_key):
    hit = _cache.get(sport_key)
    return bool(hit and time.time() - hit[0] < CACHE_TTL)


def has_games(sport_key, day):
    """Consulta GRATIS (el endpoint /events no gasta créditos): ¿quedan partidos ese día?"""
    start = datetime.combine(day, dtime(0, 0), tzinfo=TZ)
    end = start + timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    params = {
        "apiKey": ODDS_API_KEY,
        "commenceTimeFrom": max(start, datetime.now(TZ)).astimezone(timezone.utc).strftime(fmt),
        "commenceTimeTo": end.astimezone(timezone.utc).strftime(fmt),
    }
    try:
        res = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/", params=params, timeout=15)
        if res.status_code == 200:
            return len(res.json()) > 0
    except Exception as e:
        print(f"Error en events {sport_key}: {e}")
    return True  # si la consulta falla, mejor intentar con las cuotas


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ── Análisis de valor ────────────────────────────────────────────────
def devig_groups(market_key, outcomes):
    """Quita el margen de la casa: devuelve [((nombre, punto), prob_sin_margen)]."""
    groups = defaultdict(list)
    for o in outcomes:
        price, name, point = o.get("price"), o.get("name"), o.get("point")
        if not price or price <= 1:
            continue
        gk = None if market_key == "h2h" else (abs(point) if point is not None else None)
        groups[gk].append((name, point, price))

    res = []
    for g in groups.values():
        if market_key == "h2h" and len(g) < 2:
            continue
        if market_key != "h2h" and len(g) != 2:
            continue
        total = sum(1 / price for _, _, price in g)
        for name, point, price in g:
            res.append(((name, point), (1 / price) / total))
    return res


def event_picks(event, category, sport_key, commence):
    probs = defaultdict(list)  # (mercado, nombre, punto) -> [prob sin margen por casa]
    best = {}                  # (mercado, nombre, punto) -> (mejor cuota, casa)

    for book in event.get("bookmakers", []):
        for m in book.get("markets", []):
            mk = m.get("key")
            if mk not in MARKETS:
                continue
            outcomes = m.get("outcomes", [])
            for key, p in devig_groups(mk, outcomes):
                probs[(mk,) + key].append(p)
            for o in outcomes:
                price = o.get("price")
                if not price:
                    continue
                k = (mk, o.get("name"), o.get("point"))
                if k not in best or price > best[k][0]:
                    best[k] = (price, book.get("title"))

    home, away = event.get("home_team"), event.get("away_team")
    picks = []
    for k, plist in probs.items():
        if len(plist) < MIN_BOOKS or k not in best:
            continue
        p = sum(plist) / len(plist)
        price, book = best[k]
        ev = p * price - 1
        if not (PRICE_RANGE[0] <= price <= PRICE_RANGE[1]):
            continue
        if not (MIN_EV <= ev <= MAX_EV):
            continue
        picks.append({
            "event_id": event.get("id"), "sport_key": sport_key, "category": category,
            "home": home, "away": away, "match": f"{home} vs {away}", "commence": commence,
            "market": k[0], "name": k[1], "point": k[2],
            "price": price, "book": book, "p": p, "ev": ev, "n": len(plist),
        })
    return picks


def collect(group_key, day):
    """Picks con valor de partidos que aún no empiezan y se juegan el día indicado (hora CDMX)."""
    now = datetime.now(timezone.utc)
    picks = []
    for category in GROUPS[group_key]:
        for sport_key in SPORTS[category]:
            if not is_cached(sport_key) and not has_games(sport_key, day):
                continue  # sin partidos: no se gastan créditos
            for event in get_odds(sport_key):
                commence = parse_dt(event["commence_time"])
                if commence <= now or commence.astimezone(TZ).date() != day:
                    continue
                picks += event_picks(event, category, sport_key, commence)
    return sorted(picks, key=lambda x: x["ev"], reverse=True)


def top_unique(picks, n):
    """Mejores picks, máximo uno por partido (evita selecciones correlacionadas)."""
    chosen, used = [], set()
    for p in picks:
        if p["event_id"] in used:
            continue
        chosen.append(p)
        used.add(p["event_id"])
        if len(chosen) == n:
            break
    return chosen


TIERS = [
    ("🟢 BAJO", 2, lambda p: (-p["p"], -p["ev"])),      # selecciones más probables
    ("🟡 MEDIO", 3, lambda p: -p["ev"]),                # mayor valor esperado
    ("🔴 ALTO", 4, lambda p: (-p["price"], -p["ev"])),  # cuotas más altas, más selecciones
]
TIER_COLORS = {"🟢 BAJO": 3066993, "🟡 MEDIO": 16763904, "🔴 ALTO": 15158332}


def tiered_parlays(picks):
    """Un parlay por nivel de riesgo, sin repetirse y evitando partidos ya usados cuando se puede."""
    out, seen, used = [], set(), set()
    for name, n, key in TIERS:
        ordered = sorted(picks, key=key)
        legs = top_unique([p for p in ordered if p["event_id"] not in used], n)
        if len(legs) < n:
            alt = top_unique(ordered, n)
            if len(alt) > len(legs):
                legs = alt
        if len(legs) < 2:
            continue
        sig = frozenset((p["event_id"], p["market"], p["name"], p["point"]) for p in legs)
        if sig in seen:
            continue
        seen.add(sig)
        used |= {p["event_id"] for p in legs}
        out.append((name, legs))
    return out


def label(p):
    m, n, pt = p["market"], p["name"], p["point"]
    if m == "totals":
        return f"{'Más de' if n == 'Over' else 'Menos de'} {pt:g}"
    if m == "spreads":
        return f"{n} ({pt:+g})"
    return "Empate" if n == "Draw" else f"Gana {n}"


# ── Base de datos: registro de picks y resultados ────────────────────
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with closing(db()) as con, con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS parlays(
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, user_id TEXT,
            kind TEXT, signature TEXT, total_odds REAL, combined_p REAL);
        CREATE TABLE IF NOT EXISTS legs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, parlay_id INTEGER, event_id TEXT,
            sport_key TEXT, match TEXT, home TEXT, away TEXT, commence TEXT,
            market TEXT, name TEXT, point REAL, price REAL, p REAL, result TEXT);
        """)


def signature(kind, picks):
    return kind + "|" + "|".join(sorted(
        f"{p['event_id']}:{p['market']}:{p['name']}:{p['point']}" for p in picks))


def find_parlay(kind, picks):
    """ID del parlay si ya fue registrado antes (mismas selecciones), si no None."""
    with closing(db()) as con:
        row = con.execute("SELECT id FROM parlays WHERE signature=?", (signature(kind, picks),)).fetchone()
    return row["id"] if row else None


def save_parlay(user_id, kind, picks):
    """Guarda el parlay (o una apuesta simple). Si ya existe idéntico, no lo duplica."""
    sig = signature(kind, picks)
    with closing(db()) as con, con:
        row = con.execute("SELECT id FROM parlays WHERE signature=?", (sig,)).fetchone()
        if row:
            return row["id"]
        cur = con.execute(
            "INSERT INTO parlays(created_at,user_id,kind,signature,total_odds,combined_p) VALUES(?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), user_id, kind, sig,
             math.prod(p["price"] for p in picks), math.prod(p["p"] for p in picks)))
        pid = cur.lastrowid
        con.executemany(
            "INSERT INTO legs(parlay_id,event_id,sport_key,match,home,away,commence,market,name,point,price,p) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [(pid, p["event_id"], p["sport_key"], p["match"], p["home"], p["away"],
              p["commence"].isoformat(), p["market"], p["name"], p["point"], p["price"], p["p"])
             for p in picks])
        return pid


def grade(leg, event):
    try:
        sc = {s["name"]: float(s["score"]) for s in event["scores"]}
    except (KeyError, TypeError, ValueError):
        return None
    h, a = sc.get(leg["home"]), sc.get(leg["away"])
    if h is None or a is None:
        return None
    m, name, point = leg["market"], leg["name"], leg["point"]
    if m == "h2h":
        winner = leg["home"] if h > a else leg["away"] if a > h else "Draw"
        return "win" if name == winner else "loss"
    if m == "totals":
        total = h + a
        if total == point:
            return "push"
        return "win" if (name == "Over") == (total > point) else "loss"
    if m == "spreads":
        mine, other = (h, a) if name == leg["home"] else (a, h)
        diff = mine + point - other
        return "push" if diff == 0 else ("win" if diff > 0 else "loss")
    return None


def resolve_pending():
    """Consulta marcadores finales (endpoint /scores, hasta 3 días atrás) y califica los picks."""
    now = datetime.now(timezone.utc)
    with closing(db()) as con, con:
        pend = con.execute(
            "SELECT * FROM legs WHERE result IS NULL AND commence < ?", (now.isoformat(),)).fetchall()
        by_sport = defaultdict(list)
        for leg in pend:
            by_sport[leg["sport_key"]].append(leg)

        for sport_key, legs in by_sport.items():
            events = {}
            try:
                res = requests.get(
                    f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores/",
                    params={"apiKey": ODDS_API_KEY, "daysFrom": 3}, timeout=15)
                if res.status_code == 200:
                    events = {e["id"]: e for e in res.json()}
            except Exception as e:
                print(f"Error en scores {sport_key}: {e}")

            for leg in legs:
                ev = events.get(leg["event_id"])
                result = grade(leg, ev) if ev and ev.get("completed") else None
                if result is None and now - parse_dt(leg["commence"]) > timedelta(days=4):
                    result = "n/d"  # ya no se puede consultar el marcador
                if result:
                    con.execute("UPDATE legs SET result=? WHERE id=?", (result, leg["id"]))


def stats():
    with closing(db()) as con:
        rows = con.execute(
            "SELECT p.id pid, p.kind, l.result, l.price, l.p FROM parlays p "
            "JOIN legs l ON l.parlay_id=p.id").fetchall()

    by = defaultdict(list)
    for r in rows:
        by[r["pid"]].append(r)

    out = {k: {"w": 0, "l": 0, "push": 0, "pend": 0, "profit": 0.0} for k in ("parlay", "simple")}
    legs_hit = legs_n = 0
    exp_sum = 0.0
    for legs in by.values():
        s = out[legs[0]["kind"]]
        res = [x["result"] for x in legs]
        for x in legs:
            if x["result"] in ("win", "loss"):
                legs_n += 1
                legs_hit += x["result"] == "win"
                exp_sum += x["p"]
        if "loss" in res:
            s["l"] += 1
            s["profit"] -= 1
        elif None in res:
            s["pend"] += 1
        elif "n/d" in res:
            continue
        elif "win" in res:
            s["w"] += 1
            s["profit"] += math.prod(x["price"] for x in legs if x["result"] == "win") - 1
        else:
            s["push"] += 1
    return out, legs_hit, legs_n, exp_sum


# ── Embeds ───────────────────────────────────────────────────────────
FOOTER = "Juega con responsabilidad · +18 · Ningún pick es seguro"


def footer():
    return FOOTER + (f" · Créditos API: {API_REMAINING}" if API_REMAINING else "")


def leg_field(i, p):
    hora = p["commence"].astimezone(TZ).strftime("%H:%M")
    return (
        f"🔹 {i}. {p['category']} · {hora} h",
        (
            f"🏟️ {p['match']}\n"
            f"🎯 `{label(p)}`\n"
            f"📈 Mejor cuota: `{p['price']:.2f}` ({p['book']}) · {p['n']} casas\n"
            f"📊 Prob. consenso: `{p['p'] * 100:.1f}%` · Edge: `{p['ev'] * 100:+.1f}%`\n"
            f"✅ Tiene valor si Draftea paga ≥ `{1 / p['p']:.2f}`"
        ),
    )


def parlay_embed(legs, pid, day, risk_label=None):
    total_odds = math.prod(p["price"] for p in legs)
    comb_p = math.prod(p["p"] for p in legs)
    comb_ev = comb_p * total_odds - 1

    if comb_p >= 0.30:
        risk, color = "🟢 MODERADO", 3066993
    elif comb_p >= 0.15:
        risk, color = "🟡 MEDIO", 16763904
    else:
        risk, color = "🔴 ALTO", 15158332

    if risk_label:
        risk, color = risk_label, TIER_COLORS[risk_label]

    if comb_ev <= 0:
        stake = "⛔ Sin valor esperado: no recomendado"
    elif total_odds < 3:
        stake = "0.5% – 1% de tu bankroll"
    elif total_odds < 8:
        stake = "0.25% – 0.5% de tu bankroll"
    else:
        stake = "0.1% – 0.25% de tu bankroll (es de azar: tómalo como diversión)"

    embed = discord.Embed(
        title=(f"{risk_label} · PARLAY DEL {day:%d/%m} · #{pid}" if risk_label
               else f"⚡ PARLAY DEL {day:%d/%m} · #{pid}"),
        description=(
            f"🔥 **Cuota combinada:** `{total_odds:.2f}`\n"
            f"💵 **Retorno por $100:** `${100 * total_odds:,.2f}`"
        ),
        color=color,
    )
    for i, p in enumerate(legs, 1):
        embed.add_field(name=leg_field(i, p)[0], value=leg_field(i, p)[1], inline=False)

    embed.add_field(
        name="🛡️ RIESGO Y BANCA",
        value=(
            f"⚡ **Riesgo:** {risk}\n"
            f"🎯 **Prob. combinada (sin margen):** `{comb_p * 100:.1f}%`\n"
            f"📐 **Valor esperado combinado:** `{comb_ev * 100:+.1f}%`\n"
            f"💰 **Stake sugerido:** {stake}\n"
            f"✅ **Cuota combinada mínima en Draftea:** `{1 / comb_p:.2f}`"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚠️ Antes de apostar",
        value="Las cuotas son el mejor precio entre varias casas (Draftea no está en la API). "
              "Confirma la cuota en Draftea: si paga menos que el mínimo indicado, el valor desaparece.",
        inline=False,
    )
    embed.set_footer(text=footer())
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def singles_embed(picks, ids, day):
    embed = discord.Embed(title=f"🎯 APUESTAS SIMPLES DEL {day:%d/%m}", color=3447003,
                          description="Las simples evitan multiplicar el margen de la casa. Stake sugerido: 0.5% – 1% por pick.")
    for i, (p, pid) in enumerate(zip(picks, ids), 1):
        name, value = leg_field(i, p)
        embed.add_field(name=f"{name} · #{pid}", value=value, inline=False)
    embed.set_footer(text=footer())
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def empty_embed(day):
    return discord.Embed(
        title="⚠️ SIN PICKS CON VALOR",
        description=f"No hay selecciones con valor suficiente para el {day:%d/%m} (o no hay partidos de las ligas configuradas). "
                    "No forzar una apuesta también es una decisión válida.",
        color=15844367,
    )


def results_embed():
    out, hit, n, exp = stats()
    embed = discord.Embed(title="📒 RESULTADOS DEL BOT", color=3447003)
    for kind, title in (("parlay", "Parlays"), ("simple", "Simples")):
        s = out[kind]
        done = s["w"] + s["l"]
        if done + s["push"] + s["pend"] == 0:
            continue
        rate = f"{s['w'] / done * 100:.1f}%" if done else "—"
        roi = f"{s['profit'] / (done + s['push']) * 100:+.1f}%" if done + s["push"] else "—"
        embed.add_field(
            name=title,
            value=f"✅ {s['w']} · ❌ {s['l']} · ➖ {s['push']} · ⏳ {s['pend']}\n"
                  f"Acierto: `{rate}` · ROI (1 unidad plana): `{roi}`",
            inline=False,
        )
    if n:
        embed.add_field(
            name="Calibración de selecciones",
            value=f"Aciertos reales: `{hit / n * 100:.1f}%` vs. esperados por el consenso: `{exp / n * 100:.1f}%` ({n} selecciones)",
            inline=False,
        )
    if not embed.fields:
        embed.description = "Todavía no hay picks registrados. Usa `/parlay` o `/simple`."
    elif n < 100:
        embed.set_footer(text="Muestra pequeña (<100 selecciones): no saques conclusiones todavía.")
    return embed


# ── Bot ──────────────────────────────────────────────────────────────
class DrafteaBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        init_db()
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        if CHANNEL_ID:
            daily_post.start()

    async def on_ready(self):
        print(f"Conectado como {self.user}")


client = DrafteaBot()

DEPORTES = [
    app_commands.Choice(name="Todos", value="all"),
    app_commands.Choice(name="Fútbol", value="futbol"),
    app_commands.Choice(name="NFL", value="nfl"),
]
CUANDO = [
    app_commands.Choice(name="Hoy", value="hoy"),
    app_commands.Choice(name="Mañana", value="manana"),
]


def target_day(cuando):
    today = datetime.now(TZ).date()
    return today + timedelta(days=1) if cuando == "manana" else today


def build_parlay(group_key, legs, day, user_id):
    picks = collect(group_key, day)
    chosen = top_unique(picks, legs)
    if len(chosen) < 2:
        return empty_embed(day)
    pid = save_parlay(user_id, "parlay", chosen)
    return parlay_embed(chosen, pid, day)


def build_tiers(group_key, day, user_id, only_new=False):
    """Un embed por nivel de riesgo. only_new=True omite parlays ya publicados antes.
    Devuelve (embeds, total_generados)."""
    tiers = tiered_parlays(collect(group_key, day))
    embeds = []
    for name, legs in tiers:
        if only_new and find_parlay("parlay", legs):
            continue
        pid = save_parlay(user_id, "parlay", legs)
        embeds.append(parlay_embed(legs, pid, day, risk_label=name))
    return embeds, len(tiers)


def build_singles(group_key, day, user_id):
    chosen = top_unique(collect(group_key, day), 3)
    if not chosen:
        return empty_embed(day)
    ids = [save_parlay(user_id, "simple", [p]) for p in chosen]
    return singles_embed(chosen, ids, day)


@client.tree.command(name="parlay", description="Parlay con valor, todos los partidos el mismo día")
@app_commands.describe(deporte="Filtra por deporte", selecciones="Número de selecciones (2 a 4)", cuando="Día de los partidos")
@app_commands.choices(deporte=DEPORTES, cuando=CUANDO)
async def parlay(
    interaction: discord.Interaction,
    deporte: app_commands.Choice[str] = None,
    selecciones: app_commands.Range[int, 2, 4] = 3,
    cuando: app_commands.Choice[str] = None,
):
    await interaction.response.defer(thinking=True)
    day = target_day(cuando.value if cuando else "hoy")
    embed = await asyncio.to_thread(
        build_parlay, deporte.value if deporte else "all", selecciones, day, str(interaction.user.id))
    await interaction.followup.send(embed=embed)


@client.tree.command(name="riesgos", description="Tres parlays del día: riesgo bajo, medio y alto")
@app_commands.describe(deporte="Filtra por deporte", cuando="Día de los partidos")
@app_commands.choices(deporte=DEPORTES, cuando=CUANDO)
async def riesgos(
    interaction: discord.Interaction,
    deporte: app_commands.Choice[str] = None,
    cuando: app_commands.Choice[str] = None,
):
    await interaction.response.defer(thinking=True)
    day = target_day(cuando.value if cuando else "hoy")
    embeds, _ = await asyncio.to_thread(
        build_tiers, deporte.value if deporte else "all", day, str(interaction.user.id))
    if embeds:
        await interaction.followup.send(embeds=embeds)
    else:
        await interaction.followup.send(embed=empty_embed(day))


@client.tree.command(name="simple", description="Mejores apuestas simples con valor (sin parlay)")
@app_commands.describe(deporte="Filtra por deporte", cuando="Día de los partidos")
@app_commands.choices(deporte=DEPORTES, cuando=CUANDO)
async def simple(
    interaction: discord.Interaction,
    deporte: app_commands.Choice[str] = None,
    cuando: app_commands.Choice[str] = None,
):
    await interaction.response.defer(thinking=True)
    day = target_day(cuando.value if cuando else "hoy")
    embed = await asyncio.to_thread(
        build_singles, deporte.value if deporte else "all", day, str(interaction.user.id))
    await interaction.followup.send(embed=embed)


@client.tree.command(name="futbol", description="Parlay de fútbol de hoy")
async def futbol(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    embed = await asyncio.to_thread(build_parlay, "futbol", 3, target_day("hoy"), str(interaction.user.id))
    await interaction.followup.send(embed=embed)


@client.tree.command(name="nfl", description="Parlay de NFL de hoy")
async def nfl(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    embed = await asyncio.to_thread(build_parlay, "nfl", 3, target_day("hoy"), str(interaction.user.id))
    await interaction.followup.send(embed=embed)


@client.tree.command(name="resultados", description="Califica los picks pasados y muestra el rendimiento del bot")
async def resultados(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    await asyncio.to_thread(resolve_pending)
    embed = await asyncio.to_thread(results_embed)
    await interaction.followup.send(embed=embed)


@client.tree.command(name="ayuda", description="Muestra los comandos y cómo funciona el bot")
async def ayuda(interaction: discord.Interaction):
    restantes = f"\nCréditos restantes de The Odds API: `{API_REMAINING}`" if API_REMAINING else ""
    await interaction.response.send_message(
        "**Comandos**\n"
        "`/parlay [deporte] [selecciones] [cuando]` — parlay con valor, todos los partidos el mismo día\n"
        "`/riesgos` — tres parlays: riesgo bajo, medio y alto\n"
        "`/simple` — mejores apuestas simples\n"
        "`/futbol` · `/nfl` — parlay rápido por deporte\n"
        "`/resultados` — califica picks pasados y muestra aciertos/ROI\n\n"
        "**Cómo elige:** compara la mejor cuota contra la probabilidad de consenso de varias casas "
        f"(sin margen). Cuotas en caché {CACHE_TTL // 60} min." + restantes,
        ephemeral=True,
    )


@tasks.loop(time=dtime(hour=15, minute=0, tzinfo=timezone.utc))  # 09:00 hora CDMX
async def daily_post():
    channel = client.get_channel(int(CHANNEL_ID)) or await client.fetch_channel(int(CHANNEL_ID))
    embeds, _ = await asyncio.to_thread(build_tiers, "all", target_day("hoy"), "auto", True)
    for embed in embeds:
        await channel.send(embed=embed)


if __name__ == "__main__":
    if not DISCORD_TOKEN or not ODDS_API_KEY:
        raise SystemExit("Faltan las variables DISCORD_TOKEN y/o ODDS_API_KEY")
    client.run(DISCORD_TOKEN)
