export type CheckResult = {
  name: string;
  passed: boolean;
  detail: string;
  details?: Record<string, unknown> | null;
};

export type QualityReport = {
  passed: boolean;
  checks: CheckResult[];
};

export type ValidationProfile =
  | "generic_text"
  | "product_catalog_json"
  | "python_unit_tests"
  | "python_code"
  | "python_test_execution";

export type Lifecycle =
  | "created"
  | "planning"
  | "awaiting_plan_approval"
  | "generating"
  | "validating"
  | "awaiting_execution_auth"
  | "completed"
  | "failed";

export type AcceptanceCheckKind =
  | "human_review"
  | "artifact_present"
  | "quality_profile"
  | "runtime_tests";

export type AcceptanceCheck = {
  check_id: string;
  description: string;
  kind: AcceptanceCheckKind;
};

export type PlanStep = {
  step_id: string;
  description: string;
  expected_artifacts: string[];
  acceptance_check_ids: string[];
};

export type Plan = {
  schema_version: string;
  summary: string;
  goals: string[];
  steps: PlanStep[];
  acceptance_checks: AcceptanceCheck[];
  requires_execution: boolean;
};

export type ExecutionSummary = {
  execution_id: string;
  status: string;
  tests_executed: boolean;
  tests_passed: boolean;
  timed_out: boolean;
  runner_error_category: string;
  exit_code: number | null;
  duration_ms: number;
  stdout_summary: string;
  stderr_summary: string;
  detail: string;
  artifacts: { name: string; size_bytes: number }[];
};

export type Task = {
  id: string;
  title: string;
  instruction: string;
  validation_profile: ValidationProfile;
  status: "passed" | "failed" | "pending";
  lifecycle: Lifecycle;
  output: string;
  error: string | null;
  quality: QualityReport;
  execution?: ExecutionSummary | null;
  plan?: Plan | null;
  plan_approved?: boolean;
};

export type CreateTaskInput = {
  title: string;
  instruction: string;
  validation_profile?: ValidationProfile;
};

export class ApiUnavailableError extends Error {
  constructor(message = "API tidak tersedia") {
    super(message);
    this.name = "ApiUnavailableError";
  }
}

export class ValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ValidationError";
  }
}

export class ConflictError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ConflictError";
  }
}

async function parseTaskResponse(response: Response): Promise<Task> {
  if (response.status >= 500) {
    throw new ApiUnavailableError();
  }
  if (response.status === 404) {
    throw new ValidationError("Task tidak ditemukan.");
  }
  if (response.status === 409) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new ConflictError(body?.detail || "Transisi tidak diizinkan.");
  }
  if (response.status === 422) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new ValidationError(
      typeof body?.detail === "string"
        ? body.detail
        : "Permintaan tidak valid.",
    );
  }
  if (!response.ok) {
    throw new ApiUnavailableError();
  }
  return (await response.json()) as Task;
}

export async function fetchHealth(): Promise<boolean> {
  try {
    const response = await fetch("/backend/health", { cache: "no-store" });
    if (!response.ok) {
      return false;
    }
    const body = (await response.json()) as { status?: string };
    return body.status === "ok";
  } catch {
    return false;
  }
}

export async function createTask(input: CreateTaskInput): Promise<Task> {
  let response: Response;
  try {
    response = await fetch("/backend/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: input.title,
        instruction: input.instruction,
        validation_profile: input.validation_profile ?? "generic_text",
      }),
    });
  } catch {
    throw new ApiUnavailableError();
  }

  if (response.status >= 500) {
    throw new ApiUnavailableError();
  }

  if (response.status === 422) {
    throw new ValidationError("Judul, instruksi, atau profil validasi tidak valid.");
  }

  if (!response.ok) {
    throw new ApiUnavailableError();
  }

  return (await response.json()) as Task;
}

export async function approvePlan(taskId: string, approved: boolean): Promise<Task> {
  let response: Response;
  try {
    response = await fetch(`/backend/tasks/${encodeURIComponent(taskId)}/approve-plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved }),
    });
  } catch {
    throw new ApiUnavailableError();
  }
  return parseTaskResponse(response);
}

export async function executeTaskTests(taskId: string): Promise<Task> {
  let response: Response;
  try {
    response = await fetch(`/backend/tasks/${encodeURIComponent(taskId)}/execute-tests`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true }),
    });
  } catch {
    throw new ApiUnavailableError();
  }
  return parseTaskResponse(response);
}

/** Full task success: lifecycle completed and static quality passed. */
export function isTaskSuccess(task: Task): boolean {
  if (task.lifecycle !== "completed" || task.status !== "passed") {
    return false;
  }
  if (task.quality.passed !== true || !task.quality.checks.length) {
    return false;
  }
  return task.quality.checks.every((check) => check.passed === true);
}

export function describeLifecycle(task: Task): string {
  switch (task.lifecycle) {
    case "awaiting_plan_approval":
      return "Menunggu persetujuan rencana.";
    case "awaiting_execution_auth":
      return "Validasi statis lulus — menunggu otorisasi eksekusi runtime.";
    case "completed":
      return "Selesai.";
    case "failed":
      return "Gagal.";
    case "planning":
      return "Sedang merencanakan.";
    case "generating":
      return "Sedang menghasilkan artefak.";
    case "validating":
      return "Sedang validasi.";
    default:
      return task.lifecycle;
  }
}

export function describeExecution(task: Task): string {
  const execution = task.execution;
  if (!execution || execution.status === "not_requested") {
    return "Eksekusi tes belum diminta.";
  }
  if (execution.tests_passed) {
    return "Tes dijalankan dan lulus (bukti runner terisolasi).";
  }
  if (execution.timed_out) {
    return "Eksekusi timeout — tes tidak dinyatakan lulus.";
  }
  if (execution.status === "runner_unavailable") {
    return `Runner tidak tersedia (${execution.runner_error_category}).`;
  }
  if (execution.tests_executed && !execution.tests_passed) {
    return "Tes dijalankan dan gagal.";
  }
  return execution.detail || `Status eksekusi: ${execution.status}`;
}
