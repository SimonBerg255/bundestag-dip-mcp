from fastmcp import FastMCP
from mcp.server.fastmcp import Icon
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from tools import (
    aktuelle_gesetzgebung,
    drucksache_lesen,
    plenarprotokolle,
    suche_drucksachen,
    suche_vorgaenge,
    vorgang_details,
)

####### SERVER #######

icon = Icon(
    src="https://www.bundestag.de/resource/image/196108/16x9/999/562/fa5e3b26f9a5a60e01e4bfadffa50bc4/A31C6F2B4F5D63E2EA58A779C3895E74/bt-logo.jpg",
)

INSTRUCTION_STRING = """
Server für parlamentarische Recherche im Deutschen Bundestag (DIP API).
Alle Inhalte sind auf Deutsch. Suchbegriffe IMMER auf Deutsch formulieren.

ENTSCHEIDUNGSBAUM — wähle das richtige Tool:

1. NUTZER FRAGT NACH THEMA / AKTIVITÄTEN IM BUNDESTAG:
   → suche_vorgaenge — findet parlamentarische Prozesse nach Stichwort, Typ, Datum
   Beispiele: "Was läuft gerade zu Klimaschutz?", "Welche Anträge zur Rente?",
              "Anfragen zur Digitalisierung der letzten 3 Monate"
   WORKFLOW: suche_vorgaenge → vorgang_details für Einzelheiten

2. NUTZER FRAGT NACH KONKRETEN DOKUMENTEN / DRUCKSACHEN:
   → suche_drucksachen — findet Gesetzentwürfe, Anfragen, Antworten, Berichte
   → drucksache_lesen — liest den Volltext eines gefundenen Dokuments
   WORKFLOW: suche_drucksachen → drucksache_lesen mit der ID

3. NUTZER FRAGT NACH STATUS EINES GESETZENTWURFS:
   → aktuelle_gesetzgebung — Überblick laufender und abgeschlossener Gesetze
   → vorgang_details — Detailstatus eines bestimmten Vorgangs
   Typischer Flow: aktuelle_gesetzgebung → vorgang_details(vorgang_id='...')

4. NUTZER FRAGT "WAS WAR DIESE WOCHE IM BUNDESTAG?":
   → plenarprotokolle — listet Sitzungen mit behandelten Themen auf
   Sitzungs-PDFs sind als Links enthalten.

5. NUTZER WILL VOLLTEXT LESEN (Gesetzentwurf, Anfrage, Antwort):
   → drucksache_lesen(drucksache_id='...', volltext=False) für Vorschau
   → drucksache_lesen(drucksache_id='...', volltext=True) für vollständigen Text
   KONTEXT-LIMIT: Text wird auf max. 22.000 Zeichen begrenzt. Längere Dokumente
   über den PDF-Link abrufen, der immer mitgeliefert wird.

WICHTIGE HINTERGRUNDINFORMATIONEN:
- Wahlperiode 20 = Bundestag 2021–2025, Wahlperiode 21 = ab Oktober 2025
- Datumsfilter (datum_von/datum_bis) sind zuverlässiger als Wahlperiode
- Ein Vorgang fasst alle Drucksachen eines Prozesses zusammen
- Eine Drucksache ist das konkrete Dokument (hat eine Nummer wie "21/4870")
- Vorgangstypen: "Gesetzgebung", "Kleine Anfrage", "Große Anfrage", "Antrag"
- Drucksachetypen: "Gesetzentwurf", "Kleine Anfrage", "Antwort", "Bericht"
- Sehr neue Dokumente (<1-2 Tage) haben ggf. noch keinen indexierten Volltext
"""

mcp = FastMCP(
    name="Bundestag DIP — Parlamentarische Recherche",
    instructions=INSTRUCTION_STRING,
    version="1.0.0",
    website_url="https://dip.bundestag.de",
    icons=[icon],
)

####### TOOLS #######

mcp.tool(meta={"requires_permission": False})(suche_vorgaenge)
mcp.tool(meta={"requires_permission": False})(suche_drucksachen)
mcp.tool(meta={"requires_permission": False})(vorgang_details)
mcp.tool(meta={"requires_permission": False})(drucksache_lesen)
mcp.tool(meta={"requires_permission": False})(aktuelle_gesetzgebung)
mcp.tool(meta={"requires_permission": False})(plenarprotokolle)

####### ROUTES #######


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


####### APP #######
# Run with: uvicorn server:app --host 0.0.0.0 --port $PORT

app = mcp.http_app()
