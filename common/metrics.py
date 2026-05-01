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

argo_submission_seconds = Histogram(
    "mycroft_argo_submission_seconds",
    "Argo workflow submission latency",
    ["agent_type", "result"],
    buckets=[0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30],
)

task_queue_wait_seconds = Histogram(
    "mycroft_task_queue_wait_seconds",
    "Time from task creation to task start",
    ["agent_type"],
    buckets=[0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600],
)

task_transitions_total = Counter(
    "mycroft_task_transitions_total",
    "Task status transitions observed by coordinator",
    ["agent_type", "to_status", "source"],
)

task_terminal_result_total = Counter(
    "mycroft_task_terminal_result_total",
    "Terminal task outcomes observed by coordinator",
    ["agent_type", "status", "source"],
)

workflow_runs_total = Counter(
    "mycroft_workflow_runs_total",
    "Workflow run starts and outcomes",
    ["workflow", "result"],  # started, completed, failed
)

workflow_steps_total = Counter(
    "mycroft_workflow_steps_total",
    "Workflow step execution lifecycle",
    ["workflow", "step_kind", "event"],  # sequential|parallel, started|completed|failed
)

coordinator_api_requests_total = Counter(
    "mycroft_coordinator_api_requests_total",
    "HTTP requests served by coordinator API",
    ["method", "route", "status_class"],
)

coordinator_api_request_seconds = Histogram(
    "mycroft_coordinator_api_request_seconds",
    "HTTP request latency for coordinator API",
    ["method", "route"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10],
)

coordinator_sse_clients = Gauge(
    "mycroft_coordinator_sse_clients",
    "Currently connected SSE clients",
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

llm_job_status_total = Counter(
    "mycroft_llm_job_status_total",
    "Observed llm-manager job states",
    ["model", "status"],
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

kb_operation_seconds = Histogram(
    "mycroft_kb_operation_seconds",
    "Latency for KB operations",
    ["operation"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5],
)

kb_operation_errors_total = Counter(
    "mycroft_kb_operation_errors_total",
    "KB operation failures",
    ["operation"],
)

agent_runs_total = Counter(
    "mycroft_agent_runs_total",
    "Agent run outcomes",
    ["agent_type", "status"],  # completed, failed
)

agent_iteration_seconds = Histogram(
    "mycroft_agent_iteration_seconds",
    "End-to-end iteration latency inside agent loop",
    ["agent_type"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60],
)

agent_exit_total = Counter(
    "mycroft_agent_exit_total",
    "Agent loop exit reasons",
    ["agent_type", "reason"],  # finish_tool, submit_report_tool, text_response, max_iterations, failed
)

agent_empty_response_total = Counter(
    "mycroft_agent_empty_response_total",
    "Empty assistant responses returned by model",
    ["agent_type"],
)

agent_text_tool_call_total = Counter(
    "mycroft_agent_text_tool_call_total",
    "Tool calls parsed from assistant text (not API tool calls)",
    ["agent_type", "tool"],
)

agent_tool_errors_total = Counter(
    "mycroft_agent_tool_errors_total",
    "Tool execution failures in agent loop",
    ["agent_type", "tool"],
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
        status = labels.get("status", "unknown")
        llm_job_status_total.labels(model=model, status=status).inc()
    elif event == "llm_inference_seconds":
        llm_inference_seconds.labels(model=model).observe(value)
    elif event == "llm_error":
        reason = labels.get("reason", "error")
        llm_errors_total.labels(model=model, reason=reason).inc()
