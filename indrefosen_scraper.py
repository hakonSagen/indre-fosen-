"""
Indre Fosen kommunestyremøte-skraper

Henter møteliste → laster ned protokoll + innkalling (PDF) →
trekker ut tekst → genererer artikkel med Claude API.

Avhengigheter:
    pip install requests pdfplumber anthropic playwright
    playwright install chromium

Bruk:
    python indrefosen_scraper.py
    python indrefosen_scraper.py --alle        # inkluder fremtidige møter uten protokoll
    python indrefosen_scraper.py --utvalg alle # alle utvalg, ikke bare kommunestyret
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import pdfplumber
import anthropic
from playwright.sync_api import sync_playwright

# ── Konfigurasjon ─────────────────────────────────────────────────────────────

BASE_URL = "https://www.indrefosen.kommune.no"
INNSYN_BASE = f"{BASE_URL}/api/presentation/v2/nye-innsyn"
MOTEOVERSIKT_PAGE = f"{BASE_URL}/innsyn/moteoversikt/"

# Verdier lest fra ACOS CMS-konteksten på møteoversikts-siden
PORTAL_HEADERS = {
    "PortalID": "1",
    "MenypunktID": "2387",
    "SprakID": "1",
    "WebObjektID": "4013",
    "ObjektID": "-1",
    "DeviceType": "desktop",
    "X-ANTI-CSRF": "1",
}

UTVALG_FILTER = "Kommunestyret"   # Sett til None for å hente alle utvalg

# DATA_DIR peker på Render-tjenestens persistente disk (mountet via render.yaml).
# Lokalt faller den tilbake til ./data slik at scriptet kan kjøres uendret.
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
PDF_DIR = DATA_DIR / "pdfs"
ARTIKLER_DIR = DATA_DIR / "artikler"
PDF_DIR.mkdir(parents=True, exist_ok=True)
ARTIKLER_DIR.mkdir(parents=True, exist_ok=True)


# ── HTTP-sesjon ───────────────────────────────────────────────────────────────

def lag_sesjon() -> requests.Session:
    """Oppretter sesjon med ACOS anonymous-cookie."""
    s = requests.Session()
    s.headers.update({
        "Content-Type": "application/json",
        "Referer": MOTEOVERSIKT_PAGE,
        **PORTAL_HEADERS,
    })
    # Hent anonymous session-cookie
    s.get(MOTEOVERSIKT_PAGE, timeout=15)
    return s


# ── API-kall ──────────────────────────────────────────────────────────────────

def hent_moter(sesjon: requests.Session, fra_dato: str | None = None) -> list[dict]:
    """
    Returnerer møter fra møteoversikten.
    fra_dato: "YYYY-MM-DD" — bare møter fra og med denne datoen.
    Uten dato hentes alle kommende møter.
    """
    key_values = []
    if fra_dato:
        key_values.append({"key": "Dato", "value": fra_dato})
        key_values.append({"key": "Dato", "value": "ComingMeetings"})

    body = {"type": 1, "keyValues": key_values}
    r = sesjon.post(f"{INNSYN_BASE}/overviewInit", json=body, timeout=15)
    r.raise_for_status()

    data = r.json()
    return data.get("content", {}).get("searchItems", {}).get("items", [])


def hent_moter_historiske(sesjon: requests.Session) -> list[dict]:
    """Henter historiske møter (siste 12 måneder) ved å søke uten datofilter."""
    body = {"type": 1, "keyValues": []}
    r = sesjon.post(f"{INNSYN_BASE}/overviewInit", json=body, timeout=15)
    r.raise_for_status()
    return r.json().get("content", {}).get("searchItems", {}).get("items", [])


def hent_motedetaljer(sesjon: requests.Session, identifier: str) -> dict:
    """Henter full møtestruktur: innkalling, protokoll, saksliste."""
    r = sesjon.post(f"{INNSYN_BASE}/mote/{identifier}", json={}, timeout=15)
    r.raise_for_status()
    return r.json().get("content", {})


def last_ned_pdf(pw_context, fil_identifier: str, filnavn: str) -> Path | None:
    """
    Laster ned en PDF via Playwright headless browser (omgår Azure WAF JS-challenge).
    Åpner en side, navigerer til PDF-URL og fanger opp nedlastingen.
    """
    sti = PDF_DIR / filnavn
    if sti.exists():
        return sti   # allerede cachet

    url = f"{BASE_URL}/api/presentation/v2/nye-innsyn/filer/{fil_identifier}?pid=1"
    side = pw_context.new_page()
    try:
        with side.expect_download(timeout=30_000) as dl_info:
            side.goto(url, wait_until="commit")
        nedlasting = dl_info.value
        nedlasting.save_as(str(sti))
        print(f"  ✓ Lastet ned {filnavn} ({sti.stat().st_size // 1024} KB)")
        return sti
    except Exception as e:
        # PDF åpnet i browser i stedet for nedlasting — les bytes fra respons
        try:
            # PDF åpnet i browser i stedet for nedlasting — prøv via XHR
            pdf_bytes = side.evaluate(f"""async () => {{
                const r = await fetch('{url}', {{
                    headers: {json.dumps({**PORTAL_HEADERS, "Referer": MOTEOVERSIKT_PAGE})}
                }});
                const buf = await r.arrayBuffer();
                return Array.from(new Uint8Array(buf));
            }}""")
            data = bytes(pdf_bytes)
            if data[:4] == b'%PDF':
                sti.write_bytes(data)
                print(f"  ✓ Lastet ned {filnavn} ({len(data)//1024} KB)")
                return sti
        except Exception:
            pass
        print(f"  ⚠  Klarte ikke laste ned {filnavn}: {e}")
        return None
    finally:
        side.close()


# ── PDF-tekst-ekstraksjon ─────────────────────────────────────────────────────

def trekk_ut_tekst(pdf_sti: Path, maks_tegn: int = 25_000) -> str:
    """Trekker ut tekst fra PDF med pdfplumber."""
    tekst_deler = []
    with pdfplumber.open(pdf_sti) as pdf:
        for side in pdf.pages:
            tekst_deler.append(side.extract_text() or "")
    full_tekst = "\n".join(tekst_deler).strip()
    return full_tekst[:maks_tegn]


# ── Artikkels-generering ──────────────────────────────────────────────────────

def generer_artikkel(
    claude: anthropic.Anthropic,
    mote_tittel: str,
    mote_dato: str,
    protokoll_tekst: str,
    innkalling_tekst: str,
) -> str:
    """Sender tekst til Claude og ber om en journalistisk artikkel."""

    system = (
        "Du er en erfaren lokalreporter som skriver korte, faktabaserte nyhetsartikler "
        "om kommunepolitikk for lesere i Indre Fosen. "
        "Skriv alltid på norsk (bokmål). "
        "Hold deg til fakta fra de oppgitte kildedokumentene. "
        "Ikke spekul. Ikke skriv om deg selv."
    )

    prompt_deler = [
        f"Skriv en kort nyhetsartikkel (ca. 250–400 ord) om {mote_tittel} den {mote_dato}.",
        "",
        "Artikkelen skal:",
        "- Ha en tydelig ingress som oppsummerer de viktigste sakene.",
        "- Gå gjennom de mest betydningsfulle vedtakene eller diskusjonene.",
        "- Nevne eventuelle interpellasjoner eller særlig kontroversielle saker.",
        "- Bruke en nøytral, journalistisk tone.",
        "",
    ]

    if protokoll_tekst:
        prompt_deler += [
            "## Møteprotokoll (primær kilde):",
            protokoll_tekst[:20_000],
            "",
        ]

    if innkalling_tekst:
        prompt_deler += [
            "## Møteinnkalling (tilleggsinformasjon):",
            innkalling_tekst[:8_000],
            "",
        ]

    melding = "\n".join(prompt_deler)

    svar = claude.messages.create(
        model="claude-opus-4-8",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": melding}],
    )
    return svar.content[0].text


# ── Hjelpefunksjoner ──────────────────────────────────────────────────────────

def finn_filer_i_detaljer(detaljer: dict) -> dict:
    """
    Returnerer dict med nøkler 'innkalling' og 'protokoll'.
    Hver verdi er enten None eller {'identifier': str, 'title': str}.
    """
    resultat = {"innkalling": None, "protokoll": None}

    for blokk in detaljer.get("b", []):
        if blokk.get("key") != "MEETING_FILES":
            continue
        for fil in blokk.get("b", []):
            fil_key = fil.get("key", "")
            if fil_key == "MEETING_FILES_SUMMON" and fil.get("identifier"):
                resultat["innkalling"] = {
                    "identifier": fil["identifier"],
                    "title": fil.get("title", "innkalling"),
                }
            elif fil_key == "MEETING_FILES_PROTOCOL" and fil.get("identifier"):
                resultat["protokoll"] = {
                    "identifier": fil["identifier"],
                    "title": fil.get("title", "protokoll"),
                }

    return resultat


def rens_filnavn(tekst: str) -> str:
    return re.sub(r"[^\w\-]", "_", tekst)[:80]


# ── Hoved-pipeline ────────────────────────────────────────────────────────────

def behandle_mote(
    sesjon: requests.Session,
    pw_context,
    claude: anthropic.Anthropic,
    mote: dict,
    krever_protokoll: bool = True,
) -> str | None:
    """
    Behandler ett møte. Returnerer generert artikkel eller None.
    """
    identifier = mote["identifier"]
    tittel = mote["title"]
    dato = mote.get("properties", {}).get("dato", "ukjent dato")
    status = mote.get("status", "")

    print(f"\n─── {tittel} — {dato} ({status}) ───")

    detaljer = hent_motedetaljer(sesjon, identifier)
    filer = finn_filer_i_detaljer(detaljer)

    if krever_protokoll and not filer["protokoll"]:
        print("  → Protokoll ikke publisert enda, hopper over.")
        return None

    # Last ned innkalling
    innkalling_tekst = ""
    if filer["innkalling"]:
        safe_navn = rens_filnavn(f"{tittel}_{dato}_innkalling") + ".pdf"
        pdf_sti = last_ned_pdf(pw_context, filer["innkalling"]["identifier"], safe_navn)
        if pdf_sti:
            innkalling_tekst = trekk_ut_tekst(pdf_sti)

    # Last ned protokoll
    protokoll_tekst = ""
    if filer["protokoll"]:
        safe_navn = rens_filnavn(f"{tittel}_{dato}_protokoll") + ".pdf"
        pdf_sti = last_ned_pdf(pw_context, filer["protokoll"]["identifier"], safe_navn)
        if pdf_sti:
            protokoll_tekst = trekk_ut_tekst(pdf_sti)

    if not protokoll_tekst and not innkalling_tekst:
        print("  → Ingen tekst å generere artikkel fra.")
        return None

    print("  → Genererer artikkel med Claude …")
    artikkel = generer_artikkel(claude, tittel, dato, protokoll_tekst, innkalling_tekst)
    return artikkel


def kjor_scraping(
    alle: bool = False,
    utvalg: str | None = UTVALG_FILTER,
    historisk: bool = False,
    fra_dato: str | None = None,
) -> list[dict]:
    """
    Kjører hele pipelinen én gang og returnerer listen med genererte artikler.
    Brukes både av CLI-inngangen (main) og av service-loopen (run_service.py).
    """
    utvalg_filter = None if utvalg == "alle" else utvalg

    print("Kobler til Indre Fosen kommune …")
    sesjon = lag_sesjon()
    claude = anthropic.Anthropic()

    # Hent møteliste
    if historisk:
        moter = hent_moter_historiske(sesjon)
    else:
        fra_dato = fra_dato or datetime.today().strftime("%Y-%m-%d")
        moter = hent_moter(sesjon, fra_dato)

    print(f"Fant {len(moter)} møter totalt.")

    # Filtrer på utvalg
    if utvalg_filter:
        moter = [m for m in moter if utvalg_filter.lower() in m.get("title", "").lower()]
        print(f"Etter filter '{utvalg_filter}': {len(moter)} møte(r).")

    if not moter:
        print("Ingen møter å behandle.")
        return []

    artikler = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # Besøk hovudsiden for å etablere gyldig WAF-sesjon
        pw_context = browser.new_context(base_url=BASE_URL)
        pw_context.new_page().goto(MOTEOVERSIKT_PAGE, wait_until="domcontentloaded")

        for mote in moter:
            time.sleep(0.5)  # Vær snill mot serveren
            artikkel = behandle_mote(
                sesjon, pw_context, claude, mote,
                krever_protokoll=not alle,
            )
            if artikkel:
                tittel = mote["title"]
                dato = mote.get("properties", {}).get("dato", "")
                artikler.append({"tittel": tittel, "dato": dato, "artikkel": artikkel})

        browser.close()

    if not artikler:
        print("\nIngen artikler ble generert.")
        return []

    output_fil = ARTIKLER_DIR / f"artikler_{datetime.today().strftime('%Y%m%d_%H%M')}.json"
    output_fil.write_text(json.dumps(artikler, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"Genererte {len(artikler)} artikkel(er). Lagret i {output_fil}")
    print(f"{'='*60}\n")

    for item in artikler:
        print(f"## {item['tittel']} — {item['dato']}\n")
        print(item["artikkel"])
        print("\n" + "─"*60 + "\n")

    return artikler


def main():
    parser = argparse.ArgumentParser(description="Skraper for Indre Fosen kommunestyremøter")
    parser.add_argument("--alle", action="store_true",
                        help="Inkluder også møter uten publisert protokoll")
    parser.add_argument("--utvalg", default=UTVALG_FILTER,
                        help="Filtrer på utvalgsnavn (standard: Kommunestyret, 'alle' for ingen filter)")
    parser.add_argument("--historisk", action="store_true",
                        help="Hent historiske møter i stedet for kommende")
    parser.add_argument("--fra-dato", default=None,
                        help="Hent møter fra og med denne datoen (YYYY-MM-DD)")
    args = parser.parse_args()

    artikler = kjor_scraping(
        alle=args.alle,
        utvalg=args.utvalg,
        historisk=args.historisk,
        fra_dato=args.fra_dato,
    )
    if not artikler:
        sys.exit(0)


if __name__ == "__main__":
    main()
