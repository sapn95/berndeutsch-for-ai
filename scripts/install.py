#!/usr/bin/env python3
"""Link the hook and the dictionary lookup into the Claude config dir, then
print the settings block to merge.

settings.json is deliberately not written. It is the user's file, it usually
already has hooks in it, and a script that rewrites it is a script that
eventually eats somebody's config.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def config_dir():
    explicit = os.environ.get("CLAUDE_CONFIG_DIR")
    # expanduser and resolve: a tilde or a relative value would otherwise
    # produce a green self-test plus a settings block pointing nowhere useful.
    return Path(explicit).expanduser().resolve() if explicit else Path.home() / ".claude"


def link(src, dst):
    # Never unlink the source. When CLAUDE_CONFIG_DIR resolves to the
    # repository itself, dst IS src, and unlink-then-symlink replaced the hook
    # and bdw with self-referential symlinks: 32 KB of working code gone,
    # unreadable afterwards with ELOOP. An installer that can destroy what it
    # installs is worse than one that does nothing.
    if dst.exists() and dst.resolve() == src.resolve():
        print(f"already in place: {dst}")
        return dst
    if dst.parent.exists() and not dst.parent.is_dir():
        raise SystemExit(f"{dst.parent} exists and is not a directory")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.exists():
        if dst.is_dir() and not dst.is_symlink():
            raise SystemExit(f"refusing to replace the directory {dst}")
        dst.unlink()
    dst.symlink_to(src)
    return dst


def probe(hook, prompt):
    # No session_id on purpose. The hook only serves the full rulebook once per
    # session, so a fixed probe id would pass on a fresh machine and then fail
    # on every reinstall because the state file already exists.
    payload = json.dumps({"prompt": prompt})
    # The probe asserts on the BUNDLED rulebook, so it must not inherit an
    # override that points somewhere else; otherwise merely having
    # BERNDEUTSCH_RULES set in your shell makes the installer fail.
    env = {k: v for k, v in os.environ.items()
           if k not in ("BERNDEUTSCH_RULES", "BERNDEUTSCH_IDIOLECT")}
    # Point the config dir at an empty directory as well, so an existing
    # personal memory overlay cannot satisfy the assertion on the BUNDLED
    # rulebook and turn a broken install into a passing self-test.
    with tempfile.TemporaryDirectory() as sandbox:
        env["CLAUDE_CONFIG_DIR"] = sandbox
        return subprocess.run(
            [sys.executable, str(hook)], input=payload, capture_output=True, encoding="utf-8", errors="replace", env=env
        )


def main():
    cfg = config_dir()
    hook = REPO / "hooks" / "berndeutsch_gate.py"
    bdw = REPO / "scripts" / "bdw"
    for path in (hook, bdw):
        path.chmod(path.stat().st_mode | 0o111)

    print("linked:")
    for src, dst in ((hook, cfg / "hooks" / hook.name), (bdw, cfg / "scripts" / "bdw")):
        link(src, dst)
        print(f"  {dst} -> {src}")

    print()
    # Three assertions, not one. Checking only that additionalContext exists
    # would pass even when no rulebook file was found and the payload is an
    # empty shell, which is exactly the install worth catching.
    fired = probe(hook, "Chasch mer säge öb das itz guet isch, gäu?")
    if fired.returncode != 0 or not fired.stdout.strip():
        print("self-test FAILED: the hook produced nothing on a Bärndütsch prompt", file=sys.stderr)
        return 1
    context = json.loads(fired.stdout)["hookSpecificOutput"]["additionalContext"]
    if "Bärndütschi Schrybwys" not in context:
        print("self-test FAILED: the rulebook was not found, only a stub was injected", file=sys.stderr)
        print(f"  expected {REPO / 'rules' / 'schrybwys.md'} to be readable", file=sys.stderr)
        return 1
    print(f"self-test: fires on Bärndütsch and injects the rulebook ({len(context)} chars)")

    silent = probe(hook, "Run git status and then git commit -m 'fix'")
    if silent.stdout.strip():
        print("self-test FAILED: the hook fired on a plain English prompt", file=sys.stderr)
        return 1
    print("self-test: stays silent on English")

    # The settings block names "python3" from PATH, not this interpreter, so
    # check that one runs the hook too. Otherwise the installer can report a
    # green self-test for a configuration that will not start.
    which = shutil.which("python3")
    if not which:
        print("self-test FAILED: no python3 on PATH, which is what the settings "
              "block invokes", file=sys.stderr)
        return 1
    on_path = subprocess.run(
        [which, str(hook)], input=json.dumps({"prompt": "Chasch mer hälfe?"}),
        capture_output=True, encoding="utf-8", errors="replace",
    )
    if on_path.returncode != 0 or not on_path.stdout.strip():
        print(f"self-test FAILED: {which} could not run the hook", file=sys.stderr)
        print(on_path.stderr.strip(), file=sys.stderr)
        return 1
    print(f"self-test: python3 on PATH ({which}) runs it too")

    block = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            # "python3" from PATH, not sys.executable: the
                            # installer may well be running inside a virtualenv
                            # that the hook has no business depending on. The
                            # hook is stdlib-only, so any python3 will do.
                            "command": "python3",
                            "args": [str(cfg / "hooks" / hook.name)],
                            "timeout": 10,
                            "statusMessage": "Bärndütsch-Schrybwys lade…",
                        }
                    ]
                }
            ]
        }
    }
    print(f"\nNow merge this into {cfg / 'settings.json'}, keeping any hooks already there:\n")
    print("\n".join("  " + line for line in json.dumps(block, indent=2, ensure_ascii=False).splitlines()))
    print(
        "\nThe exec form (`command` + `args`) runs the hook directly with no shell"
        "\nin between, so nothing re-interprets the path."
        "\n\nThen open /hooks once in Claude Code, or restart it, so the new config is read."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
