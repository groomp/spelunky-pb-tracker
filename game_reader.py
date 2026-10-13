"""
Reads Spelunky HD's own live game state directly - a focused Python port
of spelunky-fyi/HD-Toolbox's Run Tracker
(https://github.com/spelunky-fyi/HD-Toolbox, Apache License 2.0 - the
process-memory offsets and overall approach below are ported from its
libs/mem_reader and src-tauri/src/tasks/session.rs), covering only the
fields this app's live splits need (screen state, level, timers, worm/
mothership flags) rather than its full entity/input/inventory tracking.

The one deliberate difference from HD Toolbox's own version: HD Toolbox
only records an area as complete when a poll happens to catch the game in
ScreenState.LEVEL_COMPLETED - a screen that can be on-screen for less time
than the ~16ms gap between polls (especially at speedrun pace, or under
any scheduling jitter under system load), silently and permanently
dropping that data with no retry - confirmed by reading its source, see
session.rs's RunState::update(). This reader instead treats the player's
area (derived from `level`) actually changing between two consecutive
polls - or reaching death/victory - as proof the previous area finished,
using the last time_pace we did observe for it. That can only be missed
if BOTH polls land inside that same few-millisecond transition window,
not just one of them.
"""

import sys
import threading
import time
from pathlib import Path

import pymem

PROCESS_NAME = "Spelunky.exe"
POLL_INTERVAL_SECONDS = 0.05

# Writable next to the actual exe when frozen (PyInstaller --onefile runs
# from a read-only temp extraction dir, see server.py's own APP_DIR for
# the same split), next to this file otherwise.
_APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
# TEMPORARY: per-poll timing diagnostics for tracking down the exact
# source of sub-millisecond drift in recovered sub-90s times (see
# SessionTracker.poll's _log_timing calls) - remove once that's resolved.
TIMING_DEBUG_LOG = _APP_DIR / "timing_debug.log"
# TEMPORARY: logs every (level, screen_state, total_time_ms) whenever
# screen_state isn't the plain "running" value - tracking down why Yama's
# own level never finalizes (area 4, slot 3 - see RunTracker's class
# docstring) on a genuine Hell-ending run. The real ending (killing Yama)
# might cycle through a screen state VICTORY_SCREENS doesn't include, or
# might never actually be caught by a 50ms poll - this records every raw
# value actually observed so that can be confirmed instead of guessed.
# Remove once that's resolved.
SCREEN_DEBUG_LOG = _APP_DIR / "screen_debug.log"

# Both known Spelunky HD executable versions - same two spelunky-fyi/
# HD-Toolbox supports (libs/mem_reader/src/constants.rs). global_state is
# a pointer (not a value) sitting at that fixed distance from the module's
# own base address; kali_accepts is a per-version address this build's own
# "KALI_ACCEPTS" string constant happens to live at, used only to confirm
# which version is actually running before trusting either offset.
OFFSETS_BY_VERSION = {
    "1.47": {"global_state": 0x15446C, "kali_accepts": 0x112D20},
    "1.4": {"global_state": 0x1384B4, "kali_accepts": 0x108BE4},
}
KALI_ACCEPTS_BYTES = "KALI_ACCEPTS".encode("utf-16-le") + b"\x00\x00"

# ScreenState values read straight from the game's own live state - matches
# spelunky-fyi/HD-Toolbox's own From<i32> for ScreenState mapping exactly
# (the gaps between values are other, unused screen IDs, not a mistake).
SCREEN_RUNNING = 0
SCREEN_LEVEL_COMPLETED = 11
SCREEN_VICTORY_WALKING = 18
SCREEN_VICTORY_ERUPTION = 19
SCREEN_VICTORY_OUTSIDE = 20
SCREEN_DEATH = 30
VICTORY_SCREENS = {SCREEN_VICTORY_WALKING, SCREEN_VICTORY_ERUPTION, SCREEN_VICTORY_OUTSIDE}
ACTIVE_SCREENS = {0, 1, 2, 3, SCREEN_LEVEL_COMPLETED} | VICTORY_SCREENS | {SCREEN_DEATH}


class GameNotFound(Exception):
    """Spelunky.exe isn't running (or couldn't be attached to)."""


