"use client";

import { FormEvent, useEffect, useState } from "react";

import {
  ApiUnavailableError,
  ValidationError,
  createTask,
  fetchHealth,
  isTaskSuccess,
  type Task,
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
  const [apiHealthy, setApiHealthy] = useState<boolean | null>(null);
  const [panel, setPanel] = useState<PanelState>({ kind: "idle" });

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
      const task = await createTask({ title, instruction });
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

  return (
    <section className="panel">
      <header className="panel-header">
        <h1>Rajosa AI Software Factory</h1>
        <p className="muted">
          Proof of concept: kirim satu task ke adapter tiruan dan quality gate.
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
            <p>Task lolos quality gate.</p>
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
