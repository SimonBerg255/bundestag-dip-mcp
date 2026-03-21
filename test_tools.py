"""
Live integration tests for Bundestag DIP MCP tools.

Run with:  python test_tools.py
All tests make real calls to the DIP API. No mocking.
"""

import asyncio
import time

# Patch env before import
import os
os.environ.setdefault("MCP_SERVER_JWT_SECRET", "test-secret-not-used-in-tests-xxxxx")

from tools import (
    aktuelle_gesetzgebung,
    drucksache_lesen,
    plenarprotokolle,
    suche_drucksachen,
    suche_vorgaenge,
    vorgang_details,
)

PASS = "✓"
FAIL = "✗"
results = []


def check(name: str, result: str, *, max_chars: int = 25_000):
    char_count = len(result)
    ok_size = char_count <= max_chars
    ok_content = len(result.strip()) > 50  # non-empty
    ok = ok_size and ok_content

    symbol = PASS if ok else FAIL
    results.append((name, ok, char_count))
    print(f"  {symbol} {name}")
    print(f"    chars={char_count:,}  size_ok={ok_size}  content_ok={ok_content}")
    if not ok_size:
        print(f"    !! EXCEEDS {max_chars:,} char limit !!")
    if not ok_content:
        print(f"    !! EMPTY or near-empty response !!")
    print(f"    Preview: {result[:120].strip()!r}")
    print()
    return ok


