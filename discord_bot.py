"""
Bot de parlays con valor para Draftea  ·  Fútbol + NFL + NBA

- Busca partidos de las próximas 24 h (ventana móvil).
- Compara la mejor cuota contra la probabilidad real (consenso sin margen + Pinnacle).
- Mercados principales (ganador, goles/puntos, hándicap) + props de Draftea:
  córners, tarjetas y (opcional) tiros de jugadores y props de NBA.
- Gasta pocos créditos de la API: revisa gratis qué hay y prioriza.
- Se usa como bot de Discord (comandos /) o desde daily.py (GitHub Actions + webhook).
"""
import os
import math
import time
import asyncio
import sqlite3
import statistics
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo

import discord
import requests
from discord import app_commands
from discord.ext import tasks
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_list(name, default):
    raw = os.getenv(name, default)
    return tuple(x.strip() for x in raw.split(",") if x.strip())


# ── Variables de entorno ─────────────────────────────────────────────
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")      # Opcional: tu servidor (comandos al instante)
CHANNEL_ID = os.getenv("CHANNEL_ID")  # Opcional: canal para el parlay diario automático
DB_PATH = os.getenv("DB_PATH", "picks.db")
CACHE_TTL = _env_int("CACHE_TTL_MIN", 60) * 60
WINDOW_HOURS = max(1, min(_env_int("WINDOW_HOURS", 24), 24))  # ventana de búsqueda (máx. 24 h)
MIN_LEAD_MIN = _env_int("MIN_LEAD_MIN", 15)   # ignora partidos que empiezan en menos de X min
MIN_CREDITS = _env_int("MIN_CREDITS", 15)     # si quedan menos créditos de API, no consulta
TZ = ZoneInfo("America/Mexico_City")

# ── Ligas ────────────────────────────────────────────────────────────
# (categoría, clave de The Odds API, prioridad, nombre a mostrar)
# Prioridad 1 = más importante (se consulta primero cuando hay poco presupuesto de créditos).
# Las ligas sin temporada o sin partidos en la ventana NO gastan créditos (se revisan gratis).
# Para quitar una liga, borra su línea. Claves: the-odds-api.com/sports-odds-data/sports-apis.html
FUT, NFL_C, NBA_C = "⚽ Fútbol", "🏈 NFL", "🏀 NBA"
LEAGUES = [
    (NFL_C, "americanfootball_nfl", 1, "NFL"),
    (NBA_C, "basketball_nba", 1, "NBA"),
    (FUT, "soccer_mexico_ligamx", 1, "Liga MX"),
    (FUT, "soccer_epl", 1, "Premier League"),
    (FUT, "soccer_spain_la_liga", 1, "La Liga"),
    (FUT, "soccer_italy_serie_a", 1, "Serie A"),
    (FUT, "soccer_germany_bundesliga", 1, "Bundesliga"),
    (FUT, "soccer_france_ligue_one", 1, "Ligue 1"),
    (FUT, "soccer_uefa_champs_league", 1, "Champions League"),
    (FUT, "soccer_uefa_europa_league", 2, "Europa League"),
    (FUT, "soccer_uefa_europa_conference_league", 2, "Conference League"),
    (FUT, "soccer_usa_mls", 2, "MLS"),
    (FUT, "soccer_concacaf_leagues_cup", 2, "Leagues Cup"),
    (FUT, "soccer_conmebol_copa_libertadores", 2, "Copa Libertadores"),
    (FUT, "soccer_conmebol_copa_sudamericana", 2, "Copa Sudamericana"),
    (FUT, "soccer_brazil_campeonato", 2, "Brasileirão Serie A"),
    (FUT, "soccer_argentina_primera_division", 2, "Primera División Argentina"),
    (FUT, "soccer_netherlands_eredivisie", 2, "Eredivisie"),
    (FUT, "soccer_portugal_primeira_liga", 2, "Primeira Liga"),
    (FUT, "soccer_efl_champ", 2, "Championship"),
    (FUT, "soccer_turkey_super_league", 2, "Süper Lig"),
    (FUT, "soccer_belgium_first_div", 2, "Primera Bélgica"),
    (FUT, "soccer_spl", 2, "Premiership Escocia"),
    (FUT, "soccer_spain_copa_del_rey", 2, "Copa del Rey"),
    (FUT, "soccer_fa_cup", 2, "FA Cup"),
    (FUT, "soccer_england_efl_cup", 2, "EFL Cup"),
    (FUT, "soccer_germany_dfb_pokal", 2, "DFB-Pokal"),
    (FUT, "soccer_italy_coppa_italia", 2, "Coppa Italia"),
    (FUT, "soccer_france_coupe_de_france", 2, "Copa de Francia"),
    (FUT, "soccer_uefa_nations_league", 2, "UEFA Nations League"),
    (FUT, "soccer_germany_bundesliga2", 3, "2. Bundesliga"),
    (FUT, "soccer_italy_serie_b", 3, "Serie B"),
    (FUT, "soccer_france_ligue_two", 3, "Ligue 2"),
    (FUT, "soccer_spain_segunda_division", 3, "La Liga 2"),
    (FUT, "soccer_england_league1", 3, "League One"),
    (FUT, "soccer_england_league2", 3, "League Two"),
    (FUT, "soccer_brazil_serie_b", 3, "Brasileirão Serie B"),
    (FUT, "soccer_chile_campeonato", 3, "Primera División Chile"),
    (FUT, "soccer_austria_bundesliga", 3, "Bundesliga Austria"),
    (FUT, "soccer_switzerland_superleague", 3, "Superliga Suiza"),
    (FUT, "soccer_greece_super_league", 3, "Superliga Grecia"),
    (FUT, "soccer_denmark_superliga", 3, "Superliga Dinamarca"),
    (FUT, "soccer_sweden_allsvenskan", 3, "Allsvenskan"),
    (FUT, "soccer_norway_eliteserien", 3, "Eliteserien"),
    (FUT, "soccer_poland_ekstraklasa", 3, "Ekstraklasa"),
    (FUT, "soccer_saudi_arabia_pro_league", 3, "Saudi Pro League"),
    (FUT, "soccer_japan_j_league", 3, "J League"),
    (FUT, "soccer_korea_kleague1", 3, "K League 1"),
    (FUT, "soccer_australia_aleague", 3, "A-League"),
    (FUT, "soccer_china_superleague", 3, "Superliga China"),
    (FUT, "soccer_league_of_ireland", 3, "Liga de Irlanda"),
    (FUT, "soccer_finland_veikkausliiga", 3, "Veikkausliiga"),
]
LEAGUE_INFO = {key: (cat, tier, name) for cat, key, tier, name in LEAGUES}
GROUPS = {
    "futbol": [FUT],
    "nfl": [NFL_C],
    "nba": [NBA_C],
    "all": [FUT, NFL_C, NBA_C],
}

