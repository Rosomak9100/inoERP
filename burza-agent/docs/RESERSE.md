# Rešerše: intradenní strategie, zprávy a ověřování

Verze 1 – 25. 10. 2026. Podklad pro etapu 1 (rešerše) a 2 (backtest) ze zadání.
Čísla z článků jsou **výsledky uváděné autory**; nezávislá ověření bývají horší a jsou uvedena u každé položky.

---

## 1. Nejdřív realita

- Studie brazilských day traderů (Chague, De-Losso & Giovannetti, *Day Trading for a Living?*, 2019/2020):
  z lidí, kteří vydrželi obchodovat aspoň 300 dní, **97 % prodělalo** a jen zlomek procenta vydělal víc
  než bankovní úředník. Autoři nenašli žádné „učení se“ v čase.
- Hrubá výhoda (edge) intradenních strategií bývá malá a **náklady (spread, skluz, poplatky) ji často celou sežerou**
  – viz nezávislé ověření ORB níže.
- Z toho plynou pravidla, která agent už dodržuje: malé riziko na obchod (1 %), tvrdé limity ztrát,
  započtené náklady, žádné přikupování do ztráty, test na datech, na kterých se nic neladilo.

„Opravdový master broker“ se tedy nepozná podle počtu obchodů, ale podle toho, že **obchoduje jen to,
co prošlo nezávislým testem, a přežije špatná období**.

## 2. Strategie (Studie A – jen cena a objem)

| Strategie | Myšlenka | Důkazy | Stav v agentovi |
|---|---|---|---|
| **Pásmo šumu – intradenní momentum** (`noise_momentum`) | Když cena v kontrolním čase (každých 30 min) vyjede nad „pásmo šumu“ (běžný pohyb od otevření v tutéž denní dobu za 14 dní) a je nad VWAP, jde se s trendem; výstup při návratu pod pásmo/VWAP, jinak na konci dne. | Zarattini, Aziz & Barbon (2024), SPY 2007–2024: autoři uvádějí 19,6 % ročně a Sharpe 1,33 po nákladech, long i short. Nezávislá replikace (TradingView) dala nižší Sharpe. | **Nově implementováno** (zatím jen long) – čeká na backtest na reálných datech |
| **Opening range breakout** (`orb`) | Průraz rozpětí prvních minut dne ve směru průrazu. | Zarattini & Aziz (2023, QQQ, 5min ORB) uvádějí výrazný zisk s pákou. Nezávislá replikace na 5 indexech (2015–2026): hrubý výsledek zopakován, **po nákladech ≈ 0**, 4 z 5 trhů záporné. | Implementováno (15min rozpětí) – skepse |
| **VWAP pullback** (`vwap_pullback`) | Trend nad VWAP, návrat k EMA/VWAP a obnovení růstu. | Praktická strategie, bez silné akademické opory. | Implementováno – rozhodne backtest |
| **Intradenní momentum poslední půlhodiny** (`intraday_momentum`) | Výnos od včerejšího close do 10:00 ET předpovídá výnos poslední půlhodiny; long v 15:30 ET po kladném ránu. | Gao, Han, Li & Zhou (2018, *Journal of Financial Economics*), SPY 1993–2013 + 10 dalších ETF; silnější ve volatilních dnech, při vysokém objemu a **ve dnech makro zpráv**. Vysvětlená variabilita je malá (R² ≈ 2 %). | **Implementováno** (jen long; agent zavírá 10 min před koncem, drží tedy 20 min) |
| Mean reversion (Bollinger, RSI extrémy) | Návrat k průměru po přestřelení. | Krátkodobý reversal je známý spíš na denním/týdenním horizontu; intradenně u likvidních ETF slabý po nákladech. | Neimplementováno |

**Omezení:** agent je zatím jen long. Většina citovaných studií obchoduje oběma směry, takže naše výsledky
nebudou přímo srovnatelné. Short přes CFD je možné doplnit (vyžaduje stop-loss povinně).

### Výsledky prvního backtestu (120 dní reálných dat, 25. 10. 2026)

Žádná ze tří tehdejších strategií neprošla – v testovací části všechny prodělaly
(vwap_pullback −2,5 %, PF 0,51; orb −0,3 %; noise_momentum −0,3 %). Vzorek byl ale malý (15–141 obchodů).
Další test: 1 rok dat, 4 strategie, 6 instrumentů vč. komodit, ukazatel t-stat.
Opraveno: backtest při denním limitu zavíral pozice cenou jiného symbolu; živý agent nově
používá jen svíčky řádné seance (stejně jako backtest).

