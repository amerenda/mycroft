"""Prometheus metrics for Mycroft coordinator and agent runtime."""

from prometheus_client import Counter, Gauge, Histogram, Info

# ── Coordinator metrics ──────────────────────────────────────────────────────

coordinator_info = Info("mycroft_coordinator", "Coordinator build info")

tasks_created_total = Counter(
    "mycroft_tasks_created_total",
    "Total tasks created",
    ["agent_type", "trigger"],
)

tasks_completed_total = Counter(
    "mycroft_tasks_completed_total",
    "Total tasks completed",
    ["agent_type", "status"],  # status: completed, failed, cancelled
)

tasks_active = Gauge(
    "mycroft_tasks_active",
    "Currently running tasks",
    ["agent_type"],
)

task_duration_seconds = Histogram(
    "mycroft_task_duration_seconds",
    "Total task duration from creation to completion",
    ["agent_type", "status"],
    buckets=[10, 30, 60, 120, 300, 600, 1200, 1800, 3600],
)

argo_submissions_total = Counter(
    "mycroft_argo_submissions_total",
    "Argo workflow submission attempts",
    ["agent_type", "result"],  # result: success, failure
)

telegram_messages_total = Counter(
    "mycroft_telegram_messages_total",
    "Telegram messages",
    ["direction"],  # inbound, outbound
)

intent_classifications_total = Counter(
    "mycroft_intent_classifications_total",
    "Intent classifications",
    ["intent_type"],
)

# ── LLM metrics (shared by coordinator + agent runtime) ─────────────────────

llm_calls_total = Counter(
    "mycroft_llm_calls_total",
    "Total LLM inference calls",
    ["model"],
)

llm_call_seconds = Histogram(
    "mycroft_llm_call_seconds",
    "Total LLM call duration (queue wait + inference)",
    ["model"],
    buckets=[1, 2, 5, 10, 30, 60, 120, 300, 600],
)

llm_queue_wait_seconds = Histogram(
    "mycroft_llm_queue_wait_seconds",
    "Time spent waiting in llm-manager queue",
    ["model"],
    buckets=[0.5, 1, 2, 5, 10, 30, 60, 120, 300],
)

llm_inference_seconds = Histogram(
    "mycroft_llm_inference_seconds",
    "Pure inference time (after model loaded)",
    ["model"],
    buckets=[0.5, 1, 2, 5, 10, 30, 60, 120],
)

llm_tokens_total = Counter(
    "mycroft_llm_tokens_total",
    "Total tokens processed",
    ["model", "type"],  # type: prompt, completion
)

llm_queue_position = Histogram(
    "mycroft_llm_queue_position",
    "Queue position at submission time",
    ["model"],
    buckets=[0, 1, 2, 3, 5, 10, 20],
)

llm_errors_total = Counter(
    "mycroft_llm_errors_total",
    "LLM call failures",
    ["model", "reason"],  # reason: timeout, rejected, failed, error
)

# ── Agent runtime metrics ────────────────────────────────────────────────────

agent_iterations_total = Counter(
    "mycroft_agent_iterations_total",
    "Total agent loop iterations",
    ["agent_type"],
)

agent_tool_calls_total = Counter(
    "mycroft_agent_tool_calls_total",
    "Total tool calls by agents",
    ["agent_type", "tool"],
)

agent_tool_call_seconds = Histogram(
    "mycroft_agent_tool_call_seconds",
    "Tool execution duration",
    ["tool"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60],
)

kb_operations_total = Counter(
    "mycroft_kb_operations_total",
    "Knowledge base operations",
    ["operation"],  # read, write, recall, update_task, delete
)


def llm_metrics_callback(event: str, labels: dict, value: float = 1.0) -> None:
    """Callback for LLMClient to emit Prometheus metrics."""
    model = labels.get("model", "unknown")

    if event == "llm_call_total_seconds":
        llm_calls_total.labels(model=model).inc()
        llm_call_seconds.labels(model=model).observe(value)
    elif event == "llm_queue_wait_seconds":
        llm_queue_wait_seconds.labels(model=model).observe(value)
    elif event == "llm_queue_position":
        llm_queue_position.labels(model=model).observe(value)
    elif event == "llm_tokens":
        token_type = labels.get("type", "unknown")
        llm_tokens_total.labels(model=model, type=token_type).inc(value)
    elif event == "llm_job_status":
        pass  # status transitions are informational, not metrics
