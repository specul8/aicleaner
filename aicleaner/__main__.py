"""python -m aicleaner {run|check|tokenise|rehydrate|scan|scan-tokens|set-key|seed|split|detail|mcp-audit} [--config aicleaner-config.json]

scan is the token builder: it reports what a folder would leak, and with --add writes those
identifiers into a client's mapping. preflight does the same for a repo AND the Claude Code
transcripts for it, and says whether resuming that work through the proxy is safe yet. seed fills a
client from the tenant it is signed in to.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
from collections import Counter
from pathlib import Path

from .config import APP_ROOT, CONFIG_NAME, LEGACY_CONFIG_NAME, Config
from .mapping import SOURCE_SCAN, Mapper, MappingError


def _config_folders() -> list[Path]:
    """Where the config is looked for: the working folder, then the app root."""
    return [Path.cwd(), APP_ROOT]


def _default_config() -> str | None:
    for folder in _config_folders():
        if (folder / CONFIG_NAME).exists():
            return str(folder / CONFIG_NAME)
    return None


def _legacy_config() -> Path | None:
    for folder in _config_folders():
        if (folder / LEGACY_CONFIG_NAME).exists():
            return folder / LEGACY_CONFIG_NAME
    return None


def cmd_check(cfg: Config, sample: str | None) -> int:
    from .detect import is_public

    st = _store(cfg)
    try:
        mapper = st.mapper()
    except MappingError as exc:
        print(f"MAPPING INVALID: {exc}", file=sys.stderr)
        return 2
    print(f"mapping OK: {', '.join(sorted({p.name for p in st.load().sources.values()}))}")
    print(f"  clients: {', '.join(mapper.clients) or '(none)'}")
    if not mapper.forms:
        print("  WARNING: the mapping is empty; nothing is tokenised until forms are added", file=sys.stderr)
    print(f"  forms:   {len(mapper.forms)}")
    public = sorted({f.real for f in mapper.forms if is_public(f.real)})
    if public:
        print(f"  NOTE: {len(public)} form(s) are public identifiers (the same in every tenant): "
              f"{', '.join(public[:3])}{' ...' if len(public) > 3 else ''}")
        print("        removing them gives the model back a fact it already knows", file=sys.stderr)
    print(f"  longest token: {mapper.max_token_len} chars (stream tail buffer = {mapper.max_token_len - 1})")
    print(f"  dialects: {', '.join(f'{k} -> {v}' for k, v in sorted(cfg.upstreams.items()))}")
    if sample is not None:
        out: Counter[str] = Counter()
        tok = mapper.tokenise(sample, out)
        back = mapper.rehydrate(tok)
        print("  sample tokenised:", tok)
        print("  substitutions:  ", dict(out))
        if back == sample:
            print("  round-trip:      OK")
        elif back.lower() == sample.lower():
            print("  round-trip:      OK (case normalised to the first-declared form)")
        else:
            print("  round-trip:      MISMATCH")
        leak = mapper.find_real(tok)
        print("  canary:         ", "clean" if not leak else f"LEAK {leak}")
    return 0


def cmd_pipe(cfg: Config, direction: str) -> int:
    mapper = _mapper(cfg)
    text = sys.stdin.read()
    fn = mapper.tokenise if direction == "tokenise" else mapper.rehydrate
    sys.stdout.write(fn(text))
    return 0


def _store(cfg: Config):
    """The mapping store the proxy uses: per-client files, Global and learned case variants."""
    from .store import MappingStore

    return MappingStore(Path(cfg.mapping), Path(cfg.clients_dir), Path(cfg.state_dir))


def _mapper(cfg: Config):
    """The mapping as the proxy sees it, tolerant of one bad client file."""
    loaded, _reports = _store(cfg).load_tolerant(record=False)
    mapper = Mapper.from_dict(loaded.data)
    mapper.ignore = loaded.ignore
    return mapper


def _say_would_change(say, rows, high, add_to: str, skipped, unvouched) -> None:
    """What a scan would put in the mapping, before it puts it there (feature 29).

    Every collection rule so far was found by running a scan and reading what it produced - 1,733
    forms, then 480, then a RecursionError, then 692 of which 120 named the client. Each time the
    feedback came *after* the mapping had been written, and twice the remedy was to delete the
    client and start again.

    The scan has the finds and the mapping before it writes anything, so it can say what it is
    about to do. `--add` becomes a confirmation rather than a leap, and a rule that is wrong
    costs a glance instead of a rebuild.
    """
    from collections import Counter

    if not rows:
        return
    by_kind: Counter[str] = Counter(row.kind for row in high)
    named = sum(1 for row in high if row.client)
    say("")
    say(f"what --add would put in the mapping: {len(high)} of {len(rows)} finds")
    for kind, count in by_kind.most_common():
        # Domains no longer go in at all, so saying "12 domains" without that would be a lie.
        tail = "  -> Review, not the mapping (D3)" if kind == "domain" else ""
        say(f"  {count:5} {kind}{tail}")
    say(f"  {named:5} of them carry a name this mapping already knows")
    if skipped:
        worst = ", ".join(f"{count} {reason}" for reason, count in skipped.most_common(3))
        say(f"  not read at all: {worst}")
    if unvouched:
        say(f"  {len(set(unvouched))} held back by Policy C (machine-written, no client name in sight)")
    if not add_to:
        say("  a sample is above; --top raises how much is shown")


def _queue_for_review(cfg, roots, client: str, rows, queue_store=None) -> int:
    """Put finds on the Review queue rather than into the mapping (defect D3).

    The same store the proxy uses when there is one: two CandidateStore objects over one file each
    write the whole thing from their own copy, so the second save would drop what the first had
    learned and the running proxy would not see these until it restarted.
    """
    import socket
    from pathlib import Path

    from .detect import CandidateStore

    store = queue_store or CandidateStore(
        Path(cfg.mapping).parent / f"candidates-{socket.gethostname().split('.')[0].lower()}.jsonl")
    store.quiet_kinds = frozenset(cfg.quiet_kinds)   # a scan honours a quieted kind too (31)
    where = ", ".join(sorted({root.name for root in roots}))
    queued = 0
    for row in rows:
        if store.offer(row.value, row.kind, row.confidence, client, row.count,
                       f"scan {where}" if where else ""):
            queued += 1
    return queued


def _say_unvouched(say, unvouched: list[str]) -> None:
    """What was found in machine-written files and left out, because silence would be worse.

    A diagnostic dump or a portal export is mostly somebody else's infrastructure, so a find in one
    is only kept when it visibly belongs to a client already in the mapping. That is only as good
    as the names declared: the 28 client mailboxes this rule nearly lost were vouched for by a
    domain the person running the scan had not added yet. Saying so turns a silent loss into a
    prompt.
    """
    if not unvouched:
        return
    distinct = sorted(set(unvouched))
    say(f"{len(distinct)} find(s) in machine-written files carried no name this mapping knows, so "
        f"they were left out (for example: {', '.join(distinct[:5])}).")
    say("If one of your clients' names is missing from the mapping, add it and scan again.")


def _scan_roots(cfg: Config, roots: list[Path], min_count: int, add_to: str, queue: str | None,
                top: int, preflight: bool = False, out=None, queue_store=None, on_file=None,
                resource_names: bool = False, on_write=None) -> int:
    """The body shared by scan and preflight: report, add or queue, then say where that leaves us.

    `out` collects the report instead of printing it, so a page can run the same scan the
    command line runs and show the same words back rather than a second implementation of it.

    `on_write(client, done, total)` is called around the writing, which is a phase of its own and
    used to be invisible: the bar reached 100% at the end of reading and then sat there.
    """
    say = out if out is not None else print
    from .detect import CandidateStore
    from .scan import format_candidates, iter_text_files, routing_overrides, scan_candidates

    overrides = routing_overrides(roots[0])
    for path, key, value in overrides:
        say(f"ROUTING OVERRIDE {path}: {key} = {value}  (requests from this project would bypass aicleaner)")
    store = _store(cfg)
    mapper = _mapper(cfg)
    from collections import Counter

    from .mapping import CASE_INSENSITIVE_KINDS
    from .scan import Candidate

    # Case variants of a host, GUID or address are the same identifier: mapping one covers them all,
    # so they are reported once, under the spelling seen first.
    merged: dict[tuple[str, str, str, str], list] = {}
    skipped: Counter[str] = Counter()
    unvouched: list[str] = []
    markers = list(cfg.scan_generated_markers) or None
    for root in roots:
        for row in scan_candidates(root, mapper, min_count=1, skipped=skipped, on_file=on_file,
                                   resource_names=resource_names, markers=markers,
                                   unvouched=unvouched):
            folded = row.value.lower() if row.kind in CASE_INSENSITIVE_KINDS else row.value
            key = (folded, row.kind, row.confidence, row.client)
            if key in merged:
                merged[key][1] += row.count
            else:
                merged[key] = [row.value, row.count]
    rows = [Candidate(value, kind, confidence, client, count)
            for (_folded, kind, confidence, client), (value, count) in merged.items() if count >= min_count]
    rows.sort(key=lambda c: (c.confidence != "high", -c.count, c.value.lower()))
    high = [r for r in rows if r.confidence == "high"]
    if add_to:
        _say_would_change(say, rows, high, add_to, skipped, unvouched)
        # Domains go to Review rather than straight into the mapping (defect D3, decided
        # 2026-09-27). Measured: of 419 domains a scan would collect across client projects, 43
        # named the client. The rest were vendors, documentation fixtures and the user's own
        # employer - a Terraform file listing allowed SaaS, a committed Defender allowlist. One
        # repository produced 163 domains of which 61 were Salesforce case-routing hosts.
        #
        # In practice this is every high-confidence domain: a scan does not attribute a domain to
        # a client even when it visibly carries their name - `fabrikam-portal.nwtraders.biz`
        # comes back with `client=''` like the rest. The `not r.client` guard is kept so that
        # attribution, if it is ever added, automatically keeps those 10% out of the queue
        # without this line having to be found again.
        #
        # Reviewing a domain that obviously belongs to the client costs one click, so the simple
        # rule is the right one until the measurement says otherwise.
        review_first = [r for r in high if r.kind == "domain" and not r.client]
        high = [r for r in high if not (r.kind == "domain" and not r.client)]
        # One write per client, not one per find. Adding singly re-read every client file, rebuilt
        # the whole trie and rewrote the client document for each of 1,733 forms - about an hour,
        # and the synced folder was handed 1,733 versions of a file being written once a second.
        wanted: dict[str, list[str]] = {}
        for row in high:
            wanted.setdefault(row.client or add_to, []).append(row.value)
        added, failed = 0, 0
        for client, values in wanted.items():
            if on_write is not None:
                on_write(client, 0, len(values))
            try:
                for real, token in store.add_forms(client, values, origin="scan",
                                                   source=SOURCE_SCAN):
                    say(f"  {real!r:45} -> {token}  ({client})")
                    added += 1
            except MappingError as exc:
                say(f"  skipped {len(values)} form(s) for {client}: {exc}")
                failed += len(values)
            if on_write is not None:
                on_write(client, len(values), len(values))
        say(f"added {added} form(s){f', {failed} skipped' if failed else ''}; run again to confirm")
        if review_first:
            queued = _queue_for_review(cfg, roots, add_to, review_first, queue_store)
            say(f"{queued} domain(s) went to Review instead of straight in (D3): about one in ten "
                f"of the domains in a client project name the client, and the rest are vendors, "
                f"documentation fixtures and your own employer.")
            for row in review_first[:8]:
                say(f"  {row.value}")
            if len(review_first) > 8:
                say(f"  ... and {len(review_first) - 8} more, on the Review page")
        _say_unvouched(say, unvouched)
        return 1 if (failed or overrides) else 0
    if queue is not None:
        from .detect import SeenContext

        # The proxy's own store when there is one. Two CandidateStore objects over one file each
        # write the whole thing from their own copy, so the second save drops whatever the first
        # had learned - and the running proxy would not see these finds until it restarted.
        candidates = queue_store or CandidateStore(
            Path(cfg.mapping).parent / f"candidates-{socket.gethostname().split('.')[0].lower()}.jsonl")
        candidates.quiet_kinds = frozenset(cfg.quiet_kinds)   # a scan honours a quieted kind too (31)
        # Pointing a scan at a client's repo is a statement of ownership: you knew whose it was
        # before you started. Without it, every find has to have that decided again by hand, once
        # each, in a queue that can hold hundreds.
        new = 0
        for root in roots:
            seen = SeenContext(clients={queue: 1}, project=root.name) if queue else None
            for path in iter_text_files(root, None, on_file):
                new += candidates.record(path.read_text(encoding="utf-8", errors="replace"), mapper,
                                         f"scan {path.name}", cfg.placeholder_kinds, seen)
        whose = f" for {queue}" if queue else ""
        say(f"{new} new candidate(s) queued for review in the UI{whose}")
        return 1 if overrides else 0
    if skipped and (preflight or rows):
        # Blind spots are worth naming: a file that was not read cannot be checked.
        worst = ", ".join(f"{count} {reason}" for reason, count in skipped.most_common(4))
        say(f"not read: {worst}")
        say("  (binaries, and anything over the size limit; convert documents to text to include them)")
    _say_unvouched(say, unvouched)
    if rows:
        where = "in this repo and its history" if preflight else f"under {roots[0]}"
        say(f"unmapped identifiers {where} (occurrences >= {min_count}); "
              f"{len(high)} high confidence of {len(rows)}:")
        say("\n".join(format_candidates(rows, top=top)))
    if not preflight:
        if not rows:
            say(f"no unmapped candidates in {roots[0]} (min occurrences {min_count})")
        else:
            _say_would_change(say, rows, high, "", skipped, unvouched)
            say("")
            say("map them with --add CLIENTID, or --queue to review them in the UI")
        return 1 if overrides else 0
    if high or overrides:
        reason = f"{len(high)} identifier(s) would go out or be held back" if high else "the routing is overridden"
        say(f"NOT READY: {reason}")
        say(f"  map them:  aicleaner preflight {roots[0]} --add CLIENTID   (or --queue to sort them in the UI)")
        return 1
    say("READY: every high-confidence identifier here is mapped; resuming this work is safe")
    if rows:
        say("  the rest are low confidence (capitalised phrases and the like); Review can sort them later")
    return 0


# What marks a progress line on stderr. A record separator, so it cannot be confused with
# anything a traceback or a warning would print on the same stream.
PROGRESS_MARK = "\x1eprogress "


def _progress_channel():
    """(on_file, on_write) that write one JSON line per event to stderr, or (None, None).

    The Session and Clients pages show a live count while a scan runs, and a scan started from a
    page now runs as a separate process (D14) - so the numbers have to cross a pipe. stderr,
    because stdout is the report the page prints verbatim.

    Counting every file individually would write a line per file; the parent only draws a bar, so
    this reports bytes and files as running totals and lets the reader coalesce.
    """
    import json as _json

    def emit(kind: str, **fields) -> None:
        try:
            sys.stderr.write(PROGRESS_MARK + _json.dumps({"kind": kind, **fields}) + chr(10))
            sys.stderr.flush()
        except OSError:
            pass          # the parent has gone: the scan itself is unaffected

    def on_file(path: Path, size: int) -> None:
        emit("file", name=path.name, size=int(size))

    def on_write(client: str, done: int, total: int) -> None:
        emit("write", client=client, done=int(done), total=int(total))

    return on_file, on_write


def cmd_scan(cfg: Config, root: str, min_count: int, add_to: str, queue: str | None, top: int,
             resource_names: bool = False, progress: bool = False) -> int:
    """Report (and optionally map) identifiers a folder holds that the mapping does not know."""
    on_file, on_write = _progress_channel() if progress else (None, None)
    return _scan_roots(cfg, [Path(root)], min_count, add_to, queue, top,
                       resource_names=resource_names, on_file=on_file, on_write=on_write)


def cmd_preflight(cfg: Config, root: str, min_count: int, add_to: str, queue: str | None, top: int,
                  resource_names: bool = False) -> int:
    """Is it safe to resume this work through the proxy? Scan the repo and its Claude Code history.

    Nothing needs converting: the proxy tokenises the whole request, history included, so a resumed
    session goes out in tokens from the first turn. What matters is whether the mapping knows the
    identifiers that history holds, which is what this checks.
    """
    from .scan import transcript_folders

    repo = Path(root)
    if not repo.is_dir():
        print(f"not a folder: {repo}", file=sys.stderr)
        return 2
    folders = transcript_folders(repo)
    print(f"preflight for {repo}")
    if folders:
        for folder in folders:
            sessions = len(list(folder.glob("*.jsonl")))
            print(f"  history: {folder}  ({sessions} session{'s' if sessions != 1 else ''})")
    else:
        print("  history: none found under %USERPROFILE%\\.claude\\projects (nothing has been "
              "worked on here with Claude Code yet)")
    return _scan_roots(cfg, [repo, *folders], min_count, add_to, queue, top, preflight=True,
                       resource_names=resource_names)


def cmd_scan_tokens(cfg: Config, root: str) -> int:
    from .scan import scan_tokens

    mapper = _mapper(cfg)
    hits = scan_tokens(Path(root), mapper)
    if not hits:
        print(f"no token residue under {root}")
        return 0
    print(f"token residue under {root}: {len(hits)} line(s) still carry a token instead of the real value")
    for path, no, token, line in hits:
        print(f"  {path}:{no}  [{token}]  {line}")
    return 1


def cmd_set_key(cfg: Config, dialect: str, from_stdin: bool) -> int:
    """Store the upstream API key for a dialect at the path aicleaner-config.json names."""
    from getpass import getpass

    from .secrets import write_secret

    spec = cfg.credentials.get(dialect)
    if not spec or not spec.get("upstream_key_file"):
        print(f"aicleaner-config.json has no credentials.{dialect}.upstream_key_file", file=sys.stderr)
        return 2
    secret = sys.stdin.read() if from_stdin else getpass(f"{dialect} API key (input hidden): ")
    if not secret.strip():
        print("empty key; nothing written", file=sys.stderr)
        return 2
    written = write_secret(Path(spec["upstream_key_file"]), secret)
    print(f"stored {dialect} upstream key at {written}")
    return 0


def cmd_seed(cfg: Config, client_id: str, index: int | None, resources: bool) -> int:
    from .seed import seed

    if client_id in (".", ""):
        print("usage: aicleaner seed CLIENTID [--index N] [--resources]", file=sys.stderr)
        return 2
    try:
        added = seed(Path(cfg.mapping), client_id, index, Path(cfg.clients_dir), resources=resources,
                     state_dir=Path(cfg.state_dir))
    except RuntimeError as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        return 2
    if not added:
        print(f"{client_id}: nothing new to add")
        return 0
    print(f"{client_id}: added {len(added)} form(s)")
    for real, tok in added:
        print(f"  {real!r:45} -> {tok}")
    print("review them at the /ui page; the proxy has already reloaded the mapping")
    return 0


def cmd_prune(cfg: Config, client_id: str, apply: bool, out=None) -> int:
    """Re-apply today's collection rules to a client file already on disk (defect D5).

    A dry run by default, because the point of the item is that deleting the client and scanning
    again was the only remedy and it took the hand-added entries with it. Nothing is removed
    until the list has been read.
    """
    say = out if out is not None else print
    store = _store(cfg)
    try:
        drop = store.prune_preview(client_id)
    except MappingError as exc:
        say(str(exc))
        return 1
    if not drop:
        say(f"{client_id}: nothing to prune. Every collected entry is one today's rules would "
            f"still collect, and hand-added entries are never touched.")
        return 0
    say(f"{client_id}: {len(drop)} collected entr{'y' if len(drop) == 1 else 'ies'} today's rules "
        f"would not collect.")
    by_reason: dict[str, list[str]] = {}
    for real, why in drop:
        by_reason.setdefault(why, []).append(real)
    for why, reals in sorted(by_reason.items(), key=lambda pair: -len(pair[1])):
        say(f"  {len(reals)}: {why}")
        for real in reals[:10]:
            say(f"      {real}")
        if len(reals) > 10:
            say(f"      ... and {len(reals) - 10} more")
    say("Entries you added by hand are not listed and are never pruned.")
    if not apply:
        say("Dry run. Re-run with --apply to remove them.")
        return 0
    removed, refused = store.prune(client_id, [real for real, _why in drop])
    say(f"removed {len(removed)}"
        + (f"; {len(refused)} refused - {refused[0][0]}: {refused[0][1]}" if refused else ""))
    return 1 if refused else 0


def cmd_split(cfg: Config) -> int:
    from .store import MappingStore

    try:
        written = MappingStore(Path(cfg.mapping), Path(cfg.clients_dir)).split()
    except MappingError as exc:
        print(f"split failed: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {len(written)} client file(s) under {cfg.clients_dir}; {Path(cfg.mapping).name} renamed to .migrated")
    for p in written:
        print(f"  {p.name}")
    return 0


def cmd_detail(cfg: Config, date: str) -> int:
    """Print the substitution detail for a date (YYYYMMDD, default today) from this host."""
    import socket
    from datetime import datetime

    from .evidence import EvidenceLog

    host = socket.gethostname().split(".")[0].lower()
    date = date if date not in (".", "") else f"{datetime.now():%Y%m%d}"
    path = Path(cfg.log_dir) / "detail" / f"substitutions-{host}-{date}.jsonl.gz"
    if not path.exists():
        print(f"no detail file for {date}: {path}", file=sys.stderr)
        return 1
    print(f"# {path}  (contains real identifiers; do not share)")
    for e in EvidenceLog.read_detail(path):
        if "substitutions" in e:
            # A file written before the substitution log (DECISIONS 38): one line per request.
            for sub in e["substitutions"]:
                arrow = "->" if sub["count"] > 0 else "<-"
                print(f"{e['ts']} {e['dialect']} {e['path']} {e['auth']:12} {sub['client']:10} {sub['real']!r} {arrow} {sub['token']!r} x{abs(sub['count'])}")
        elif e.get("type") == "substitution":
            arrow = "->" if e["direction"] == "out" else "<-"
            driver = e.get("who", {}).get("email") or e.get("who", {}).get("user", "")
            print(f"{e['ts']} {e['action']:16} {e.get('client', ''):10} {e['real']!r} {arrow} {e['token']!r} "
                  f"x{e.get('count', 1)} {e.get('session', '')} {driver}")
        elif e.get("type") == "ui":
            driver = e.get("who", {}).get("email") or e.get("who", {}).get("user", "")
            print(f"{e['ts']} ui {e['method']} {e['path']} {e['status']} {driver}")
    return 0


def cmd_mcp_audit(cfg: Config, config_path: str, write: bool) -> int:
    """Every MCP server on this machine, and whether its tool calls would carry real names.

    A tool call is the one path out of here that does not go through the proxy's own filter: the
    arguments are rehydrated before the tool runs, which is right for a local tool and wrong for a
    server that posts them somewhere. `keep_tokenised_tools` is what decides, and maintaining it by
    hand means it is always one newly installed server out of date.
    """
    from .mcp_audit import discover, patterns_to_keep, report

    servers = discover(project=Path.cwd())
    configured = list(cfg.keep_tokenised_tools)
    for line in report(servers, configured):
        print(line)

    wanted = patterns_to_keep(servers)
    missing = [pattern for pattern in wanted
               if not any(_covers(existing, pattern) for existing in configured)]
    print()
    if not missing:
        print(f"keep_tokenised_tools already covers all {len(wanted)} of them.")
        return 0
    print(f"{len(missing)} server(s) would send tool-call arguments out with real names in them:")
    for pattern in missing:
        print(f"  {pattern}")
    if not write:
        print()
        print("Run it again with --write to add these to keep_tokenised_tools, or copy them in by hand.")
        return 1

    document = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    merged = sorted(set(document.get("keep_tokenised_tools", [])) | set(missing))
    document["keep_tokenised_tools"] = merged
    Path(config_path).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print()
    print(f"Written to {config_path}: keep_tokenised_tools now has {len(merged)} pattern(s).")
    print("Restart the proxy for it to take effect.")
    return 0


def cmd_unwire_claude(cfg: Config) -> int:
    """For the uninstaller: take out of Claude Code's files what the proxy's switches put there - the
    Code tab's proxy settings, hook and MCP entries (feature 117), its certificate authority, the
    Bash guard's hook (feature 98) and the between-tool-calls hook (feature 119) - by the same code
    that put them there."""
    from . import bashguard, codetab_settings, deliver
    from .codetab_ca import Authority

    state = Path(cfg.state_dir) if cfg.state_dir else Path(cfg.mapping).parent / "state"
    failed = 0
    for label, step in (("Code tab", lambda: codetab_settings.apply(False, state, "", "", cfg.listen)),
                        ("Bash guard", lambda: bashguard.apply(False)),
                        ("Between-tool-calls hook", lambda: deliver.apply(False, cfg.port)),
                        ("Rename-collapse guard", lambda: deliver.apply_collapse_guard(False, cfg.port))):
        try:
            said = step()
            print(said or f"{label}: nothing to remove")
        except (codetab_settings.SettingsError, bashguard.GuardError, deliver.DeliverError) as exc:
            print(f"{label}: {exc}", file=sys.stderr)
            failed += 1
    Authority(state).remove()
    return 1 if failed else 0


def cmd_codetab_off(cfg: Config) -> int:
    """For Disable-AiCleaner.ps1 when the proxy is not answering (feature 117, decision 4): the Code tab
    switch turned off in the config and its settings taken out of Claude Code's files, so the Code
    tab reaches Anthropic directly again."""
    from . import codetab_settings
    from .codetab_ca import Authority

    state = Path(cfg.state_dir) if cfg.state_dir else Path(cfg.mapping).parent / "state"
    try:
        if cfg.codetab_enabled:
            cfg.update_settings({"codetab.enabled": False})
        said = codetab_settings.apply(False, state, "", "", cfg.listen)
    except (codetab_settings.SettingsError, ValueError, OSError) as exc:
        print(f"Code tab: {exc}", file=sys.stderr)
        return 1
    Authority(state).remove()
    print(said or "Code tab: already off")
    return 0


def cmd_copilot_off(cfg: Config, keep_switch: bool, remove_cert: bool) -> int:
    """Copilot in VS Code put back (release 6, spec 2026-10-19-copilot-6-wiring.md): VS Code's proxy
    setting taken out, so VS Code connects directly. Stop-AiCleaner keeps the switch on (`keep_switch`:
    the next start points VS Code here again); Disable turns it off; Uninstall also takes the Copilot
    certificate out of the person's Windows store (Windows asks them to confirm) and its files."""
    from . import copilot_settings
    from .codetab_ca import Authority

    state = Path(cfg.state_dir) if cfg.state_dir else Path(cfg.mapping).parent / "state"
    url = f"http://127.0.0.1:{cfg.codetab_port}"
    try:
        if cfg.copilot_enabled and not keep_switch:
            cfg.update_settings({"copilot.enabled": False})
        said = copilot_settings.apply(False, state, url)
        if remove_cert:
            authority = Authority(state, kind="copilot")
            if authority.ca_path.exists():
                thumb = copilot_settings.thumbprint(authority.ca_path)
                store = copilot_settings.cert_store()
                if store.has(thumb):
                    store.remove(thumb)
                    said = "; ".join(s for s in (said, "the Copilot certificate removed from your Windows certificates") if s)
            authority.remove()
    except (copilot_settings.SettingsError, ValueError, OSError) as exc:
        print(f"Copilot: {exc}", file=sys.stderr)
        return 1
    print(said or "Copilot: VS Code was not pointed at aicleaner")
    return 0


