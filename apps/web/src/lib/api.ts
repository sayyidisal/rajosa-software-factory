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
  status: "passed" | "failed";
  output: string;
  error: string | null;
  quality: QualityReport;
  execution?: ExecutionSummary | null;
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

  if (response.status === 404) {
    throw new ValidationError("Task tidak ditemukan.");
  }

  if (response.status === 422) {
    throw new ValidationError("Permintaan eksekusi tidak valid.");
  }

  if (response.status >= 500) {
    throw new ApiUnavailableError();
  }

  if (!response.ok) {
    throw new ApiUnavailableError();
  }

  return (await response.json()) as Task;
}

export function isTaskSuccess(task: Task): boolean {
  if (task.status !== "passed" || task.quality.passed !== true) {
    return false;
  }
  if (!task.quality.checks.length) {
    return false;
  }
  return task.quality.checks.every((check) => check.passed === true);
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
