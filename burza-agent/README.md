# Burza – AI obchodní agent pro eToro

Dvě souběžné studie, každá s virtuálními **200 000 Kč** přepočtenými stejným kurzem ke dni startu:

| | Studie A – technická | Studie B – AI + zprávy |
|---|---|---|
| Rozhoduje podle | cena a objem (VWAP pullback / opening range breakout) | stejný technický signál, který navíc posoudí **Claude** podle zpráv, výsledků firem, makrodat, Fedu a tržního přehledu |
| Zdroje | eToro svíčky 1/5/15 min | + Finnhub (zprávy, kalendář výsledků), FRED (Fed funds, inflace, nezaměstnanost, zaměstnanost, výnosy, USD, ropa, VIX), kalendář FOMC |
| Evidence | signály, odmítnuté signály, obchody | + každá zpráva (zdroj, vydavatel, čas publikace) a každé rozhodnutí AI se zdůvodněním |

Agent běží nepřetržitě: každých 30 s načte uzavřené svíčky, vyhodnotí signál, projde risk enginem a obchod
otevře nebo zavře sám, bez potvrzování. Výsledky ukazuje **dashboard** se záložkami *Studie A*, *Studie B* a *Porovnání*
(společný graf výnosu vs. S&P 500, metodika, kontrola srovnatelnosti).

| Režim | Co dělá | Peníze |
|---|---|---|
| `paper` (výchozí) | vlastní simulace s náklady, data z eToro | žádné |
| `demo` | obchoduje na virtuálním účtu eToro | virtuální |
| `real` | obchoduje přes klíč **Agent Portfolia** | **skutečné** |

> ⚠️ Automatické obchodování může vést k významným ztrátám. Limity nejsou zárukou, že se ztráta
> nepřekročí (gapy, výpadky, skluz). Výsledek je experiment, ne slib zisku.

---

## Rychlý start – Windows

1. Nainstaluj **Python 3.12** z <https://www.python.org/downloads/> a v instalátoru zaškrtni **„Add python.exe to PATH“**.
2. Stáhni kód: v PowerShellu

   ```powershell
   cd $HOME
   git clone -b claude/amazing-tesla-45tb88 https://github.com/Rosomak9100/inoERP.git
   cd inoERP\burza-agent
   ```

   (Bez gitu: stáhni ZIP větve `claude/amazing-tesla-45tb88` z GitHubu, rozbal a otevři v PowerShellu složku `burza-agent`.)
3. Instalace (vytvoří prostředí, configy a otevře `.env` v Poznámkovém bloku – vyplň klíče, ulož, zavři):

   ```powershell
   powershell -ExecutionPolicy Bypass -File windows\instalace.ps1
   ```

