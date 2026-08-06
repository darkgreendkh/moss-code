"""Pure helpers for rendering and measuring session history."""

SHELL_SIGNAL_KEYWORDS = (
    "exit_code",
    "error",
    "err:",
    "fail",
    "failed",
    "failure",
    "traceback",
    "exception",
    "fatal",
    "no such",
    "not found",
    "cannot",
    "denied",
    "assert",
)


def shell_summary(content):
    lines = [line.strip() for line in str(content).splitlines() if line.strip()]
    if not lines:
        return "(empty)"
    signal_lines = [
        line
        for line in lines
        if any(keyword in line.lower() for keyword in SHELL_SIGNAL_KEYWORDS)
    ]
    chosen = signal_lines[:3] if signal_lines else lines[:3]
    return " | ".join(chosen)


def history_staleness(entries):
    """Return how many prior entries are still present in the model context."""
    return len(entries)
