#!/usr/bin/env python3
"""Daily website update for the B1G Hoops fantasy league.

Runs on GitHub Actions (see .github/workflows/daily-update.yml) and writes to the league's
Firebase database with the service account key stored in the FIREBASE_SERVICE_ACCOUNT secret.

Each run:
  1. Reads the league from Firebase (config, rosters, rounds, coach lineups, added players).
  2. Downloads this season's ESPN box scores and schedule from the sportsdataverse hoopR data
     (updated several times a day on GitHub).
  3. Loads the hub page (index.html) in a headless browser so rounds, lock times, lineups and
     player ids come from the hub's own code.
  4. Writes:
     - league/tips       tip times for games in the next 14 days that have none (never changes a time)
     - rounds/rN         locks a round's lineups once its first tip has passed, then fills box score
                         lines for lineup players and team units after each game (never overwrites)
     - league/curstats   season to date fantasy points per game, games started, minutes, team scores
     - league/injuries   from feed/injuries.json (written by the daily Claude task), keeping the
                         commissioner's manual entries
Nothing is written when nothing changed. Use --dry-run to print the writes instead.
"""
import argparse, datetime as dt, io, json, os, re, sys, time, urllib.request
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo('America/New_York')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = 'https://njbaker614.github.io/b1g-hoops/'
HOOPR = 'https://raw.githubusercontent.com/sportsdataverse/hoopR-mbb-data/main/mbb/{k}/parquet/{f}_2027.parquet'
ESPN = {'ILL': 356, 'IND': 84, 'IOWA': 2294, 'MD': 120, 'MICH': 130, 'MSU': 127, 'MINN': 135, 'NEB': 158, 'NU': 77,
        'OSU': 194, 'ORE': 2483, 'PSU': 213, 'PUR': 2509, 'RUT': 164, 'UCLA': 26, 'USC': 30, 'UW': 264, 'WIS': 275}
ID2K = {v: k for k, v in ESPN.items()}
SUFFIX = re.compile(r'-(jr|sr|ii|iii|iv)$')
# Same player, different spelling (team, ESPN name slug without suffix) -> pool name slug without suffix
ALIASES = {('OSU', 'john-mobley'): 'juni-mobley', ('MD', 'mike-mcnair'): 'michael-mcnair', ('MICH', 'marcus-moller'): 'marcus-mller'}
COLLECTIONS = ['league', 'rosters', 'rounds', 'coachlu', 'players', 'changes']
LOG = []


def log(*a):
    s = ' '.join(str(x) for x in a)
    LOG.append(s)
    print(s, flush=True)


def mdY(d):  # "Nov 26, 2026"
    return d.strftime('%b ') + str(d.day) + d.strftime(', %Y')


# ---------------------------------------------------------------- data in
def load_hoopr(local_dir=None):
    out = {}
    for key, kind, fname in [('box', 'player_box', 'player_box'), ('sched', 'schedules', 'mbb_schedule')]:
        if local_dir:
            out[key] = pd.read_parquet(os.path.join(local_dir, f'{fname}_2027.parquet'))
            continue
        url = HOOPR.format(k=kind, f=fname)
        for i in range(4):
            try:
                with urllib.request.urlopen(url, timeout=120) as r:
                    out[key] = pd.read_parquet(io.BytesIO(r.read()))
                break
            except Exception as e:
                if i == 3:
                    raise
                log('retrying hoopR download:', e)
                time.sleep(10 * (i + 1))
    s = out['sched']
    s['home_id'] = pd.to_numeric(s['home_id'], errors='coerce')
    s['away_id'] = pd.to_numeric(s['away_id'], errors='coerce')
    s['et'] = pd.to_datetime(s['game_date_time'], utc=True).dt.tz_convert(ET)
    s['ymd'] = s['et'].dt.strftime('%Y-%m-%d')
    b = out['box']
    b['team_id'] = pd.to_numeric(b['team_id'], errors='coerce')
    b['game_id'] = pd.to_numeric(b['game_id'], errors='coerce')
    s['game_id'] = pd.to_numeric(s['game_id'], errors='coerce')
    return out


