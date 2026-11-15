# Projekt-Spezifikation: OrcaSlicer 2.5.0 Plugin für Anycubic Kobra S1 (Vanilla Klipper + ACE Pro)

## Zielumgebung & Runtime
- **Slicer:** OrcaSlicer 2.5.0 (Linux Flatpak) Embedded CPython Plugin API (`import orca`).
- **Abhängigkeiten:** Ausschließlich Python-Standardbibliothek (`urllib.request`, `urllib.error`, `json`, `re`, `math`, `os`) verwenden, damit das Plugin ohne externe Wheels oder `glibc`-Konflikte in der Flatpak-Runtime läuft.
- **Drucker-Backend:** Moonraker REST API (Standard-URL: `http://192.0.2.10:7125`, über Plugin-UI konfigurierbar).
- **Netzwerk-Berechtigungen:** Sauber über `orca.request_permissions(...)` für Moonraker HTTP-Requests anfordern.

## Verbindliche Referenzen in `./refs/`
- `./refs/cloud-plugin-ref/`: Exakte API-Struktur (`@orca.plugin`, Capability-Klassen, HTML-UI-Integration, Post-Processing-Hook) für OrcaSlicer 2.5.0 übernehmen.
- `./refs/ACEPRO/`: Exakte Klipper-Makros (`CUT_TIP`, `ACE_SET_PURGE_AMOUNT`, `ACE_FEED`, `ACE_RETRACT`, Dryer-Befehle) und Moonraker-Statusfelder aus `ace.py` / `ace.cfg` / `printer_KS1.cfg` auslesen. Keine Makronamen erfinden!
- `./refs/vanilla-klipper/`: Start- und Leveling-Makros (`G9111`, `G31`, `BED_MESH_CALIBRATE`) prüfen.
- `./refs/orca_flush_to_purgelength.py`: Exakte Berechnungslogik 1:1 übernehmen.

## Kritische G-Code- & Post-Processing-Logik
1. **Purge-Länge berechnen:** `flush_multiplier`, `flush_volumes_matrix` und `flush_volumes_vector` aus den G-Code-Kommentaren parsen und `PURGELENGTH` (mm) über die originalen **Slicer-Tool-Indizes** (`old_slicer_tool`, `new_slicer_tool`) für 1.75 mm Filament berechnen.
2. **Color Mapping anwenden:** Erst nach der Purge-Berechnung `T<n>` auf den physischen ACE-Slot (`T<mapped>`) umschreiben und `ACE_SET_PURGE_AMOUNT PURGELENGTH=<len>` direkt davor einfügen.
3. **Start-G-Code (`G9111`):** Den Parameter `tool=<n>` in `G9111` (z. B. `G9111 bedTemp=80 extruderTemp=250 tool=1`) ebenfalls auf den gemappten Start-Slot umschreiben.
4. **Bed Leveling & Cut Tip:**
   - Pre-Print-Toggle für Bed Leveling (`G31` aktiv lassen oder auskommentieren/überspringen).
   - Pre-/Post-Print-Option sowie Sofort-Trigger-Buttons in der UI für `CUT_TIP` und Bed Leveling.
