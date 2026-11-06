"""Work-executor contract and registry, independent of transports and task I/O.

Register an Executor (or subclass) with register(instance). Its name is the
persisted executor value and a CLI choice; account_field, cursor_field and
cursor_label describe its existing task-state fields. Override state-only
policies here: reviewer defaults, sandbox/effort support, status and telemetry.
from_state selects the saved executor (legacy tasks default to Codex); for_turn
always selects Codex for review/scout transport, regardless of the work executor.

Higher-layer owners bind hooks at import with bind(module, operation="function").
Hooks store the module and attribute name, never the function, so later patches
of the owning module remain visible. Required operations and signatures are:
  turn(st, cfg, prompt, effort, current_message, stopped, sleep, new_turn=True)
  wrapup(st, cfg, account, prompt, effort, stopped, cursor) -> outcome
  last_message(st) -> str; messages(st) -> list[str]; pr_messages(st) -> list[str]
Optional status_message(st) overrides the last_message fallback for status.
Optional prepare(effort, cfg) validates startup/resume and requires its binary;
optional stats(st) prints executor-specific statistics. Missing required hooks
fail explicitly. Work hooks retain ownership of transport, rotation and state
writes; this module neither imports those owners nor changes the disk schema.
"""


class Executor:
    """Codex-compatible state policies with late-bound work and I/O hooks."""

    supports_sandbox = True
    event_log = True
    inherit_pr_text = False
    provider = None

    def __init__(self, name, account_field="account", cursor_field="thread", cursor_label="thread"):
        self.name = name
        self.account_field = account_field
        self.cursor_field = cursor_field
        self.cursor_label = cursor_label
        self.hooks = {}

    def bind(self, module, **operations):
        for operation, attribute in operations.items():
            self.hooks[operation] = (module, attribute)

    def call(self, operation, *args, **kwargs):
        module, attribute = self.hooks[operation]
        return getattr(module, attribute)(*args, **kwargs)

    def prepare(self, effort, cfg):
        if "prepare" in self.hooks:
            self.call("prepare", effort, cfg)

    def stats(self, st):
        if "stats" in self.hooks:
            self.call("stats", st)

    def account(self, st):
        return st.get(self.account_field)

    def status_account(self, st):
        return self.account(st)

    def cursor(self, st):
        return st.get(self.cursor_field) or ""

    def reviewer(self, cfg):
        return cfg["reviewer"]

    def review_effort(self, effort):
        return effort

    def review_accounts(self, st):
        return set()

    def review_status(self, st):
        return ""

    def status_error(self, st):
        return ""

    def status_message(self, st):
        return self.call("status_message" if "status_message" in self.hooks else "last_message", st)

    def has_history(self, st):
        return bool(st.get("threads") or self.cursor(st)
                    or not self.event_log and self.call("messages", st))

    def telemetry_provider(self, st, phase):
        return (self.provider if st.get("kind", "work") == "work"
                and phase in ("work", "fix", "wrapup", "turn") else None)

    def turn_fields(self, st):
        if self.telemetry_provider(st, "turn") is None:
            return {}
        return {"provider": self.provider, self.cursor_field: st.get(self.cursor_field)}

    def phase_fields(self, st, last, cost_usd):
        return {"provider": self.provider, "account": last.get("account", self.account(st)),
                "thread": None, self.cursor_field: last.get(self.cursor_field, st.get(self.cursor_field)),
                "cost_usd": cost_usd}


class ClaudeExecutor(Executor):
    supports_sandbox = False
    event_log = False
    inherit_pr_text = True
    provider = "claude"

    def __init__(self):
        super().__init__("claude", "claude_account", "session_id", "session")

    def status_account(self, st):
        return self.account(st) or "?"

    def reviewer(self, cfg):
        return "codex"

    def review_effort(self, effort):
        return None if effort == "max" else effort

    def review_accounts(self, st):
        return {self.account(st)} if self.account(st) else set()

    def review_status(self, st):
        return f"\nClaude work account {self.account(st) or '?'}, session {self.cursor(st) or '?'}"

    def status_error(self, st):
        return "\n" + st["error"] if st.get("error") else ""

    def status_message(self, st):
        return "\n\n".join(self.call("messages", st))


EXECUTORS = {}


def register(executor):
    if executor.name in EXECUTORS:
        raise ValueError("executor already registered: " + executor.name)
    EXECUTORS[executor.name] = executor


def get(name):
    return EXECUTORS[name]


def names():
    return EXECUTORS.keys()


def from_state(st):
    return get(st.get("executor", "codex"))


def for_turn(st):
    return get("codex") if st.get("kind") in ("review", "scout") else from_state(st)


register(Executor("codex"))
register(ClaudeExecutor())
