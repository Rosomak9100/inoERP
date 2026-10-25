# Návod pro Windows – krok za krokem

Nic nemusíš psát ani programovat. Po instalaci máš na **Ploše ikony** „Burza - SPUSTIT“,
„Burza - ZASTAVIT“, „Burza - PREHLED“, „Burza - AKTUALIZOVAT“ a „Burza - INSTALACE a klice“.
Stejné soubory jsou i ve složce `burza-agent` (např. `C:\Users\<tvé jméno>\inoERP\burza-agent`).

| Soubor | Co udělá |
|---|---|
| `1_INSTALACE.bat` | nainstaluje vše potřebné, zeptá se na klíče a vyzkouší spojení s eToro |
| `2_SPUSTIT.bat` | spustí obě studie a otevře přehled (dashboard) v prohlížeči |
| `3_ZASTAVIT.bat` | nouzově zastaví agenty (zavřou své pozice) |
| `4_AKTUALIZOVAT.bat` | stáhne novou verzi, kterou připraví Claude |
| `5_DASHBOARD.bat` | znovu otevře přehled v prohlížeči |
| `7_PREPNOUT_NA_SWING.bat` | přepne obě studie na swing strategii RSI(2) a založí čistý start experimentu |
| `6_BACKTEST.bat` | porovná intradenní (1 rok) i swing strategie (8 let) na reálných datech – výsledek pošli Claudovi |

---

## Jednorázová příprava (cca 10 minut)

### 1. Python
Pokud ho ještě nemáš: <https://www.python.org/downloads/> → velké žluté tlačítko *Download Python*.
Při instalaci **zaškrtni dole „Add python.exe to PATH“** a klikni *Install Now*.

### 2. Klíče – připrav si je předem
- **eToro (povinné):** <https://api-portal.etoro.com> → *API Key Management* → vytvořit klíč.
  Pro zkušební provoz stačí oprávnění na čtení tržních dat a demo účet. Dostaneš **API Key** a **User Key**
  (User Key se ukáže jen jednou – hned si ho zkopíruj).
- **Pro Studii B (zprávy + makro) – zdarma, jen registrace e-mailem, můžeš doplnit i později:**
  - Finnhub: <https://finnhub.io> → *Get free API key*
  - FRED: <https://fred.stlouisfed.org/docs/api/api_key.html>

Nic z toho se neplatí. Zprávy a makrodata vyhodnocují pravidla přímo ve tvém počítači.

Klíče **nikdy neposílej do chatu** ani nikomu jinému.

### 3. Instalace
Dvojklik na **`1_INSTALACE.bat`**. Otevře se modré/černé okno, které:
1. zkontroluje Python,
2. nainstaluje knihovny (chvíli to trvá),
3. připraví Studii A a B,
4. **zeptá se postupně na klíče** – klíč vlož pravým tlačítkem myši (nebo Ctrl+V) a stiskni Enter;
   u nepovinných stačí Enter,
5. vyzkouší spojení s eToro a napíše **VŠE V POŘÁDKU** a **HOTOVO**.

Když se objeví červený text, udělej snímek obrazovky a pošli ho Claudovi (klíče na něm být nesmí).
Špatně zadaný klíč opravíš tak, že smažeš soubor `.env` a spustíš `1_INSTALACE.bat` znovu.

---

## Každý obchodní den

1. Dvojklik na **`2_SPUSTIT.bat`** – otevřou se okna *Burza Studie A*, *Burza Studie B* a *Burza dashboard*
   a v prohlížeči přehled. **Okna nezavírej** (můžeš je minimalizovat).
2. Americký trh je otevřený **Po–Pá 15:30–22:00** našeho času. Počítač musí v tu dobu běžet
   a být připojený k internetu. Mimo tuto dobu agenti jen čekají.
3. Agenti obchodují sami (virtuálně, 200 000 Kč pro každou studii). Před koncem obchodování vše uzavřou,
   přes noc nic nedrží.
4. Výsledky vidíš v prohlížeči na adrese <http://127.0.0.1:8080> (nebo dvojklik na `5_DASHBOARD.bat`).

**Zastavení:** dvojklik na `3_ZASTAVIT.bat`. Znovu spustíš přes `2_SPUSTIT.bat`.

**Nová verze od Clauda:** zastav agenty (`3_ZASTAVIT.bat`), dvojklik na `4_AKTUALIZOVAT.bat`, pak `2_SPUSTIT.bat`.

---

## Časté problémy

| Co vidím | Co s tím |
|---|---|
| „Python není nainstalovaný“ | nainstaluj Python (krok 1) a zaškrtni *Add python.exe to PATH* |
| „eToro klíče nefungují“ | smaž soubor `.env`, spusť `1_INSTALACE.bat` a klíče vlož znovu celé |
| „Studie B se nespouští“ | chybí klíč Finnhub nebo FRED – smaž `.env` a doplň je v instalaci |
| Dashboard se neotevře | okno *Burza dashboard* musí běžet; zkus `5_DASHBOARD.bat` |
| Windows hlásí „Systém Windows ochránil váš počítač“ | klikni *Další informace* → *Přesto spustit* |
| Soubor `.env` nevidím | je skrytý typ souboru; v Průzkumníku *Zobrazit → Přípony názvů souborů* |

## Jak je to zabezpečené
- Výchozí režim je **simulace (paper)** – žádné skutečné obchody, žádné peníze.
- Každá studie má limity: max. 1 % rizika na obchod, 3 % denní ztráty, 15 % celkové ztráty (pak se sama zastaví).
- Přechod na skutečné peníze (Agent Portfolio) agent sám neudělá – vyžaduje ruční změnu nastavení na třech místech.