def firestore_client():
    import firebase_admin
    from firebase_admin import credentials, firestore
    raw = os.environ.get('FIREBASE_SERVICE_ACCOUNT', '').strip()
    if not raw:
        sys.exit('FIREBASE_SERVICE_ACCOUNT secret is missing. Add it in GitHub: Settings, Secrets and variables, Actions.')
    firebase_admin.initialize_app(credentials.Certificate(json.loads(raw)))
    return firestore.client()


def load_state(db):
    st = {}
    for c in COLLECTIONS:
        for d in db.collection(c).stream():
            st[f'{c}/{d.id}'] = d.to_dict()
    return json.loads(json.dumps(st, default=str))


# ---------------------------------------------------------------- the hub page
JS_BASE = """()=>{const ymd=d=>d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
return {games:GAMES.map(g=>({id:g.id,date:ymd(g.date),away:g.away,home:g.home,tip0:TIP0[g.id]||''})),
 teams:TEAMS.map(t=>t.k),
 players:allPlayers().map(p=>({id:p.id,name:p.name,team:p.team})),
 rounds:ROUNDS.map(R=>({n:R.n,games:R.entries.map(e=>({team:e.team,gid:e.game.id}))})),
 coaches:S.cfg.coaches.map(c=>c.id)}}"""
JS_LOCKS = """([tips,now])=>{S.tips=tips;const cs=S.cfg.coaches;
const ready=cs.length>0&&cs.every(c=>rosterAt(c.id,now).starters.length);
return {ready,rounds:ROUNDS.map(R=>{const L=lockTs(R);return {n:R.n,lock:L,lineups:ready&&L<=now?Object.fromEntries(cs.map(c=>{const ro=rosterAt(c.id,L);
 return [c.id,{starters:[...ro.starters],reserves:[...ro.reserves],off:ro.off||'',def:ro.def||''}]})):null}})}}"""


class Hub:
    def __init__(self, store):
        from playwright.sync_api import sync_playwright
        mock = open(os.path.join(ROOT, 'sync', 'pagemock.js')).read().replace('__STORE__', json.dumps(store))
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch()
        ctx = self.browser.new_context(timezone_id='America/New_York')

        def route(r):
            u = r.request.url
            if not u.startswith(BASE):
                return r.abort()
            f = u[len(BASE):].split('?')[0].split('#')[0] or 'index.html'
            if f == 'firebase-shim.js':
                return r.fulfill(body=mock, content_type='text/javascript')
            if f == 'firebase-config.js':
                return r.fulfill(body='window.FIREBASE_CONFIG={};', content_type='text/javascript')
            p = os.path.join(ROOT, f)
            return r.fulfill(path=p) if os.path.isfile(p) else r.fulfill(status=404)

        ctx.route('**/*', route)
        self.page = ctx.new_page()
        self.errors = []
        self.page.on('pageerror', lambda e: self.errors.append(str(e)))
        self.page.goto(BASE + 'index.html')
        self.page.wait_for_function('typeof S!=="undefined" && S.loaded', timeout=60000)
        self.page.wait_for_timeout(1000)

    def ev(self, js, arg=None):
        return self.page.evaluate(js, arg)

    def close(self):
        self.browser.close()
        self.pw.stop()


# ---------------------------------------------------------------- computing
def num0(x, kind=int):
    v = pd.to_numeric(x, errors='coerce')
    return kind(0 if pd.isna(v) else v)


def r1(x):
    return round(float(x) + 1e-9, 1)


def build_matcher(hub, base, box):
    """Map ESPN athlete ids on Big Ten teams to pool player ids."""
    pool = {}
    for p in base['players']:
        stem = SUFFIX.sub('', p['id'][: -(len(p['team']) + 1)])
        pool.setdefault((p['team'], stem), p['id'])
    rows = box[box['team_id'].isin(ID2K.keys())][['athlete_id', 'athlete_display_name', 'team_id']].drop_duplicates('athlete_id')
    names = rows['athlete_display_name'].fillna('').tolist()
    slugs = hub.ev('n=>n.map(x=>slug(x))', names)
    m = {}
    for (aid, tid), s in zip(zip(rows['athlete_id'], rows['team_id']), slugs):
        k = ID2K[int(tid)]
        stem = SUFFIX.sub('', s)
        stem = ALIASES.get((k, stem), stem)
        pid = pool.get((k, stem))
        if pid:
            m[str(aid)] = pid
    return m


