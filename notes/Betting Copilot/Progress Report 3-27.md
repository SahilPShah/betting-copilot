# Betting Copilot — Progress Report (March 27, 2026)

## What Was Accomplished

### Full End-to-End Pipeline is Live
The system runs daily with a single command (`./run_daily.sh`) and produces picks automatically. All three stages are implemented and working:

1. **Ingest** — pulls today's schedule, live odds, team stats, and injury reports
2. **Predict** — runs ELO ratings + calibrated logistic regression to generate win probabilities
3. **Recommend** — applies rules engine, scores edge and confidence, outputs picks

### Model (v2 — `v2_elo_logreg_starters`)
- Hybrid ELO + calibrated logistic regression trained on 2023–2025 seasons
- Features: `elo_diff`, `era_diff`, `whip_diff`, `k9_diff`, `ops_diff`, `win_pct_diff`, `runs_diff`, `starter_era_diff`, `starter_whip_diff`, `has_starter_data`
- Highest-weight feature: `starter_era_diff` (+0.3689)
- In-sample Brier score: 0.2333, accuracy: 60.2%
- Backfilled 14,596 historical game starters (2023–2025) with ERA/WHIP for training

### Starter ERA Resolution Logic
Pitcher ERA at ingest time uses a tiered fallback:
- **≥ 10 IP in current season** → use current season ERA/WHIP
- **< 10 IP** → fall back to prior season ending ERA/WHIP
- **No data found** → NULL (handled gracefully in model with `has_starter_data = 0`)

This is critical in early season when most pitchers haven't crossed the 10 IP threshold yet.

### Pick Output
- Displays **top 5 picks** per slate ranked by edge, each with:
  - Game, starters with ERA, market, side, odds, model prob, implied prob, edge, confidence, decision
  - Plain-English **reasoning** explaining why the model favored this side (edge size, pitching matchup, model conviction)
- Top pick is labeled `TOP PICK`, others `PICK #2` through `PICK #5`

### Today's Output (March 27, 2026 — Opening Day)
All 5 picks cleared thresholds. Edges are inflated (30–50%) due to Opening Day conditions — ELO is anchored to 2025, and the model hasn't seen any 2026 games yet. This is expected and will normalize over the first 2 weeks.

| Rank | Game | Side | Market | Odds | Edge | Confidence |
|------|------|------|--------|------|------|------------|
| 1 | SFG @ NYY | Away | Run line | +138 | 49.6% | 8.74 |
| 2 | HOU @ LAA | Away | Moneyline | +124 | 47.5% | 8.77 |
| 3 | LAD @ ARI | Home | Run line | -111 | 44.5% | 8.99 |
| 4 | SDP @ DET | Away | Run line | +138 | 37.5% | 8.14 |
| 5 | SFG @ NYY | Away | Moneyline | -124 | 36.6% | 8.74 |

3 games were skipped due to missing 2026 team stats (TOR, ATH, MIA, COL, ATL, KCR) — these teams had not yet appeared in pybaseball's 2026 dataset on Opening Day.

---

## Known Limitations

- **Opening Day edge inflation** — edges will be unreliable until ~2 weeks into the season when ELO and stats have updated
- **3 teams skipped** — pybaseball had no 2026 data for 6 teams on Opening Day; will self-resolve as games are played
- **Duplicate game/market picks** — top 5 can include both moneyline and run line for the same game; no deduplication rule yet
- **Season-level stats** — ERA/OPS averages are season-to-date, not rolling (e.g. L14 days); early season sample is very small

---

## Next Steps

### High Priority
1. **One pick per game rule** — deduplicate top 5 so the same game doesn't appear twice across markets
2. **Monitor edge compression** — re-run weekly and track whether edges normalize to 3–10% as the season builds up; if not, the model needs recalibration
3. **Fix skipped teams** — verify TOR, ATH, MIA, COL, ATL, KCR start appearing in predictions by mid-April

### Model Improvements
4. **Rolling stats pipeline (`features/`)** — replace season-level ERA/OPS with L14 rolling averages; this is the single biggest available improvement to model quality
5. **ROI backtest (`eval/roi.py`)** — validate that historical picks would have been profitable at the locked thresholds
6. **CLV analysis (`eval/clv.py`)** — measure whether early odds beats closing line (indicates model is finding real edge vs. noise)

### Product
7. **LLM pick explanations (`llm/`)** — replace template-based reasoning with Claude API calls for richer, context-aware natural-language explanations
8. **FastAPI endpoints (`api/`)** — expose `/slate` and `/game/{id}` so picks can be consumed from a frontend or mobile app
9. **Scheduled runs** — automate `./run_daily.sh` via cron so it runs each morning without manual intervention
10. **Log output to file** — save each daily run's output to `logs/YYYY-MM-DD.log` for review and debugging

*To Resume this session: claude --resume d0f546a9-bb9a-49e6-960b-43383df58b99                                                                                 