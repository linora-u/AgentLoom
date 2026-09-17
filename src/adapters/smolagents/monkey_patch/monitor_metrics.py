"""Monkey-patch ``smolagents.monitoring.Monitor.update_metrics``.

Replaces the default step-log output with a richer format that includes
step duration **and** cumulative token counts styled with ``rich`` markup.
"""

from rich.text import Text
from smolagents.monitoring import Monitor


def _custom_update_metrics(self, step_log):
    step_duration = step_log.timing.duration
    self.step_durations.append(step_duration)
    console_outputs = (
        f"\\[Step [red]{len(self.step_durations)}[/red]] "
        f"Duration [red]{step_duration:.2f}[/red] seconds"
    )

    if step_log.token_usage is not None:
        self.total_input_token_count += step_log.token_usage.input_tokens
        self.total_output_token_count += step_log.token_usage.output_tokens
        console_outputs += (
            f" | Input tokens: [red]{self.total_input_token_count:,}[/red]"
            f" (+[red]{step_log.token_usage.input_tokens}[/red])"
            f" | Output tokens: [red]{self.total_output_token_count:,}[/red]"
            f" (+[red]{step_log.token_usage.output_tokens}[/red])"
        )
    self.logger.log(Text.from_markup(console_outputs), level=1)


def patch_monitor_metrics() -> None:
    """Apply the custom ``update_metrics`` to :class:`Monitor`."""
    Monitor.update_metrics = _custom_update_metrics
