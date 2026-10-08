# gh-ci

A CLI tool that helps agents (and humans) watch GitHub CI reliably.

Replaces hand-rolled `gh run watch` + `gh pr checks` loops with a tool that:
- Detects PR conflicts and "won't run" states up front (no waiting on CI that will never start).
- Exits on first red check (fail fast).
- Downloads logs for in-progress jobs too (not only completed ones).
- Accepts any GitHub URL — PR, run, job, or workflow — as a target.

## Install

```sh
uv tool install ~/src/gh-ci                              # local clone
uv tool install git+https://github.com/<user>/gh-ci      # from GitHub
```

Then optionally symlink the skill into your Claude Code config:

```sh
# remove any prior watching-ci skill first
rm -rf ~/.claude/skills/watching-ci

mkdir -p ~/.claude/skills/gh-ci
ln -sf ~/src/gh-ci/SKILL.md ~/.claude/skills/gh-ci/SKILL.md
```

## Subcommands

```
gh-ci status <target> [--ignore <prefix:id> ...]
gh-ci watch  <target> [--ignore <prefix:id> ...] [--interval <duration>] [--timeout <duration>] [--stalled-timeout <duration>]
gh-ci logs   <target> [--failed] [--ignore <prefix:id> ...] [--output-dir <path>]
```

Run `gh-ci <subcommand> --help` for full details, including the exit-code table.

### `<target>` accepts

- PR URL: `https://github.com/<owner>/<repo>/pull/<num>`
- Run URL: `https://github.com/<owner>/<repo>/actions/runs/<run_id>` (optional `/attempts/<n>`)
- Job URL: `https://github.com/<owner>/<repo>/actions/runs/<run_id>/job/<job_id>` (or `/jobs/<job_id>`)
- Workflow URL: `https://github.com/<owner>/<repo>/actions/workflows/<name>.yml` (optional `?branch=<b>`)

GitHub Enterprise Server hosts are accepted; the host is plumbed through to `gh`.

### `--ignore prefix:id`

- `workflow:<name>` — skip a whole Actions workflow.
- `job:<name>` — skip one Actions job.
- `check:<name>` — skip a non-Actions check or status context (CircleCI, Jenkins, etc.).

May be repeated. Matching is exact and case-sensitive.

## Defaults

- `--interval` = `10s`
- `--timeout` = `30m` (`0` disables)
- `--stalled-timeout` = `60s`

Duration suffix syntax: `30s`, `5m`, `1.5h`, `2h30m` (parsed by `pytimeparse2`).

## Stdout vs stderr

- **stderr** — confirmation line, per-event status lines, download paths, warnings, ctrl-C.
- **stdout** — final summary block (watch/status). Empty for `logs`.

Pipe stdout if you want to capture just the summary.

## Logs output

`gh-ci logs <target>` downloads each job's log into `<download-dir>/gh-ci-<run_id>-<UTC-timestamp>/`. Default download dir is `platformdirs.user_downloads_dir()` (macOS: `~/Downloads`; Linux: XDG; Windows: Known Folders). Override with `--output-dir`.

Each per-run directory contains:
- `<job_id>-<sanitized-name>.log` — completed job logs.
- `<job_id>-<sanitized-name>.partial.log` — job was in flight at download start.
- `<job_id>-<sanitized-name>.log.tmp` (or `.partial.log.tmp`) — download was truncated by an error; left on disk for forensics.
- `manifest.json` — schema version 1; per-job metadata (status, conclusion, file path, bytes_written, partial, truncated, error).

Logs are saved byte for byte, including ANSI color codes (view with `less -R`).

## Development

```sh
cd ~/src/gh-ci
uv sync --all-groups
uv run pytest
```

### Capturing test fixtures

Bugs found while dogfooding become a fixture + a regression test before the fix lands. The helper script wraps `gh`, runs it, sanitizes tokens and emails, and writes JSON to `tests/fixtures/`:

```sh
uv run python scripts/capture_fixture.py <fixture-name> -- gh api ...
```

### Conventions

- TDD throughout: write the failing test first.
- `gh.py` is the only module that calls `subprocess`. All other tests use `FakeGh`.
- `clock.py` provides a `Clock` protocol. All time-dependent code uses an injected clock; tests use `FakeClock`. No real `time.sleep` in tests.
- Every `gh api` call passes `-X GET` (or the appropriate method) explicitly.

## Color

`gh-ci` emits ANSI color on result lines, group labels, watch events, and errors. The `--color` flag must appear **before** the subcommand:

```sh
gh-ci --color=always status <url>
```

### Decision tree

1. `--color=always` → enable
2. `--color=never` → disable
3. `--color=auto` → use color iff stderr is a TTY (env vars deliberately bypassed)
4. else `NO_COLOR` set and non-empty → disable
5. else `FORCE_COLOR` set and non-empty → enable
6. else stderr is a TTY → enable
7. else → disable

`NO_COLOR` and `FORCE_COLOR` follow the [no-color.org](https://no-color.org/) and [force-color.org](https://force-color.org/) specs: **any non-empty value** enables/disables, including the literal string `"0"`. This matches Rich behavior; only Node/chalk has the special `FORCE_COLOR=0`-disables carve-out, which gh-ci intentionally does not follow.

### Pipeline caveat

The TTY check inspects **stderr**. When stderr is a TTY and stdout is piped (`gh-ci status <url> | grep Result:`), the stdout summary still carries ANSI codes under `auto`/default. In scripts that parse stdout, pass `--color=never` or set `NO_COLOR=1`.

Argparse-generated errors (unknown subcommand, `--help`) are not colored — they fire before the palette is built.

## Known limitations (v1)

- A PR is reported `green` only once every GitHub Actions check suite on the head commit has reached a terminal state — including a workflow run that is queued but has not yet created its job check runs (it shows under `In progress (no jobs reported yet)`). Third-party app check suites with no workflow run (e.g. renovate) are not waited on. Residual: a single `status` snapshot taken before GitHub has created a workflow's check suite at all cannot see that workflow; `watch` covers it on a later poll.
- Required-check stalled detection only fires once the whole run goes quiescent — no job *in progress* and no state change for the `--stalled-timeout` window — with a required check still unreported, so a check waiting on a *running* `needs:` upstream is not falsely flagged. gh-ci does not read the workflow's `needs:`-graph, so it can't fully tell "blocked" from "orphaned", and two cases remain: an upstream that is only *queued* (not yet running) with nothing else active is treated as quiescent and its downstream can still be flagged; and because liveness is whole-run, a single in-progress job anywhere holds the gate open for every required check. It only catches checks GitHub lists in the rollup (including `state == "expected"`); checks GitHub never lists at all are invisible.
- Cancelled jobs (including supersede-by-newer-push cancellations) count as failures. Re-run on the fresh push if you hit this.
- No integration tests against real GitHub. Bugs found while dogfooding become unit tests.

## License

<img align="right" src="https://www.gnu.org/graphics/gplv3-127x51.png">

Copyright (C) 2026, Anna Zhukova

This project is licensed under the [GNU GPL version 3.0](LICENSE).
