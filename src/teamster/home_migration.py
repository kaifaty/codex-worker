"""migrate-home: move our legacy files out of the Codex home into the data home."""
import fcntl
import json
import os
from contextlib import ExitStack
from pathlib import Path

from . import config, procs, task_state


# Layout purposes that move, in order. Worktrees are linked, never moved. The
# legacy lock files stay: migrate-home holds them while it moves, and a moved
# lock would let a new process lock a fresh file at the old path.
MOVED = ("tasks", "settings", "telemetry", "resets", "rules", "duty", "duty_log", "duty_launchd_log",
         "board_machines", "board_log")
HELD_LOCKS = ("settings_lock", "duty_lock")
LAUNCHD_LABELS = ("teamster.board", "teamster.duty")


def legacy_name(purpose):
    return config.LAYOUTS["legacy"][purpose]


def new_name(purpose):
    return config.LAYOUTS["new"][purpose]


def legacy_sources(codex):
    """(purpose, path) for each legacy item present in the Codex home."""
    found = []
    for purpose in MOVED:
        path = codex / legacy_name(purpose)
        if purpose == "settings" and not os.path.lexists(path):
            # The settings fallback moves only when it is the file the tool reads.
            path = codex / legacy_name("settings_fallback")
        if os.path.lexists(path):
            found.append((purpose, path))
    return found


def new_plan(codex, target):
    worktrees = codex / legacy_name("worktrees")
    return {"version": 1, "from": str(codex), "to": str(target),
            "moved": [{"from": str(path), "to": str(target / new_name(purpose))}
                      for purpose, path in legacy_sources(codex)],
            "linked": ({"link": str(target / new_name("worktrees")), "target": os.path.realpath(worktrees)}
                       if os.path.lexists(worktrees) else None),
            "marker": str(codex / config.HOME_MARKER)}


