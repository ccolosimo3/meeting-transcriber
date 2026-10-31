(() => {
  "use strict";

  const byId = id => document.getElementById(id);
  const ui = Object.fromEntries([
    "theme-toggle", "choose-library", "refresh-library", "library-search", "library-path",
    "library-errors", "meeting-count", "meeting-list", "library-empty", "reader-empty",
    "reader-empty-detail", "reader-content", "meeting-title", "meeting-added", "meeting-status",
    "run-select", "open-html", "reader-notice", "transcript-tools", "transcript-summary",
    "transcript-search", "speaker-filter", "find-previous", "find-next", "find-status",
    "language-label", "passages", "transcript-empty", "audio-container", "audio-status",
    "audio-duration", "back-ten", "playback-speed", "follow-toggle", "jump-playing", "playing-status",
    "workspace", "library", "reader", "library-toggle", "library-close", "library-show",
    "library-feedback", "library-feedback-message", "library-recovery", "library-folder", "folder-toggle",
    "find-toggle", "find-panel", "find-close", "details-toggle", "details-panel", "details-close",
    "details-title", "selected-version", "meeting-warning", "active-filters", "passage-count",
    "clear-text", "clear-speaker", "clear-filters", "actions-menu", "actions-toggle",
    "listening", "listening-toggle", "listening-state", "fallback-open-html", "transcript-empty-message"
  ].map(id => [id, byId(id)]));
  const state = {
    meetings: [], meeting: null, runId: null, data: null,
    rows: [], matches: [], matchIndex: -1, audio: null,
    generation: 0, libraryRequest: 0, busy: false, loading: false,
    mediaReady: false, follow: false, activeOrdinals: [], lastFollowOrdinal: null
  };
  const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
  const narrowWindow = window.matchMedia("(max-width: 760px)");
  let libraryOpen = !narrowWindow.matches;
  let wideLibraryOpen = true;
  let themeChosen = false;
  let programmaticScroll = 0;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function bounded(value, fallback) {
    return typeof value === "string" && value.trim() ? value.slice(0, 600) : fallback;
  }

  function setTheme(dark) {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    ui["theme-toggle"].textContent = dark ? "Light mode" : "Dark mode";
    ui["theme-toggle"].setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
    ui["theme-toggle"].setAttribute("aria-pressed", String(dark));
  }

  function timestamp(value) {
    const total = Math.max(0, Math.floor(value));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor(total / 60) % 60;
    const seconds = String(total % 60).padStart(2, "0");
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}` : `${minutes}:${seconds}`;
  }

  function api() {
    return window.pywebview && window.pywebview.api;
  }

  function notice(message) {
    ui["reader-notice"].textContent = message || "";
    ui["reader-notice"].hidden = !message;
  }

  function updateLibraryFeedback() {
    const errors = ui["library-errors"].textContent;
    ui["library-feedback-message"].textContent = errors;
    ui["library-feedback"].hidden = libraryOpen || !errors;
    ui["library-toggle"].title = errors ? "Library needs attention · Open Library for Choose or Refresh" : "";
  }

  function setLibraryOpen(open, restoreFocus = false) {
    libraryOpen = open;
    if (!narrowWindow.matches) wideLibraryOpen = open;
    ui["library"].hidden = !open;
    ui["reader"].hidden = narrowWindow.matches && open;
    ui["workspace"].classList.toggle("library-hidden", !open);
    ui["library-toggle"].textContent = open ? "Hide library" : "Library";
    ui["library-toggle"].setAttribute("aria-expanded", String(open));
    if (open && narrowWindow.matches) closeDisclosures("library-folder");
    if (!open) ui["library-folder"].open = false;
    updateLibraryFeedback();
    if (restoreFocus) ui["library-toggle"].focus();
  }

  const panels = ["find", "details"];
  const disclosures = [
    ["library-folder", "folder-toggle"], ["actions-menu", "actions-toggle"], ["listening", "listening-toggle"]
  ];

  function closeDisclosures(except) {
    for (const name of panels) {
      if (name === except) continue;
      ui[`${name}-panel`].hidden = true;
      ui[`${name}-toggle`].setAttribute("aria-expanded", "false");
    }
    for (const [id] of disclosures) if (id !== except) ui[id].open = false;
    updateFilterContext();
  }

  function setPanelOpen(name, open, focus = true) {
    if (open) {
      if (narrowWindow.matches && libraryOpen) setLibraryOpen(false);
      closeDisclosures(name);
    }
    ui[`${name}-panel`].hidden = !open;
    ui[`${name}-toggle`].setAttribute("aria-expanded", String(open));
    updateFilterContext();
    if (focus) ui[open ? (name === "find" ? "transcript-search" : "details-close") : `${name}-toggle`].focus();
  }

  function updateListeningContext() {
    const labels = [];
    const speed = state.audio ? state.audio.playbackRate : Number(ui["playback-speed"].value);
    if (speed !== 1) labels.push(`${speed}×`);
    if (state.follow) labels.push("Follow on");
    ui["listening-state"].textContent = labels.join(" · ");
  }

  function updateFilterContext() {
    const query = ui["transcript-search"].value;
    const speaker = ui["speaker-filter"].value;
    const visible = state.rows.filter(row => !row.node.hidden).length;
    ui["passage-count"].textContent = `${visible} / ${state.rows.length} passages`;
    ui["clear-text"].hidden = !query;
    ui["clear-text"].textContent = `Text: ${query} ×`;
    ui["clear-text"].setAttribute("aria-label", `Clear text filter: ${query}`);
    const speakerLabel = ui["speaker-filter"].selectedOptions[0]?.textContent || speaker;
    ui["clear-speaker"].hidden = !speaker;
    ui["clear-speaker"].textContent = `Speaker: ${speakerLabel} ×`;
    ui["clear-speaker"].setAttribute("aria-label", `Clear speaker filter: ${speakerLabel}`);
    ui["active-filters"].hidden = !ui["find-panel"].hidden || !(query || speaker);
  }

  function setFollow(enabled) {
    state.follow = Boolean(enabled && state.mediaReady);
    state.lastFollowOrdinal = null;
    ui["follow-toggle"].textContent = state.follow ? "Follow on" : "Follow off";
    ui["follow-toggle"].setAttribute("aria-pressed", String(state.follow));
    updateListeningContext();
    if (state.follow) followCurrent(true);
  }

  function updateMediaControls() {
    for (const id of ["back-ten", "playback-speed", "follow-toggle", "jump-playing"]) {
      ui[id].disabled = !state.mediaReady;
    }
    for (const row of state.rows) row.timeButton.disabled = !state.mediaReady;
    ui["audio-status"].hidden = state.mediaReady;
    ui["audio-duration"].hidden = state.mediaReady;
  }

  function detachMedia() {
    const old = state.audio;
    state.audio = null;
    state.mediaReady = false;
    setFollow(false);
    if (old) {
      old.pause();
      old.removeAttribute("src");
      old.load();
      old.remove();
    }
    ui["audio-container"].replaceChildren();
    ui["audio-duration"].textContent = "Duration unavailable";
    state.activeOrdinals = [];
    state.rows.forEach(row => row.node.classList.remove("active"));
    updateMediaControls();
  }

  function clearReader(message) {
    state.generation += 1;
    detachMedia();
    state.meeting = null;
    state.runId = null;
    state.data = null;
    state.rows = [];
    state.loading = false;
    ui["reader-content"].hidden = true;
    ui["reader-empty"].hidden = false;
    ui["reader-empty-detail"].textContent = message || "Choose a meeting from your library to begin.";
    ui["passages"].replaceChildren();
    ui["meeting-title"].textContent = "Meeting Transcriber";
    ui["meeting-title"].removeAttribute("title");
    ui["details-toggle"].textContent = "Details";
    ui["details-toggle"].removeAttribute("title");
    ui["open-html"].hidden = true;
    ui["fallback-open-html"].hidden = true;
    setPanelOpen("find", false, false);
    setPanelOpen("details", false, false);
    document.querySelector(".reader").setAttribute("aria-busy", "false");
    document.title = "Meeting Transcriber";
  }

  function updateLibraryControls() {
    const available = Boolean(api());
    ui["choose-library"].disabled = !available || state.busy;
    ui["refresh-library"].disabled = !available || state.busy;
    ui["library-search"].disabled = !available || state.busy;
    ui["library-show"].disabled = !available || state.busy;
    ui["meeting-list"].setAttribute("aria-busy", String(state.busy));
    for (const button of ui["meeting-list"].querySelectorAll("button")) button.disabled = state.busy;
    ui["run-select"].disabled = state.busy || !state.meeting || state.meeting.runs.length === 0;
    ui["open-html"].disabled = state.busy || state.loading;
    ui["fallback-open-html"].disabled = state.busy || state.loading;
    ui["find-toggle"].disabled = !state.data?.canonical || state.loading;
    ui["details-toggle"].disabled = !state.meeting;
  }

  function needsAttention(status) {
    return /cleanup unconfirmed|unfinished|incomplete|unavailable/i.test(status);
  }

  function renderLibrary() {
    const query = ui["library-search"].value.toLowerCase();
    const show = ui["library-show"].value;
    const meetings = state.meetings.filter(meeting => meeting.title.toLowerCase().includes(query) && (
      show === "all" || (show === "transcripts" && meeting.runs.length > 0) ||
      (show === "audio" && meeting.status === "Recording saved") ||
      (show === "attention" && needsAttention(meeting.status))
    ));
    ui["meeting-list"].replaceChildren();
    const fragment = document.createDocumentFragment();
    for (const meeting of meetings) {
      const button = element("button", "meeting-row");
      button.type = "button";
      button.dataset.meetingId = meeting.id;
      button.setAttribute("aria-pressed", String(Boolean(state.meeting && state.meeting.id === meeting.id)));
      button.append(element("span", "meeting-name", meeting.title));
      button.append(element("span", "meeting-date", meeting.added ? `Added ${meeting.added}` : "Added date unavailable"));
      button.append(element("span", `meeting-state${needsAttention(meeting.status) ? " warning" : ""}`, meeting.status));
      button.addEventListener("click", () => selectMeeting(meeting, meeting.default_run_id, true));
      fragment.append(button);
    }
    ui["meeting-list"].append(fragment);
    ui["meeting-count"].textContent = `${meetings.length} / ${state.meetings.length}`;
    ui["meeting-count"].setAttribute("aria-label", `${meetings.length} shown of ${state.meetings.length} meetings`);
    ui["library-empty"].hidden = meetings.length > 0;
    ui["library-empty"].textContent = (query || show !== "all") && state.meetings.length
      ? "No meetings match. Try another title or choose Show All."
      : "No meetings found. Choose a library or refresh after copying a meeting folder here.";
    updateLibraryControls();
  }

  async function scanLibrary(choose) {
    if (!api() || state.busy) return;
    const request = ++state.libraryRequest;
    const previous = state.meeting && { meetingId: state.meeting.id, runId: state.runId };
    const previousEmptyText = ui["library-empty"].textContent;
    state.busy = true;
    if (!choose) {
      state.generation += 1;
      detachMedia();
      state.loading = true;
      if (state.meeting) notice("Refreshing the library…");
    }
    ui["library-empty"].textContent = choose ? "Choosing a library…" : "Refreshing the library…";
    updateLibraryControls();
    try {
      const result = await api()[choose ? "choose_library" : "refresh_library"]();
      if (request !== state.libraryRequest) return;
      if (result && result.cancelled) {
        ui["library-empty"].textContent = previousEmptyText;
        return;
      }
      clearReader();
      ui["library-path"].textContent = bounded(result && result.library, "No library selected");
      ui["library-path"].title = ui["library-path"].textContent;
      state.meetings = result && Array.isArray(result.meetings) ? result.meetings : [];
      const errors = result && Array.isArray(result.errors) ? [...result.errors] : [];
      if (result && result.error) errors.unshift(result.error);
      const errorText = errors.slice(0, 5).map(error => bounded(error && typeof error === "object" ? error.message : error, "A meeting could not be read.")).join("\n");
      ui["library-errors"].textContent = errorText;
      ui["library-errors"].hidden = !errorText;
      updateLibraryFeedback();
      if (choose) ui["library-search"].value = "";
      state.busy = false;
      renderLibrary();
      const retained = previous && state.meetings.find(meeting => meeting.id === previous.meetingId);
      if (retained) {
        const validRun = retained.runs.some(run => run.id === previous.runId);
        selectMeeting(retained, validRun ? previous.runId : retained.default_run_id);
      } else if (previous && !choose) {
        ui["reader-empty-detail"].textContent = "The selected meeting is no longer available. Choose another meeting, or refresh after restoring its folder.";
      }
      if (!state.meetings.length && errorText) {
        ui["library-empty"].textContent = "The library could not be fully read. Check the message above, choose a library, or try Refresh.";
      }
    } catch (_) {
      if (request !== state.libraryRequest) return;
      ui["library-errors"].textContent = "The library could not be read. Choose a library or try Refresh.";
      ui["library-errors"].hidden = false;
      updateLibraryFeedback();
      clearReader("The library could not be read. Choose a library or try Refresh.");
      state.meetings = [];
      renderLibrary();
      ui["library-empty"].textContent = "The library is unavailable. Choose a library or try Refresh.";
    } finally {
      if (request === state.libraryRequest) {
        state.busy = false;
        updateLibraryControls();
      }
    }
  }

  function setMeetingHeader(meeting) {
    ui["meeting-title"].textContent = meeting.title;
    ui["meeting-title"].title = meeting.title;
    ui["details-title"].textContent = meeting.title;
    ui["meeting-added"].textContent = meeting.added ? `Added ${meeting.added}` : "Added date unavailable";
    ui["meeting-status"].textContent = meeting.status;
    ui["meeting-status"].classList.toggle("warning", needsAttention(meeting.status));
    ui["meeting-warning"].textContent = meeting.status;
    ui["meeting-warning"].hidden = !/cleanup|historical|unavailable|incomplete|unfinished/i.test(meeting.status);
    ui["meeting-warning"].classList.toggle("warning", needsAttention(meeting.status));
    const selected = state.meeting.runs.find(run => run.id === state.runId);
    ui["selected-version"].hidden = !selected || state.runId === state.meeting.default_run_id;
    ui["selected-version"].textContent = selected ? `Selected saved version: ${selected.label} · ${meeting.status}` : "";
    ui["details-toggle"].textContent = state.meeting.runs.length > 1 ? `Details · ${state.meeting.runs.length}` : "Details";
    ui["details-toggle"].title = `${state.meeting.runs.length} saved ${state.meeting.runs.length === 1 ? "version" : "versions"}`;
    document.title = `${meeting.title} · Meeting Transcriber`;
  }

  async function selectMeeting(meeting, requestedRun, fromLibrary = false) {
    if (state.busy) return;
    const generation = ++state.generation;
    detachMedia();
    state.meeting = meeting;
    state.runId = requestedRun || null;
    state.data = null;
    state.rows = [];
    state.matches = [];
    state.matchIndex = -1;
    state.loading = true;
    ui["reader-empty"].hidden = true;
    ui["reader-content"].hidden = false;
    document.querySelector(".reader").setAttribute("aria-busy", "true");
    const selected = meeting.runs.find(run => run.id === state.runId);
    setMeetingHeader({ ...meeting, status: selected ? selected.status : meeting.status });
    ui["run-select"].replaceChildren();
    if (!meeting.runs.length) {
      const option = element("option", "", "No saved transcript run");
      option.value = "";
      ui["run-select"].append(option);
    }
    for (const run of meeting.runs) {
      const option = element("option", "", `${run.label} · ${run.status}`);
      option.value = run.id;
      ui["run-select"].append(option);
    }
    ui["run-select"].value = state.runId || "";
    ui["transcript-search"].value = "";
    ui["speaker-filter"].replaceChildren(element("option", "", "All speakers"));
    ui["speaker-filter"].firstChild.value = "";
    setPanelOpen("find", false, false);
    ui["transcript-tools"].hidden = true;
    ui["transcript-summary"].hidden = true;
    ui["open-html"].hidden = true;
    ui["fallback-open-html"].hidden = true;
    ui["language-label"].textContent = "";
    ui["passages"].replaceChildren();
    ui["passages"].hidden = true;
    ui["transcript-empty"].hidden = false;
    ui["transcript-empty-message"].textContent = "Opening this meeting…";
    ui["audio-status"].textContent = "Audio loading…";
    ui["playing-status"].textContent = "Select a timestamp to listen from that moment.";
    ui["playback-speed"].value = "1";
    updateListeningContext();
    notice("");
    renderLibrary();
    if (fromLibrary) {
      if (narrowWindow.matches) setLibraryOpen(false);
      ui["meeting-title"].focus();
    }
    try {
      const data = await api().load_run(meeting.id, state.runId);
      if (generation !== state.generation) return;
      if (!data || data.error) {
        notice(bounded(data && data.error, "This meeting could not be opened. Refresh the library and try again."));
        ui["transcript-empty-message"].textContent = "The saved transcript is unavailable. Choose another run or refresh the library.";
        ui["audio-status"].textContent = "Audio unavailable";
        return;
      }
      state.data = data;
      setMeetingHeader({ ...meeting, title: data.title || meeting.title, added: data.added || meeting.added, status: data.status || meeting.status });
      ui["open-html"].hidden = !(data.exports && data.exports.html && state.runId);
      if (data.canonical) renderTranscript(data.canonical);
      else {
        ui["transcript-empty-message"].textContent = data.exports && data.exports.html
          ? "A structured transcript is unavailable for this run. You can open its saved HTML, or choose another run."
          : "There is no readable transcript for this meeting yet. Its saved audio is available below when present.";
        ui["fallback-open-html"].hidden = ui["open-html"].hidden;
      }
      bindMedia(data, generation);
    } catch (_) {
      if (generation !== state.generation) return;
      notice("This meeting could not be opened. Refresh the library and try again.");
      ui["transcript-empty-message"].textContent = "The saved transcript is unavailable.";
      ui["audio-status"].textContent = "Audio unavailable";
    } finally {
      if (generation === state.generation) {
        state.loading = false;
        document.querySelector(".reader").setAttribute("aria-busy", "false");
        updateLibraryControls();
      }
    }
  }

  function renderTranscript(canonical) {
    const speakers = [...new Set(canonical.segments.map(segment => segment.speaker))];
    const names = new Map(Object.entries(canonical.speaker_names || {}));
    for (const speaker of speakers) {
      const option = element("option", "", names.has(speaker) ? `${names.get(speaker)} · ${speaker}` : speaker);
      option.value = speaker;
      ui["speaker-filter"].append(option);
    }
    ui["transcript-tools"].hidden = false;
    ui["transcript-summary"].hidden = false;
    ui["language-label"].textContent = canonical.language ? `Language: ${canonical.language.toUpperCase()}` : "";
    const generation = state.generation;
    const fragment = document.createDocumentFragment();
    state.rows = canonical.segments.map((segment, ordinal) => {
      const node = element("article", "passage");
      node.id = `passage-${ordinal}`;
      node.dataset.ordinal = String(ordinal);
      const timeButton = element("button", "timestamp", timestamp(segment.start));
      timeButton.type = "button";
      timeButton.disabled = true;
      timeButton.setAttribute("aria-label", `Play passage ${ordinal + 1} at ${timestamp(segment.start)}`);
      timeButton.title = `Play from ${timestamp(segment.start)}`;
      const body = element("div", "passage-body");
      const speaker = element("p", `speaker speaker-color-${speakers.indexOf(segment.speaker) % 8}`);
      speaker.append(document.createTextNode(names.get(segment.speaker) || segment.speaker));
      if (names.has(segment.speaker)) speaker.append(element("span", "speaker-label", segment.speaker));
      const text = element("p", "passage-text", segment.text);
      body.append(speaker, text);
      const location = element("div", "passage-location");
      location.append(element("span", "ordinal", `#${String(ordinal + 1).padStart(2, "0")}`), timeButton);
      node.append(location, body);
      node.addEventListener("click", () => {
        if (generation !== state.generation || window.getSelection().toString()) return;
        seekSegment(segment);
      });
      fragment.append(node);
      return { segment, ordinal, node, text, timeButton };
    });
    ui["passages"].append(fragment);
    ui["passages"].scrollTop = 0;
    applyFilters();
  }

  function queryPattern(query) {
    return query ? new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "giu") : null;
  }

  function highlightedText(node, text, pattern) {
    node.replaceChildren();
    if (!pattern) { node.textContent = text; return false; }
    let cursor = 0;
    let found = false;
    for (const match of text.matchAll(pattern)) {
      found = true;
      node.append(document.createTextNode(text.slice(cursor, match.index)));
      node.append(element("mark", "", match[0]));
      cursor = match.index + match[0].length;
    }
    node.append(document.createTextNode(text.slice(cursor)));
    return found;
  }

  function applyFilters() {
    const query = ui["transcript-search"].value;
    const pattern = queryPattern(query);
    const speaker = ui["speaker-filter"].value;
    state.matches = [];
    state.matchIndex = -1;
    let visible = 0;
    for (const row of state.rows) {
      const matches = highlightedText(row.text, row.segment.text, pattern);
      row.node.hidden = Boolean((speaker && speaker !== row.segment.speaker) || (query && !matches));
      row.node.classList.remove("current-match");
      if (!row.node.hidden) {
        visible += 1;
        if (query) state.matches.push(row);
      }
    }
    ui["passages"].hidden = visible === 0;
    ui["transcript-empty"].hidden = visible > 0;
    ui["transcript-empty-message"].textContent = "No passages match. Try another search or choose All speakers.";
    ui["find-previous"].disabled = state.matches.length === 0;
    ui["find-next"].disabled = state.matches.length === 0;
    ui["find-status"].textContent = query
      ? `${state.matches.length} matching ${state.matches.length === 1 ? "passage" : "passages"}`
      : `${visible} ${visible === 1 ? "passage" : "passages"}${speaker ? " for this speaker" : ""}`;
    updateFilterContext();
    updateActive();
  }

  function scrollToRow(row) {
    programmaticScroll += 1;
    const token = programmaticScroll;
    const viewport = ui["passages"];
    const rowBounds = row.node.getBoundingClientRect();
    const viewportBounds = viewport.getBoundingClientRect();
    viewport.scrollTop += rowBounds.top - viewportBounds.top - (viewport.clientHeight - rowBounds.height) / 2;
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
      if (programmaticScroll === token) programmaticScroll = 0;
    }));
  }

  function findNext(direction) {
    if (!state.matches.length) return;
    if (state.matchIndex >= 0) state.matches[state.matchIndex].node.classList.remove("current-match");
    state.matchIndex = state.matchIndex < 0
      ? (direction < 0 ? state.matches.length - 1 : 0)
      : (state.matchIndex + direction + state.matches.length) % state.matches.length;
    const row = state.matches[state.matchIndex];
    row.node.classList.add("current-match");
    scrollToRow(row);
    ui["find-status"].textContent = `${state.matchIndex + 1} of ${state.matches.length} matching ${state.matches.length === 1 ? "passage" : "passages"}`;
  }

  function followCurrent(force = false) {
    if (!state.follow) return;
    const row = state.rows.find(candidate => !candidate.node.hidden && state.activeOrdinals.includes(candidate.ordinal));
    if (!row) { state.lastFollowOrdinal = null; return; }
    if (force || row.ordinal !== state.lastFollowOrdinal) {
      scrollToRow(row);
      state.lastFollowOrdinal = row.ordinal;
    }
  }

  function updateActive() {
    const audio = state.audio;
    const time = audio ? audio.currentTime : -1;
    state.activeOrdinals = [];
    for (const row of state.rows) {
      const active = Boolean(state.mediaReady && row.segment.start <= time && time < row.segment.end);
      row.node.classList.toggle("active", active);
      if (active) state.activeOrdinals.push(row.ordinal);
    }
    if (state.mediaReady) {
      const visible = state.rows.filter(row => !row.node.hidden && state.activeOrdinals.includes(row.ordinal)).length;
      const label = `${state.activeOrdinals.length === 1 ? "Passage" : "Passages"} ${state.activeOrdinals.map(ordinal => String(ordinal + 1).padStart(2, "0")).join(", ")}`;
      ui["playing-status"].textContent = state.activeOrdinals.length
        ? `${label} playing${visible === state.activeOrdinals.length ? " · Highlighted in transcript." : visible ? " · Some hidden by your filters." : " · Hidden by your filters."}`
        : "No transcript passage at this moment.";
    }
    followCurrent();
  }

  function mediaFailure(message) {
    detachMedia();
    ui["audio-status"].textContent = message;
    ui["playing-status"].textContent = "The transcript remains available to read and search.";
  }

  function bindMedia(data, generation) {
    if (data.media_state !== "ready" || !data.media_url) {
      mediaFailure(data.media_error && data.media_error.message ? bounded(data.media_error.message, "Audio unavailable") : data.media_state === "ambiguous" ? "Audio ambiguous · More than one recording was found." : "Audio missing · No complete recording was found.");
      return;
    }
    const audio = document.createElement("audio");
    audio.id = "meeting-audio";
    audio.controls = true;
    audio.preload = "metadata";
    audio.setAttribute("aria-label", "Meeting recording");
    audio.setAttribute("controlslist", "nodownload");
    const current = () => generation === state.generation && state.audio === audio;
    const metadata = () => {
      if (!current()) return;
      state.mediaReady = Number.isFinite(audio.duration) && audio.duration > 0;
      ui["audio-duration"].textContent = state.mediaReady ? `Duration ${timestamp(audio.duration)}` : "Duration unavailable";
      ui["audio-status"].textContent = state.mediaReady ? "Meeting recording" : "Audio loading…";
      updateMediaControls();
      updateActive();
    };
    audio.addEventListener("loadedmetadata", metadata);
    audio.addEventListener("durationchange", metadata);
    for (const event of ["timeupdate", "seeked", "play", "pause", "ended"]) {
      audio.addEventListener(event, () => { if (current()) updateActive(); });
    }
    audio.addEventListener("ratechange", () => { if (current()) updateListeningContext(); });
    audio.addEventListener("error", () => {
      if (current()) mediaFailure("Audio unavailable · The recording was removed or this format cannot be played.");
    });
    state.audio = audio;
    ui["audio-container"].append(audio);
    audio.src = data.media_url;
    ui["audio-status"].textContent = "Audio loading…";
  }

  function seekSegment(segment) {
    const audio = state.audio;
    if (!audio || !state.mediaReady) return;
    if (segment.start >= audio.duration) {
      notice("This passage starts beyond the available recording. Its text remains readable.");
      return;
    }
    notice("");
    audio.currentTime = Math.min(Math.max(0, segment.start), audio.duration);
    updateActive();
    const generation = state.generation;
    audio.play().catch(() => {
      if (generation === state.generation && state.audio === audio) notice("Playback could not start. Try the recording’s Play control.");
    });
  }

  function releaseFollow() { if (state.follow) setFollow(false); }
  ui["library-toggle"].addEventListener("click", () => {
    setLibraryOpen(!libraryOpen, libraryOpen);
    if (libraryOpen && narrowWindow.matches) ui["library-search"].focus();
  });
  ui["library-close"].addEventListener("click", () => setLibraryOpen(false, true));
  ui["library-recovery"].addEventListener("click", () => {
    setLibraryOpen(true);
    ui["library-folder"].open = true;
    ui["folder-toggle"].focus();
  });
  narrowWindow.addEventListener("change", () => {
    const focusInLibrary = ui["library"].contains(document.activeElement);
    setLibraryOpen(narrowWindow.matches ? false : wideLibraryOpen, focusInLibrary && narrowWindow.matches);
  });
  setLibraryOpen(libraryOpen);
  for (const name of panels) {
    ui[`${name}-toggle`].addEventListener("click", () => setPanelOpen(name, ui[`${name}-panel`].hidden));
    ui[`${name}-close`].addEventListener("click", () => setPanelOpen(name, false));
  }
  for (const [id, opener] of disclosures) {
    ui[id].addEventListener("toggle", () => {
      ui[opener].setAttribute("aria-expanded", String(ui[id].open));
      if (ui[id].open) closeDisclosures(id);
    });
  }
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    for (const [id, opener] of disclosures) {
      if (!ui[id].open) continue;
      event.preventDefault();
      ui[id].open = false;
      ui[opener].focus();
      return;
    }
    for (const name of panels) {
      if (ui[`${name}-panel`].hidden) continue;
      event.preventDefault();
      setPanelOpen(name, false);
      return;
    }
    if (libraryOpen) {
      event.preventDefault();
      setLibraryOpen(false, true);
    }
  });
  ui["theme-toggle"].addEventListener("click", () => {
    themeChosen = true;
    setTheme(document.documentElement.dataset.theme !== "dark");
  });
  systemTheme.addEventListener("change", event => { if (!themeChosen) setTheme(event.matches); });
  setTheme(systemTheme.matches);
  ui["choose-library"].addEventListener("click", () => scanLibrary(true));
  ui["refresh-library"].addEventListener("click", () => scanLibrary(false));
  ui["library-search"].addEventListener("input", renderLibrary);
  ui["library-show"].addEventListener("change", renderLibrary);
  ui["run-select"].addEventListener("change", () => {
    if (state.meeting) selectMeeting(state.meeting, ui["run-select"].value || null);
  });
  async function openSavedHtml() {
    if (!state.meeting || !state.runId || state.busy || state.loading) return;
    const generation = state.generation;
    ui["open-html"].disabled = true;
    ui["fallback-open-html"].disabled = true;
    try {
      const result = await api().open_saved_html(state.meeting.id, state.runId);
      if (generation === state.generation && (!result || result.error || !result.opened)) {
        notice(bounded(result && result.error, "The saved HTML could not be opened. Refresh the library and try again."));
      }
    } catch (_) {
      if (generation === state.generation) notice("The saved HTML could not be opened. Refresh the library and try again.");
    } finally {
      if (generation === state.generation) updateLibraryControls();
    }
  }
  ui["open-html"].addEventListener("click", openSavedHtml);
  ui["fallback-open-html"].addEventListener("click", openSavedHtml);
  function clearFilters(text, speaker) {
    if (text) ui["transcript-search"].value = "";
    if (speaker) ui["speaker-filter"].value = "";
    releaseFollow();
    applyFilters();
    ui[!ui["clear-text"].hidden ? "clear-text" : !ui["clear-speaker"].hidden ? "clear-speaker" : "find-toggle"].focus();
  }
  ui["clear-text"].addEventListener("click", () => clearFilters(true, false));
  ui["clear-speaker"].addEventListener("click", () => clearFilters(false, true));
  ui["clear-filters"].addEventListener("click", () => clearFilters(true, true));
  for (const id of ["transcript-search", "speaker-filter"]) {
    ui[id].addEventListener(id === "speaker-filter" ? "change" : "input", () => {
      releaseFollow();
      applyFilters();
    });
  }
  ui["transcript-search"].addEventListener("keydown", event => {
    if (event.key === "Enter") { event.preventDefault(); findNext(event.shiftKey ? -1 : 1); }
  });
  ui["find-previous"].addEventListener("click", () => findNext(-1));
  ui["find-next"].addEventListener("click", () => findNext(1));
  ui["back-ten"].addEventListener("click", () => {
    if (state.audio && state.mediaReady) { state.audio.currentTime = Math.max(0, state.audio.currentTime - 10); updateActive(); }
  });
  ui["playback-speed"].addEventListener("change", () => {
    if (state.audio) state.audio.playbackRate = Number(ui["playback-speed"].value);
    updateListeningContext();
  });
  ui["follow-toggle"].addEventListener("click", () => setFollow(!state.follow));
  ui["jump-playing"].addEventListener("click", () => setFollow(true));
  ui["passages"].addEventListener("wheel", releaseFollow, { passive: true });
  ui["passages"].addEventListener("touchstart", releaseFollow, { passive: true });
  ui["passages"].addEventListener("pointerdown", event => {
    const viewport = ui["passages"];
    if (event.target === viewport) releaseFollow();
  });
  ui["passages"].addEventListener("keydown", event => {
    if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) releaseFollow();
  });
  ui["passages"].addEventListener("scroll", () => { if (!programmaticScroll) releaseFollow(); }, { passive: true });
  window.addEventListener("beforeunload", detachMedia);
  window.addEventListener("pywebviewready", () => scanLibrary(false), { once: true });
  if (api()) scanLibrary(false);
})();
