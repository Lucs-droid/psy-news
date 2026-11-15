# Psihologie & Oameni — dashboard zilnic de știri

Dashboard care adună automat, la fiecare 2 ore, știri și studii din psihologie, sănătate mintală,
wellbeing, People & Culture, leadership și România.

- `sources.json` — lista de surse (adaugi sau ștergi surse aici)
- `build.py` — scriptul care adună știrile și scrie `data.json`
- `index.html` — pagina dashboard
- `.github/workflows/update.yml` — rularea automată

Rezumatul zilnic generat de AI apare doar dacă există secretul `ANTHROPIC_API_KEY`
(Settings → Secrets and variables → Actions).