# ── Mercados principales (1 consulta por liga; cuesta 1 crédito por mercado) ──
DEFAULT_MARKETS = _env_list("ODDS_MARKETS", "h2h,totals")
CATEGORY_MARKETS = {
    FUT: DEFAULT_MARKETS,
    NFL_C: ("h2h", "spreads", "totals"),
    NBA_C: ("h2h", "spreads", "totals"),
}
REGIONS = os.getenv("ODDS_REGIONS", "eu")  # "eu" incluye a Pinnacle, la mejor referencia

# ── Props de Draftea (1 consulta POR PARTIDO; cuesta 1 crédito por mercado devuelto) ──
# Córners y tarjetas vienen activados. Tiros de jugadores y props de NBA se activan con
# variables (PROPS_JUGADORES=1 / PROPS_NBA=1): usan casas de EE. UU. y gastan más créditos.
PLAYER_LEAGUES = {
    "soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a",
    "soccer_germany_bundesliga", "soccer_france_ligue_one", "soccer_usa_mls",
}
PROP_SETS = [
    {"key": "esquinas", "name": "Córners y tarjetas", "category": FUT, "leagues": None,
     "markets": _env_list("PROP_MARKETS_FUT", "alternate_totals_corners,alternate_totals_cards"),
     "regions": REGIONS, "on": os.getenv("PROPS_FUT", "1") == "1"},
    {"key": "tiros", "name": "Tiros de jugadores", "category": FUT, "leagues": PLAYER_LEAGUES,
     "markets": ("player_shots_on_target", "player_shots"),
     "regions": "us", "on": os.getenv("PROPS_JUGADORES", "0") == "1"},
    {"key": "nba", "name": "Props NBA", "category": NBA_C, "leagues": None,
     "markets": ("player_points", "player_rebounds", "player_assists", "player_threes",
                 "player_points_rebounds_assists"),
     "regions": "us", "on": os.getenv("PROPS_NBA", "0") == "1"},
]
STAT_NAMES = {
    "alternate_totals_corners": "córners",
    "alternate_team_totals_corners": "córners",
    "alternate_totals_cards": "tarjetas",
    "player_shots_on_target": "tiros al arco",
    "player_shots": "tiros",
    "player_points": "puntos",
    "player_rebounds": "rebotes",
    "player_assists": "asistencias",
    "player_threes": "triples",
    "player_points_rebounds_assists": "pts+reb+ast",
}
GRADABLE = ("h2h", "totals", "spreads")  # lo demás (córners, props) no se califica solo