def played(df):
    mins = pd.to_numeric(df['minutes'], errors='coerce').fillna(0)
    dnp = df['did_not_play'].fillna(False).astype(bool) if 'did_not_play' in df else False
    return df[(mins > 0) & ~dnp]


def compute_curstats(box, sched, match, prev):
    b = box[box['team_id'].isin(ID2K.keys())].copy()
    b = played(b)
    num = lambda c: pd.to_numeric(b[c], errors='coerce').fillna(0)
    for c in ['minutes', 'points', 'rebounds', 'assists', 'steals', 'blocks', 'turnovers']:
        b[c] = num(c)
    b['gs'] = b['starter'].fillna(False).astype(bool).astype(int)
    b['pid'] = b['athlete_id'].astype(str).map(match)
    players = dict((prev or {}).get('players') or {})
    unmatched = []
    for aid, g in b.groupby('athlete_id'):
        n = len(g)
        mp, pts, reb, ast, stl, blk, tov = (g[c].mean() for c in ['minutes', 'points', 'rebounds', 'assists', 'steals', 'blocks', 'turnovers'])
        pid = g['pid'].iloc[0]
        if not isinstance(pid, str):
            if mp >= 12:
                unmatched.append(f"{g['athlete_display_name'].iloc[0]} ({ID2K[int(g['team_id'].iloc[0])]}, {mp:.1f} min)")
            continue
        players[pid] = {'g': int(n), 'gs': int(g['gs'].sum()), 'mp': r1(mp),
                        'fp': r1(pts + 1.2 * reb + 1.5 * ast + 3 * stl + 3 * blk - 3 * tov)}
    teams = dict((prev or {}).get('teams') or {})
    done = sched[sched['status_type_completed'].fillna(False).astype(bool)]
    for k, tid in ESPN.items():
        rows = []
        for _, r in done[(done['home_id'] == tid) | (done['away_id'] == tid)].iterrows():
            home = r['home_id'] == tid
            pf = pd.to_numeric(r['home_score' if home else 'away_score'], errors='coerce')
            pa = pd.to_numeric(r['away_score' if home else 'home_score'], errors='coerce')
            if pd.notna(pf) and pd.notna(pa):
                rows.append((float(pf), float(pa)))
        if rows:
            n = len(rows)
            teams[k] = {'g': n, 'pf': r1(sum(p for p, _ in rows) / n), 'pa': r1(sum(a for _, a in rows) / n),
                        'ofp': r1(sum(min(20, 0.5 * max(0, p - 48)) for p, _ in rows) / n),
                        'dfp': r1(sum(max(0, 20 - 0.5 * max(0, a - 55)) for _, a in rows) / n)}
    bt = done[done['home_id'].isin(ID2K.keys()) | done['away_id'].isin(ID2K.keys())]
    as_of = mdY(dt.datetime.strptime(bt['ymd'].max(), '%Y-%m-%d')) if len(bt) else (prev or {}).get('asOf', '')
    return {'asOf': as_of, 'players': players, 'teams': teams}, unmatched


def new_tips(base, sched, have, now_et):
    add = {}
    horizon = (now_et + dt.timedelta(days=14)).strftime('%Y-%m-%d')
    today = now_et.strftime('%Y-%m-%d')
    for g in base['games']:
        gid = str(g['id'])
        if gid in have or g['tip0'] or not (today <= g['date'] <= horizon):
            continue
        r = sched[(sched['ymd'] == g['date']) & (sched['away_id'] == ESPN[g['away']]) & (sched['home_id'] == ESPN[g['home']])]
        if not len(r) or not bool(r['time_valid'].iloc[0]):
            continue
        t = r['et'].iloc[0]
        add[gid] = t.strftime('%I:%M %p').lstrip('0')
    return add


