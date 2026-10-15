"use client";

import { FormEvent, useEffect, useState } from "react";

import {
  ApiUnavailableError,
  ConflictError,
  ValidationError,
  approvePlan,
  createTask,
  describeExecution,
  describeLifecycle,
  executeTaskTests,
  fetchHealth,
  isTaskSuccess,
  type Task,
  type ValidationProfile,
} from "@/lib/api";

type PanelState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "awaiting_plan"; task: Task }
  | { kind: "awaiting_execution"; task: Task }
  | { kind: "success"; task: Task }
  | { kind: "failed"; task: Task }
  | { kind: "api_unavailable"; message: string }
  | { kind: "validation"; message: string };

function panelFromTask(task: Task): PanelState {
  if (task.lifecycle === "awaiting_plan_approval") {
    return { kind: "awaiting_plan", task };
  }
  if (task.lifecycle === "awaiting_execution_auth") {
    return { kind: "awaiting_execution", task };
  }
  if (isTaskSuccess(task)) {
    return { kind: "success", task };
  }
  return { kind: "failed", task };
}

export function TaskPanel() {
  const [title, setTitle] = useState("");
  const [instruction, setInstruction] = useState("");
  const [validationProfile, setValidationProfile] =
    useState<ValidationProfile>("generic_text");
  const [apiHealthy, setApiHealthy] = useState<boolean | null>(null);
  const [panel, setPanel] = useState<PanelState>({ kind: "idle" });
  const [approving, setApproving] = useState(false);
  const [executing, setExecuting] = useState(false);

  useEffect(() => {
    let active = true;
    fetchHealth().then((healthy) => {
      if (active) {
        setApiHealthy(healthy);
        if (!healthy) {
          setPanel({
            kind: "api_unavailable",
            message: "Backend tidak dapat dihubungi.",
          });
        }
      }
    });
    return () => {
      active = false;
    };
  }, []);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPanel({ kind: "loading" });

    try {
      const task = await createTask({
        title,
        instruction,
        validation_profile: validationProfile,
      });
      setPanel(panelFromTask(task));
      setApiHealthy(true);
    } catch (error) {
      if (error instanceof ValidationError) {
        setPanel({ kind: "validation", message: error.message });
        return;
      }
      setApiHealthy(false);
      setPanel({
        kind: "api_unavailable",
        message:
          error instanceof ApiUnavailableError
            ? error.message
            : "API tidak tersedia",
      });
    }
  }

  async function onDecidePlan(task: Task, approved: boolean) {
    setApproving(true);
    try {
      const updated = await approvePlan(task.id, approved);
      setPanel(panelFromTask(updated));
      setApiHealthy(true);
    } catch (error) {
      if (error instanceof ValidationError || error instanceof ConflictError) {
        setPanel({ kind: "validation", message: error.message });
        return;
      }
      setApiHealthy(false);
      setPanel({
        kind: "api_unavailable",
        message:
          error instanceof ApiUnavailableError
            ? error.message
            : "API tidak tersedia",
      });
    } finally {
      setApproving(false);
    }
  }

  async function onExecuteTests(task: Task) {
    setExecuting(true);
    try {
      const updated = await executeTaskTests(task.id);
      setPanel(panelFromTask(updated));
      setApiHealthy(true);
    } catch (error) {
      if (error instanceof ValidationError || error instanceof ConflictError) {
        setPanel({ kind: "validation", message: error.message });
        return;
      }
      setApiHealthy(false);
      setPanel({
        kind: "api_unavailable",
        message:
          error instanceof ApiUnavailableError
            ? error.message
            : "API tidak tersedia",
      });
    } finally {
      setExecuting(false);
    }
  }

  function renderPlan(task: Task) {
    const plan = task.plan;
    if (!plan) {
      return null;
    }
    return (
      <div className="plan-block">
        <p>
          <span className="muted">Rencana:</span> {plan.summary}
        </p>
        <p>
          <span className="muted">Memerlukan eksekusi runtime:</span>{" "}
          {plan.requires_execution ? "ya" : "tidak"}
        </p>
        <p className="muted">Goals</p>
        <ul>
          {plan.goals.map((goal) => (
            <li key={goal}>{goal}</li>
          ))}
        </ul>
        <p className="muted">Steps</p>
        <ol>
          {plan.steps.map((step) => (
            <li key={step.step_id}>
              <strong>{step.step_id}</strong>: {step.description}
              {step.acceptance_checks.length ? (
                <ul>
                  {step.acceptance_checks.map((check) => (
                    <li key={check}>{check}</li>
                  ))}
                </ul>
              ) : null}
            </li>
          ))}
        </ol>
        {plan.acceptance_checks.length ? (
          <>
            <p className="muted">Acceptance checks</p>
            <ul>
              {plan.acceptance_checks.map((check) => (
                <li key={check}>{check}</li>
              ))}
            </ul>
          </>
        ) : null}
      </div>
    );
  }

  function renderMeta(task: Task) {
    return (
      <>
        <p>
          <span className="muted">ID:</span> {task.id}
        </p>
        <p>
          <span className="muted">Lifecycle:</span> {task.lifecycle} —{" "}
          {describeLifecycle(task)}
        </p>
        <p>
          <span className="muted">Status (legacy):</span> {task.status}
        </p>
      </>
    );
  }

  function renderQuality(task: Task) {
    if (!task.quality.checks.length) {
      return null;
    }
    return (
      <ul>
        {task.quality.checks.map((check) => (
          <li key={check.name}>
            {check.name}: {check.detail}
          </li>
        ))}
      </ul>
    );
  }

  function renderExecution(task: Task) {
    const canExecute =
      task.lifecycle === "awaiting_execution_auth" &&
      task.validation_profile === "python_test_execution";
    return (
      <div className="execution-block">
        <p>
          <span className="muted">Eksekusi:</span> {describeExecution(task)}
        </p>
        {task.execution && task.execution.status !== "not_requested" ? (
          <ul>
            <li>status: {task.execution.status}</li>
            <li>tests_executed: {String(task.execution.tests_executed)}</li>
            <li>tests_passed: {String(task.execution.tests_passed)}</li>
            {task.execution.stdout_summary ? (
              <li>
                stdout: <pre>{task.execution.stdout_summary}</pre>
              </li>
            ) : null}
          </ul>
        ) : null}
        {canExecute ? (
          <button
            type="button"
            disabled={executing || approving || panel.kind === "loading"}
            onClick={() => onExecuteTests(task)}
          >
            {executing ? "Menjalankan tes..." : "Jalankan tes terisolasi"}
          </button>
        ) : null}
      </div>
    );
  }

  const busy = panel.kind === "loading" || approving || executing;

  return (
    <section className="panel">
      <header className="panel-header">
        <h1>Rajosa AI Software Factory</h1>
        <p className="muted">
          Proof of concept: buat task → tinjau rencana → setujui eksplisit →
          validasi deterministik. Eksekusi kode hanya lewat aksi terpisah,
          default dinonaktifkan.
        </p>
        <p className="status-line">
          Status API:{" "}
          {apiHealthy === null
            ? "Memeriksa..."
            : apiHealthy
              ? "Terhubung"
              : "Tidak tersedia"}
        </p>
      </header>

      <form className="task-form" onSubmit={onSubmit}>
        <label>
          Judul
          <input
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            maxLength={120}
            required
            disabled={busy}
          />
        </label>
        <label>
          Instruksi
          <textarea
            value={instruction}
            onChange={(event) => setInstruction(event.target.value)}
            maxLength={4000}
            rows={6}
            required
            disabled={busy}
          />
        </label>
        <label>
          Profil validasi
          <select
            value={validationProfile}
            onChange={(event) =>
              setValidationProfile(event.target.value as ValidationProfile)
            }
            disabled={busy}
          >
            <option value="generic_text">generic_text</option>
            <option value="product_catalog_json">product_catalog_json</option>
            <option value="python_unit_tests">python_unit_tests</option>
            <option value="python_code">python_code</option>
            <option value="python_test_execution">python_test_execution</option>
          </select>
        </label>
        <button type="submit" disabled={busy}>
          Buat task &amp; rencana
        </button>
      </form>

      <div className="result" aria-live="polite">
        {panel.kind === "loading" && (
          <div className="banner loading">
            <strong>Memproses</strong>
            <p>Rencana sedang dibuat.</p>
          </div>
        )}

        {panel.kind === "awaiting_plan" && (
          <div className="banner awaiting">
            <strong>Menunggu persetujuan rencana</strong>
            <p>
              Implementasi belum dimulai. Setujui atau tolak rencana secara
              eksplisit.
            </p>
            {renderMeta(panel.task)}
            {renderPlan(panel.task)}
            <div className="plan-actions">
              <button
                type="button"
                disabled={approving}
                onClick={() => onDecidePlan(panel.task, true)}
              >
                {approving ? "Memproses..." : "Setujui rencana"}
              </button>
              <button
                type="button"
                className="secondary"
                disabled={approving}
                onClick={() => onDecidePlan(panel.task, false)}
              >
                Tolak rencana
              </button>
            </div>
          </div>
        )}

        {panel.kind === "awaiting_execution" && (
          <div className="banner awaiting">
            <strong>Menunggu otorisasi eksekusi</strong>
            <p>
              Validasi statis lulus. Runtime belum dijalankan — otorisasi
              eksekusi terpisah dari persetujuan rencana.
            </p>
            {renderMeta(panel.task)}
            {renderPlan(panel.task)}
            {panel.task.output ? <pre>{panel.task.output}</pre> : null}
            {renderQuality(panel.task)}
            {renderExecution(panel.task)}
          </div>
        )}

        {panel.kind === "success" && (
          <div className="banner success">
            <strong>Berhasil</strong>
            <p>Task mencapai lifecycle completed.</p>
            {renderMeta(panel.task)}
            <pre>{panel.task.output}</pre>
            {renderQuality(panel.task)}
            {renderExecution(panel.task)}
          </div>
        )}

        {panel.kind === "failed" && (
          <div className="banner failed">
            <strong>Gagal</strong>
            <p>
              {panel.task.error ??
                panel.task.quality.checks.find((check) => !check.passed)
                  ?.detail ??
                "Task gagal."}
            </p>
            {renderMeta(panel.task)}
            {panel.task.plan ? renderPlan(panel.task) : null}
            {panel.task.output ? <pre>{panel.task.output}</pre> : null}
            {renderQuality(panel.task)}
            {renderExecution(panel.task)}
          </div>
        )}

        {panel.kind === "api_unavailable" && (
          <div className="banner unavailable">
            <strong>API tidak tersedia</strong>
            <p>{panel.message}</p>
          </div>
        )}

        {panel.kind === "validation" && (
          <div className="banner validation">
            <strong>Permintaan tidak valid</strong>
            <p>{panel.message}</p>
          </div>
        )}
      </div>
    </section>
  );
}