# ── Reglas para elegir picks ─────────────────────────────────────────
PRICE_RANGE = (1.40, 2.30)            # cuota decimal aceptada por selección
MIN_BOOKS = _env_int("MIN_BOOKS", 3)  # casas mínimas (liga prioridad 1; +1 en prioridad 2, +2 en 3)
MIN_EV = _env_float("MIN_EV", 0.015)  # ventaja mínima en mercados principales (1.5%)
MIN_EV_PROPS = _env_float("MIN_EV_PROPS", 0.03)  # en props se exige más (3%)
MAX_EV = _env_float("MAX_EV", 0.15)   # más de esto suele ser línea vieja o error: se descarta
SHARP_BOOK = "pinnacle"
SHARP_WEIGHT = 0.6                    # peso de Pinnacle frente al consenso del resto
KELLY_FRACTION = 0.25                 # Kelly fraccionado (conservador)
MAX_STAKE = 0.01                      # tope: 1% de la banca por apuesta
SCORES_COST = 2                       # créditos que cuesta consultar marcadores de una liga

API = "https://api.the-odds-api.com/v4"
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
NUMS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]

# ── Sesión HTTP con reintentos ───────────────────────────────────────
SESSION = requests.Session()
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=("GET",), raise_on_status=False)))

# ── API de cuotas: caché y presupuesto de créditos ───────────────────
_cache = {}
_events_cache = {}
_props_cache = {}
_active = {"keys": None, "at": 0.0}
API_REMAINING = None
RUN_BUDGET = None     # tope de créditos para mercados principales (daily.py lo define)
RUN_SPENT = 0
PROPS_BUDGET = None   # tope de créditos para props
PROPS_SPENT = 0


def begin_run(budget, props_budget=None):
    """Inicia una corrida con topes de créditos (None = sin tope)."""
    global RUN_BUDGET, RUN_SPENT, PROPS_BUDGET, PROPS_SPENT
    RUN_BUDGET, RUN_SPENT = budget, 0
    PROPS_BUDGET, PROPS_SPENT = props_budget, 0


def _credits_left():
    try:
        return int(float(API_REMAINING))
    except (TypeError, ValueError):
        return None


def _call_cost(res, default):
    try:
        return int(float(res.headers.get("x-requests-last")))
    except (TypeError, ValueError):
        return default


def markets_for(category):
    return CATEGORY_MARKETS.get(category, DEFAULT_MARKETS)


def load_active_sports():
    """Consulta GRATIS qué ligas están en temporada y si alguna clave es inválida."""
    global API_REMAINING
    _active["at"] = time.time()
    try:
        res = SESSION.get(f"{API}/sports/", params={"apiKey": ODDS_API_KEY, "all": "true"}, timeout=15)
        API_REMAINING = res.headers.get("x-requests-remaining", API_REMAINING)
        if res.status_code != 200:
            print(f"/sports respondió {res.status_code}: se usarán todas las ligas configuradas")
            return
        everything = res.json()  # con all=true vienen también las ligas fuera de temporada
        _active["keys"] = {s["key"] for s in everything if s.get("active", True)}
        known = {s["key"] for s in everything}
        invalid = sorted(k for k in LEAGUE_INFO if k not in known)
        if invalid:
            print("⚠️ Claves que la API no reconoce (revísalas):", ", ".join(invalid))
        on = [k for k in LEAGUE_INFO if k in _active["keys"]]
        print(f"Ligas en temporada: {len(on)} de {len(LEAGUE_INFO)}")
    except Exception as e:
        print(f"Error consultando /sports: {e}")


def sport_is_active(sport_key):
    if time.time() - _active["at"] > 6 * 3600:
        load_active_sports()
    keys = _active["keys"]
    return True if keys is None else sport_key in keys


def get_odds(sport_key, category):
    """Cuotas de una liga (mercados principales). Respeta caché, mínimo de créditos y tope de la corrida."""
    global API_REMAINING, RUN_SPENT
    now = time.time()
    hit = _cache.get(sport_key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    markets = markets_for(category)
    cost = len(markets) * len(REGIONS.split(","))
    left = _credits_left()
    if left is not None and left < MIN_CREDITS:
        print(f"  {sport_key}: omitida (quedan {left} créditos, mínimo {MIN_CREDITS})")
        return []
    if RUN_BUDGET is not None and RUN_SPENT + cost > RUN_BUDGET:
        print(f"  {sport_key}: omitida (tope de {RUN_BUDGET} créditos alcanzado)")
        return []
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": ",".join(markets),
        "oddsFormat": "decimal",
    }
    try:
        res = SESSION.get(f"{API}/sports/{sport_key}/odds/", params=params, timeout=20)
        API_REMAINING = res.headers.get("x-requests-remaining", API_REMAINING)
        if res.status_code == 200:
            RUN_SPENT += _call_cost(res, cost)
            data = res.json()
            _cache[sport_key] = (now, data)
            return data
        print(f"  {sport_key}: status {res.status_code}")
    except Exception as e:
        print(f"  Error consultando {sport_key}: {e}")
    return []


def props_budget_ok(cost):
    left = _credits_left()
    if left is not None and left < MIN_CREDITS:
        return False
    return PROPS_BUDGET is None or PROPS_SPENT + cost <= PROPS_BUDGET


