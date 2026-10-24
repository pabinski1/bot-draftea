"""Ejecución única (GitHub Actions, cada 3 horas): no necesita estar encendido 24/7."""
import os
from datetime import datetime

import requests

import discord_bot as b

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")


def post(embed):
    res = requests.post(WEBHOOK, json={"embeds": [embed.to_dict()]}, timeout=15)
    res.raise_for_status()


def main():
    if not WEBHOOK or not b.ODDS_API_KEY:
        raise SystemExit("Faltan DISCORD_WEBHOOK_URL y/o ODDS_API_KEY")

    b.init_db()
    now = datetime.now(b.TZ)
    first_run = now.hour < 12  # primera corrida del día (9:00 CDMX)

    if first_run:
        b.resolve_pending()  # califica picks pasados (una vez al día para ahorrar créditos)

    day = b.target_day("hoy")
    embeds, total = b.build_tiers("all", day, "auto", only_new=True)

    for embed in embeds:
        post(embed)

    if not embeds and total == 0 and first_run:
        post(b.empty_embed(day))
    elif not embeds:
        print("Sin parlays nuevos en esta corrida.")

    # Los lunes, en la primera corrida, publica el resumen de resultados
    if first_run and now.weekday() == 0:
        resumen = b.results_embed()
        if resumen.fields:
            post(resumen)

    print(f"Créditos API restantes: {b.API_REMAINING}")


if __name__ == "__main__":
    main()