class VersionNotRecognized(Exception):
    """Spelunky.exe is running, but isn't a build this recognizes."""


class GameState:
    __slots__ = (
        "screen_state", "level", "total_time_ms",
        "is_mothership", "is_worm", "is_city_of_gold", "respawn_level",
    )

    def __init__(
        self, screen_state, level, total_time_ms,
        is_mothership, is_worm, is_city_of_gold, respawn_level,
    ):
        self.screen_state = screen_state
        self.level = level
        self.total_time_ms = total_time_ms
        self.is_mothership = is_mothership
        self.is_worm = is_worm
        self.is_city_of_gold = is_city_of_gold
        self.respawn_level = respawn_level


class SpelunkyProcess:
    """Attaches to a running Spelunky.exe and reads one GameState snapshot
    at a time. Raises GameNotFound/VersionNotRecognized from __init__ -
    callers should retry (the process may just not be open yet)."""

    def __init__(self):
        self.pm = pymem.Pymem()
        try:
            self.pm.open_process_from_name(PROCESS_NAME)
        except Exception as e:
            raise GameNotFound(str(e)) from e
        try:
            self.base_addr = self.pm.process_base.lpBaseOfDll
        except Exception as e:
            raise GameNotFound(f"Couldn't locate the game's base module: {e}") from e
        self.offsets = self._detect_version()

    def _detect_version(self):
        for offsets in OFFSETS_BY_VERSION.values():
            try:
                data = self.pm.read_bytes(self.base_addr + offsets["kali_accepts"], 26)
            except Exception:
                continue
            if data == KALI_ACCEPTS_BYTES:
                return offsets
        raise VersionNotRecognized("Unrecognized Spelunky.exe build.")

    def close(self):
        try:
            self.pm.close_process()
        except Exception:
            pass

    def read_game_state(self):
        """Returns a GameState, or None when there's no single active
        player to read right now (menus, character select, multiplayer) -
        mirrors HD Toolbox's own InactiveScreenState/NoPlayers/Multiplayer
        handling, just collapsed to "nothing to report this poll"."""
        pm = self.pm
        global_state = pm.read_uint(self.base_addr + self.offsets["global_state"])

        screen_state = pm.read_int(global_state + 0x58)
        if screen_state not in ACTIVE_SCREENS:
            return None

        player_ptrs = pm.read_bytes(global_state + 0x440684, 16)
        active_slots = [
            i for i in range(4)
            if int.from_bytes(player_ptrs[i * 4:i * 4 + 4], "little") != 0
        ]
        if len(active_slots) != 1:
            return None  # no player seated yet, or multiplayer (unsupported, same as HD Toolbox)

        level = pm.read_uint(global_state + 0x4405D4)
        if level == 0:
            return None

        is_mothership = pm.read_uchar(global_state + 0x440602) != 0
        is_city_of_gold = pm.read_uchar(global_state + 0x440604) != 0
        is_worm = pm.read_uchar(global_state + 0x440606) != 0
        respawn_level = pm.read_uint(global_state + 0x44734C)

        total_minutes = pm.read_uint(global_state + 0x445940)
        total_seconds = pm.read_uint(global_state + 0x445944)
        total_ms = pm.read_double(global_state + 0x445948)
        total_time_ms = total_minutes * 60_000 + total_seconds * 1000 + int(total_ms)

        return GameState(
            screen_state=screen_state, level=level, total_time_ms=total_time_ms,
            is_mothership=is_mothership, is_worm=is_worm, is_city_of_gold=is_city_of_gold,
            respawn_level=respawn_level,
        )


