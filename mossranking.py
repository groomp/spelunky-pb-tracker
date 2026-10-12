"""
Scrapes a Moss Ranking (mossranking.com) Spelunky HD profile for category
PBs (time/rank/rating), given a profile URL, bare numeric user ID, or
username (resolved via the site's own player search).

No external dependencies - uses only the standard library, so this bundles
cleanly into a standalone .exe. A plain request with a normal browser
User-Agent reaches the real page directly (verified against mossranking.com
directly); no headless browser or Cloudflare workaround is needed.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Group IDs (Spelunky HD, t=2, the "g=" param on a user's own profile page)
# that contain named full-category runs, as opposed to score boards, daily
# leaderboards, or individual-level/area/pace splits (g=7-12, excluded).
# Confirmed against mossranking.com/user.php?id_user=<id>&t=2's own group
# nav links - every g= value there that leads to a real category table:
#   1 Main Speed, 6 Main Score, 14 Main Challenge, 2 Misc, 13 Bookskip,
#   3 Memes, 5 Pacifist.
# Previously only [1, 14, 2, 3] - missing 5 (Pacifist) and 13 (Bookskip)
# entirely is exactly why a category like "Pacifist No%" never showed up
# for anyone syncing their profile, even though scrape_world_records's own
# CATS_GROUPS below always had the right 6 top-level groups. 6 (Main
# Score) contributes no actual PBs here (its rows are all $-amounts, which
# TIME_RE filters out below) but is kept for completeness - if the site
# ever reclassifies something there as timed, this picks it up for free.
PROFILE_GROUPS = {
    1: "Main Speed", 6: "Main Score", 14: "Main Challenge",
    2: "Misc", 13: "Bookskip", 3: "Memes", 5: "Pacifist",
}

# cid values for mossranking.com/spelunky-hd/cats - a different group
# numbering than PROFILE_GROUPS above (that's the "g=" param on a user's
# own profile page; this is the "cid=" param on the site-wide category
# overview). Main Score (cid 6) is included for completeness even though
# every one of its rows is a score, not a time, so TIME_RE filters it down
# to nothing - leaving it out would just silently drop any category the
# site later reclassifies as a hybrid.
CATS_GROUPS = {1: "Main Speed", 6: "Main Score", 2: "Misc", 5: "Pacifist", 13: "Bookskip", 3: "Memes"}

SKIP_CATEGORIES = {"Tutorial Score", "Daily High Score", "Daily Average", "Daily Wins"}

TIME_RE = re.compile(r"^\d+:\d{2}(\.\d+)?$")
RANK_RE = re.compile(r"^\d+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROFILE_ID_RE = re.compile(r"id_user=(\d+)")
TITLE_RE = re.compile(r"<title>\s*Profile:\s*(.*?)\s*</title>", re.S)


class ProfileNotFound(Exception):
    pass


class _TableRowParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self._cur_row = None
        self._cur_cell = None
        self._in_td = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._cur_row = []
        elif tag == "td":
            self._in_td = True
            self._cur_cell = []

    def handle_endtag(self, tag):
        if tag == "td" and self._in_td:
            text = re.sub(r"\s+", " ", "".join(self._cur_cell)).strip()
            self._cur_row.append(text)
            self._in_td = False
            self._cur_cell = None
        elif tag == "tr" and self._cur_row is not None:
            if self._cur_row:
                self.rows.append(self._cur_row)
            self._cur_row = None

    def handle_data(self, data):
        if self._in_td and self._cur_cell is not None:
            self._cur_cell.append(data)


def _search_players(query):
    """Hits the site's own live-search API (the same one behind the search
    box in its navbar) and returns the raw list of {"type", "id", "label",
    "url"} matches, of any type - callers filter for type == "user"."""
    data = urllib.parse.urlencode({"q": query, "type": "all"}).encode()
    req = urllib.request.Request(
        "https://mossranking.com/api/autocomplete.php",
        data=data,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def resolve_user_id(text):
    """Accepts a bare numeric ID, a full profile URL, or a MossRanking
    username, and returns the numeric user ID as a string. A username is
    resolved via the site's own player search - an exact (case-insensitive)
    name match wins outright; otherwise, if the search turned up exactly
    one player at all, that one is used; a genuinely ambiguous query (more
    than one candidate, none an exact match) is rejected rather than
    guessing which one was meant."""
    text = text.strip()
    m = PROFILE_ID_RE.search(text)
    if m:
        return m.group(1)
    if text.isdigit():
        return text

    try:
        results = _search_players(text)
    except urllib.error.URLError as e:
        raise ConnectionError(f"Couldn't reach MossRanking: {e}") from e
    players = [r for r in results if r.get("type") == "user" and r.get("id")]
    if not players:
        raise ValueError(
            f'No MossRanking player found matching "{text}". Try their exact '
            "username, their user ID, or a profile link."
        )
    exact = [p for p in players if p.get("label", "").lower() == text.lower()]
    if len(exact) == 1:
        return exact[0]["id"]
    if len(players) == 1:
        return players[0]["id"]
    names = ", ".join(p["label"] for p in players[:8])
    raise ValueError(f'Multiple MossRanking players match "{text}": {names}. Be more specific, or use a profile link.')


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="replace")


def _parse_rows(html):
    parser = _TableRowParser()
    parser.feed(html)
    return parser.rows


def scrape_profile(user_id):
    """Returns (pbs, username). pbs is {category_name: {"time", "rank",
    "rating", "date", "group"}} - "group" is which of PROFILE_GROUPS the
    category was actually found under (e.g. "Pacifist", "Bookskip"), kept
    for whenever a group/category filter gets built on top of this.
    username is the player's Moss Ranking display name (from the page's
    own "Profile: <name>" title), or None if it couldn't be found."""
    pbs = {}
    username = None

    for i, (g, group_name) in enumerate(PROFILE_GROUPS.items()):
        url = f"https://mossranking.com/user.php?id_user={user_id}&t=2&g={g}"
        try:
            html = _fetch(url)
        except urllib.error.URLError as e:
            raise ConnectionError(f"Couldn't reach Moss Ranking: {e}") from e

        if i == 0 and html.strip() == "No such account.":
            raise ProfileNotFound(f"No Moss Ranking profile found for user ID {user_id}")

        if username is None:
            m = TITLE_RE.search(html)
            if m:
                username = m.group(1).strip()

        for row in _parse_rows(html):
            cells = [c for c in row if c]
            if len(cells) < 5:
                continue
            date_idx = next((i for i, c in enumerate(cells) if DATE_RE.match(c)), None)
            if date_idx is None:
                continue
            try:
                category = cells[date_idx + 1]
                time_or_score = cells[date_idx + 2]
                rank = cells[date_idx + 3]
                rating = cells[date_idx + 4]
            except IndexError:
                continue
            if category in SKIP_CATEGORIES:
                continue
            if not TIME_RE.match(time_or_score):
                continue  # skip score-type rows (e.g. "$80,300")
            if not RANK_RE.match(rank):
                continue
            if category not in pbs:
                pbs[category] = {
                    "time": time_or_score, "rank": int(rank), "rating": rating,
                    "date": cells[date_idx], "group": group_name,
                }

    return pbs, username


