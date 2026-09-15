# Trening UberPoB — dane treningowe dla Gemini

## Cel
Fine-tuning Gemini pod relational reasoning w PoE — nie surowe fakty, ale rozumienie **relacji między statami i mechanikami** ("łączenie kropek").

---

## Pipeline

```
1. ZBIERANIE (auto)
   YouTube podcasty/vody → yt-dlp → audio → whisper → transkrypt

2. EKSTRAKCJA FAKTÓW (semi-auto)
   transkrypt → LLM → chunki {mechnika, stat, kontekst, relacja}

3. GOLDEN INSIGHTS (manual — bottleneck)
   Ręczna identyfikacja konceptów wyższego rzędu (patrz sekcja niżej)

4. SYNTETYCZNE WARIANTY (auto)
   1x golden example → strong model → N analogicznych przykładów dla innych mechanik
   Format: chain-of-thought reasoning

5. WERYFIKACJA przez PoB headless
   claim z liczbami → PoB → match: include / mismatch: odrzuć
```

**Kluczowy insight:** PoB headless jako zewnętrzny ground-truth verifier = można używać tańszego modelu do generowania, PoB odfiltrowuje halucynacje numeryczne.

---

## Stan infrastruktury

| Komponent | Stan |
|---|---|
| PoB headless | Testowane, działało. MCP odłączone po incydencie z wyciekiem tokenów |
| yt-dlp + whisper | Do postawienia |
| Verification pipeline | Do zbudowania |

---

## Golden Insights

Koncepty wyższego rzędu zidentyfikowane ręcznie — podstawa syntetycznych wariantów.

### GI-001 — Axes of Scaling (accuracy stacker)
**Źródło:** Podcast Jungroan × Palsteron (YouTube)
**Koncept:** Ten sam typ buildu (accuracy stacker) ma inną ilość "osi skalowania" w różnych kontekstach ligowych.

- Liga standard: accuracy → głównie attack speed (Juggernaut) + flat acc z implicit butów
- Event Phrecia (zmienione ascendancy): accuracy → dodatkowo more crit, more damage, base multiplier

**Relacja:** Więcej osi skalowania z jednej statystyki = wyższy DPS mimo podobnych itemów / niższej bazowej accuracy.
**Zastosowanie syntetyczne:** Wygenerować analogiczne porównania dla innych stat-stackerów (ele pen, poison, evasion rating itd.) w różnych kontekstach ligowych.

---

## Model do generowania syntetycznych przykładów

- **Nie Gemini** (distillation loop, quality ceiling)
- Kandydaci: Claude Opus, GPT-4o, DeepSeek R1
- Model drugorzędny — PoB jest filtrem jakości, nie model

---

## TODO

- [ ] Zdecydować jak bezpiecznie podłączyć PoB MCP z powrotem (token leak fix)
- [ ] Zbudować transkrypcję YouTube pipeline
- [ ] Wybrać format training examples (JSONL? konkretny Gemini fine-tune format?)
- [ ] Zebrać więcej golden insights