### Výsledky ročního backtestu a skutečné náklady eToro (25. 10. 2026)

Rok dat, 6 instrumentů, původní odhad nákladů 2 bps spread + 1 bps skluz: žádná strategie neprošla.
Bez nákladů: vwap_pullback −0,56 % (žádná výhoda), orb +1,37 %, noise_momentum +1,45 %,
intraday_momentum +0,49 % – malá hrubá výhoda, kterou náklady sežerou.

Skutečné náklady eToro (what-if API, nákup za 2000 USD, 23. 10. 2026):

| Instrument | Poplatek za obchod | Spread |
|---|---|---|
| SPY, QQQ, GLD, USO (ETF jako CFD) | 3 USD | 0,05–0,24 USD (0,25–1,2 bps) |
| AAPL, NVDA (skutečné akcie) | 1 USD | 0,17–0,19 USD (~0,9 bps) |

**Pevný poplatek je hlavní náklad.** Při pozici ~2000 USD a poplatku i při prodeji stojí obchod s ETF
tam a zpět ~0,3 %, s akcií ~0,1 % – víc, než intradenní strategie průměrně vydělají na obchod.
Poplatek je pevný, takže jeho váha klesá s velikostí pozice a s počtem obchodů.
Důsledek: s kapitálem 200 000 Kč mají šanci jen strategie s **méně obchody a většími pohyby**
(držení hodin až dní). Poplatek za držení přes noc je u pozic bez páky podle API nulový.
Kandidáti (vyžadují schválení změny metodiky – zadání počítá primárně s intradenním obchodováním):
trendové / momentum strategie na denních svíčkách pro ETF (SPY, QQQ, GLD, USO), např. filtr 200denního
průměru nebo 12měsíční momentum (Moskowitz, Ooi & Pedersen 2012, *Time series momentum*).

### Rozhodnutí 26. 10. 2026: méně obchodů, delší držení (swing)

Schváleno uživatelem po zjištění, že poplatky eToro převyšují hrubou výhodu intradenních strategií.
Nový režim `horizon = "swing"`: rozhodnutí jednou denně 15 min před koncem seance (jako obchod na close),
pozice se drží přes noc (u pozic bez páky bez poplatku za noc), stop-loss na eToro je jen pojistka (2–3 ATR).

| Strategie | Pravidlo | Zdroj |
|---|---|---|
| `trend_sma200` | drž, když je close > SMA200 +1 %, prodej pod SMA200 −1 % | Faber (2007), *A Quantitative Approach to Tactical Asset Allocation* |
| `momentum_12m` | kontrola 1× měsíčně: drž, když je 12měsíční výnos kladný | Moskowitz, Ooi & Pedersen (2012), *Time series momentum* |
| `donchian_55_20` | nákup na novém 55denním maximu, prodej pod 20denním minimem | „Turtle“ pravidla, trend following |
| `rsi2_pullback` | v trendu nad SMA200 nákup při RSI(2) < 10, prodej nad SMA5 (max 10 dní) | L. Connors, krátkodobý návrat k průměru |

Backtest: 8 let denních dat z eToro, 70 % vývoj / 30 % test, skutečné poplatky, srovnání s „kup a drž“.
Universe: ETF (CFD, 3 USD) + velké akcie (skutečné, 1 USD): AAPL, NVDA, MSFT, AMZN, GOOGL, META, JPM, XOM.
Pozor na **survivorship bias** – dnešní velké firmy jsou vybrané zpětně podle úspěchu, minulé výnosy
strategií na nich jsou proto nadhodnocené. Hlavní měřítko je poměr výnosu a rizika proti „kup a drž“.

### Oprava: splity akcií (26. 10. 2026)

První swing backtest ukázal ve vývojovém období (2018–2024) nesmyslné ztráty trendových strategií.
Příčina: historie z eToro **není přepočtená na splity** (AMZN 3. 6. 2022 close 2 443,80 → po splitu
20:1 open 125,60). Backtest to bral jako pád o 95 % → stop-loss a obří „ztráta“ (AAPL 2020, NVDA 2021
a 2024, AMZN a GOOGL 2022). Oprava: modul `splits.py` rozpozná skok odpovídající běžnému poměru splitu
(±5 %) a přepočte starší ceny; simulace přepočte i otevřenou pozici, pokud split přijde během držení.
Výsledky swing backtestu před opravou jsou neplatné.

