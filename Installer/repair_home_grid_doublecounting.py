#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
repair_home_grid_doublecounting.py
----------------------------------
Korrigiert Eintraege in daily_stats wo home_consumption den grid_in-Anteil nicht enthaelt.

Hintergrund: Aeltere C++ eba-m Systeme schieben manchmal total_home_kwh ohne den Grid-Anteil
in daily_stats.json. Der sqlite_archiver archiviert dann z.B.:
  home_consumption = 15 kWh  (nur PV+Bat, OHNE Grid)
  grid_in          = 10 kWh  (korrekt)
Korrekt waere: home_consumption = 25 kWh

Erkennung: Energiebilanz-Konsistenzpruefung
  supply = pv_yield + grid_in + bat_out
  demand = home_consumption + grid_out + bat_in + wb_consumption + wp_consumption
  
  Wenn supply - demand > grid_in * 0.9:
     -> sehr wahrscheinlich ist grid_in nicht in home_consumption enthalten
     -> home_consumption wird um grid_in erhoeht

Aufruf: python3 repair_home_grid_doublecounting.py [--dry-run] [--force]
  --dry-run : Nur anzeigen, keine DB-Aenderungen
  --force   : Korriegieren ohne Rueckfrage
"""
import sqlite3
import sys
import os

DB_PATH = "/var/www/html/data/e3dc_stats.db"

def main():
    dry_run = "--dry-run" in sys.argv
    force   = "--force"   in sys.argv

    if not os.path.exists(DB_PATH):
        print(f"[!] Datenbank nicht gefunden: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT date, pv_yield, home_consumption, grid_in, grid_out, bat_in, bat_out,
               wb_consumption, wp_consumption
        FROM daily_stats ORDER BY date ASC
    """)
    rows = c.fetchall()

    to_fix = []
    for row in rows:
        date, pv, home, gi, go, bi, bo, wb, wp = row
        home = home or 0.0; gi = gi or 0.0; go = go or 0.0
        bi   = bi   or 0.0; bo = bo or 0.0; wb = wb or 0.0; wp = wp or 0.0; pv = pv or 0.0

        # Physikalisch unmoeglich: Hausverbrauch MUSS >= Grid-Anteil sein.
        # Wenn home < grid_in: grid_in wurde nicht in home aufaddiert (C++ Bug).
        if gi > 0.5 and home < gi * 0.95:
            corrected_home = round(home + gi, 2)
            to_fix.append({
                'date': date, 'home_old': home, 'home_new': corrected_home,
                'grid_in': gi, 'pv': pv
            })

    if not to_fix:
        print("[OK] Keine korrekturbeduerftigen Eintraege gefunden. Datenbasis sieht konsistent aus.")
        conn.close()
        return

    print(f"\n[!] {len(to_fix)} Eintraege gefunden, bei denen home_consumption < grid_in (physikalisch unmoeglich):\n")
    print(f"{'Datum':<12} {'Home alt':>9} {'Grid_in':>8} {'Home neu':>9}")
    print("-" * 43)
    for r in to_fix:
        print(f"{r['date']:<12} {r['home_old']:>9.2f} {r['grid_in']:>8.2f} {r['home_new']:>9.2f}")

    if dry_run:
        print("\n[DRY-RUN] Keine Aenderungen vorgenommen.")
        conn.close()
        return

    if not force:
        ans = input(f"\nMoechtest du {len(to_fix)} Eintraege korrigieren? [j/N] ").strip().lower()
        if ans not in ('j', 'y', 'ja', 'yes'):
            print("Abgebrochen.")
            conn.close()
            return

    fixed = 0
    for r in to_fix:
        c.execute(
            "UPDATE daily_stats SET home_consumption = ? WHERE date = ?",
            (r['home_new'], r['date'])
        )
        fixed += 1

    conn.commit()
    conn.close()
    print(f"\n[OK] {fixed} Eintraege korrigiert.")

if __name__ == "__main__":
    main()