class RunTracker:
    """Tracks both area completion/time_pace (what liveSplitsFromRunState
    actually reads) AND a per-level breakdown within each area - the
    latter exists purely so the per-level debug view can verify this
    reader's area-transition fix is actually catching every level, the
    same way counting them was how the original HD Toolbox per-level bug
    got diagnosed in the first place. See module docstring for why a
    level/area is detected via `level` changing instead of requiring a
    poll to catch ScreenState.LEVEL_COMPLETED.

    Each area gets 6 level slots (not 4) - only Ice Caves (area index 2)
    ever uses slots 4-5, for Mothership's two extra levels; see
    _resolve_target.

    Worm/Mothership/City of Gold are each one level OFF from what the
    game's own is_worm/is_mothership/is_city_of_gold flags suggest -
    confirmed live: the flag turns true on the level you find the
    ENTRANCE on (e.g. 2-1 for Worm, 3-4 for Mothership), not the real
    encounter itself (2-1/3-1 specifically can never actually BE the
    worm). So instead of trusting a level's own raw flag reads, whatever
    was raw-observed DURING a level only gets applied to the level AFTER
    it (see the _carry_*/_current_* split in update()) - the entrance
    level itself always ends up recorded with the flag off, and the real
    encounter, one level later, gets it instead.

    Area/slot placement for normal levels is tracked with a plain count
    of normal levels recorded so far (_normal_level_count), NOT derived
    from the game's own raw `level` number - that number just keeps
    incrementing by one for every level entered, bonus ones included, so
    after a Mothership detour it no longer lines up with the standard
    1-20 numbering at all (it's permanently shifted by however many extra
    levels Mothership added). Mothership's own 2 levels are genuinely
    EXTRA (Ice Caves still has its own un-detoured 3-1..3-4 first) -
    appended into slots 4-5 instead, without advancing the normal count
    at all.

    Worm is the opposite kind of exception: confirmed live, it's a single
    recorded level that's a shortcut standing in for what would otherwise
    have been 2 separate normal levels (it "counts as 2-2 and 2-3"
    combined, not two levels each individually flagged is_worm) - so the
    slot right after it never exists for this run at all. Recording the
    Worm advances _normal_level_count by 2 instead of 1, and the slot
    that gets jumped over is explicitly marked {"skipped": True} (see
    _mark_skipped) rather than left None, so the per-level debug view can
    tell "doesn't exist for this run" apart from a real missed level."""

    def __init__(self):
        self.died = False
        self.won = False
        self.visit_mothership = False
        self.visit_worm = False
        self.areas = [None] * 5  # None, or {"completed", "time_pace", "levels": [slot_or_None]*6}
        self._prev_level = None
        self._prev_total_time_ms = None
        self._level_start_time_ms = 0  # total_time_ms when the CURRENT level began
        # Raw-accumulated (OR'd) across every poll of the level currently
        # in progress - becomes the _carry_* for the level AFTER this one
        # once it ends (see update()), never applied to this level itself.
        self._current_is_mothership = False
        self._current_is_worm = False
        self._current_is_city_of_gold = False
        # What carried INTO the level currently in progress, from
        # whatever the level before it raw-accumulated - this (not
        # _current_is_*) is what the level in progress actually gets
        # recorded with.
        self._carry_mothership = False
        self._carry_worm = False
        self._carry_city_of_gold = False
        # How many levels have actually been RECORDED (see _record_level)
        # with each flag true - Mothership legitimately spans 2 levels
        # (its own encounter); Worm and City of Gold only ever 1 each
        # (Worm's one recorded level already stands in for 2 normal ones
        # - see class docstring). Past that count, any further true
        # reading is assumed to be the flag still lingering rather than a
        # real repeat.
        self._worm_recorded = 0
        self._mothership_recorded = 0
        self._city_of_gold_recorded = 0
        # Where the level currently in progress resolves to (area, slot,
        # flags, normal_count_advance - NOT is_last_slot, see
        # _is_last_slot's own docstring for why) - computed ONCE, the
        # first poll of this level that needs it, and reused for every
        # later poll of the SAME level (the fast path, and eventually the
        # level_changed block once it ends). Confirmed live this must
        # never be recomputed a second time from scratch: _record_level
        # bumps _worm_recorded/_mothership_recorded/_city_of_gold_recorded
        # the moment the fast path first writes this level, so a second,
        # independent _resolve_target call later (e.g. from the
        # level_changed block, deciding how much to advance
        # _normal_level_count) would see that cap already spent and
        # silently compute a different, wrong answer - exactly what
        # caused the Worm's skip-next-slot advance to go missing.
        self._pending_resolution = None
        # How many levels have been recorded into the normal main
        # sequence (area = count // 4, slot = count % 4) - see class
        # docstring for why this is a plain count rather than derived
        # from the game's own raw level number.
        self._normal_level_count = 0

    def to_run_state(self, current_total_time_ms):
        return {
            "time": current_total_time_ms,
            "areas": [
                {
                    "completed": a["completed"], "time_pace": a["time_pace"],
                    "levels": [dict(lv) if lv else None for lv in a["levels"]],
                }
                if a else None
                for a in self.areas
            ],
        }

    def _ensure_area(self, idx):
        if self.areas[idx] is None:
            self.areas[idx] = {"completed": False, "time_pace": None, "levels": [None] * 6}
        return self.areas[idx]

    def _next_mothership_slot(self):
        levels = self.areas[2]["levels"] if self.areas[2] else [None] * 6
        return 4 if levels[4] is None else 5

    def _mark_skipped(self, count):
        """Marks the normal slot at this count as not existing for this
        run (jumped over by the Worm's single combined level - see class
        docstring) rather than leaving it None, which would read as a
        real missed level to the per-level debug view."""
        area, slot = count // 4, count % 4
        a = self._ensure_area(area)
        if a["levels"][slot] is None:
            a["levels"][slot] = {"skipped": True}

    def _resolve_target(self, carry_worm, carry_mothership, carry_city_of_gold):
        """Given what's carrying into the level about to be recorded (see
        class docstring), returns (area, slot, is_worm, is_mothership,
        is_city_of_gold, normal_count_advance) it should be recorded as.
        normal_count_advance is 0 (Mothership, doesn't touch the normal
        sequence at all), 1 (an ordinary level), or 2 (the Worm's one
        combined level, standing in for 2).

        Deliberately does NOT decide is_last_slot - see _is_last_slot's
        own docstring for why that has to stay a fresh, uncached read."""
        is_worm = carry_worm and self._worm_recorded < 1
        if carry_mothership and self._mothership_recorded < 2:
            area, slot, is_mothership, advance = 2, self._next_mothership_slot(), True, 0
        else:
            area = self._normal_level_count // 4
            slot = self._normal_level_count % 4
            is_mothership, advance = False, (2 if is_worm else 1)
        is_city_of_gold = carry_city_of_gold and self._city_of_gold_recorded < 1
        return area, slot, is_worm, is_mothership, is_city_of_gold, advance

    def _is_last_slot(self, area, slot, is_mothership):
        """Whether this (area, slot) is the last one before its area
        completes - deliberately re-read fresh every time a level gets
        finalized (the fast path, AND the eventual level_changed
        fallback), never cached alongside the rest of a level's
        resolution: self.visit_mothership can flip from False to True
        PARTWAY through 3-4 itself (the moment the player actually finds
        the Mothership entrance, which can be well after 3-4's own
        resolution was first cached) - confirmed live, caching this
        would let 3-4 finalize as Ice Caves' own last slot before
        Mothership was ever detected, splitting Ice Caves as complete
        right as the player entered Mothership instead of after its own
        2 levels too."""
        return (
            (area == 2 and slot == 5) if is_mothership
            # Ice Caves' own slot 3 (3-4) isn't its last slot after all
            # once Mothership's been visited - its own 2 levels (above)
            # still have to follow before the area's actually done.
            else (slot == 3 and not (area == 2 and self.visit_mothership))
        )

    def _record_level(self, area, slot, end_time_ms, died, is_worm, is_mothership, is_city_of_gold):
        a = self._ensure_area(area)
        if a["levels"][slot] is not None:
            return
        duration = max(0, end_time_ms - self._level_start_time_ms)
        a["levels"][slot] = {
            "completed": not died, "time": duration,
            # Cumulative-since-run-start, same idea as an area's own
            # time_pace just one level finer - lets a sub-split within a
            # combined area (e.g. Temple (4-3), slot 2 of area index 3,
            # before Olmec's own slot 3) be read directly off a single
            # level entry instead of summing durations (the exact pattern
            # that made area.time unreliable - see liveSplitsFromRunState).
            "time_pace": end_time_ms,
            "is_worm": is_worm, "is_mothership": is_mothership, "is_city_of_gold": is_city_of_gold,
        }
        if is_worm:
            self._worm_recorded += 1
        if is_mothership:
            self._mothership_recorded += 1
        if is_city_of_gold:
            self._city_of_gold_recorded += 1

    def update(self, gs: GameState):
        level = gs.level
        level_changed = self._prev_level is not None and level != self._prev_level

        if level_changed:
            # The level that's ending gets recorded/finalized using
            # whatever it resolved to the FIRST time it was ever
            # resolved (cached in _pending_resolution, set below) - never
            # recomputed fresh here. Recomputing it here via a second,
            # independent _resolve_target call is exactly what caused the
            # Worm's skip-next-slot advance to silently go missing: the
            # fast path below had already recorded this level and bumped
            # _worm_recorded, so a second resolve would see that cap
            # already spent and compute is_worm=False / advance=1 instead
            # of the correct is_worm=True / advance=2.
            prev_area, prev_slot, prev_is_worm, prev_is_mothership, prev_is_city_of_gold, prev_advance = \
                self._pending_resolution if self._pending_resolution is not None \
                else self._resolve_target(self._carry_worm, self._carry_mothership, self._carry_city_of_gold)
            self._record_level(
                prev_area, prev_slot, self._prev_total_time_ms, died=False,
                is_worm=prev_is_worm, is_mothership=prev_is_mothership, is_city_of_gold=prev_is_city_of_gold,
            )
            if prev_advance == 2:
                self._mark_skipped(self._normal_level_count + 1)
            self._normal_level_count += prev_advance
            if self._is_last_slot(prev_area, prev_slot, prev_is_mothership):
                self._complete_area(prev_area, self._prev_total_time_ms, died=False)
            self._level_start_time_ms = self._prev_total_time_ms

            # Whatever raw-accumulated DURING the level that just ended
            # carries forward into the level starting now - fresh raw
            # accumulators for IT, folded in from this very poll below.
            self._carry_worm = self._current_is_worm
            self._carry_mothership = self._current_is_mothership
            self._carry_city_of_gold = self._current_is_city_of_gold
            self._current_is_worm = False
            self._current_is_mothership = False
            self._current_is_city_of_gold = False
            self._pending_resolution = None  # a fresh level is starting now

        if gs.is_worm:
            self._current_is_worm = True
            self.visit_worm = True
        if gs.is_mothership:
            self._current_is_mothership = True
            self.visit_mothership = True
        if gs.is_city_of_gold:
            self._current_is_city_of_gold = True

        # Resolved ONCE per level (see _pending_resolution's own comment)
        # and reused for every poll of this same level from here on.
        if self._pending_resolution is None:
            self._pending_resolution = \
                self._resolve_target(self._carry_worm, self._carry_mothership, self._carry_city_of_gold)
        area, slot, is_worm, is_mothership, is_city_of_gold, _advance = self._pending_resolution
        level_flags = dict(is_worm=is_worm, is_mothership=is_mothership, is_city_of_gold=is_city_of_gold)

        # Fast path: this poll actually caught ScreenState.LEVEL_COMPLETED
        # directly (gs.level is still the level that just finished while
        # this screen is up - same as HD Toolbox's own original read).
        # Recording right here, using THIS poll's timers, is what makes
        # the split land the instant the transition screen appears -
        # including if you then stand on it for a while to look at your
        # pace, since total_time_ms is a live clock that keeps counting
        # through that screen. Without this, the only other place a level
        # ever gets recorded is the fallback above, using whatever
        # total_time_ms happened to be at the LAST poll before `level`
        # actually changed - which, if you lingered on the transition
        # screen, is inflated by however long you stood there, silently
        # padding the level/area split that just finished. The fallback
        # still exists for whenever this fast path gets missed (a level
        # transition too quick for any poll to land on it) - whichever of
        # the two happens first wins, via _record_level/_complete_area's
        # own idempotency guards.
        if gs.screen_state == SCREEN_LEVEL_COMPLETED:
            self._record_level(area, slot, gs.total_time_ms, died=False, **level_flags)
            if self._is_last_slot(area, slot, is_mothership):
                self._complete_area(area, gs.total_time_ms, died=False)

        if gs.screen_state == SCREEN_DEATH:
            self._record_level(area, slot, gs.total_time_ms, died=True, **level_flags)
            self._complete_area(area, gs.total_time_ms, died=True)
        elif gs.screen_state in VICTORY_SCREENS:
            self._record_level(area, slot, gs.total_time_ms, died=False, **level_flags)
            self._complete_area(area, gs.total_time_ms, died=False)

        self._prev_level = level
        self._prev_total_time_ms = gs.total_time_ms

    def _complete_area(self, idx, time_pace, died):
        a = self._ensure_area(idx)
        if a["completed"] or a["time_pace"] is not None:
            return
        a["completed"] = not died
        a["time_pace"] = time_pace
        if died:
            self.died = True


