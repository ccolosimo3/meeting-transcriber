// Tauri injects this adapter in the bundled main frame. Python supplies its own bridge.
Object.defineProperty(window, "meetingReaderHost", {
  value: Object.freeze({
    choose_library: () => window.__TAURI__.core.invoke("choose_library"),
    refresh_library: () => window.__TAURI__.core.invoke("refresh_library"),
    load_run: (meetingId, runId) => window.__TAURI__.core.invoke("load_run", { meetingId, runId }),
  }),
});
