"""Backtesting: replaying completed drafts to measure the engine instead of trusting it.

Every constant in the recommendation engine -- demand floors, depth discounts, the ADP
spread curve -- is a modeling choice, and the only honest way to pick between choices is
to score them against drafts that actually happened. This package holds the pieces:

* ``capture``  -- a completed draft as a file, so backtests need no network.
* ``calibration`` -- were the survival probabilities right? (Brier score, reliability)
* ``counterfactual`` -- had the engine made your picks, how good a roster results?
"""
