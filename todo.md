# TODO — Low Priority

## Price Model Improvements
- [ ] Goalie features: games played, save percentage, team defense quality
- [ ] Auction position effect: early picks tend to sell higher
- [ ] Non-linear points x team_probability interaction term

## Known Limitations to Address
- [ ] Price model is static — does not adjust for budget depletion mid-auction
- [ ] No positional scarcity awareness in the model layer (handled by market layer, but model could be better)
- [ ] Goalie confidence intervals are wide (R²=0.61) — more features needed