### Opravené výsledky swing (8 let, 26. 10. 2026) a volba strategie

| Strategie | Vývoj 2018–2024 | Test 2024–2026 | Obchodů (test) | PF (test) | Max DD (test) | Sharpe (test) |
|---|---|---|---|---|---|---|
| trend_sma200 | +50,1 % | +20,3 % | 9 | 4,86 | 10,7 % | 0,94 |
| momentum_12m | +48,4 % | +14,4 % | 3 | 6,23 | 11,6 % | 0,63 |
| donchian_55_20 | +30,8 % | +16,6 % | 27 | 2,47 | 8,7 % | 0,76 |
| rsi2_pullback | +18,0 % | +9,5 % | 179 | 1,45 | 3,8 % | 0,87 |
| kup a drž (12 instrumentů) | +257 % | +47,5 % | – | – | 19,4 % | 1,10 |

Všechny strategie byly kladné v obou obdobích; „kup a drž“ vybraných (zpětně úspěšných) firem měl vyšší
výnos i Sharpe – srovnání je ovlivněné survivorship bias a tím, že strategie drží max. 3 pozice.
t-stat (dny) < 2 → statisticky zatím neprokázané.
**Volba (schválena uživatelem): obě studie `rsi2_pullback` ve swing režimu** – nejvíc obchodů (≈75/rok),
takže srovnání Studie A (technika) a B (technika + zprávy) bude mít dost dat; nejmenší propad.
Dashboard i backtest vždy ukazují i „kup a drž“ S&P 500 (SPY) a Nasdaq 100 (QQQ) za stejné období.

### Komodity

Agent obchoduje komodity přes ETF na americké burze – **GLD** (zlato) a **USO** (ropa WTI). Mají stejné
obchodní hodiny, nízký spread (~0,02 USD) a eToro je umožňuje obchodovat bez páky. Pozor: USO drží futures
kontrakty, při rolování se může dlouhodobě odchylovat od ceny ropy – pro intradenní obchody to nevadí.
Studie B u nich má méně firemních zpráv; důležitější je makro (USD, ropa WTI, VIX z FRED).

## 3. Zprávy a makro (Studie B)

| Poznatek | Zdroj | Jak to agent používá |
|---|---|---|
| Pesimismus v médiích tlačí ceny dolů, ale **následuje návrat** – zpráva je často krátkodobý šok, ne trvalá informace. | Tetlock (2007), *Journal of Finance* | Jedna zpráva nikdy není pokyn; silný pohyb + staré pozitivní zprávy = „už v ceně“ → poloviční velikost |
| Akcie rostly ve **24 h před rozhodnutím Fedu** (pre-FOMC drift), průměr kolem 0,5 % na S&P 500 ve vzorku 1994–2011. Novější data naznačují oslabení – ověřit. | Lucca & Moench (2015), *Journal of Finance* | Den Fedu = zvýšené riziko → poloviční velikost; kandidát na úpravu (plná velikost před 14:00 ET) |
| Intradenní momentum je silnější ve dnech makro zpráv. | Gao a kol. (2018) | Podklad pro budoucí kombinaci se Studií A |
| Výsledky firem způsobují gapy, které stop-loss nezastaví. | praxe, PEAD literatura | Blokace vstupu v den výsledků a den před ranními výsledky |
| Vysoká volatilita (VIX) zvětšuje skluzy a falešné signály. | praxe | VIX ≥ 25 poloviční velikost, ≥ 35 neobchodovat |

## 4. Jak poznat, že strategie opravdu funguje

- **Přeoptimalizace (overfitting):** když zkusíš dost variant, nějaká vyjde skvěle náhodou. Bailey, Borwein,
  López de Prado & Zhu – *Probability of Backtest Overfitting* (2013/2017) – ukazují, jak pravděpodobnost
  přeoptimalizace změřit; Bailey & López de Prado – *Deflated Sharpe Ratio* (2014) – jak Sharpe opravit
  o počet vyzkoušených variant.
- Co z toho agent dělá: walk-forward rozdělení 70 % vývoj / 30 % nezávislý test, započtené náklady, jen
  uzavřené svíčky (žádný pohled do budoucnosti), výsledky každého běhu se jen přidávají do `backtests.jsonl`.
- **Pravidlo pro nasazení:** strategie musí mít kladný výnos a profit factor > 1,2 v **testovací** části,
  s rozumným počtem obchodů (≥ 30). Parametry se neladí podle testovací části.
