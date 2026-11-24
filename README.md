# B1G Hoops Fantasy League

Live site: https://njbaker614.github.io/b1g-hoops/

Big Ten men's basketball fantasy league hub for the 2026-27 season: draft board, player and team pools with daily projections, game logs, roster summary with drag and drop lineups, head to head matchups, round schedule, playoff bracket, results, standings and commissioner tools.

## How it is built
- `index.html` is the whole app (one page, no build step).
- `firebase-shim.js` connects the page to Firebase: Firestore for league data and Google sign-in for coaches.
- `firebase-config.js` holds the Firebase web app settings (public by design).
- `firestore.rules` are the database security rules: signed in people can read; only the commissioner writes league data; each coach can save only his own lineup moves.
- `gamelog.json` is the historical box score log used by the Game Log page.