# Confirmed live, across three genuine (non-skip) sub-90s clears: the game
# snaps total_time_ms to an obviously-fake flat sentinel - reads back as
# exactly 99 minutes, 0 seconds, 0 ms - once it detects a run finishing in
# under 90 real seconds. Not display-only: the same raw fields
# game_reader.py reads get overwritten, and it never reverts within that
# run - confirmed, it reads a perfectly flat 5,940,000ms on every poll
# afterward.
#
# What actually happens, confirmed from raw per-poll logs of two separate
# genuine clears: the real clock advances completely normally right up to
# the true finish, then freezes dead flat (no change at all) for roughly
# 1-1.5 real seconds, and ONLY THEN jumps straight to the sentinel. The
# value right before that freeze began matched the real in-game time (read
# frame-by-frame off footage) within ~100ms both times - the freeze and
# the eventual jump to the sentinel are both just dead transition/anti-
# cheat-processing time with no further real gameplay happening, not
# something that needs crediting.
#
# TWO EARLIER versions of this guard got that wrong in opposite
# directions:
#   1. Flagging any poll where total_time_ms jumped by far more than real
#      wall-clock time could justify, and resuming a synthetic wall-clock
#      driven timer from there - this mistook ordinary level-load pauses
#      and alt-tabbing (which also freeze-then-jump, just not all the way
#      to the sentinel) for corruption, and worse, once it mis-fired it
#      compared every later poll against an increasingly-stale baseline
#      instead of ever resyncing, so small errors compounded for the rest
#      of the run.
#   2. Detecting the sentinel by its actual value (>= SENTINEL_THRESHOLD_MS,
#      unambiguous - no real run gets anywhere close) fixed that, but then
#      kept ADVANCING a synthetic wall-clock timer once triggered, same as
#      approach 1 - which overshot the true finish time by crediting the
#      dead freeze-then-jump gap as if it were real elapsed time (observed:
#      a real 37.8s clear recorded as 38.257s - the ~450ms gap between the
#      last frozen reading and the poll that caught the sentinel, wrongly
#      added on top).
#
# The fix: once the sentinel is detected, freeze at the last trusted
# reading (self._prev_total_time_ms from the poll immediately before) and
# hold there for the rest of the run - add nothing further. Confirmed one
# earlier test (using a level-SKIP tool, not a genuine clear) seemed to
# need continued advancement instead, but skip tools manipulate game state
# in ways that may not reflect genuine corruption timing, and two
# independent genuine clears now consistently show holding is correct - if
# a genuine run is ever found where the sentinel strikes mid-level while
# real play continues well past it, this will need revisiting with that
# run's own data in hand, not fixed blind.
SENTINEL_THRESHOLD_MS = 30 * 60_000  # 30 minutes - ~3x any realistic run, ~3x under the true 99-minute sentinel


