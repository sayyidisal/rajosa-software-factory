export type CheckResult = {
  name: string;
  passed: boolean;
  detail: string;
};

export type QualityReport = {
  passed: boolean;
  checks: CheckResult[];
};

export type Task = {
  id: string;
  title: string;
  instruction: string;
  status: "passed" | "failed";
  output: string;
  error: string | null;
  quality: QualityReport;
};

export type CreateTaskInput = {
  title: string;
  instruction: string;
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
      body: JSON.stringify(input),
    });
  } catch {
    throw new ApiUnavailableError();
  }

  if (response.status >= 500) {
    throw new ApiUnavailableError();
  }

  if (response.status === 422) {
    throw new ValidationError("Judul dan instruksi wajib diisi dengan benar.");
  }

  if (!response.ok) {
    throw new ApiUnavailableError();
  }

  return (await response.json()) as Task;
}

export function isTaskSuccess(task: Task): boolean {
  return task.status === "passed" && task.quality.passed === true;
}
