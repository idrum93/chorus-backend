# Archived cards

`upcoming.py` copies `site/predictions.json` here whenever the event changes,
before overwriting it.

This exists because of a race. `refresh_upcoming` advances `data/upcoming.txt`
to the next card the moment Wikipedia lists one, while results only reach the
corpus when the Greco1899 scrape updates — often a day or more later. Grading
therefore only ever worked in the overlap between "the card is over" and "the
card is still the current file", which is frequently empty. UFC 331 fell
straight into that gap: the site moved on before its results existed.

`grade_archive()` re-grades every archived card against the corpus on each
run and rewrites `site/history.json`. Re-grading from scratch means a card
archived before its results landed simply grades on a later pass, with no
stuck state to repair.
