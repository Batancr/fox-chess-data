# Fox Chess Lab: FIDE game files

Over-the-board games broadcast on Lichess, sorted by FIDE ID, for the player search in
[Fox Chess Lab](https://batancr.github.io/fox-chess-lab/). The site downloads one small file for the player
you search; nothing else in the site depends on this repo.

## Where the games come from

The [Lichess open database](https://database.lichess.org/#broadcasts) publishes every broadcast game each month,
with both players' FIDE IDs in the headers. Those files are released under **CC0** ("use them for anything you
like"), so they can be republished here. Coverage is only events broadcast on Lichess, from January 2020.

Lichess only started putting FIDE IDs on broadcast games in 2023. Older games (and players without an ID in a
newer game) are placed by the player's **name**, using the names seen next to FIDE IDs in later games, and only
when that name belongs to exactly one FIDE ID. Those games are marked as placed by name (the last field of each
game is 1), so the site can say so. `names.json.gz` holds the names learnt.

## How it runs

`.github/workflows/build.yml` runs on GitHub's computers (free for public repos), never on your own:

- **Once a month** (the 9th, 07:23 UTC) it downloads only the months not included yet, adds those games, and
  rewrites only the files of players who played.
- **By hand**: Actions tab → *Build FIDE game files* → *Run workflow*. Choose `full` to rebuild everything
  from 2020 (needed only if the file format changes).

The result is pushed to the `gh-pages` branch as **one fresh commit each time**, so the repo doesn't grow month
after month. GitHub Pages serves that branch at `https://batancr.github.io/fox-chess-data/`.

Each run also updates `status.json` on `main` (when it was built, how many players and games). That small commit
also keeps the monthly schedule switched on: GitHub pauses schedules in repos with no activity for 60 days.

## The files

- `p/XYZ.json.gz`: the players whose FIDE ID leaves remainder `XYZ` (hexadecimal) when divided by 4096, with
  each game's date, colour, result, opponent, ratings, event, speed, the first 15 moves each side, and the game's
  Lichess link. The full layout is described in `meta.json` under `format`.
- `meta.json`: months included, totals, and when it was built.

## Removing a player

Add their FIDE ID to `removed.txt` (one per line) and commit. The next run leaves them out; to do it straight
away, run the workflow by hand.

## Running it yourself (optional)

```
pip install zstandard
python build_fide_index.py --mode full --out out
```