def fill_round(doc, rnd, base, sched, box, match):
    """Box score lines for lineup players and teams whose round game is final. Never overwrites."""
    team_of = {p['id']: p['team'] for p in base['players']}
    game_of = {e['team']: base['games'][e['gid']] for e in rnd['games']}
    stats, teams = doc.get('stats') or {}, doc.get('teams') or {}
    need = {}
    for lu in (doc.get('lineups') or {}).values():
        for pid in (lu.get('starters') or []) + (lu.get('reserves') or []):
            if pid and pid in team_of:
                need.setdefault(team_of[pid], set()).add(pid)
        for k in (lu.get('off'), lu.get('def')):
            if k:
                need.setdefault(k, set())
    add_s, add_t, flags, through, waiting = {}, {}, {}, '', []
    for k, pids in sorted(need.items()):
        g = game_of.get(k)
        if not g:
            continue
        r = sched[(sched['ymd'] == g['date']) & (sched['away_id'] == ESPN[g['away']]) & (sched['home_id'] == ESPN[g['home']])]
        if not len(r) or not bool(r['status_type_completed'].fillna(False).iloc[0]):
            if g['date'] < dt.datetime.now(ET).strftime('%Y-%m-%d'):
                waiting.append(f"{g['away']} at {g['home']} ({g['date']})")
            continue
        r = r.iloc[0]
        gb = box[(box['game_id'] == int(r['game_id'])) & (box['team_id'] == ESPN[k])]
        lines = {}
        for _, x in played(gb).iterrows():
            pid = match.get(str(x['athlete_id']))
            if pid:
                v = lambda c: num0(x[c])
                lines[pid] = {'min': int(round(num0(x['minutes'], float))), 'pts': v('points'),
                              'reb': v('rebounds'), 'ast': v('assists'), 'stl': v('steals'), 'blk': v('blocks'), 'to': v('turnovers')}
        for pid in pids:
            cur = stats.get(pid) or {}
            if cur.get('min') not in (None, ''):
                continue
            add_s[pid] = lines.get(pid, {'min': 0, 'pts': 0, 'reb': 0, 'ast': 0, 'stl': 0, 'blk': 0, 'to': 0})
            flags[pid] = True
        t = teams.get(k) or {}
        if t.get('pf') in (None, '') or t.get('pa') in (None, ''):
            home = ESPN[k] == r['home_id']
            add_t[k] = {'pf': int(r['home_score' if home else 'away_score']), 'pa': int(r['away_score' if home else 'home_score'])}
            flags['T:' + k] = True
        through = max(through, g['date'])
    return add_s, add_t, flags, through, waiting


LOC2K = {'Illinois': 'ILL', 'Michigan State': 'MSU', 'Michigan': 'MICH', 'Nebraska': 'NEB', 'Ohio State': 'OSU', 'Purdue': 'PUR',
         'Indiana': 'IND', 'Iowa': 'IOWA', 'Maryland': 'MD', 'Wisconsin': 'WIS', 'Oregon': 'ORE', 'Minnesota': 'MINN',
         'Washington': 'UW', 'Rutgers': 'RUT', 'Northwestern': 'NU', 'Penn State': 'PSU'}


