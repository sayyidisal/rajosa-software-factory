"use client";

import { FormEvent, useEffect, useState } from "react";

import {
  ApiUnavailableError,
  ValidationError,
  createTask,
  describeExecution,
  executeTaskTests,
  fetchHealth,
  isTaskSuccess,
  type Task,
  type ValidationProfile,
} from "@/lib/api";

type PanelState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "success"; task: Task }
  | { kind: "failed"; task: Task }
  | { kind: "api_unavailable"; message: string }
  | { kind: "validation"; message: string };

export function TaskPanel() {
  const [title, setTitle] = useState("");
  const [instruction, setInstruction] = useState("");
  const [validationProfile, setValidationProfile] =
    useState<ValidationProfile>("generic_text");
  const [apiHealthy, setApiHealthy] = useState<boolean | null>(null);
  const [panel, setPanel] = useState<PanelState>({ kind: "idle" });
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
      if (isTaskSuccess(task)) {
        setPanel({ kind: "success", task });
      } else {
        setPanel({ kind: "failed", task });
      }
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

  async function onExecuteTests(task: Task) {
    setExecuting(true);
    try {
      const updated = await executeTaskTests(task.id);
      if (isTaskSuccess(updated)) {
        setPanel({ kind: "success", task: updated });
      } else {
        setPanel({ kind: "failed", task: updated });
      }
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
    } finally {
      setExecuting(false);
    }
  }

  function renderExecution(task: Task) {
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
        {task.validation_profile === "python_test_execution" &&
        task.status === "passed" ? (
          <button
            type="button"
            disabled={executing || panel.kind === "loading"}
            onClick={() => onExecuteTests(task)}
          >
            {executing ? "Menjalankan tes..." : "Jalankan tes terisolasi"}
          </button>
        ) : null}
      </div>
    );
  }

  return (
    <section className="panel">
      <header className="panel-header">
        <h1>Rajosa AI Software Factory</h1>
        <p className="muted">
          Proof of concept: kirim satu task ke adapter tiruan dan quality gate.
          Eksekusi kode hanya lewat aksi eksplisit, default dinonaktifkan.
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
            disabled={panel.kind === "loading"}
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
            disabled={panel.kind === "loading"}
          />
        </label>
        <label>
          Profil validasi
          <select
            value={validationProfile}
            onChange={(event) =>
              setValidationProfile(event.target.value as ValidationProfile)
            }
            disabled={panel.kind === "loading"}
          >
            <option value="generic_text">generic_text</option>
            <option value="product_catalog_json">product_catalog_json</option>
            <option value="python_unit_tests">python_unit_tests</option>
            <option value="python_code">python_code</option>
            <option value="python_test_execution">python_test_execution</option>
          </select>
        </label>
        <button type="submit" disabled={panel.kind === "loading"}>
          Jalankan task
        </button>
      </form>

      <div className="result" aria-live="polite">
        {panel.kind === "loading" && (
          <div className="banner loading">
            <strong>Memproses</strong>
            <p>Task sedang dijalankan.</p>
          </div>
        )}

        {panel.kind === "success" && (
          <div className="banner success">
            <strong>Berhasil</strong>
            <p>Task lolos quality gate (validasi statis).</p>
            <p>
              <span className="muted">ID:</span> {panel.task.id}
            </p>
            <pre>{panel.task.output}</pre>
            <ul>
              {panel.task.quality.checks.map((check) => (
                <li key={check.name}>
                  {check.name}: {check.detail}
                </li>
              ))}
            </ul>
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
            {panel.task.output ? <pre>{panel.task.output}</pre> : null}
            <ul>
              {panel.task.quality.checks.map((check) => (
                <li key={check.name}>
                  {check.name}: {check.detail}
                </li>
              ))}
            </ul>
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
            <strong>Input tidak valid</strong>
            <p>{panel.message}</p>
          </div>
        )}
      </div>
    </section>
  );
}