def get_event_props(sport_key, event_id, ps):
    """Props de UN partido (córners, tarjetas, jugadores). Cuesta créditos por cada mercado devuelto."""
    global API_REMAINING, PROPS_SPENT
    ck = (event_id, ps["key"])
    hit = _props_cache.get(ck)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": ps["regions"],
        "markets": ",".join(ps["markets"]),
        "oddsFormat": "decimal",
    }
    try:
        res = SESSION.get(f"{API}/sports/{sport_key}/events/{event_id}/odds", params=params, timeout=20)
        API_REMAINING = res.headers.get("x-requests-remaining", API_REMAINING)
        if res.status_code == 200:
            PROPS_SPENT += _call_cost(res, 0)
            data = res.json()
            _props_cache[ck] = (time.time(), data)
            return data
        print(f"  props {event_id}: status {res.status_code}")
    except Exception as e:
        print(f"  Error en props {event_id}: {e}")
    return None


def is_cached(sport_key):
    hit = _cache.get(sport_key)
    return bool(hit and time.time() - hit[0] < CACHE_TTL)


def games_in_window(sport_key, start, end):
    """Consulta GRATIS (/events no gasta créditos): lista de partidos en la ventana (None si falló)."""
    key = (sport_key, int(start.timestamp() // 600), int(end.timestamp() // 600))
    if key in _events_cache:
        return _events_cache[key]
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    params = {
        "apiKey": ODDS_API_KEY,
        "commenceTimeFrom": start.astimezone(timezone.utc).strftime(fmt),
        "commenceTimeTo": end.astimezone(timezone.utc).strftime(fmt),
    }
    try:
        res = SESSION.get(f"{API}/sports/{sport_key}/events/", params=params, timeout=15)
        if res.status_code == 200:
            evs = res.json()
            _events_cache[key] = evs
            return evs
    except Exception as e:
        print(f"  Error en events {sport_key}: {e}")
    return None


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def window(hours):
    now = datetime.now(timezone.utc)
    return now + timedelta(minutes=MIN_LEAD_MIN), now + timedelta(hours=hours)


def when(dt):
    local = dt.astimezone(TZ)
    days = (local.date() - datetime.now(TZ).date()).days
    day = "hoy" if days == 0 else "mañana" if days == 1 else DIAS[local.weekday()]
    return f"{day} {local:%H:%M}"


# ── Análisis de valor ────────────────────────────────────────────────
def devig_groups(market_key, outcomes):
    """Quita el margen de la casa: devuelve [((nombre, punto, jugador), prob_sin_margen)]."""
    groups = defaultdict(list)
    for o in outcomes:
        price, name, point, who = o.get("price"), o.get("name"), o.get("point"), o.get("description")
        if not price or price <= 1:
            continue
        gk = None if market_key == "h2h" else (who, abs(point) if point is not None else None)
        groups[gk].append((name, point, who, price))
    res = []
    for g in groups.values():
        if market_key == "h2h" and len(g) < 2:
            continue
        if market_key != "h2h" and len(g) != 2:
            continue
        total = sum(1 / price for _, _, _, price in g)
        for name, point, who, price in g:
            res.append(((name, point, who), (1 / price) / total))
    return res


def event_picks(event, category, sport_key, commence, tier=1, league="", markets=None, kind="main"):
    markets = markets or markets_for(category)
    min_books = MIN_BOOKS + (tier - 1)  # ligas menores: se exige más consenso
    min_ev = MIN_EV if kind == "main" else MIN_EV_PROPS
    probs = defaultdict(list)   # (mercado, nombre, punto, jugador) -> [prob sin margen por casa]
    sharp = {}                  # lo mismo, pero solo Pinnacle
    best = {}                   # lo mismo -> (mejor cuota, casa)
    for book in event.get("bookmakers", []):
        is_sharp = book.get("key") == SHARP_BOOK
        for m in book.get("markets", []):
            mk = m.get("key")
            if mk not in markets:
                continue
            outcomes = m.get("outcomes", [])
            for key, p in devig_groups(mk, outcomes):
                probs[(mk,) + key].append(p)
                if is_sharp:
                    sharp[(mk,) + key] = p
            for o in outcomes:
                price = o.get("price")
                if not price:
                    continue
                k = (mk, o.get("name"), o.get("point"), o.get("description"))
                if k not in best or price > best[k][0]:
                    best[k] = (price, book.get("title"))
    home, away = event.get("home_team"), event.get("away_team")
    picks = []
    for k, plist in probs.items():
        if len(plist) < min_books or k not in best:
            continue
        p = statistics.median(plist)  # la mediana resiste casas con líneas raras
        if k in sharp:
            p = SHARP_WEIGHT * sharp[k] + (1 - SHARP_WEIGHT) * p
        if not (0.03 <= p <= 0.97):
            continue
        price, book = best[k]
        ev = p * price - 1
        if not (PRICE_RANGE[0] <= price <= PRICE_RANGE[1]):
            continue
        if not (min_ev <= ev <= MAX_EV):
            continue
        picks.append({
            "event_id": event.get("id"), "sport_key": sport_key, "category": category,
            "league": f"{category.split()[0]} {league}".strip(), "kind": kind,
            "home": home, "away": away, "match": f"{home} vs {away}", "commence": commence,
            "market": k[0], "name": k[1], "point": k[2], "who": k[3],
            "price": price, "book": book, "p": p, "ev": ev, "n": len(plist),
        })
    return picks


def collect(group_key, hours=WINDOW_HOURS, main=True, props=True):
    """Picks con valor de partidos que empiezan dentro de la ventana (ahora → ahora + hours).
    Primero mercados principales, luego props. Cada etapa respeta su tope de créditos."""
    start, end = window(hours)
    leagues = []
    for idx, (category, sport_key, tier, name) in enumerate(LEAGUES):
        if category not in GROUPS[group_key] or not sport_is_active(sport_key):
            continue
        evs = games_in_window(sport_key, start, end)
        n = 1 if evs is None else len(evs)  # si falló la consulta gratis, se intenta igual
        if n > 0:
            leagues.append({"idx": idx, "category": category, "key": sport_key, "tier": tier,
                            "name": name, "n": n, "evs": evs or []})
    leagues.sort(key=lambda L: (L["tier"], -L["n"], L["idx"]))
    print(f"Ligas con partidos en las próximas {hours} h: {len(leagues)}")
    picks = []
    if main:
        for L in leagues:
            events = get_odds(L["key"], L["category"])
            found = []
            for event in events:
                commence = parse_dt(event["commence_time"])
                if start <= commence <= end:
                    found += event_picks(event, L["category"], L["key"], commence, L["tier"], L["name"])
            if events:
                print(f"  {L['name']}: {L['n']} partidos · {len(found)} selecciones con valor")
            picks += found
    if props:
        picks += collect_props(leagues, start, end)
    return sorted(picks, key=lambda x: x["ev"], reverse=True)


def collect_props(leagues, start, end):
    """Córners, tarjetas y props de jugadores. Una consulta por partido, en orden de importancia."""
    out = []
    for ps in PROP_SETS:
        if not ps["on"]:
            continue
        cands = []
        for L in leagues:
            if L["category"] != ps["category"] or (ps["leagues"] and L["key"] not in ps["leagues"]):
                continue
            for e in L["evs"]:
                commence = parse_dt(e["commence_time"])
                if start <= commence <= end:
                    cands.append((L["tier"], commence, L, e))
        cands.sort(key=lambda t: (t[0], t[1]))
        cost = len(ps["markets"]) * len(ps["regions"].split(","))
        asked = found_n = 0
        for tier, commence, L, e in cands:
            if (e["id"], ps["key"]) not in _props_cache and not props_budget_ok(cost):
                print(f"  {ps['name']}: tope de créditos alcanzado")
                break
            data = get_event_props(L["key"], e["id"], ps)
            if not data:
                continue
            asked += 1
            found = event_picks(data, L["category"], L["key"], commence, L["tier"], L["name"],
                                markets=ps["markets"], kind="prop")
            found_n += len(found)
            out += found
        if cands:
            print(f"  {ps['name']}: {asked} partidos consultados · {found_n} selecciones con valor")
    return out


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
    ("🟡 MEDIO", 3, lambda p: -p["ev"]),                # mayor ventaja
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
        sig = frozenset((p["event_id"], p["market"], p["name"], p["point"], p["who"]) for p in legs)
        if sig in seen:
            continue
        seen.add(sig)
        used |= {p["event_id"] for p in legs}
        out.append((name, legs))
    return out


def label(p):
    """Texto corto de la selección, con los nombres que usa Draftea."""
    m, n, pt, who = p["market"], p["name"], p["point"], p["who"]
    over = "Más de" if n == "Over" else "Menos de"
    if m == "h2h":
        return "Empate" if n == "Draw" else f"Gana {n}"
    if m == "totals":
        return f"{over} {pt:g} {'goles' if p['sport_key'].startswith('soccer') else 'puntos'}"
    if m == "spreads":
        return f"{n} ({pt:+g})"
    base = f"{over} {pt:g} {STAT_NAMES.get(m, m.replace('_', ' '))}"
    return f"{who}: {base}" if who else base


def stake_pct(p, odds):
    """Kelly fraccionado con tope (% de la banca). None si no hay ventaja."""
    if odds <= 1:
        return None
    f = (p * odds - 1) / (odds - 1)
    if f <= 0:
        return None
    return max(min(f * KELLY_FRACTION, MAX_STAKE) * 100, 0.05)


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
        cols = {r["name"] for r in con.execute("PRAGMA table_info(legs)")}
        if "who" not in cols:  # bases viejas: agrega la columna del jugador/equipo
            con.execute("ALTER TABLE legs ADD COLUMN who TEXT")


def signature(kind, picks):
    return kind + "|" + "|".join(sorted(
        f"{p['event_id']}:{p['market']}:{p['name']}:{p['point']}" + (f":{p['who']}" if p.get("who") else "")
        for p in picks))


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
            "INSERT INTO legs(parlay_id,event_id,sport_key,match,home,away,commence,market,name,point,price,p,who) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(pid, p["event_id"], p["sport_key"], p["match"], p["home"], p["away"],
              p["commence"].isoformat(), p["market"], p["name"], p["point"], p["price"], p["p"], p.get("who"))
             for p in picks])
    return pid