def read_record(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        procs.die(f"migrate-home: unreadable migration record {path}: {exc}")


def write_atomic(path, text):
    # A crash between writing and renaming leaves a temporary file; drop it.
    for stale in path.parent.glob(f".{path.name}.*.tmp"):
        stale.unlink()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.rename(temporary, path)


def current_plan(codex, target, staging):
    """The plan to carry out and the stage it starts from: new, staging or marker."""
    if target.exists():
        record = read_record(target / config.MIGRATION_RECORD)
        if record is not None and not (codex / config.HOME_MARKER).exists():
            return record, "marker"
        return None, None
    record = read_record(staging / config.MIGRATION_RECORD) if staging.exists() else None
    if record is not None:
        return record, "staging"
    return new_plan(codex, target), "new"


def refusals(codex, target, staging):
    """Every reason not to migrate, checked again under the locks before anything moves."""
    reasons = [f"{name} is set; migrate-home moves only the default homes, run it without {name}"
               for name, value in config.HOME_VARIABLES.items() if value]
    if reasons:
        return reasons
    marker = codex / config.HOME_MARKER
    if marker.exists():
        return [f"the data is already in the new layout: {marker} names {marker.read_text(encoding='utf-8').strip()}"]
    record, stage = current_plan(codex, target, staging)
    if record is None:
        if not legacy_sources(codex):
            return [f"the data is already in the new layout: {codex} holds none of the legacy files"]
        if any(target.iterdir()):
            return [f"{target} exists and is not empty; move it away or empty it first"]
        # Commands already use an existing ~/.teamster and its own lock, which the move does not hold.
        return [f"{target} exists; remove the empty directory first (rmdir {target})"]
    if stage == "new" and not record["moved"]:
        return [f"the data is already in the new layout: {codex} holds none of the legacy files"]
    destination = staging if staging.exists() else (target if target.exists() else target.parent)
    if codex.exists() and os.stat(codex).st_dev != os.stat(destination).st_dev:
        reasons.append(f"{target} and {codex} are on different devices; migrate-home only renames, never copies")
    if stage != "marker":
        for pair in record["moved"]:
            staged = staging / Path(pair["to"]).name
            if os.path.lexists(pair["from"]) and os.path.lexists(staged):
                reasons.append(f"both {pair['from']} and {staged} exist; keep one of them")
    roots = [codex / legacy_name("tasks"), staging / new_name("tasks")]
    running = sorted({slug for root in roots for slug in task_state.running_tasks(root=root, scouts=True)})
    if running:
        reasons.append("tasks are running: " + ", ".join(running) + "; wait for them or stop them first")
    return reasons


def plan_lines(record):
    lines = [f"migrate-home: move the data home from {record['from']} (legacy layout) "
             f"to {record['to']} (new layout)"]
    lines += [f"  move {pair['from']} -> {pair['to']}" for pair in record["moved"]]
    if record["linked"]:
        lines.append(f"  link {record['linked']['link']} -> {record['linked']['target']} "
                     "(worktrees stay where they are)")
    lines.append(f"  write {record['marker']}")
    lines.append(f"  keep in {record['from']}: Codex's own files, checks, briefs, .bak-* files, "
                 f"{legacy_name('worktrees')}, {legacy_name('settings_lock')} and {legacy_name('duty_lock')}")
    return lines


def apply(codex, target, staging, record, stage):
    if stage == "new":
        staging.mkdir(mode=0o700, exist_ok=True)
        write_atomic(staging / config.MIGRATION_RECORD, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    if stage != "marker":
        for pair in record["moved"]:
            staged = staging / Path(pair["to"]).name
            if os.path.lexists(pair["from"]):
                os.rename(pair["from"], staged)
            elif not os.path.lexists(staged):
                procs.die(f"migrate-home: {pair['from']} disappeared before it was moved; "
                          f"{staging} holds what was moved so far")
        linked = record["linked"]
        if linked:
            link = staging / Path(linked["link"]).name
            if not os.path.lexists(link):
                os.symlink(linked["target"], link)
        os.rename(staging, target)
    write_atomic(Path(record["marker"]), record["to"] + "\n")


def cmd_migrate_home(a):
    codex = config.default_codex_home().resolve()
    home = config.USER_HOME.resolve()
    target, staging = home / config.default_data_home().name, home / config.migrating_home().name
    reasons = refusals(codex, target, staging)
    if not reasons:
        record, stage = current_plan(codex, target, staging)
        print("\n".join(plan_lines(record)))
        if stage != "new" or staging.exists():
            print("an earlier migrate-home stopped part way; --apply finishes it")
    if reasons:
        procs.die("migrate-home: " + "; ".join(reasons))
    if not a.apply:
        print(f"nothing changed; run {config.COMMAND} migrate-home --apply to do it")
        return
    with ExitStack() as stack:
        # Commands that write task records hold the settings lock shared; holding
        # it here keeps them from starting until the move and the marker are done.
        for purpose in HELD_LOCKS:
            lock = stack.enter_context((codex / legacy_name(purpose)).open("a"))
            fcntl.flock(lock, fcntl.LOCK_EX)
        reasons = refusals(codex, target, staging)
        if reasons:
            procs.die("migrate-home: " + "; ".join(reasons))
        record, stage = current_plan(codex, target, staging)
        apply(codex, target, staging, record, stage)
    print(f"moved the data home to {target}; the record of every move is {target / config.MIGRATION_RECORD}")
    print("Restart the launchd jobs so they use the new data home:")
    for label in LAUNCHD_LABELS:
        print(f"  launchctl kickstart -k gui/$(id -u)/{label}")
    print(f"Regenerate printed plists ({config.COMMAND} duty plist, teamster-board plist): "
          "they now set TEAMSTER_HOME.")
    print("Jobs loaded under old codex-worker.* labels need their plists regenerated and reloaded:")
    for label in LAUNCHD_LABELS:
        old_label = label.replace("teamster.", "codex-worker.")
        command = "teamster-board" if label.endswith(".board") else config.COMMAND + " duty"
        plist = f"~/Library/LaunchAgents/{label}.plist"
        print(f"  launchctl bootout gui/$(id -u)/{old_label}")
        print(f"  {command} plist > {plist}")
        print(f"  launchctl bootstrap gui/$(id -u) {plist}")