- Co přidat později: počítat Deflated Sharpe podle počtu vyzkoušených strategií; test na více obdobích.

## 5. Odkud brát aktuální informace (vše zdarma)

| Co | Zdroj | Stav |
|---|---|---|
| Ceny, svíčky, objemy | eToro API | používá se |
| Zprávy k firmám, tržní zprávy, kalendář výsledků | Finnhub (free) | používá se |
| Makrodata (sazby, inflace, nezaměstnanost, zaměstnanost, výnosy, USD, ropa, VIX) | FRED | používá se |
| Termíny Fedu | federalreserve.gov – FOMC calendar | používá se (ručně vložené termíny 2026) |
| Termíny CPI, zaměstnanosti (Employment Situation) | bls.gov/schedule | plánováno |
| Oficiální oznámení firem (8-K) | SEC EDGAR | plánováno |
| HDP, PCE inflace | BEA | plánováno |
| Sentiment | VIX (FRED), CBOE put/call, AAII sentiment (týdně) | VIX používá se |

## 6. Doporučená literatura

- Ernest P. Chan – *Algorithmic Trading: Winning Strategies and Their Rationale* (praktické, momentum i mean reversion)
- David Aronson – *Evidence-Based Technical Analysis* (jak testovat technickou analýzu vědecky)
- Robert Carver – *Systematic Trading* (řízení rizika a velikosti pozic)
- Marcos López de Prado – *Advances in Financial Machine Learning* (backtesting, přeoptimalizace)
- Perry Kaufman – *Trading Systems and Methods* (encyklopedie systémů)

## 7. Další kroky

1. **Spustit `6_BACKTEST.bat`** – porovná všechny čtyři strategie na roce reálných dat z eToro.
2. Pro Studii A a jako základ Studie B zvolit strategii podle sloupce TEST (ne podle vývojové části).
3. Další výzkum a implementace: strategie poslední půlhodiny (Gao a kol.), short strana, kalendář BLS/EDGAR
   pro Studii B, filtr „stocks in play“ (vysoký relativní objem) pro ORB.
4. Po několika týdnech souběhu porovnat studie v dashboardu (stejný start, kurz, náklady).

## Zdroje

- Zarattini, Aziz, Barbon (2024): *Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY)* – [SFI](https://www.sfi.ch/de/publications/n-24-97-beat-the-market-an-effective-intraday-momentum-strategy-for-s-p500-etf-spy), [RePEc](https://ideas.repec.org/p/chf/rpseri/rp2497.html); souhrn [CXO Advisory](https://www.cxoadvisory.com/?p=45565); replikace [TradingView](https://kr.tradingview.com/script/gJeM3LZ5-Out-of-the-Noise-Intraday-Strategy-with-VWAP-YuL), pásma [Concretum Bands](https://kr.tradingview.com/script/CUpWCZhe-Concretum-Bands)
- Nezávislé ověření ORB: [MQL5 blog – „gross reproduced, net zero“](https://www.mql5.com/en/blogs/post/776235)
- Gao, Han, Li, Zhou (2018): *Market intraday momentum*, JFE – [WUSTL](https://profiles.wustl.edu/en/publications/market-intraday-momentum/), [Alpha Architect](https://alphaarchitect.com/2014/08/attention-prop-traders-the-first-half-hour-of-trading-predicts-the-last-half-hour/), [QuantConnect](https://www.quantconnect.com/learning/articles/investment-strategy-library/intraday-etf-momentum)
- Chague, De-Losso, Giovannetti: *Day Trading for a Living?* – [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3423101), [Quantpedia](https://quantpedia.com/retail-day-trading-is-an-uphill-battle/), [CXO Advisory](https://www.cxoadvisory.com/investing-expertise/day-trading-a-bust/)
- Lucca, Moench (2015): *The Pre-FOMC Announcement Drift* – [NY Fed](https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr512.pdf), [VoxEU](https://cepr.org/voxeu/columns/predictable-movements-asset-prices-around-fomc-meetings)
- Tetlock (2007): *Giving Content to Investor Sentiment* – [Columbia](https://www.columbia.edu/~pt2238/papers/Tetlock_Media_Sentiment_JF.pdf)
- Bailey, Borwein, López de Prado, Zhu: *The Probability of Backtest Overfitting* – [SSRN](https://papers.ssrn.com/abstract=2326253)
- BLS kalendář zveřejnění 2026 – [bls.gov/schedule/2026](https://www.bls.gov/schedule/2026/)
