---
name: gh-ci
description: Use whenever you need to check GitHub CI on a PR, run, workflow, or job — always go through the `gh-ci` CLI, never hand-roll polling.
---

# gh-ci — watching GitHub CI

When verifying CI on a PR or run, **always** use `gh-ci`. Do not hand-roll polling with `gh run watch`, `gh pr checks`, or shell loops; they miss conflicts, stalled required checks, and in-progress logs.

## Three subcommands

- `gh-ci status <target>` — single snapshot; never blocks. Use this first.
- `gh-ci watch <target>` — blocks until decisive. Always pass `--timeout` if you have a budget; default is 30m.
- `gh-ci logs <target>` — downloads all job logs (including in-progress) to `<download dir>/gh-ci-<run_id>-<ts>/`. Paths print on stderr. Read `manifest.json` to know which are partial or truncated.

## `<target>` is a GitHub URL

PR, run, job, or workflow URL — see `gh-ci --help`. Bare numbers/SHAs/branches are not supported.

## `--ignore prefix:id`

- `workflow:<name>` — skip a whole Actions workflow.
- `job:<name>` — skip one Actions job.
- `check:<name>` — skip a non-Actions check or status context.

Repeatable. Exact, case-sensitive match. Ignoring a check that would have been the only passing one flips the PR to "no productive CI ran" — be precise about which to ignore.

## Output

- Streaming events and download paths on **stderr**.
- Final summary block (watch/status) on **stdout** — pipe stdout to capture it.

## Exit codes

Non-zero on any failure. Run `gh-ci <subcommand> --help` for the exact mapping. Special-case actions:

- Non-zero with conflict / closed / behind / "no productive CI" → don't retry; **fix the PR's gate** (resolve conflicts, update branch, etc.).
- Non-zero with `stalled` in the message → don't retry; **a required check is misconfigured**. Investigate, don't loop.
- Non-zero with `timeout` from `watch` → you exceeded the budget you set. Decide whether to keep waiting or escalate.

## Known limitations

- Required-check stalled detection only fires once the whole run goes quiescent — no job *in progress* and no state change for the `--stalled-timeout` window — while a required check is still unreported. A check waiting on a *running* `needs:` upstream is *not* flagged. Caveat: an upstream that is only *queued* (not yet running) with nothing else active is treated as quiescent, so its downstream can still be flagged (gh-ci does not read the `needs:`-graph). It sees only checks GitHub surfaces in the rollup (including the `state == "expected"` "waiting to be reported" case); it does *not* catch checks that GitHub never lists at all.
- Cancelled jobs (including supersede-by-newer-push cancellations) count as failures. Re-run on the fresh push if you hit this.