def delete_parlay(pid):
    """Borra un parlay (se usa si falló el envío a Discord, para que se pueda volver a publicar)."""
    with closing(db()) as con, con:
        con.execute("DELETE FROM legs WHERE parlay_id=?", (pid,))
        con.execute("DELETE FROM parlays WHERE id=?", (pid,))


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
        if h == a and not str(leg["sport_key"]).startswith("soccer"):
            return "push"  # en NFL/NBA un empate devuelve la apuesta
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


def resolve_pending(max_credits=None):
    """Califica picks pasados con los marcadores finales (/scores, hasta 3 días atrás).
    Córners, tarjetas y props no se califican solos (la API no da esas estadísticas): quedan "n/d".
    max_credits limita lo que se gasta; las ligas con más picks pendientes van primero."""
    global API_REMAINING
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=3)).isoformat()  # da tiempo a que terminen los partidos
    spent = 0
    with closing(db()) as con, con:
        pend = con.execute(
            "SELECT * FROM legs WHERE result IS NULL AND commence < ?", (cutoff,)).fetchall()
        by_sport = defaultdict(list)
        for leg in pend:
            if leg["market"] not in GRADABLE:
                con.execute("UPDATE legs SET result='n/d' WHERE id=?", (leg["id"],))
            else:
                by_sport[leg["sport_key"]].append(leg)
        for sport_key, legs in sorted(by_sport.items(), key=lambda kv: -len(kv[1])):
            events = {}
            left = _credits_left()
            enough = left is None or left >= MIN_CREDITS
            in_budget = max_credits is None or spent + SCORES_COST <= max_credits
            if enough and in_budget:
                try:
                    res = SESSION.get(
                        f"{API}/sports/{sport_key}/scores/",
                        params={"apiKey": ODDS_API_KEY, "daysFrom": 3}, timeout=20)
                    API_REMAINING = res.headers.get("x-requests-remaining", API_REMAINING)
                    if res.status_code == 200:
                        spent += _call_cost(res, SCORES_COST)
                        events = {e["id"]: e for e in res.json()}
                except Exception as e:
                    print(f"  Error en scores {sport_key}: {e}")
            for leg in legs:
                ev = events.get(leg["event_id"])
                result = grade(leg, ev) if ev and ev.get("completed") else None
                if result is None and now - parse_dt(leg["commence"]) > timedelta(days=4):
                    result = "n/d"  # ya no se puede consultar el marcador
                if result:
                    con.execute("UPDATE legs SET result=? WHERE id=?", (result, leg["id"]))
    return spent