def build_gamelog(box, sched, match, base, old):
    """Game log file for the Game Log page: keeps past seasons as they are and rebuilds this season
    (2027) from ESPN box scores. Row: [season, date, team, opponent, H/A/N, result, started, min,
    pts, reb, ast, stl, blk, to, fp, conference game]."""
    gl = {k: [r for r in v if r[0] != 2027] for k, v in old.items() if not k.startswith('_')}
    conf = {(g['date'], g['away'], g['home']) for g in base['games']}
    sc = sched.drop_duplicates('game_id').set_index('game_id')
    b = played(box[box['team_id'].isin(ID2K.keys())]).copy()
    b['pid'] = b['athlete_id'].astype(str).map(match)
    b = b[b['pid'].notna()]
    for _, x in b.iterrows():
        gid = int(x['game_id'])
        if gid not in sc.index:
            continue
        g = sc.loc[gid]
        k, ok = ID2K[int(x['team_id'])], int(pd.to_numeric(x['opponent_team_id'], errors='coerce') or 0)
        home = x['home_away'] == 'home'
        ha = 'N' if bool(g['neutral_site']) else ('H' if home else 'A')
        n = lambda c: num0(x[c])
        fp = n('points') + 1.2 * n('rebounds') + 1.5 * n('assists') + 3 * n('steals') + 3 * n('blocks') - 3 * n('turnovers')
        opp = str(x['opponent_team_location'])
        opp = ID2K.get(ok, LOC2K.get(opp, opp)) if opp not in ('UCLA', 'USC') else opp
        date = str(g['ymd'])
        ak, hk = (ID2K.get(ok), k) if home else (k, ID2K.get(ok))
        res = ('W ' if bool(x['team_winner']) else 'L ') + f"{num0(x['team_score'])}-{num0(x['opponent_team_score'])}"
        gl.setdefault(x['pid'], []).append([2027, date, str(x['team_location']), opp, ha, res, int(bool(x['starter'])),
                                            int(round(num0(x['minutes'], float))), n('points'), n('rebounds'), n('assists'),
                                            n('steals'), n('blocks'), n('turnovers'), r1(fp), int((date, ak, hk) in conf)])
    for k in gl:
        gl[k].sort(key=lambda r: (r[0], r[1]))
    done = sched[sched['status_type_completed'].fillna(False).astype(bool)]
    bt = done[done['home_id'].isin(ID2K.keys()) | done['away_id'].isin(ID2K.keys())]
    gl['_meta'] = {'asOf': mdY(dt.datetime.strptime(bt['ymd'].max(), '%Y-%m-%d')) if len(bt) else ''}
    return gl


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--state', help='read league data from this JSON file instead of Firebase (testing)')
    ap.add_argument('--hoopr-dir', help='read hoopR parquet files from this folder (testing)')
    ap.add_argument('--now', help='pretend the time is this ISO time (testing)')
    a = ap.parse_args()
    now = dt.datetime.fromisoformat(a.now) if a.now else dt.datetime.now(dt.timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    now_et = now.astimezone(ET)
    iso = now.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    db = None
    if a.state:
        state = json.load(open(a.state))
    else:
        db = firestore_client()
        state = load_state(db)
    if 'league/config' not in state:
        log('No league in the database yet (league/config missing). Nothing to do.')
        return
    hr = load_hoopr(a.hoopr_dir)
    box, sched = hr['box'], hr['sched']
    log(f"hoopR data: {len(box)} player lines, games through {sched[sched['status_type_completed'].fillna(False).astype(bool)]['ymd'].max()}")

    writes = []  # (path, data, merge)
    hub = Hub(state)
    try:
        base = hub.ev(JS_BASE)
        match = build_matcher(hub, base, box)

        # 1. tip times
        tips_doc = state.get('league/tips') or {}
        have = dict(tips_doc.get('times') or {})
        add = new_tips(base, sched, have, now_et)
        if add:
            writes.append(('league/tips', {'times': add}, True))
            have.update(add)
            log(f'Tip times added: {len(add)}')

        # 2. lock rounds
        lk = hub.ev(JS_LOCKS, [have, now_ms])
        locked = []
        for R in lk['rounds']:
            path = f"rounds/r{R['n']}"
            cur = state.get(path) or {}
            if R['lineups'] and not cur.get('lineups'):
                doc = {'n': R['n'], 'lineups': R['lineups'], 'stats': cur.get('stats') or {}, 'teams': cur.get('teams') or {},
                       'final': bool(cur.get('final')), 'lockedAt': now_ms}
                writes.append((path, doc, False))
                state[path] = doc
                locked.append(R['n'])
        if locked:
            log('Rounds locked:', ', '.join(map(str, locked)))
        elif not lk['ready']:
            log('Rosters are not all set yet (draft not done), so no rounds were locked.')
        if hub.errors:
            log('Page warnings:', '; '.join(hub.errors[:5]))
    finally:
        hub.close()

    # 3. box scores
    rounds_by_n = {r['n']: r for r in base['rounds']}
    filled_p = filled_t = 0
    for n, rnd in rounds_by_n.items():
        path = f'rounds/r{n}'
        doc = state.get(path)
        if not doc or not doc.get('lineups') or doc.get('final') is True:
            continue
        s, t, flags, through, waiting = fill_round(doc, rnd, base, sched, box, match)
        for w in waiting:
            log(f'Round {n}: box score not posted yet for {w}')
        if s or t:
            through_md = mdY(dt.datetime.strptime(through, '%Y-%m-%d'))[:-6] if through else ''
            writes.append((path, {'stats': s, 'teams': t, 'autoFilled': flags,
                                  'auto': {'updatedAt': iso, 'through': through_md, 'sources': ['ESPN box scores via sportsdataverse hoopR']}}, True))
            filled_p += len(s)
            filled_t += len(t)
            log(f'Round {n}: filled {len(s)} player lines and {len(t)} team lines')

    # 4. season to date stats
    prev = state.get('league/curstats') or {}
    cs, unmatched = compute_curstats(box, sched, match, prev)
    if cs['players'] != (prev.get('players') or {}) or cs['teams'] != (prev.get('teams') or {}) or cs['asOf'] != prev.get('asOf'):
        writes.append(('league/curstats', {**cs, 'updatedAt': iso}, False))
        log(f"Current stats: {len(cs['players'])} players, {len(cs['teams'])} teams, games through {cs['asOf']}")
    else:
        log('Current stats unchanged.')
    if unmatched:
        log('Players with 12+ minutes not in the pool:', '; '.join(sorted(unmatched)))

    # 5. injuries from the Claude task's feed
    feed_path = os.path.join(ROOT, 'feed', 'injuries.json')
    if os.path.isfile(feed_path):
        feed = json.load(open(feed_path))
        cur = state.get('league/injuries') or {}
        if (feed.get('updatedAt') or '') > (cur.get('updatedAt') or ''):
            ids = {p['id'] for p in base['players']}
            players = {k: v for k, v in (cur.get('players') or {}).items() if v.get('manual')}
            for k, v in (feed.get('players') or {}).items():
                if k in ids and k not in players:
                    players[k] = {**v, 'manual': False}
            writes.append(('league/injuries', {'asOf': feed.get('asOf', ''), 'updatedAt': feed['updatedAt'], 'players': players}, False))
            log(f'Injury report: {len(players)} tagged players (as of {feed.get("asOf", "")})')

    # 6. game log file (a site file, committed by the workflow when it changes)
    gl_path = os.path.join(ROOT, 'gamelog.json')
    if os.path.isfile(gl_path):
        old = json.load(open(gl_path))
        gl = build_gamelog(box, sched, match, base, old)
        if gl != old:
            n27 = sum(1 for v in gl.values() if isinstance(v, list) for r in v if r[0] == 2027)
            if a.dry_run:
                log(f"[dry run] game log: {n27} games this season, through {gl['_meta']['asOf']}")
            else:
                with open(gl_path, 'w') as f:
                    json.dump(gl, f, separators=(',', ':'), ensure_ascii=False)
                log(f"Game log updated: {n27} games this season, through {gl['_meta']['asOf']}")

    # write
    if not writes:
        log('Nothing changed.')
    for path, data, merge in writes:
        if a.dry_run or db is None:
            log(f'[dry run] {"merge" if merge else "set"} {path}: {json.dumps(data) if os.environ.get("SYNC_FULL_LOG") else json.dumps(data)[:300]}')
        else:
            db.document(path).set(data, merge=merge)
    summ = os.environ.get('GITHUB_STEP_SUMMARY')
    if summ:
        with open(summ, 'a') as f:
            f.write('### League update\n\n' + '\n'.join('- ' + x for x in LOG) + '\n')


if __name__ == '__main__':
    main()
