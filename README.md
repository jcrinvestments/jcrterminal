# JCR Investments Terminal

Bloomberg-stijl terminal applicatie voor JCR Investments (VOF, 3 partners, 20+ jaar horizon).

## Features

- **Live portfolio dashboard** via IBKR MCP — posities, P&L, gewichten, bucket allocatie
- **Macro dashboard** — Fed/ECB rentes, CPI, GDP, yields, VIX, gold, crude
- **Live nieuws feed** — automatisch verversend, prioriteit op basis van portfolio
- **DCA suggesties** — berekent dips + onderwogen posities, minimaliseert kosten
- **Risk flags** — concentratie, bucket drift, FX exposure
- **Week overview** — meeting prep met winnaars/verliezers en actielijs

## Installatie

```bash
# 1. Clone / ga naar de projectmap
cd jcrterminal

# 2. Maak een virtual environment
python3 -m venv venv
source venv/bin/activate       # Linux/Mac
# venv\Scripts\activate        # Windows

# 3. Installeer dependencies
pip install -r requirements.txt

# 4. Configureer .env
cp .env.example .env
# Bewerk .env met je instellingen

# 5. Start de terminal
python main.py
```

## Keys

| Key | Actie |
|-----|-------|
| `1` / `F1` | DCA Suggesties |
| `2` / `F2` | Week Overzicht / Meeting Prep |
| `3` / `F3` | Risk Flags |
| `4` / `F4` | Macro Nieuws Detail |
| `q` | Afsluiten |

## Architectuur

```
main.py      — Hoofdloop, Rich UI, keybindings, layout
ibkr.py      — IBKR MCP calls (posities, prijzen, account)
market.py    — Live marktdata via web search (FX, VIX, gold)
macro.py     — Macro-economische indicatoren
news.py      — Nieuws feed met tagging en sentiment
dca.py       — DCA suggestie engine, risk flags, week overview
config.py    — Risk buckets, drempelwaarden, instellingen
```

## Risk Bucket Framework

| Bucket | Target | Range | Criteria |
|--------|--------|-------|----------|
| Hedge | 2.5% | 0–5% | GLD, SLV, crypto-hedge; beta < 0.3 |
| Low Risk | 27.5% | 25–30% | Beta < 0.8, P/E 10–20, ICR > 8x |
| Medium Risk | 42.5% | 40–45% | D/E < 1.5, ROE 10–20%, beta ~1.0 |
| High Risk | 22.5% | 20–25% | Beta > 1.5, negatieve earnings OK |
| Cash/DCA | 5% | 3–8% | Vrij kapitaal voor DCA rotatie |

Drift alert wordt getriggerd bij > 2% afwijking van target.

## MCP Integratie

De terminal werkt in twee modi:

1. **Live (Claude Code omgeving)** — IBKR MCP en web search zijn automatisch beschikbaar
2. **Demo mode (standalone)** — Realistische demo data zodat de UI altijd werkt

## Logging

Alle errors worden gelogd naar `jcr_terminal.log` zonder de UI te crashen.

## DCA Logica

1. Identificeer posities die ≥ 10% gedaald zijn in 2 weken
2. Vergelijk met bucket target gewichten
3. Sorteer op prioriteit: grootste daling + meeste onderwogen
4. Minimale ordergrootte: €200 (commissie < 0.5%)
5. Rotatieschema A/B/C voor gespreide inkoop
