"""
Service-runner for Render (Private Service).

Render Cron Jobs kan ikke ha persistent disk, så denne tjenesten kjører
kontinuerlig og trigger scrapingen selv på et fast intervall — PDF-er og
artikler lagres på den monterte disken (se render.yaml / DATA_DIR).

Miljøvariabler:
    RUN_INTERVAL_HOURS  Timer mellom hver kjøring (standard: 24)
    RUN_ON_START        "true" for å kjøre én gang umiddelbart ved oppstart (standard: true)
"""

import os
import time
import traceback
from datetime import datetime

from indrefosen_scraper import kjor_scraping

INTERVAL_HOURS = float(os.environ.get("RUN_INTERVAL_HOURS", "24"))
RUN_ON_START = os.environ.get("RUN_ON_START", "true").lower() == "true"


def kjor_en_runde():
    print(f"\n[{datetime.now().isoformat()}] Starter scraping-runde …")
    try:
        kjor_scraping()
    except Exception:
        print(f"[{datetime.now().isoformat()}] Feil under kjøring:")
        traceback.print_exc()
    print(f"[{datetime.now().isoformat()}] Runde ferdig. Neste kjøring om {INTERVAL_HOURS} time(r).")


def main():
    if RUN_ON_START:
        kjor_en_runde()

    while True:
        time.sleep(INTERVAL_HOURS * 3600)
        kjor_en_runde()


if __name__ == "__main__":
    main()