async def run_tests():
    print("=" * 70)
    print("BUNDESTAG DIP MCP — LIVE INTEGRATION TESTS")
    print("=" * 70)
    print()

    # ------------------------------------------------------------------
    # Test 1: Vorgänge search — Klimaschutz, WP20
    # ------------------------------------------------------------------
    print("TEST 1: suche_vorgaenge — 'Klimaschutz', Wahlperiode 20")
    t0 = time.time()
    result = await suche_vorgaenge(
        suchbegriff="Klimaschutz",
        wahlperiode=20,
        limit=5,
    )
    elapsed = time.time() - t0
    print(f"  Elapsed: {elapsed:.2f}s")
    ok1 = check("suche_vorgaenge(Klimaschutz, WP20)", result)
    assert "Klimaschutz" in result or "VORGÄNGE" in result, "Expected Klimaschutz results"
    assert elapsed < 15, f"Too slow: {elapsed:.1f}s"

    # ------------------------------------------------------------------
    # Test 2: Recent Gesetzentwürfe
    # ------------------------------------------------------------------
    print("TEST 2: aktuelle_gesetzgebung — letzte 30 Tage")
    t0 = time.time()
    result = await aktuelle_gesetzgebung(limit=5)
    elapsed = time.time() - t0
    print(f"  Elapsed: {elapsed:.2f}s")
    ok2 = check("aktuelle_gesetzgebung(30 Tage)", result)
    assert "GESETZGEBUNG" in result, "Expected GESETZGEBUNG header"
    assert elapsed < 15

    # ------------------------------------------------------------------
    # Test 3: Fetch specific Vorgang by ID (from research data)
    # ------------------------------------------------------------------
    print("TEST 3: vorgang_details — ID 332609 (Gewerbesteuergesetz)")
    t0 = time.time()
    result = await vorgang_details("332609")
    elapsed = time.time() - t0
    print(f"  Elapsed: {elapsed:.2f}s")
    ok3 = check("vorgang_details('332609')", result)
    assert "332609" in result, "Expected ID in result"
    assert "Gesetzgebung" in result or "Gewerbe" in result, "Expected content"
    assert elapsed < 15

    # ------------------------------------------------------------------
    # Test 4: Drucksachen — Digitalisierung, Kleine Anfrage, last 6 months
    # ------------------------------------------------------------------
    print("TEST 4: suche_drucksachen — 'Digitalisierung', Kleine Anfrage, ab 2025-09-01")
    t0 = time.time()
    result = await suche_drucksachen(
        suchbegriff="Digitalisierung",
        drucksachetyp="Kleine Anfrage",
        datum_von="2025-09-01",
        limit=5,
    )
    elapsed = time.time() - t0
    print(f"  Elapsed: {elapsed:.2f}s")
    ok4 = check("suche_drucksachen(Digitalisierung, Kleine Anfrage)", result)
    assert "DRUCKSACHEN" in result, "Expected DRUCKSACHEN header"
    assert elapsed < 15

    # ------------------------------------------------------------------
    # Test 5: drucksache_lesen — known doc with text (WP20)
    # ------------------------------------------------------------------
    print("TEST 5: drucksache_lesen — ID 279131 (known large text)")
    t0 = time.time()
    result = await drucksache_lesen("279131", volltext=False)
    elapsed = time.time() - t0
    print(f"  Elapsed: {elapsed:.2f}s")
    ok5 = check("drucksache_lesen(279131, preview)", result)
    assert "279131" in result or "DRUCKSACHE" in result
    assert elapsed < 15

    # ------------------------------------------------------------------
    # Test 6: drucksache_lesen volltext — context safety
    # ------------------------------------------------------------------
    print("TEST 6: drucksache_lesen — volltext=True — context cap check")
    t0 = time.time()
    result = await drucksache_lesen("279131", volltext=True)
    elapsed = time.time() - t0
    print(f"  Elapsed: {elapsed:.2f}s")
    ok6 = check("drucksache_lesen(279131, volltext=True)", result, max_chars=25_000)
    assert len(result) <= 25_000, f"CONTEXT SAFETY VIOLATION: {len(result):,} chars"
    assert elapsed < 15

    # ------------------------------------------------------------------
    # Test 7: Plenarprotokolle — last 14 days
    # ------------------------------------------------------------------
    print("TEST 7: plenarprotokolle — letzte 14 Tage")
    t0 = time.time()
    result = await plenarprotokolle(limit=5)
    elapsed = time.time() - t0
    print(f"  Elapsed: {elapsed:.2f}s")
    ok7 = check("plenarprotokolle(14 Tage)", result)
    assert "PLENARSITZUNGEN" in result
    assert elapsed < 15

    # ------------------------------------------------------------------
    # Test 8: Anfragen on topic — realistic policy worker query
    # ------------------------------------------------------------------
    print("TEST 8: suche_drucksachen — 'Wohnungsbau', Antwort, WP21")
    t0 = time.time()
    result = await suche_drucksachen(
        suchbegriff="Wohnungsbau",
        drucksachetyp="Antwort",
        wahlperiode=21,
        limit=5,
    )
    elapsed = time.time() - t0
    print(f"  Elapsed: {elapsed:.2f}s")
    ok8 = check("suche_drucksachen(Wohnungsbau, Antwort, WP21)", result)
    assert elapsed < 15

    # ------------------------------------------------------------------
    # Test 9: Gesetzgebung with beratungsstand filter
    # ------------------------------------------------------------------
    print("TEST 9: suche_vorgaenge — Gesetzgebung, beratungsstand=Überwiesen")
    t0 = time.time()
    result = await suche_vorgaenge(
        vorgangstyp="Gesetzgebung",
        beratungsstand="Überwiesen",
        datum_von="2025-10-01",
        limit=5,
    )
    elapsed = time.time() - t0
    print(f"  Elapsed: {elapsed:.2f}s")
    ok9 = check("suche_vorgaenge(Gesetzgebung, Überwiesen)", result)
    assert "Überwiesen" in result or "VORGÄNGE" in result
    assert elapsed < 15

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("=" * 70)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"ERGEBNIS: {passed}/{total} Tests bestanden")
    print()
    for name, ok, chars in results:
        symbol = PASS if ok else FAIL
        print(f"  {symbol} {name}  ({chars:,} chars)")
    print()

    if passed == total:
        print("✓ Alle Tests bestanden — Server bereit für Deployment.")
    else:
        failed = [name for name, ok, _ in results if not ok]
        print(f"✗ Fehlgeschlagene Tests: {failed}")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(run_tests())