class SessionTracker:
    """Owns one RunTracker, swapping in a fresh one whenever the game's
    own total_time_ms visibly resets (a new attempt starting) or its
    respawn_level changes (restarting via a checkpoint/seed) - same two
    "this is a new run" signals HD Toolbox's own SessionState::update
    uses, unrelated to the area-detection fix above. Also guards against
    the under-90-second sentinel (see SENTINEL_THRESHOLD_MS) by freezing
    at the last trusted reading once the game's own clock hits that
    obviously-fake value."""

    def __init__(self):
        self.run = RunTracker()
        self._prev_total_time_ms = None
        self._prev_respawn_level = None
        self._sentinel_triggered = False
        self._frozen_total_time_ms = None  # the one value used for every poll once triggered
        self._log_file = None
        self._screen_log_file = None

    def _log_timing(self, raw_total_time_ms, sentinel_triggered, final_total_time_ms):
        # TEMPORARY - see TIMING_DEBUG_LOG. Opened lazily (not in __init__)
        # so a SessionTracker that's created but never actually polled
        # (shouldn't happen, but cheap to guard) never touches disk.
        if self._log_file is None:
            self._log_file = open(TIMING_DEBUG_LOG, "a", encoding="utf-8")
            self._log_file.write("wall_clock_s,raw_total_time_ms,sentinel_triggered,final_total_time_ms\n")
        self._log_file.write(
            f"{time.monotonic():.6f},{raw_total_time_ms},{sentinel_triggered},{final_total_time_ms}\n"
        )
        self._log_file.flush()

    def _log_screen(self, level, screen_state, total_time_ms):
        # TEMPORARY - see SCREEN_DEBUG_LOG.
        if self._screen_log_file is None:
            self._screen_log_file = open(SCREEN_DEBUG_LOG, "a", encoding="utf-8")
            self._screen_log_file.write("wall_clock_s,level,screen_state,total_time_ms\n")
        self._screen_log_file.write(f"{time.monotonic():.6f},{level},{screen_state},{total_time_ms}\n")
        self._screen_log_file.flush()

    def poll(self, gs: GameState):
        raw_total_time_ms = gs.total_time_ms
        is_new_run = self._prev_total_time_ms is not None and (
            (gs.total_time_ms < 5000 and gs.total_time_ms < self._prev_total_time_ms)
            or (self._prev_respawn_level is not None and gs.respawn_level != self._prev_respawn_level)
        )
        if is_new_run:
            self.run = RunTracker()
            self._sentinel_triggered = False
            self._frozen_total_time_ms = None
        elif gs.total_time_ms >= SENTINEL_THRESHOLD_MS and not self._sentinel_triggered:
            # First poll to see the sentinel - freeze here, at the last
            # trusted reading, not this (fake) one.
            self._sentinel_triggered = True
            self._frozen_total_time_ms = self._prev_total_time_ms or 0

        if self._sentinel_triggered and not is_new_run:
            gs.total_time_ms = self._frozen_total_time_ms

        self._log_timing(raw_total_time_ms, self._sentinel_triggered, gs.total_time_ms)
        if gs.screen_state != SCREEN_RUNNING:
            self._log_screen(gs.level, gs.screen_state, gs.total_time_ms)
        self.run.update(gs)
        self._prev_total_time_ms = gs.total_time_ms
        self._prev_respawn_level = gs.respawn_level
        return self.run.to_run_state(gs.total_time_ms)


