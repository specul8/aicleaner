"""Start Work and Start Personal (feature 152): two sets of project sessions, each started with one Go.

A run belongs to the session host, not to the page: closing the page or losing the tunnel does not stop it, and the
page shows where it got to when it comes back. A proxy restart ends it - sessions already asked to finish stay asked,
but nothing stops them afterwards (the panel says so).

What Go does with each session (spec 2026-10-27-f152-start-work-personal.md):
  - running, not in the set, idle: stopped at once;
  - running, not in the set, busy: sent a message asking it to wrap up, marked finishing, stopped once it is idle;
  - in the set and running: left as it is;
  - in the set and not running: started as slots allow, resuming its newest conversation (or fresh when it has none).

A busy session is never cut off: the only stops are of a session that is idle at that moment.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

log = logging.getLogger("aicleaner.presets")

DEFAULT_PRESETS: tuple[dict[str, Any], ...] = (
    {"id": "work", "name": "Start Work", "profiles": []},
    {"id": "personal", "name": "Start Personal", "profiles": []},
)
NAME_MAX = 40

FINISH_MESSAGE = ("Please finish what you are doing now - wrap up and stop at a safe point, summarise where things "
                  "are, and do not start anything new. This session is being closed to start {panel}.")
FINISH_PREFIX = FINISH_MESSAGE.split(" - ")[0]

# Item states, as the panel names them.
STOPPING, STOPPED, FINISHING, ANSWER = "stopping", "stopped", "finishing", "answer"
LEFT = "left"                          # was asked to finish; Cancel left it running
WAITING, STARTING, STARTED, RUNNING, FAILED = "waiting", "starting", "started", "running", "failed"
SETTLED = (STARTED, RUNNING, FAILED)


class PresetError(ValueError):
    """A panel's settings that cannot be saved, or a Go that cannot run."""


def default_presets() -> list[dict[str, Any]]:
    return [{**p, "profiles": []} for p in DEFAULT_PRESETS]


def limit_for(max_running: int) -> int | None:
    """How many projects a panel may hold: one fewer than the sessions allowed at once, so the set can start while
    another session is still finishing (Brad, 2026-10-27). None when there is no cap (max_running 0)."""
    return None if not max_running else max(0, int(max_running) - 1)


def normalise_presets(value: Any, max_running: int | None = None) -> list[dict[str, Any]]:
    """The two panels as stored: both ids present, in order, each with a name and its profiles (once each).
    With `max_running`, the max_running - 1 limit is enforced (none for 0); without it, nothing is refused for
    length - a config written before the cap was lowered still loads."""
    if value is None:
        value = []
    if not isinstance(value, list) or not all(isinstance(entry, dict) for entry in value):
        raise PresetError("session.presets is a list of {id, name, profiles}")
    known = {p["id"]: p for p in DEFAULT_PRESETS}
    given: dict[str, dict[str, Any]] = {}
    for entry in value:
        pid = str(entry.get("id", "")).strip().lower()
        if pid not in known:
            raise PresetError(f"session.presets has no panel called {pid or '(blank)'} - only "
                              + ", ".join(known))
        if pid in given:
            raise PresetError(f"session.presets names the {pid} panel twice")
        given[pid] = entry
    limit = limit_for(max_running) if max_running is not None else None
    out: list[dict[str, Any]] = []
    for pid, default in known.items():
        entry = given.get(pid, {})
        name = " ".join(str(entry.get("name", "") or "").split())[:NAME_MAX] or default["name"]
        profiles_in = entry.get("profiles") or []
        if not isinstance(profiles_in, list):
            raise PresetError(f"{name}: profiles is a list of project session names")
        profiles: list[str] = []
        for item in profiles_in:
            text = str(item or "").strip()
            if text and text.lower() not in {p.lower() for p in profiles}:
                profiles.append(text)
        if limit is not None and len(profiles) > limit:
            raise PresetError(f"{name}: at most {limit} project session{'s' if limit != 1 else ''} - one fewer than "
                              f"Sessions running at once ({max_running}), so one slot is kept free for these to start "
                              "while another finishes")
        out.append({"id": pid, "name": name, "profiles": profiles})
    return out


def is_idle(session: Any) -> bool:
    """Nothing in progress: no turn, no background task, no question waiting, nothing queued to send, not replaying
    after a re-attach. Anything else is busy, and a busy session is never stopped."""
    return bool(session.running and not session.busy and not session.background and not session.pending
                and not session.queued and not getattr(session, "_replaying", False))