def _covers(existing: str, pattern: str) -> bool:
    """True when a pattern already in the config covers this one."""
    import fnmatch

    return existing == pattern or fnmatch.fnmatch(pattern, existing)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aicleaner")
    parser.add_argument("command", choices=["run", "check", "tokenise", "rehydrate", "scan", "preflight",
                                            "scan-tokens", "set-key", "seed", "split", "prune", "detail",
                                            "mcp-audit", "unwire-claude", "codetab-off", "copilot-off"])
    parser.add_argument("path", nargs="?", default=".",
                        help="scan / preflight / scan-tokens: folder; set-key: dialect; seed and prune: client id")
    parser.add_argument("--index", type=int, default=None, help="seed: numeric client index (default: digits in the id)")
    parser.add_argument("--stdin", action="store_true", help="set-key: read the key from stdin")
    parser.add_argument("--keep-switch", action="store_true",
                        help="copilot-off: leave copilot.enabled on (Stop-AiCleaner; the next start points VS Code here)")
    parser.add_argument("--remove-cert", action="store_true",
                        help="copilot-off: also remove the Copilot certificate from your Windows store (uninstall)")
    parser.add_argument("--apply", action="store_true",
                        help="prune: actually remove what the dry run listed")
    parser.add_argument("--config", default=_default_config(), help="path to aicleaner-config.json")
    parser.add_argument("--sample", default=None, help="check: text to round-trip")
    parser.add_argument("--min-count", type=int, default=2, help="scan: minimum occurrences to report")
    parser.add_argument("--add", default="", metavar="CLIENTID",
                        help="scan: map the high-confidence finds to this client (names carrying a "
                             "client's own form go to that client)")
    parser.add_argument("--queue", nargs="?", const="", default=None, metavar="CLIENTID",
                        help="scan: put the finds in the review inbox instead. With a client id, "
                             "they arrive already marked as that client's, which is what pointing "
                             "a scan at their repo already said")
    parser.add_argument("--top", type=int, default=40, help="scan: how many finds to print (default 40)")
    parser.add_argument("--write", action="store_true",
                        help="mcp-audit: add the missing patterns to keep_tokenised_tools")
    parser.add_argument("--resources", action="store_true",
                        help="seed: also add every resource name in the tenant's subscriptions. "
                             "scan/preflight: also report naming-shaped names with no client in "
                             "them (rg-aue-prd-management). Off by default: the shape is your own "
                             "convention, repeated for every engagement, so it names nobody")
    parser.add_argument("--progress", action="store_true",
                        help="scan / preflight: write progress as JSON lines to stderr, for a "
                             "parent process drawing a bar (see scanjob.py)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    legacy = _legacy_config() if args.config is None else None
    if legacy is not None:
        print(f"found {legacy}: rename it to {CONFIG_NAME} (the config file was renamed)", file=sys.stderr)
        return 2
    if args.config and not Path(args.config).exists():
        print(f"config not found: {args.config} (copy aicleaner-config.example.json to aicleaner-config.json)", file=sys.stderr)
        return 2
    cfg = Config.load(args.config)

    if args.command == "check":
        return cmd_check(cfg, args.sample)
    if args.command in ("tokenise", "rehydrate"):
        return cmd_pipe(cfg, args.command)
    if args.command == "scan":
        return cmd_scan(cfg, args.path, args.min_count, args.add, args.queue, args.top,
                        resource_names=args.resources, progress=args.progress)
    if args.command == "mcp-audit":
        return cmd_mcp_audit(cfg, args.config, bool(args.write))
    if args.command == "preflight":
        return cmd_preflight(cfg, args.path, args.min_count, args.add, args.queue, args.top,
                             resource_names=args.resources)
    if args.command == "scan-tokens":
        return cmd_scan_tokens(cfg, args.path)
    if args.command == "set-key":
        return cmd_set_key(cfg, "anthropic" if args.path == "." else args.path, args.stdin)
    if args.command == "seed":
        return cmd_seed(cfg, args.path, args.index, args.resources)
    if args.command == "prune":
        return cmd_prune(cfg, args.path, args.apply)
    if args.command == "split":
        return cmd_split(cfg)
    if args.command == "detail":
        return cmd_detail(cfg, args.path)
    if args.command == "unwire-claude":
        return cmd_unwire_claude(cfg)
    if args.command == "codetab-off":
        return cmd_codetab_off(cfg)
    if args.command == "copilot-off":
        return cmd_copilot_off(cfg, args.keep_switch, args.remove_cert)

    from .proxy import run

    try:
        run(cfg)
    except MappingError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