4. Spuštění obou studií + dashboardu (otevře se prohlížeč na <http://127.0.0.1:8080>):

   ```powershell
   powershell -ExecutionPolicy Bypass -File windows\spust.ps1
   ```

Počítač musí během obchodních hodin USA (**15:30–22:00 SEČ**) běžet a nesmí usnout (skript vypne uspávání
na síťovém napájení). Pro provoz bez zapnutého PC použij server (Linux níže).

Ruční příkazy ve Windows: místo `python` piš `.\.venv\Scripts\python`, např.
`.\.venv\Scripts\python -m burza --config config_b.toml status`.

## Rychlý start – Linux / server

```bash
git clone -b claude/amazing-tesla-45tb88 https://github.com/Rosomak9100/inoERP.git /opt/burza && cd /opt/burza/burza-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config_a.example.toml config_a.toml && cp config_b.example.toml config_b.toml
cp .env.example .env && nano .env && chmod 600 .env
.venv/bin/python -m burza --config config_a.toml check
.venv/bin/python -m burza init-start --configs config_a.toml config_b.toml
```

Služby systemd: `deploy/burza.service` (pro Studii B zkopíruj a změň `--config config_b.toml`).

## Klíče (soubor `.env`) – nikdy je neposílej do chatu

| Proměnná | Kde ji vzít | Potřeba pro |
|---|---|---|
| `ETORO_API_KEY`, `ETORO_USER_KEY` | <https://api-portal.etoro.com> → API Key Management (pro paper/demo stačí oprávnění `market-data:read`, `trade.demo:*`) | obě studie |
| `FINNHUB_API_KEY` | <https://finnhub.io> (zdarma) | Studie B – zprávy, výsledky firem |
| `FRED_API_KEY` | <https://fred.stlouisfed.org/docs/api/api_key.html> (zdarma) | Studie B – makrodata |
| `ANTHROPIC_API_KEY` | <https://console.anthropic.com> | Studie B – posouzení přes Claude |

Studie B volá Claude jen u technického kandidáta, max. `max_calls_per_day` (výchozí 40) za den.
Když AI nebo zdroje nejsou dostupné, obchod se **neotevře** (fail closed) a důvod se zapíše.

## Ochranné limity (pro každou studii zvlášť)

- riziko obchodu max. **1 %** počátečního kapitálu; Studie B smí velikost jen snížit (×0,5), nikdy zvýšit
- denní ztráta **3 %** → žádné nové obchody do dalšího obchodního dne (a zavření pozic)
- celkový propad **15 %** → vše se zavře a studie stojí do ručního schválení (`reset-halt`)
- obchod se odmítne, kdyby stop-lossy všech pozic + nové mohly překročit denní limit
- žádné přikupování do ztráty, max. 1 pozice na symbol, max. 3 pozice, max. 25 % equity na pozici
- Studie B: nevstupuje v den výsledků firmy ani den před výsledky před otevřením
- nic přes noc (10 min před koncem seance se vše zavře), zastaralá data = neobchoduje se
- **kill switch**: `python -m burza kill` (soubor `STOP` zastaví obě studie), `resume` zruší

## Přesné P/L

U demo/real se P/L uzavřených obchodů bere z **historie obchodů eToro** (`netProfit`, `closeRate`, poplatky).
Pokud tam obchod ještě není, zapíše se po 15 min odhad z poslední ceny a jednou za hodinu se automaticky
nahradí přesným číslem (změna se zapíše do událostí). V paper režimu je P/L přesný podle simulace
včetně spreadu, skluzu a poplatků.

## Příkazy

```
python -m burza --config config_a.toml check        # klíče, oprávnění, instrumenty, data
python -m burza init-start --configs config_a.toml config_b.toml   # společný kurz USD/CZK + datum startu
python -m burza --config config_a.toml fetch --days 120             # historie pro backtest
python -m burza --config config_a.toml backtest --walk-forward      # 70 % vývoj / 30 % nezávislý test
python -m burza --config config_a.toml run          # agent (nepřetržitě); --once = jeden cyklus
python -m burza dashboard --studies config_a.toml config_b.toml    # http://127.0.0.1:8080
python -m burza --config config_b.toml status | report --days 1 | report --days 7
python -m burza kill [--now]  /  resume  /  reset-halt
python -m burza --config config_a.toml test-order   # (jen mode=demo) testovací objednávka 20 USD
```

Studii B nejde poctivě backtestovat bez historického archivu zpráv (look-ahead), backtest proto testuje
jen její technický základ. Výsledky backtestů se jen přidávají do `data_*/backtests.jsonl`.

## Přechod na Agent Portfolio (skutečné peníze)

Až po backtestu a týdnech paper/demo provozu. V eToro *Agentní portfolia → Začněte*: název, částka,
oprávnění `trade.real:read` + `trade.real:write`, IP whitelist, expirace. V configu `mode = "real"` a
`real_trading_confirmed = true`, do `.env` klíč portfolia a `BURZA_I_ACCEPT_REAL_MONEY_RISK=ano`.

## Architektura

```
eToro svíčky → LiveData (jen uzavřené, kontrola stáří)
  → Studie A: Strategy        → RiskEngine → Broker (Paper | eToro demo | real) → Journal (SQLite)
  → Studie B: Strategy → ContextProvider (Finnhub, FRED, FOMC, trh) → ClaudeJudge (JSON schema) ↗
Journal (obě studie) → dashboard (záložky A / B / Porovnání) + report
```

## Testy

```bash
PYTHONPATH=.:tests python -m unittest discover -s tests
```