def poll_loop(on_update, on_status, stop_event: threading.Event):
    """Runs forever (until stop_event is set) on its own thread - pymem's
    underlying Win32 calls are blocking, so this can't share the asyncio
    event loop server.py runs everything else on. on_update(run_state_dict)
    fires on every successful poll with a player active; on_status(str)
    fires on connect/disconnect/error transitions only, not every tick,
    the same "don't spam the log" shape as server.py's own upstream_loop."""
    session = SessionTracker()
    process = None
    last_status = None

    def set_status(status):
        nonlocal last_status
        if status != last_status:
            on_status(status)
            last_status = status

    while not stop_event.is_set():
        if process is None:
            try:
                process = SpelunkyProcess()
                set_status("connected")
            except GameNotFound:
                set_status("waiting")
                time.sleep(2)
                continue
            except VersionNotRecognized as e:
                set_status(f"unsupported: {e}")
                time.sleep(5)
                continue

        try:
            gs = process.read_game_state()
        except Exception:
            # Most likely the game just closed - drop this handle and
            # retry attaching from scratch rather than assuming anything
            # about which read failed or why.
            process.close()
            process = None
            set_status("waiting")
            time.sleep(1)
            continue

        if gs is not None:
            on_update(session.poll(gs))

        time.sleep(POLL_INTERVAL_SECONDS)

    if process is not None:
        process.close()
