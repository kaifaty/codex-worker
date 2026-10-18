# Codex threads in the desktop app

For a thread the user runs in the Codex app (not started by this skill), the app owns it, so `say` and
`exec resume` can't touch it ("already has an active writer"). Instead:

- Read it: `~/.codex/sessions/YYYY/MM/DD/rollout-*-<thread-id>.jsonl`; messages have
  `payload.type == "message"` and role user/assistant. Use
  `python3 -I "${CLAUDE_SKILL_DIR}/scripts/codex-stats.py" <thread-id>` for cost.
- Send to it: `codex queue --thread <thread-id> --message "…"` with a CLI that supports `queue`.
  On macOS the bundled CLI is at
  `/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`.
  A queued message starts a turn when the thread is open in the app and idle; during a turn it waits
  for the turn's end. A thread not opened since the app started waits until the user opens it.
  Exit 0 means stored, not delivered — check the rollout. Effort can't be set per message.
- Hear back: `python3 -I "${CLAUDE_SKILL_DIR}/scripts/wait-turn.py" <thread-id>` in the background exits
  when that thread's next turn ends. It reads `~/.codex/turn-events.jsonl`, which the optional
  `${CLAUDE_SKILL_DIR}/scripts/turn-log.py` notify hook fills. See the repository README for setup.
  The hook chains to Codex Computer Use only when its client exists.

Steering a desktop turn mid-way requires the app's Steer control. Restarting the desktop app kills the
turns of the app's threads; exec tasks of this skill are separate processes and keep running.