def stats():
    with closing(db()) as con:
        rows = con.execute(
            "SELECT p.id pid, p.kind, l.result, l.price FROM parlays p "
            "JOIN legs l ON l.parlay_id=p.id").fetchall()
    by = defaultdict(list)
    for r in rows:
        by[r["pid"]].append(r)
    out = {k: {"w": 0, "l": 0, "push": 0, "pend": 0, "profit": 0.0} for k in ("parlay", "simple")}
    for legs in by.values():
        s = out[legs[0]["kind"]]
        res = [x["result"] for x in legs]
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
    return out


# ── Mensajes (cortos y claros) ───────────────────────────────────────
FOOTER = "+18 · Juega con responsabilidad"


def footer():
    return FOOTER + (f" · créditos API {API_REMAINING}" if API_REMAINING else "")


def leg_field(i, p, show_stake=False):
    value = (
        f"{p['league']} · {p['match']}\n"
        f"🕒 {when(p['commence'])} · ventaja {p['ev'] * 100:+.0f}%"
    )
    if show_stake:
        pct = stake_pct(p["p"], p["price"])
        if pct:
            value += f"\n💰 Apuesta: {pct:.1f}% de tu banca"
    return f"{NUMS[i - 1]} {label(p)} · {p['price']:.2f}", value


