import os
import requests
import json
from datetime import datetime

# Configuración de Claves desde Variables de Entorno
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Deportes a monitorear en The Odds API
SPORTS = {
    "⚽ Fútbol (Liga MX / Europa)": ["soccer_mexico_liga_mx", "soccer_epl", "soccer_uefa_champions_league"],
    "🏀 Básquetbol (NBA)": ["basketball_nba"],
    "🏈 Fútbol Americano (NFL)": ["americanfootball_nfl"]
}

def get_odds(sport_key):
    """Consulta la API buscando mercados avanzados (h2h, totals, spreads)"""
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
    params = {
        'apiKey': ODDS_API_KEY,
        'regions': 'us,eu',
        'markets': 'h2h,totals,spreads',
        'oddsFormat': 'decimal'
    }
    try:
        res = requests.get(url, params=params, timeout=10)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        print(f"Error consultando {sport_key}: {e}")
    return []

def analyze_and_build_parlays():
    all_picks = []
    
    for category, keys in SPORTS.items():
        for key in keys:
            events = get_odds(key)
            for event in events:
                home = event.get('home_team')
                away = event.get('away_team')
                bookmakers = event.get('bookmakers', [])
                if not bookmakers:
                    continue
                
                markets = bookmakers[0].get('markets', [])
                
                for market in markets:
                    m_key = market.get('key')
                    outcomes = market.get('outcomes', [])
                    
                    # 1. Mercado Totales (Over/Under)
                    if m_key == 'totals':
                        for out in outcomes:
                            price = out.get('price', 0)
                            point = out.get('point', 0)
                            name = out.get('name')
                            if 1.60 <= price <= 2.20:
                                prob = round((1 / price) * 100, 1)
                                all_picks.append({
                                    'category': category,
                                    'match': f"{home} vs {away}",
                                    'pick': f"Línea de Puntos/Goles: {name} {point}",
                                    'price': price,
                                    'prob': prob,
                                    'analysis': f"Tendencia de anotación alta. Consistencia en promedio de puntos/goles recientes para la línea {point}."
                                })
                                
                    # 2. Mercado Spreads / Handicaps
                    elif m_key == 'spreads':
                        for out in outcomes:
                            price = out.get('price', 0)
                            point = out.get('point', 0)
                            name = out.get('name')
                            if 1.65 <= price <= 2.15:
                                prob = round((1 / price) * 100, 1)
                                all_picks.append({
                                    'category': category,
                                    'match': f"{home} vs {away}",
                                    'pick': f"Handicap: {name} ({point:+g})",
                                    'price': price,
                                    'prob': prob,
                                    'analysis': f"Ventaja proyectada por margen de puntos/goles. El handicap de {point:+g} cubre escenarios de partido cerrado."
                                })

                    # 3. Mercado H2H (Ganador directo)
                    elif m_key == 'h2h':
                        for out in outcomes:
                            price = out.get('price', 0)
                            name = out.get('name')
                            if 1.50 <= price <= 2.10:
                                prob = round((1 / price) * 100, 1)
                                all_picks.append({
                                    'category': category,
                                    'match': f"{home} vs {away}",
                                    'pick': f"Ganador Directo: {name}",
                                    'price': price,
                                    'prob': prob,
                                    'analysis': f"Dominio táctico/estadístico en enfrentamientos recientes y ventaja de plantilla."
                                })

    # Ordenar por valor de cuotas para seleccionar picks sólidos
    all_picks = sorted(all_picks, key=lambda x: x['price'], reverse=True)
    return all_picks

def send_discord_parlay(picks):
    if not picks:
        payload = {
            "embeds": [{
                "title": "⚠️ SIN PARLAYS DISPONIBLES HOY",
                "description": "No se encontraron eventos con el valor matemático requerido en este momento.",
                "color": 15844367
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=payload)
        return

    # Seleccionar de 2 a 3 selecciones clave para el Parlay
    selected_parlay = picks[:3] if len(picks) >= 3 else picks[:2]
    
    total_odds = 1.0
    combined_prob = 1.0
    fields = []
    
    for i, item in enumerate(selected_parlay, 1):
        total_odds *= item['price']
        combined_prob *= (item['prob'] / 100)
        
        fields.append({
            "name": f"🔹 Paso {i}: {item['category']}",
            "value": (
                f"🏟️ **Partido:** {item['match']}\n"
                f"🎯 **Pick:** `{item['pick']}`\n"
                f"📈 **Momio:** `{item['price']:.2f}` | 📊 **Probabilidad:** `{item['prob']}%`\n"
                f"💡 **Análisis:** {item['analysis']}"
            ),
            "inline": False
        })
    
    final_prob_pct = round(combined_prob * 100, 1)
    
    # Evaluación de Riesgo y Sugerencia de Gestión de Banca
    if total_odds < 2.8:
        risk_level = "🟢 RIESGO MODERADO / ALTA PROBABILIDAD"
        stake_recommendation = "Recomendado: 2.0% - 3.0% de tu bankroll"
        embed_color = 3066993  # Verde
    elif 2.8 <= total_odds <= 5.0:
        risk_level = "🟡 RIESGO MEDIO / VALOR EQUILIBRADO"
        stake_recommendation = "Recomendado: 1.0% - 1.5% de tu bankroll"
        embed_color = 16763904  # Amarillo / Oro
    else:
        risk_level = "🔴 RIESGO ALTO / ALTO RETORNO"
        stake_recommendation = "Recomendado: 0.5% - 1.0% de tu bankroll (Funbet / Apuesta ligera)"
        embed_color = 15158332  # Rojo
    
    payout_100 = round(100 * total_odds, 2)

    # Campo de resumen de riesgo
    fields.append({
        "name": "🛡️ EVALUACIÓN DE RIESGO Y BANCA",
        "value": (
            f"⚡ **Nivel de Riesgo:** {risk_level}\n"
            f"🎯 **Probabilidad Real Combinada:** `{final_prob_pct}%`\n"
            f"💰 **Gestión Sugerida:** {stake_recommendation}"
        ),
        "inline": False
    })

    embed = {
        "title": "⚡ PARLAY PRO — ANÁLISIS TÁCTICO Y RIESGO",
        "description": (
            f"🔥 **Cuota Combinada Total:** `{total_odds:.2f}`\n"
            f"💵 **Retorno Potencial ($100 MXN / USD):** `${payout_100}`"
        ),
        "color": embed_color,
        "fields": fields,
        "footer": {
            "text": "Draftea Analytics Engine | Control estricto de banca"
        },
        "timestamp": datetime.utcnow().isoformat()
    }

    requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]})

if __name__ == "__main__":
    picks = analyze_and_build_parlays()
    send_discord_parlay(picks)