class PresetRun:
    """One Go: the panel, its set, and what is happening to each session it touches."""

    def __init__(self, preset_id: str, panel: str, profiles: list[str]) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.preset = preset_id
        self.panel = panel
        self.profiles = list(profiles)
        self.state = "running"          # running | done | cancelled | replaced
        self.started_at = time.time()
        self.ended_at = 0.0
        self.items: dict[str, dict[str, Any]] = {}
        self.planned = False

    def set_item(self, name: str, state: str, reason: str = "", in_set: bool | None = None) -> None:
        item = self.items.setdefault(name, {"name": name, "state": state, "reason": "", "in_set": False})
        item["state"] = state
        item["reason"] = reason
        if in_set is not None:
            item["in_set"] = in_set

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "preset": self.preset, "panel": self.panel, "profiles": list(self.profiles),
                "state": self.state, "started_at": self.started_at, "ended_at": self.ended_at,
                "items": [dict(item) for item in self.items.values()]}


class PresetRunner:
    """The runs, held by the session host. One loop watches the current run and every session asked to finish; it
    ends itself once there is nothing left to watch."""

    TICK_S = 1.0

    def __init__(self, host: Any) -> None:
        self.host = host
        self.run: PresetRun | None = None
        # Sessions asked to finish, by profile name -> the panel that asked. Outlives a run: a replaced or finished
        # run's finishing sessions still stop when idle, unless a later set includes them or Cancel clears them.
        self.finishing: dict[str, str] = {}
        # Called before each start (set by the UI: the MCP audit and the memory check a page Start makes).
        self.before_start: Callable[[str], Awaitable[None]] | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    # ---- what the API calls ------------------------------------------------------------------

    def go(self, preset_id: str, panel: str, profiles: list[str]) -> dict[str, Any]:
        if self.run is not None and self.run.state == "running":
            self.run.state = "replaced"
            self.run.ended_at = time.time()
        self.run = PresetRun(preset_id, panel, profiles)
        self._ensure_loop()
        return self.state()

    def cancel(self) -> dict[str, Any]:
        """End the run, and clear every "finishing" mark: nothing is stopped by aicleaner after this."""
        if self.run is not None and self.run.state == "running":
            self.run.state = "cancelled"
            self.run.ended_at = time.time()
        if self.run is not None:
            for name in list(self.finishing):
                item = self.run.items.get(name)
                if item is not None and item["state"] in (FINISHING, ANSWER):
                    # Settled, so the panel lets the line go as it does a started one (Brad, 2026-10-29).
                    self.run.set_item(name, LEFT, "cancelled - no longer stopped by aicleaner")
        names = list(self.finishing)
        self.finishing.clear()
        for name in names:
            self._withdraw_now(name)
        self._wake.set()
        return self.state()

    def _withdraw_now(self, name: str) -> None:
        """A finish request not yet delivered is taken back: the session is no longer being closed, and an unsent
        "wrap up and stop" would still make Claude stop its work. One already delivered cannot be."""
        session = self._session(name)
        if session is None:
            return
        before = len(session.queued)
        session.queued = [item for item in session.queued if not item["text"].startswith(FINISH_PREFIX)]
        if len(session.queued) != before:
            session._queue_changed()

    def state(self) -> dict[str, Any]:
        return {"run": self.run.as_dict() if self.run is not None else None,
                "finishing": dict(self.finishing),
                "active": bool(self._task is not None and not self._task.done())}

    def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    # ---- the loop ----------------------------------------------------------------------------

    def _ensure_loop(self) -> None:
        self._wake.set()
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must not end the run; the next looks again
                log.exception("start-set run: a step failed")
            if not self._work_left():
                return
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.TICK_S)
            except asyncio.TimeoutError:
                pass

    def _work_left(self) -> bool:
        return bool(self.finishing) or (self.run is not None and self.run.state == "running")

    def _session(self, name: str) -> Any:
        return self.host.sessions.get(name)

    def _running_names(self) -> list[str]:
        return [n for n, s in self.host.sessions.items() if s.running]

    async def _tick(self) -> None:
        run = self.run
        if run is not None and run.state == "running" and not run.planned:
            await self._plan(run)
        await self._watch_finishing(run)
        if run is not None and run.state == "running" and run is self.run:
            await self._start_waiting(run)
            if all(item["state"] in SETTLED for item in run.items.values() if item["in_set"]):
                run.state = "done"
                run.ended_at = time.time()

    async def _plan(self, run: PresetRun) -> None:
        """Go's first look: every running session sorted into stop now, ask to finish, or leave; the set's others
        waiting for a slot."""
        run.planned = True
        wanted = {name.lower() for name in run.profiles}
        canonical: list[str] = []
        for name in run.profiles:
            try:
                canonical.append(self.host.profile(name).name)
            except Exception as exc:  # noqa: BLE001 - a profile renamed or removed since the panel was saved
                run.set_item(name, FAILED, str(exc), in_set=True)
        for name in canonical:
            if self.finishing.pop(name, None) is not None:      # in the new set: no longer to be stopped
                self._withdraw_now(name)
        for name in self._running_names():
            if name.lower() in wanted:
                continue
            session = self._session(name)
            if name in self.finishing:
                run.set_item(name, ANSWER if session.pending else FINISHING,
                             f"still finishing for {self.finishing[name]}", in_set=False)
                continue
            if is_idle(session):
                await self._stop(run, name)
                continue
            try:
                await session.prompt(FINISH_MESSAGE.format(panel=run.panel))
            except Exception as exc:  # noqa: BLE001 - asked or not, it is still only stopped once idle
                log.info("start-set run: could not ask %s to finish: %s", name, exc)
            self.finishing[name] = run.panel
            run.set_item(name, ANSWER if session.pending else FINISHING, "asked to finish", in_set=False)
        for name in canonical:
            session = self._session(name)
            if session is not None and session.running:
                run.set_item(name, RUNNING, "already running - left as it is", in_set=True)
            else:
                run.set_item(name, WAITING, "waiting for a free slot", in_set=True)

    async def _stop(self, run: PresetRun | None, name: str) -> None:
        if run is not None:
            run.set_item(name, STOPPING, "", in_set=False)
        try:
            await self.host.stop(name, reason="start-set")
        except Exception as exc:  # noqa: BLE001 - said on the panel
            if run is not None:
                run.set_item(name, FAILED, f"could not stop: {exc}")
            return
        if run is not None:
            run.set_item(name, STOPPED, "")

    async def _watch_finishing(self, run: PresetRun | None) -> None:
        for name in list(self.finishing):
            session = self._session(name)
            shown = run if (run is not None and name in run.items) else None
            if session is None or not session.running:
                self.finishing.pop(name, None)       # stopped by hand, or ended on its own
                if shown is not None:
                    shown.set_item(name, STOPPED, "")
                continue
            if is_idle(session):
                self.finishing.pop(name, None)
                await self._stop(shown, name)
            elif shown is not None:
                if session.pending:
                    shown.set_item(name, ANSWER, "answer it, or Stop it by hand")
                elif shown.items[name]["state"] != FINISHING:
                    shown.set_item(name, FINISHING, "asked to finish")

    async def _start_waiting(self, run: PresetRun) -> None:
        for name, item in list(run.items.items()):
            if not item["in_set"] or item["state"] != WAITING:
                continue
            if run is not self.run or run.state != "running":
                return
            session = self._session(name)
            if session is not None and session.running:
                run.set_item(name, RUNNING, "already running - left as it is")
                continue
            cap = self.host.max_running
            if cap and len(self._running_names()) >= cap:
                # A slot frees only when a session the run is stopping goes; with none left, it never will.
                if not any(n in self.finishing for n in self._running_names()):
                    run.set_item(name, FAILED, f"no free slot: {cap} sessions are running and none is finishing "
                                               "(Sessions running at once)")
                return
            run.set_item(name, STARTING, "")
            try:
                resume = await self._newest(name)
                if self.before_start is not None:
                    await self.before_start(name)
                await self.host.start(name, resume=resume)
            except Exception as exc:  # noqa: BLE001 - one failed start is said and the rest go on
                run.set_item(name, FAILED, str(exc) or exc.__class__.__name__)
                continue
            run.set_item(name, STARTED, "continuing its latest conversation" if resume else "a new conversation")

    async def _newest(self, name: str) -> str:
        """The newest conversation in the profile's list - the same list as Continue - or "" for none."""
        groups = await asyncio.to_thread(self.host.past, name, 1)
        rows = [row for group in groups or [] if not group.get("outside") for row in group.get("sessions") or []]
        if not rows:
            return ""
        return str(max(rows, key=lambda row: float(row.get("updated") or 0)).get("id") or "")


def runner_of(host: Any) -> PresetRunner:
    """The host's runner, made the first time it is asked for."""
    runner = getattr(host, "preset_runner", None)
    if runner is None:
        runner = PresetRunner(host)
        host.preset_runner = runner
    return runner