def scrape_world_records():
    """Returns the current World Record holder + time for every category on
    mossranking.com/spelunky-hd/cats, grouped the same way the site groups
    them (Main Speed, Misc, Pacifist, etc.) - the same shape as the bundled
    world_records.json, so a refreshed scrape can replace it directly.

    Each cid page lists its rows as one <tr> per category with 7 <td>
    cells: [category, player, time, longevity, rank badge (no text), video
    link (no text), comment] - rank badge/video cells come through as
    empty strings, which is fine since only category/player/time are used.
    Header and <hr>-separator rows have fewer cells and are skipped
    naturally; score-only rows (Main Score) are skipped because their
    "time" cell doesn't match TIME_RE.
    """
    groups = {}
    for cid, group_name in CATS_GROUPS.items():
        url = f"https://mossranking.com/spelunky-hd/cats?cid={cid}"
        try:
            html = _fetch(url)
        except urllib.error.URLError as e:
            raise ConnectionError(f"Couldn't reach Moss Ranking: {e}") from e
        entries = {}
        for row in _parse_rows(html):
            if len(row) < 3:
                continue
            category, player, time_val = row[0], row[1], row[2]
            if not category or not TIME_RE.match(time_val):
                continue
            entries[category] = {"time": time_val, "player": player}
        groups[group_name] = entries
    return groups
