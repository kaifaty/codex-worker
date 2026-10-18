# Optional workspace sandbox

The `sandbox` setting in `~/.codex/codex-worker.json` accepts `"off"` (default) or `"workspace"`.
`start --sandbox off|workspace` overrides it for one task; `say` inherits the saved mode, and `status`
shows it. For example:

```json
{
  "sandbox": "workspace",
  "writable_dirs": ["~/.gradle", "~/.npm"]
}
```

`writable_dirs` expands `~` and makes paths absolute. In workspace mode, worker and review execs,
including resumes and account rotations, use `workspace-write` with network access. Saved checks run
through `codex sandbox` with the same permissions; scouts remain read-only.

## Limits

The sandbox blocks writes outside the worktree, protecting task state and saved checks, other
repositories, `~/.ssh`, and `~/.codex`. Temporary directories stay writable, and `writable_dirs`
explicitly grants write access to its entries. Builds with caches outside the worktree fail until the
cache directories are added. The sandbox does **not** restrict disk reads, including tokens, or network
access.

Workspace mode is unsuitable for a repository whose config references worktree files through
`include.path` or `core.attributesFile`.

## Supervisor commits

Workspace instructions override the worker's commit and publication rules: Codex leaves changes in the
worktree and returns a repository-appropriate `commit_message`, with the subject on its first line.
After each completed implementation turn, including fix rounds and any result status, the supervisor
commits before checking, disables Git hooks and fsmonitor, and pushes without force. It then uses the
same PR publication step as every other mode, with the worker's `pr_title` and `pr_body`. An empty
message uses `<slug>: turn <N>`. Failed and stopped turns are not committed. Publication errors do not
fail the turn; `wait` prints the publication result or error and `== sandbox: workspace`.

Supervisor publication works only when every filter's `clean`, `smudge` and `process` commands start
with `git-lfs ` or `git lfs `; otherwise, commit the turn manually.