def parlay_embed(legs, pid, hours, risk_label=None):
    legs = sorted(legs, key=lambda p: p["commence"])  # en orden de horario
    total_odds = math.prod(p["price"] for p in legs)
    comb_p = math.prod(p["p"] for p in legs)
    if comb_p >= 0.30:
        risk, color = "🟢 BAJO", 3066993
    elif comb_p >= 0.15:
        risk, color = "🟡 MEDIO", 16763904
    else:
        risk, color = "🔴 ALTO", 15158332
    if risk_label:
        risk, color = risk_label, TIER_COLORS[risk_label]
    pct = stake_pct(comb_p, total_odds)
    lines = [
        f"💥 Cuota **{total_odds:.2f}** · $100 pagan **${100 * total_odds:,.0f}**",
        f"✅ Juégalo en Draftea solo si paga **{1 / comb_p:.2f}** o más",
    ]
    if pct:
        lines.append(f"💰 Apuesta: **{pct:.1f}%** de tu banca")
    embed = discord.Embed(title=f"{risk} · PARLAY #{pid}", description="\n".join(lines), color=color)
    for i, p in enumerate(legs, 1):
        name, value = leg_field(i, p)
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text=footer())
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def singles_embed(picks, ids, hours, title="🎯 SIMPLES"):
    embed = discord.Embed(title=title, color=3447003,
                          description="Una sola selección por apuesta. Juégalas solo si Draftea paga igual o más.")
    for i, (p, pid) in enumerate(zip(picks, ids), 1):
        name, value = leg_field(i, p, show_stake=True)
        embed.add_field(name=name, value=value + f"\n✅ Mínimo en Draftea: {1 / p['p']:.2f}", inline=False)
    embed.set_footer(text=footer())
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def empty_embed(hours=WINDOW_HOURS):
    return discord.Embed(
        title="😴 Hoy no hay picks buenos",
        description="Mejor no apostar. Se revisa de nuevo en la próxima corrida.",
        color=15844367,
    )


def results_embed():
    out = stats()
    embed = discord.Embed(title="📒 RESULTADOS", color=3447003)
    for kind, title in (("parlay", "Parlays"), ("simple", "Simples")):
        s = out[kind]
        done = s["w"] + s["l"]
        if done + s["push"] + s["pend"] == 0:
            continue
        rate = f"{s['w'] / done * 100:.0f}%" if done else "—"
        roi = f"{s['profit'] / (done + s['push']) * 100:+.0f}%" if done + s["push"] else "—"
        embed.add_field(
            name=title,
            value=f"✅ {s['w']}  ❌ {s['l']}  ➖ {s['push']}  ⏳ {s['pend']}\nAcierto {rate} · Ganancia {roi}",
            inline=False,
        )
    if not embed.fields:
        embed.description = "Todavía no hay apuestas registradas."
    else:
        embed.set_footer(text="Córners, tarjetas y props no se califican solos. Pocas apuestas = pronto para concluir.")
    return embed


def leagues_text():
    """Texto con las ligas en temporada (consulta gratis)."""
    load_active_sports()
    keys = _active["keys"]
    lines = []
    for cat in (FUT, NFL_C, NBA_C):
        names = [name for c, key, tier, name in LEAGUES
                 if c == cat and (keys is None or key in keys)]
        if names:
            lines.append(f"**{cat}** ({len(names)}): " + ", ".join(names))
    body = "\n".join(lines) or "No hay ligas en temporada ahora mismo."
    credits = f"\nCréditos de la API: `{API_REMAINING}`" if API_REMAINING else ""
    return "**Ligas en temporada:**\n" + body + credits


# ── Constructores (los usan los comandos y daily.py) ─────────────────
def build_parlay(group_key, legs, hours, user_id):
    chosen = top_unique(collect(group_key, hours), legs)
    if len(chosen) < 2:
        return empty_embed(hours)
    pid = save_parlay(user_id, "parlay", chosen)
    return parlay_embed(chosen, pid, hours)


def build_tiers(group_key, hours, user_id, only_new=False):
    """Un parlay por nivel de riesgo. only_new=True omite los ya publicados antes.
    Devuelve ([(embed, parlay_id), ...], total_generados)."""
    tiers = tiered_parlays(collect(group_key, hours))
    items = []
    for name, legs in tiers:
        if only_new and find_parlay("parlay", legs):
            continue
        pid = save_parlay(user_id, "parlay", legs)
        items.append((parlay_embed(legs, pid, hours, risk_label=name), pid))
    return items, len(tiers)


def build_singles(group_key, hours, user_id, props_only=False):
    picks = collect(group_key, hours, main=not props_only)
    if props_only:
        picks = [p for p in picks if p["kind"] == "prop"]
    chosen = top_unique(picks, 3)
    if not chosen:
        return empty_embed(hours)
    ids = [save_parlay(user_id, "simple", [p]) for p in chosen]
    return singles_embed(chosen, ids, hours, title="🔥 PROPS (córners, tarjetas, jugadores)" if props_only else "🎯 SIMPLES")


# ── Bot de Discord ───────────────────────────────────────────────────
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
    app_commands.Choice(name="NBA", value="nba"),
]


@client.tree.command(name="parlay", description="Parlay con valor de las próximas horas")
@app_commands.describe(deporte="Filtra por deporte", selecciones="Número de selecciones (2 a 4)",
                       horas="Horas hacia adelante (1 a 24)")
