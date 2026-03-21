"""
Tools for Bundestag DIP MCP Server.

Data source: Bundestag DIP API (https://search.dip.bundestag.de/api/v1)
Swagger UI:  https://dip.bundestag.api.bund.dev/
API key env: DIP_API_KEY (falls back to the public demo key, valid until 05/2026)

Research findings (2026-03-21):
  - Endpoints: /vorgang, /drucksache, /plenarprotokoll, /aktivitaet
    plus /drucksache-text and /plenarprotokoll-text for full text.
  - Pagination: cursor-based. Top-level 'cursor' field in every list response.
    Pass ?cursor=<value> to fetch next page.
  - Filter params verified working:
      searchTerm          full-text search (must be GERMAN)
      f.vorgangstyp       e.g. "Gesetzgebung", "Kleine Anfrage", "Antrag"
      f.drucksachetyp     e.g. "Kleine Anfrage", "Gesetzentwurf", "Antwort"
      f.wahlperiode       20 (2021-2025) or 21 (from late 2025)
      f.datum.start       ISO date YYYY-MM-DD
      f.datum.end         ISO date YYYY-MM-DD
      f.beratungsstand    e.g. "Überwiesen", "Verabschiedet", "Verkündet"
      f.id                filter /drucksache-text by numeric document ID
  - IMPORTANT: The `limit` query parameter is IGNORED by the DIP API — it always
    returns 100 items per page regardless of what value is sent. Tools slice the
    response server-side (docs[:limit]) to keep output bounded.
  - Text sizes: /drucksache-text can reach 44 K+ chars. Always cap output.
  - All content is in German. Search terms must be German.
  - Wahlperiode 20 = 2021–2025, Wahlperiode 21 = from late 2025.
  - f.vorgang does NOT reliably filter /drucksache by vorgang ID.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DIP_BASE = "https://search.dip.bundestag.de/api/v1"
DIP_API_KEY = os.getenv("DIP_API_KEY", "OSOegLs.PR2lwJ1dwCeje9vTj7FPOt3hvpYKtwKkhw")

# Context safety caps
MAX_TEXT_CHARS = 22_000     # max chars returned for drucksache full text
PREVIEW_CHARS = 3_000       # default preview when volltext=False
MAX_RESPONSE_CHARS = 24_000 # hard cap on any list-tool response

# Status icons for Gesetzgebung output — module-level constant, not rebuilt per call
_STATUS_ICON: dict[str, str] = {
    "Verkündet": "✓ KRAFT",
    "Verabschiedet": "✓ VERAB",
    "Überwiesen": "⟳ LÄUFT",
    "Abgelehnt": "✗ ABGEL",
    "Zurückgezogen": "↩ ZURÜCK",
    "Erledigt durch Ablauf der Wahlperiode": "— ERLED",
}

# Shared async HTTP client — one connection pool across all tool calls,
# avoiding per-call TLS handshakes. Connection pool is initialised lazily.
_client = httpx.AsyncClient(base_url=DIP_BASE, timeout=30.0)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _params(**extra) -> dict:
    """Base query params (API key) merged with any extras."""
    return {"apikey": DIP_API_KEY, **extra}


def _days_ago(n: int) -> str:
    """Return an ISO date string for n days before today (UTC)."""
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def _truncate(output: str) -> str:
    """Hard-cap output at MAX_RESPONSE_CHARS and append a notice if truncated."""
    if len(output) > MAX_RESPONSE_CHARS:
        return output[:MAX_RESPONSE_CHARS] + f"\n\n[Ausgabe auf {MAX_RESPONSE_CHARS:,} Zeichen begrenzt]"
    return output


def _clean_abstract(text: str, br_replacement: str = " ") -> str:
    """Normalise abstract HTML tags and whitespace to plain text."""
    return (
        text
        .replace("<br />", br_replacement)
        .replace("<br/>", br_replacement)
        .replace("\r\n", " ")
        .strip()
    )


def _fmt_vorgang(doc: dict, index: int) -> list[str]:
    lines = [
        f"[{index}] ID: {doc.get('id')} | {doc.get('vorgangstyp', 'N/A')}",
        f"    Titel: {doc.get('titel', 'N/A')}",
        f"    Status: {doc.get('beratungsstand', 'N/A')}",
        f"    Datum: {doc.get('datum', 'N/A')} | Wahlperiode: {doc.get('wahlperiode', 'N/A')}",
    ]
    if doc.get("initiative"):
        lines.append(f"    Initiative: {', '.join(doc['initiative'][:3])}")
    if doc.get("sachgebiet"):
        lines.append(f"    Sachgebiet: {', '.join(doc['sachgebiet'][:2])}")
    if doc.get("abstract"):
        abstract = _clean_abstract(doc["abstract"])
        suffix = "…" if len(abstract) > 250 else ""
        lines.append(f"    Zusammenfassung: {abstract[:250]}{suffix}")
    return lines


def _fmt_drucksache(doc: dict, index: int) -> list[str]:
    fundstelle = doc.get("fundstelle", {})
    lines = [
        f"[{index}] ID: {doc.get('id')} | {doc.get('drucksachetyp', 'N/A')} {doc.get('dokumentnummer', '')}",
        f"    Titel: {doc.get('titel', 'N/A')}",
        f"    Datum: {doc.get('datum', 'N/A')} | Wahlperiode: {doc.get('wahlperiode', 'N/A')}",
    ]
    urheber = doc.get("urheber", [])
    if urheber:
        lines.append(f"    Urheber: {', '.join(u.get('titel', '') for u in urheber[:2])}")
    if fundstelle.get("pdf_url"):
        lines.append(f"    PDF: {fundstelle['pdf_url']}")
    for vb in doc.get("vorgangsbezug", [])[:2]:
        lines.append(f"    → Vorgang [{vb['id']}]: {vb['titel'][:80]}")
    return lines


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def suche_vorgaenge(
    suchbegriff: Optional[str] = None,
    vorgangstyp: Optional[str] = None,
    datum_von: Optional[str] = None,
    datum_bis: Optional[str] = None,
    wahlperiode: Optional[int] = None,
    beratungsstand: Optional[str] = None,
    limit: int = 15,
) -> str:
    """
    Durchsucht parlamentarische Vorgänge im Deutschen Bundestag.

    Ein Vorgang ist ein parlamentarischer Prozess — z. B. ein Gesetzgebungsverfahren,
    eine Anfrage oder ein Antrag — der alle zugehörigen Dokumente bündelt.
    Für den Volltext einzelner Dokumente → drucksache_lesen verwenden.

    WICHTIG: Alle Suchbegriffe müssen auf DEUTSCH sein. Die API enthält
    ausschließlich deutschsprachige Inhalte.

    Typische vorgangstyp-Werte:
      "Gesetzgebung"              → Gesetzentwürfe und Gesetzgebungsverfahren
      "Kleine Anfrage"            → Schriftliche Anfragen von Fraktionen
      "Große Anfrage"             → Umfangreichere parlamentarische Anfragen
      "Antrag"                    → Parlamentarische Anträge
      "Bericht, Gutachten, Programm" → Berichte und Gutachten

    Typische beratungsstand-Werte:
      "Überwiesen"       → An Ausschüsse überwiesen (läuft)
      "Verabschiedet"    → Vom Bundestag beschlossen
      "Verkündet"        → Im Bundesgesetzblatt (in Kraft)
      "Abgelehnt"        → Abgelehnt

    Args:
        suchbegriff: Deutschsprachiger Suchbegriff (z. B. "Klimaschutz", "Rente")
        vorgangstyp: Art des Vorgangs (z. B. "Gesetzgebung", "Kleine Anfrage")
        datum_von: Startdatum YYYY-MM-DD (z. B. "2025-01-01")
        datum_bis: Enddatum YYYY-MM-DD
        wahlperiode: 20 (2021–2025) oder 21 (ab 2025). Datumsfilter bevorzugen.
        beratungsstand: Status-Filter (z. B. "Überwiesen", "Verkündet")
        limit: Anzahl Ergebnisse (Standard: 15, max: 25)

    Returns:
        Liste der Vorgänge mit ID, Titel, Typ, Status, Datum und Initiatoren.
        Die ID kann für vorgang_details oder suche_drucksachen genutzt werden.
    """
    limit = min(limit, 25)
    p = _params()
    if suchbegriff:
        p["searchTerm"] = suchbegriff
    if vorgangstyp:
        p["f.vorgangstyp"] = vorgangstyp
    if datum_von:
        p["f.datum.start"] = datum_von
    if datum_bis:
        p["f.datum.end"] = datum_bis
    if wahlperiode:
        p["f.wahlperiode"] = wahlperiode
    if beratungsstand:
        p["f.beratungsstand"] = beratungsstand

    resp = await _client.get("/vorgang", params=p)
    resp.raise_for_status()
    data = resp.json()

    docs = data.get("documents", [])[:limit]
    num_found = data.get("numFound", 0)
    cursor = data.get("cursor")

    if not docs:
        return "Keine Vorgänge für die angegebenen Kriterien gefunden."

    lines = [
        f"VORGÄNGE — {num_found:,} gefunden, zeige {len(docs)}",
        "=" * 60,
    ]
    for i, doc in enumerate(docs, 1):
        lines.extend(_fmt_vorgang(doc, i))
        lines.append("")

    if num_found > limit:
        lines.append(f"Weitere Ergebnisse verfügbar. Cursor für nächste Seite: {cursor}")
        lines.append("Tipp: Datum eingrenzen oder anderen vorgangstyp wählen.")

    return _truncate("\n".join(lines))


async def suche_drucksachen(
    suchbegriff: Optional[str] = None,
    drucksachetyp: Optional[str] = None,
    datum_von: Optional[str] = None,
    datum_bis: Optional[str] = None,
    wahlperiode: Optional[int] = None,
    limit: int = 15,
) -> str:
    """
    Durchsucht Drucksachen (offizielle parlamentarische Dokumente) des Bundestages.

    Eine Drucksache ist ein konkretes Dokument: ein Gesetzentwurf, eine Anfrage,
    die Antwort der Bundesregierung usw. Für den Volltext → drucksache_lesen.

    WICHTIG: Alle Suchbegriffe müssen auf DEUTSCH sein.

    Typische drucksachetyp-Werte:
      "Gesetzentwurf"           → Entwürfe für neue Gesetze
      "Kleine Anfrage"          → Schriftliche Anfragen an die Bundesregierung
      "Antwort"                 → Antworten der Bundesregierung auf Anfragen
      "Antrag"                  → Parlamentarische Anträge
      "Große Anfrage"           → Umfangreichere Anfragen
      "Bericht"                 → Ausschussberichte
      "Beschlussempfehlung"     → Ausschussempfehlungen
      "Unterrichtung"           → Unterrichtungen des Bundestages

    Args:
        suchbegriff: Deutschsprachiger Suchbegriff (z. B. "Digitalisierung", "Rente")
        drucksachetyp: Art der Drucksache (z. B. "Kleine Anfrage", "Gesetzentwurf")
        datum_von: Startdatum YYYY-MM-DD
        datum_bis: Enddatum YYYY-MM-DD
        wahlperiode: 20 (2021–2025) oder 21 (ab 2025)
        limit: Anzahl Ergebnisse (Standard: 15, max: 25)

    Returns:
        Liste mit ID, Dokumentnummer, Titel, Typ, Datum, Urheber und PDF-Link.
        Die ID kann für drucksache_lesen genutzt werden.
    """
    limit = min(limit, 25)
    p = _params()
    if suchbegriff:
        p["searchTerm"] = suchbegriff
    if drucksachetyp:
        p["f.drucksachetyp"] = drucksachetyp
    if datum_von:
        p["f.datum.start"] = datum_von
    if datum_bis:
        p["f.datum.end"] = datum_bis
    if wahlperiode:
        p["f.wahlperiode"] = wahlperiode

    resp = await _client.get("/drucksache", params=p)
    resp.raise_for_status()
    data = resp.json()

    docs = data.get("documents", [])[:limit]
    num_found = data.get("numFound", 0)
    cursor = data.get("cursor")

    if not docs:
        return "Keine Drucksachen für die angegebenen Kriterien gefunden."

    lines = [
        f"DRUCKSACHEN — {num_found:,} gefunden, zeige {len(docs)}",
        "=" * 60,
    ]
    for i, doc in enumerate(docs, 1):
        lines.extend(_fmt_drucksache(doc, i))
        lines.append("")

    if num_found > limit:
        lines.append(f"Weitere Ergebnisse verfügbar. Cursor für nächste Seite: {cursor}")

    return _truncate("\n".join(lines))


async def vorgang_details(vorgang_id: str) -> str:
    """
    Gibt vollständige Details eines parlamentarischen Vorgangs zurück.

    Liefert alle Metadaten: Titel, Typ, Beratungsstand, Initiative, Sachgebiete,
    Schlagwörter (Deskriptoren), GESTA-Nummer und Zustimmungsbedürftigkeit.

    Verwendung: Wenn der Nutzer den genauen Status oder Hintergrund eines
    Vorgangs wissen möchte. Die vorgang_id erhält man über suche_vorgaenge.
    Für den Volltext der zugehörigen Dokumente → suche_drucksachen (mit dem
    Titel als Suchbegriff) + drucksache_lesen verwenden.

    Args:
        vorgang_id: Numerische ID des Vorgangs (z. B. "332609")

    Returns:
        Vollständige Metadaten des Vorgangs.
    """
    resp = await _client.get(f"/vorgang/{vorgang_id}", params=_params())
    if resp.status_code == 404:
        return f"Vorgang mit ID {vorgang_id} nicht gefunden."
    resp.raise_for_status()
    doc = resp.json()

    lines = [
        "VORGANG — VOLLSTÄNDIGE DETAILS",
        "=" * 60,
        f"ID:               {doc.get('id')}",
        f"Titel:            {doc.get('titel', 'N/A')}",
        f"Vorgangstyp:      {doc.get('vorgangstyp', 'N/A')}",
        f"Beratungsstand:   {doc.get('beratungsstand', 'N/A')}",
        f"Wahlperiode:      {doc.get('wahlperiode', 'N/A')}",
        f"Datum:            {doc.get('datum', 'N/A')}",
        f"Aktualisiert:     {doc.get('aktualisiert', 'N/A')}",
    ]

    if doc.get("gesta"):
        lines.append(f"GESTA-Nummer:     {doc['gesta']}")
    if doc.get("initiative"):
        lines.append(f"Initiative:       {', '.join(doc['initiative'])}")
    if doc.get("sachgebiet"):
        lines.append(f"Sachgebiet:       {', '.join(doc['sachgebiet'])}")
    if doc.get("zustimmungsbeduerftigkeit"):
        lines.append(f"Zustimmung BR:    {'; '.join(doc['zustimmungsbeduerftigkeit'])}")
    if doc.get("abstract"):
        # Preserve line breaks in the detail view (newline replacement vs space in list view)
        abstract = _clean_abstract(doc["abstract"], br_replacement="\n")
        lines += ["", "ZUSAMMENFASSUNG:", abstract]

    deskriptoren = doc.get("deskriptor", [])
    if deskriptoren:
        kw = [d["name"] for d in deskriptoren if d.get("name")]
        lines += ["", f"SCHLAGWÖRTER: {', '.join(kw[:20])}"]

    lines += [
        "",
        "NÄCHSTE SCHRITTE:",
        "→ Zugehörige Drucksachen finden: suche_drucksachen mit Suchbegriff aus dem Titel",
        "→ Volltext lesen: drucksache_lesen mit der Drucksachen-ID",
    ]

    return "\n".join(lines)


async def drucksache_lesen(
    drucksache_id: str,
    volltext: bool = False,
) -> str:
    """
    Liest Metadaten und Volltext einer Drucksache (parlamentarisches Dokument).

    Gibt zunächst Metadaten zurück (Nummer, Typ, Titel, Urheber, PDF-Link),
    dann den Textinhalt. Aus Kontextschutzgründen wird der Text begrenzt:
    - volltext=False (Standard): Vorschau mit ~3.000 Zeichen
    - volltext=True: bis zu 22.000 Zeichen

    Hinweis: Sehr neue Dokumente haben manchmal noch keinen indexierten Text.
    In dem Fall ist der PDF-Link in der Antwort die beste Alternative.

    Die drucksache_id erhält man über suche_drucksachen oder vorgang_details.

    Args:
        drucksache_id: Numerische ID der Drucksache (z. B. "279131")
        volltext: False = Vorschau (3.000 Z.), True = vollständiger Text (max. 22.000 Z.)

    Returns:
        Metadaten + Textinhalt (ggf. gekürzt) + PDF-Link für vollständigen Text.
    """
    # Fetch metadata and full text in parallel — they are independent endpoints
    meta_resp, text_resp = await asyncio.gather(
        _client.get(f"/drucksache/{drucksache_id}", params=_params()),
        _client.get("/drucksache-text", params=_params(**{"f.id": drucksache_id})),
    )

    if meta_resp.status_code == 404:
        return f"Drucksache mit ID {drucksache_id} nicht gefunden."
    meta_resp.raise_for_status()
    text_resp.raise_for_status()

    meta = meta_resp.json()
    text_data = text_resp.json()

    fundstelle = meta.get("fundstelle", {})
    pdf_url = fundstelle.get("pdf_url", "N/A")

    lines = [
        "DRUCKSACHE",
        "=" * 60,
        f"ID:         {meta.get('id')}",
        f"Nummer:     {meta.get('dokumentnummer', 'N/A')}",
        f"Typ:        {meta.get('drucksachetyp', 'N/A')}",
        f"Titel:      {meta.get('titel', 'N/A')}",
        f"Datum:      {meta.get('datum', 'N/A')} | Wahlperiode: {meta.get('wahlperiode', 'N/A')}",
        f"PDF:        {pdf_url}",
    ]

    urheber = meta.get("urheber", [])
    if urheber:
        lines.append(f"Urheber:    {', '.join(u.get('titel', '') for u in urheber)}")

    autoren = meta.get("autoren_anzeige", [])
    if autoren:
        lines.append(f"Autoren:    {', '.join(a.get('autor_titel', '') for a in autoren[:5])}")

    for vb in meta.get("vorgangsbezug", [])[:3]:
        lines.append(f"→ Vorgang [{vb['id']}]: {vb['titel'][:80]}")

    # Text section
    text_docs = text_data.get("documents", [])
    if not text_docs:
        lines += [
            "",
            "[Kein Volltext in der DIP-Datenbank verfügbar.]",
            f"Vollständiger Text als PDF: {pdf_url}",
        ]
        return "\n".join(lines)

    raw_text = text_docs[0].get("text", "")
    if not raw_text:
        lines += [
            "",
            "[Volltext noch nicht indexiert — bitte PDF-Link verwenden.]",
            f"PDF: {pdf_url}",
        ]
        return "\n".join(lines)

    cap = MAX_TEXT_CHARS if volltext else PREVIEW_CHARS
    is_truncated = len(raw_text) > cap

    lines += [
        "",
        f"{'—' * 40}",
        f"TEXT ({len(raw_text):,} Zeichen gesamt):",
        "—" * 40,
        raw_text[:cap],
    ]

    if is_truncated:
        remaining = len(raw_text) - cap
        lines += [
            f"\n[… TEXT GEKÜRZT — noch {remaining:,} Zeichen]",
            f"→ Für mehr Text: drucksache_lesen(drucksache_id='{drucksache_id}', volltext=True)",
            f"→ Vollständiger Text als PDF: {pdf_url}",
        ]

    return "\n".join(lines)


async def aktuelle_gesetzgebung(
    datum_von: Optional[str] = None,
    beratungsstand: Optional[str] = None,
    limit: int = 15,
) -> str:
    """
    Ruft aktuelle Gesetzgebungsverfahren des Deutschen Bundestages ab.

    Zeigt Gesetzentwürfe sortiert nach Datum (neueste zuerst) mit Status,
    Initianten und Sachgebiet. Ideal für einen schnellen Überblick über
    laufende oder abgeschlossene Gesetzgebung.

    Typische beratungsstand-Werte zum Filtern:
      "Überwiesen"        → In Ausschussberatung (laufend)
      "Verabschiedet"     → Vom Bundestag beschlossen
      "Verkündet"         → In Kraft getreten
      "Abgelehnt"         → Gescheitert
      "Zurückgezogen"     → Zurückgezogen

    Args:
        datum_von: Startdatum YYYY-MM-DD (Standard: letzte 30 Tage)
        beratungsstand: Filter nach Status (z. B. "Überwiesen", "Verkündet")
        limit: Anzahl Ergebnisse (Standard: 15, max: 25)

    Returns:
        Liste der Gesetzgebungsverfahren mit Status, Initianten und Sachgebiet.
        Für Details → vorgang_details. Für Volltext → drucksache_lesen.
    """
    datum_von = datum_von or _days_ago(30)
    limit = min(limit, 25)

    p = _params()
    p["f.vorgangstyp"] = "Gesetzgebung"
    p["f.datum.start"] = datum_von
    if beratungsstand:
        p["f.beratungsstand"] = beratungsstand

    resp = await _client.get("/vorgang", params=p)
    resp.raise_for_status()
    data = resp.json()

    docs = data.get("documents", [])[:limit]
    num_found = data.get("numFound", 0)

    if not docs:
        return f"Keine Gesetzgebungsverfahren seit {datum_von} gefunden."

    lines = [
        f"GESETZGEBUNG — seit {datum_von}",
        f"Gefunden: {num_found:,} Verfahren, zeige {len(docs)}",
        "=" * 60,
    ]

    for i, doc in enumerate(docs, 1):
        stand = doc.get("beratungsstand", "")
        icon = _STATUS_ICON.get(stand, f"· {stand[:6]}")
        lines.append(f"\n[{i}] [{icon}] {doc.get('titel', 'N/A')[:90]}")
        lines.append(f"     ID: {doc.get('id')} | Datum: {doc.get('datum', 'N/A')} | WP: {doc.get('wahlperiode', 'N/A')}")
        if doc.get("initiative"):
            lines.append(f"     Initiative: {', '.join(doc['initiative'][:3])}")
        if doc.get("sachgebiet"):
            lines.append(f"     Sachgebiet: {', '.join(doc['sachgebiet'][:2])}")

    lines.append("")
    if num_found > limit:
        lines.append(f"Weitere {num_found - limit:,} Verfahren — datum_von eingrenzen oder limit erhöhen.")
    lines.append("→ Details: vorgang_details(vorgang_id='...')")

    return _truncate("\n".join(lines))


async def plenarprotokolle(
    datum_von: Optional[str] = None,
    datum_bis: Optional[str] = None,
    limit: int = 10,
) -> str:
    """
    Listet Plenarsitzungen des Deutschen Bundestages mit behandelten Themen.

    Gibt eine Übersicht der Sitzungen mit Datum, Sitzungsnummer, PDF-Link
    und den behandelten Vorgängen (Gesetze, Anträge usw.).

    Verwendung: Wenn der Nutzer wissen möchte, was der Bundestag in einer
    bestimmten Woche debattiert hat, oder welche Themen in Sitzungen
    behandelt wurden.

    Hinweis: Volltexte von Plenarprotokollen sind sehr umfangreich (oft
    100+ Seiten). Die Sitzungs-PDFs sind über die PDF-Links abrufbar.

    Args:
        datum_von: Startdatum YYYY-MM-DD (Standard: letzte 14 Tage)
        datum_bis: Enddatum YYYY-MM-DD
        limit: Anzahl Sitzungen (Standard: 10, max: 20)

    Returns:
        Liste der Plenarsitzungen mit Sitzungsnummern, Daten, PDF-Links
        und Übersicht der behandelten Vorgänge.
    """
    datum_von = datum_von or _days_ago(14)
    limit = min(limit, 20)

    p = _params()
    p["f.datum.start"] = datum_von
    if datum_bis:
        p["f.datum.end"] = datum_bis

    resp = await _client.get("/plenarprotokoll", params=p)
    resp.raise_for_status()
    data = resp.json()

    docs = data.get("documents", [])[:limit]
    num_found = data.get("numFound", 0)

    if not docs:
        return f"Keine Plenarsitzungen seit {datum_von} gefunden."

    lines = [
        f"PLENARSITZUNGEN — seit {datum_von}",
        f"Gefunden: {num_found} Sitzungen, zeige {len(docs)}",
        "=" * 60,
    ]

    for i, doc in enumerate(docs, 1):
        fundstelle = doc.get("fundstelle", {})
        lines.append(f"\n[{i}] Sitzung {doc.get('dokumentnummer', 'N/A')}")
        lines.append(f"    Datum: {doc.get('datum', 'N/A')} | WP: {doc.get('wahlperiode', 'N/A')}")
        if fundstelle.get("pdf_url"):
            lines.append(f"    PDF: {fundstelle['pdf_url']}")

        vorgangsbezug = doc.get("vorgangsbezug", [])
        anzahl = doc.get("vorgangsbezug_anzahl", len(vorgangsbezug))
        if vorgangsbezug:
            lines.append(f"    Behandelte Vorgänge ({anzahl} gesamt):")
            for vb in vorgangsbezug[:8]:
                lines.append(
                    f"      · [{vb['id']}] ({vb.get('vorgangstyp', '?')}): {vb['titel'][:72]}"
                )
            if anzahl > 8:
                lines.append(f"      … und {anzahl - 8} weitere")
        else:
            lines.append(f"    Behandelte Vorgänge: {anzahl} (noch nicht vollständig verknüpft)")

    return _truncate("\n".join(lines))
