# Materiálový přehled (Factorify)

Interaktivní stránka pro vyhledání vstupního materiálu ve Factorify podle ID nebo názvu.
Pro každý materiál ukazuje dodavatele, MOQ, ceny (platný ceník, poslední nákup, VNC)
a spotřebu za den, měsíc a rok. Detail řádku obsahuje graf spotřeby po měsících
a po dnech, všechny nákupní ceníky, poslední nákupní objednávky a zásobu po skladech.

`index.html` je obsah artefaktu na claude.ai. Data čte živě přes konektor Factorify
(nástroje `execute_sql` a `list_accounting_units`) s oprávněními toho, kdo stránku otevře.
Mimo claude.ai stránka data nenačte.

## Jak se počítá spotřeba

- Zdroj: výdejky (skladové doklady typu výdej) za uzavřené dny, bez dneška.
- Výroba = výdej do výrobní dávky, Ostatní výdeje = výdej na pracoviště, středisko,
  odpis apod., Prodej a expedice = výdej na dodací list nebo vnitropodnikový prodej.
  Které kategorie se sčítají, se volí přímo na stránce.
- Den = průměr za posledních 30 (nebo 365) dní, měsíc = posledních 30 dní,
  minulý kalendářní měsíc nebo průměr za 12 měsíců, rok = posledních 365 dní.
- Tabulka skladových pohybů je velká, proto stránka nejdřív zjistí rozsah ID pohybů
  pro posledních 30 dní a pro rok a spotřebu čte po materiálech v několika menších
  dotazech. Když dotaz narazí na časový limit, rozdělí se automaticky na menší části.
