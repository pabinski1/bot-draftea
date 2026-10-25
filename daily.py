"""
Corrida diaria para GitHub Actions: genera los 3 parlays (bajo / medio / alto)
con partidos de las próximas 24 h (fútbol, NFL y NBA) y los publica en Discord por webhook.
"""
import os
import sys
import time
from datetime import datetime

import requests

import discord_bot as bot

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
HOURS = bot.WINDOW_HOURS
NOTIFY_EMPTY = os.getenv("NOTIFY_EMPTY", "1") == "1"        # avisar en Discord si no hay picks
RESULTS_WEEKDAY = int(os.getenv("RESULTS_WEEKDAY", "0"))   # día del resumen semanal (0=lunes, -1=nunca)
# Presupuesto de créditos por corrida. El plan gratis de The Odds API da 500 al mes (~16 al día):
# 12 para cuotas + 4 para calificar resultados. Si tienes un plan de pago, súbelos.
MAX_CREDITS_PER_RUN = int(os.getenv("MAX_CREDITS_PER_RUN", "12"))
RESULTS_BUDGET = int(os.getenv("RESULTS_BUDGET", "4"))


def send(embed):
    """Envía un embed por webhook. Respeta el límite de Discord (429) y reintenta."""
    payload = {"username": "Draftea Bot", "embeds": [embed.to_dict()]}
    for _ in range(4):
        try:
            r = requests.post(WEBHOOK, json=payload, timeout=20)
        except requests.RequestException as e:
            print(f"Error de red al enviar a Discord: {e}")
            time.sleep(2)
            continue
        if r.status_code in (200, 204):
            return True
        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 2))
            except ValueError:
                wait = 2
            time.sleep(wait + 0.5)
            continue
        print(f"Discord respondió {r.status_code}: {r.text[:300]}")
        if r.status_code >= 500:
            time.sleep(2)
            continue
        break
    return False


def main():
    if not WEBHOOK or not bot.ODDS_API_KEY:
        raise SystemExit("Faltan los secrets DISCORD_WEBHOOK_URL y/o ODDS_API_KEY")

    bot.init_db()
    bot.load_active_sports()
    bot.begin_run(MAX_CREDITS_PER_RUN)

    items, total = bot.build_tiers("all", HOURS, "auto", only_new=True)
    failed = 0

    if items:
        for embed, pid in items:
            if send(embed):
                print(f"Parlay #{pid} enviado a Discord")
            else:
                bot.delete_parlay(pid)  # que se pueda volver a publicar en la próxima corrida
                failed += 1
            time.sleep(1)
    elif total == 0:
        print(f"No hay picks con valor en las próximas {HOURS} h.")
        if NOTIFY_EMPTY and not send(bot.empty_embed(HOURS)):
            failed += 1
    else:
        print("Sin parlays nuevos en esta corrida (ya se habían publicado).")

    # Califica picks pasados y, una vez por semana, publica el resumen de resultados
    spent_results = 0
    try:
        spent_results = bot.resolve_pending(RESULTS_BUDGET)
        if RESULTS_WEEKDAY >= 0 and datetime.now(bot.TZ).weekday() == RESULTS_WEEKDAY:
            send(bot.results_embed())
    except Exception as e:  # los resultados nunca deben tumbar la corrida
        print(f"No se pudieron calificar los picks: {e}")

    print(f"Créditos gastados: {bot.RUN_SPENT} en cuotas + {spent_results} en resultados")
    print(f"Créditos API restantes: {bot.API_REMAINING}")
    if failed:
        sys.exit(1)  # la acción sale en rojo si Discord rechazó algo


if __name__ == "__main__":
    main()