@app_commands.choices(deporte=DEPORTES)
async def parlay(
    interaction: discord.Interaction,
    deporte: app_commands.Choice[str] = None,
    selecciones: app_commands.Range[int, 2, 4] = 3,
    horas: app_commands.Range[int, 1, 24] = 24,
):
    await interaction.response.defer(thinking=True)
    embed = await asyncio.to_thread(
        build_parlay, deporte.value if deporte else "all", selecciones, horas, str(interaction.user.id))
    await interaction.followup.send(embed=embed)


@client.tree.command(name="riesgos", description="3 parlays: bajo, medio y alto")
@app_commands.describe(deporte="Filtra por deporte", horas="Horas hacia adelante (1 a 24)")
@app_commands.choices(deporte=DEPORTES)
async def riesgos(
    interaction: discord.Interaction,
    deporte: app_commands.Choice[str] = None,
    horas: app_commands.Range[int, 1, 24] = 24,
):
    await interaction.response.defer(thinking=True)
    items, _ = await asyncio.to_thread(
        build_tiers, deporte.value if deporte else "all", horas, str(interaction.user.id))
    if items:
        await interaction.followup.send(embeds=[e for e, _ in items])
    else:
        await interaction.followup.send(embed=empty_embed(horas))


@client.tree.command(name="simple", description="Mejores apuestas simples (sin parlay)")
@app_commands.describe(deporte="Filtra por deporte", horas="Horas hacia adelante (1 a 24)")
@app_commands.choices(deporte=DEPORTES)
async def simple(
    interaction: discord.Interaction,
    deporte: app_commands.Choice[str] = None,
    horas: app_commands.Range[int, 1, 24] = 24,
):
    await interaction.response.defer(thinking=True)
    embed = await asyncio.to_thread(
        build_singles, deporte.value if deporte else "all", horas, str(interaction.user.id))
    await interaction.followup.send(embed=embed)


@client.tree.command(name="props", description="Córners, tarjetas y props de jugadores con valor")
@app_commands.describe(deporte="Filtra por deporte", horas="Horas hacia adelante (1 a 24)")
@app_commands.choices(deporte=DEPORTES)
async def props(
    interaction: discord.Interaction,
    deporte: app_commands.Choice[str] = None,
    horas: app_commands.Range[int, 1, 24] = 24,
):
    await interaction.response.defer(thinking=True)
    embed = await asyncio.to_thread(
        build_singles, deporte.value if deporte else "all", horas, str(interaction.user.id), True)
    await interaction.followup.send(embed=embed)


@client.tree.command(name="futbol", description="Parlay de fútbol")
async def futbol(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    embed = await asyncio.to_thread(build_parlay, "futbol", 3, WINDOW_HOURS, str(interaction.user.id))
    await interaction.followup.send(embed=embed)


@client.tree.command(name="nfl", description="Parlay de NFL")
async def nfl(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    embed = await asyncio.to_thread(build_parlay, "nfl", 3, WINDOW_HOURS, str(interaction.user.id))
    await interaction.followup.send(embed=embed)


@client.tree.command(name="nba", description="Parlay de NBA")
async def nba(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    embed = await asyncio.to_thread(build_parlay, "nba", 3, WINDOW_HOURS, str(interaction.user.id))
    await interaction.followup.send(embed=embed)


@client.tree.command(name="ligas", description="Ligas en temporada y créditos de la API")
async def ligas(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    text = await asyncio.to_thread(leagues_text)
    await interaction.followup.send(text[:1990], ephemeral=True)


@client.tree.command(name="resultados", description="Califica picks pasados y muestra cómo va el bot")
async def resultados(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    await asyncio.to_thread(resolve_pending)
    embed = await asyncio.to_thread(results_embed)
    await interaction.followup.send(embed=embed)


@client.tree.command(name="ayuda", description="Comandos del bot")
async def ayuda(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**/riesgos** → 3 parlays (bajo, medio, alto)\n"
        "**/parlay** → un parlay\n"
        "**/simple** → apuestas de una sola selección\n"
        "**/props** → córners, tarjetas y jugadores\n"
        "**/futbol · /nfl · /nba** → parlay por deporte\n"
        "**/ligas** → ligas activas y créditos\n"
        "**/resultados** → cómo va el bot\n\n"
        "Cada pick dice la cuota y el mínimo que debe pagar Draftea. Si paga menos, no lo juegues.",
        ephemeral=True,
    )


@tasks.loop(time=dtime(hour=15, minute=0, tzinfo=timezone.utc))  # 09:00 hora CDMX
async def daily_post():
    channel = client.get_channel(int(CHANNEL_ID)) or await client.fetch_channel(int(CHANNEL_ID))
    items, _ = await asyncio.to_thread(build_tiers, "all", WINDOW_HOURS, "auto", True)
    for embed, _pid in items:
        await channel.send(embed=embed)


if __name__ == "__main__":
    if not DISCORD_TOKEN or not ODDS_API_KEY:
        raise SystemExit("Faltan las variables DISCORD_TOKEN y/o ODDS_API_KEY")
    client.run(DISCORD_TOKEN)
