# Burza – AI obchodní agent pro eToro

Agent sám otevírá a zavírá pozice na eToro podle technické strategie (Studie A):
VWAP pullback nebo opening range breakout na 1/5/15min svíčkách. Každý obchod prochází
společným risk enginem. Agent umí tři režimy:

| Režim | Co dělá | Peníze |
|---|---|---|
| `paper` | vlastní simulace, data z eToro | žádné |
| `demo` | obchoduje na virtuálním účtu eToro | virtuální |
| `real` | obchoduje přes klíč **Agent Portfolia** | **skutečné** |

> ⚠️ Automatické obchodování může vést k významným ztrátám. Limity nejsou zárukou, že se
> ztráta nepřekročí (gapy, výpadky, skluz). Začni v `demo` a do `real` přecházej až po
> backtestu a aspoň několika týdnech dema.

## Ochranné limity (výchozí, viz `config.toml`)

- riziko jednoho obchodu max. **1 %** počátečního kapitálu (velikost pozice = riziko / vzdálenost stop-lossu)
- denní ztráta **3 %** → žádné nové obchody do dalšího obchodního dne (a zavření pozic)
- celkový propad **15 %** → vše se zavře a agent stojí, dokud ho ručně neschválíš (`reset-halt`)
- obchod se odmítne i tehdy, kdyby stop-loss všech otevřených pozic + nové pozice mohl překročit denní limit
- žádné přikupování do ztráty, max. 1 pozice na symbol, max. 3 pozice, max. 25 % equity na pozici
- jen long bez páky, každá pozice má stop-loss i take-profit přímo na eToro
- nic se nedrží přes noc: 10 min před zavřením trhu se vše zavře
- zastaralá data (> 15 min) = neobchoduje se
- **kill switch**: `python -m burza kill` → agent zavře své pozice a skončí
- každý signál (i odmítnutý), objednávka, obchod a equity se zapisuje do `data/burza.db`

## 1. Klíče – NIKDY je nevkládej do chatu

1. **Pro demo:** na <https://api-portal.etoro.com> v *API Key Management* vytvoř klíč
   **jen s demo oprávněními** (`trade.demo:read`, `trade.demo:write`) + `market-data:read`.
   Dostaneš *API key* (aplikace) a *User key* (zobrazí se jen jednou).
2. **Pro Agent Portfolio (real):** v eToro klikni na *Agentní portfolia → Začněte*. Zvol
   název (6–10 znaků), kolik peněz má portfolio kopírovat, a oprávnění
   `trade.real:read` + `trade.real:write`. Doporučuji nastavit i **IP whitelist** (IP serveru)
   a **datum expirace**. Vygenerovaný klíč je *User key* agenta. Agent pak obchoduje
   jen v rámci tohoto portfolia, ne na celém tvém účtu.
3. Na serveru vytvoř soubor `.env` (vzor `.env.example`, práva `chmod 600 .env`):

```
ETORO_API_KEY=...
ETORO_USER_KEY=...
```

## 2. Instalace (server/VPS nebo stále zapnutý počítač, Python 3.11+)

```bash
git clone <repo> /opt/burza-agent && cd /opt/burza-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml        # mode = "demo"
nano .env                                  # klíče
.venv/bin/python -m burza check            # ověří klíče, oprávnění, instrumenty, data
```

## 3. Backtest před spuštěním

```bash
.venv/bin/python -m burza fetch --days 120             # stáhne 5min historii z eToro
.venv/bin/python -m burza backtest --walk-forward      # 70 % dnů vývoj / 30 % nezávislý test
.venv/bin/python -m burza backtest --strategy orb --walk-forward
```

Výsledky se ukládají do `data/backtests.jsonl` (pouze přidávání, nic se nepřepisuje).
Pokud strategie v **testovací** části nevydělává po nákladech, nespouštěj ji na real.

## 4. Akceptace a spuštění (demo)

```bash
.venv/bin/python -m burza test-order --symbol SPY   # během obchodních hodin: otevře a zavře 20 USD
.venv/bin/python -m burza run --once                 # jeden cyklus na zkoušku
sudo cp deploy/burza.service /etc/systemd/system/ && sudo systemctl enable --now burza
```

Průběžně:

```bash
python -m burza status           # běží agent? stav limitů, otevřené pozice
python -m burza report --days 1  # denní report (týdenní: --days 7), cron viz deploy/crontab.example
python -m burza kill [--now]     # nouzové zastavení;  python -m burza resume  zruší
python -m burza reset-halt       # ruční schválení po celkovém limitu 15 %
```

## 5. Přechod na Agent Portfolio (skutečné peníze)

V `config.toml` nastav `mode = "real"` a `real_trading_confirmed = true`, do `.env` dej klíč
Agent Portfolia a navíc `BURZA_I_ACCEPT_REAL_MONEY_RISK=ano`. Bez všech tří podmínek
agent v režimu real nespustí.

## Architektura

```
eToro API (svíčky) → LiveData (jen uzavřené svíčky, kontrola stáří)
  → Strategy (vwap_pullback | orb) → RiskEngine (sizing, denní/celkový limit, kill switch)
  → Broker (Paper | eToro demo | eToro real; idempotentní x-request-id, dohledání po pádu)
  → Journal (SQLite: events, signals, orders, trades, equity) → report / status
```

## Co zatím chybí (další etapy)

- **Studie B** (zprávy, makro a sentiment přes LLM) – připravené rozhraní `Strategy`, zdroje dat zatím nejsou
- webový dashboard (záložky Studie A / B / Porovnání, benchmark S&P 500)
- přesné P/L uzavřených obchodů z historie eToro (teď odhad z poslední ceny; equity je přesná z účtu)

## Testy

```bash
PYTHONPATH=.:tests python -m unittest discover -s tests
```
