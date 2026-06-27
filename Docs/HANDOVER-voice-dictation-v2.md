# Voice Dictation → TaskFlow Handover (v2)

**Status:** v1 integration already implemented in voice-dictation. This document
covers the v2 updates — trigger phrase changes and the new Ctrl+Shift+Alt
dedicated capture mode — generated from the TaskFlow repo
(`D:\Dev\ai\projects\active\to-do-app`, commit d8fe5b1) on 2026-06-27.
No code in voice-dictation was touched to produce this document.

The original contract spec remains valid and is in
`docs/specs/HANDOVER-voice-dictation.md`. Read that first if you need
full context on port discovery, health check, and task creation.

---

## 1. Trigger phrase update

The phrase "add to my to-do list" is removed from defaults. Replace the
trigger phrase list in `config.json` with:

```json
"taskflow": {
  "enabled": true,
  "triggerPhrases": [
    "add this to TaskFlow",
    "add to my tasks",
    "add a task"
  ]
}
```

**Why:** "to-do list" is awkward to say clearly. "Add to my tasks" is the
natural replacement. The primary phrase "add this to TaskFlow" stays first so
it takes priority in matching.

Matching rules (unchanged from v1):
- Case-insensitive.
- Matched at the **start** of the transcribed text only.
- Strip the matched phrase and leading whitespace, remainder is the task title.
- If the stripped remainder is empty string after trim, do nothing (fall back
  to normal paste).

---

## 2. New optional mode: Ctrl+Shift+Alt dedicated capture

The problem: the user says "add this" constantly during normal speech and
coding work, so a short trigger phrase would false-positive too often. They
want a separate physical gesture for "I'm about to add a TaskFlow task right now".

The solution is a **modifier-keyed capture mode** in voice-dictation:

- Current: Ctrl+Alt held while speaking → transcribe, postprocess, then check
  trigger phrase list.
- New mode: **Ctrl+Shift+Alt held while speaking** → transcribe, postprocess,
  then **skip trigger phrase matching entirely** and route the whole transcript
  directly to the TaskFlow "Add Task" preview panel.

### Behavior when Ctrl+Shift+Alt is held

1. The Ctrl+Shift+Alt combination replaces Ctrl+Alt as the held key for this
   specific recording session.
2. Transcript is sent through the **same postprocess pipeline** (grammar
   cleanup etc.) as normal.
3. After postprocess, bypass the trigger phrase check completely — the entire
   result is treated as the task title.
4. Show the relabeled preview panel ("Add Task", "Add Task" button instead of
   "Insert") with the full transcript as the editable title.
5. Confirm → POST to TaskFlow API with `source: "voice-dictation"`.
6. Cancel → discard, nothing pasted.
7. If the health check fails: show "TaskFlow isn't running" and fall back to
   normal clipboard paste of the postprocessed text.

### Config additions

```json
"taskflow": {
  "enabled": true,
  "triggerPhrases": ["add this to TaskFlow", "add to my tasks", "add a task"],
  "directCaptureModifiers": ["ctrl", "shift", "alt"]
}
```

`directCaptureModifiers` is optional. If absent or empty, the Ctrl+Shift+Alt
mode is disabled and behaviour is identical to v1.

### Implementation notes

- The modifier detection must happen at **key-down time** for the recording
  session, not at transcript time. At the moment the user presses the hotkey
  combination, capture the modifier state and store it as a flag for this
  session only.
- Ctrl+Shift+Alt and Ctrl+Alt must be mutually exclusive for a single
  session — if both Shift and the normal modifiers are held, treat as
  Ctrl+Shift+Alt mode (more specific wins).
- Do not change how the hotkey recording works otherwise (same hold-to-record,
  release-to-transcribe flow).
- The existing Ctrl+Alt (no Shift) path is **completely unchanged**. Users
  who never configure `directCaptureModifiers` see no difference at all.

---

## 3. The frozen API contract (unchanged, for reference)

These are unchanged from v1. Do not modify these on the TaskFlow side.

| Item | Value |
|------|-------|
| Port discovery | `%APPDATA%\TaskFlow\port.json` → `{"port": <int>, "pid": <int>, "startedAt": "<iso8601>"}` |
| Install path | `%APPDATA%\TaskFlow\app-path.json` → `{"exePath": "<string>"}` |
| Health check | `GET http://127.0.0.1:<port>/health` → `200 {"status":"ok"}` |
| Create task | `POST http://127.0.0.1:<port>/tasks` |
| Source field | Always send `"source": "voice-dictation"` |
| Hidden launch | `TaskFlow.exe --hidden` |

Task body schema:
```json
{
  "title": "<string, required>",
  "notes": "<string, optional>",
  "dueDate": "yyyy-MM-dd (optional)",
  "priority": "none|low|medium|high (optional)",
  "source": "voice-dictation"
}
```

---

## 4. What NOT to do

- Do **not** use `"add this"` as a standalone trigger phrase. The user says
  this constantly during normal speech. Phrase-matching requires more context
  ("add this **to TaskFlow**", "add **to my tasks**").
- Do **not** cache the port number between sessions. Re-read `port.json` fresh
  every time — the port can change between TaskFlow restarts.
- Do **not** make voice-dictation depend on TaskFlow being installed. The
  auto-launch coupling (fire-and-forget health check on startup) must never
  block or delay voice-dictation's own startup.
- Do **not** add credentials to the API call. It is loopback-only by design.

---

## 5. Test plan for v2 changes

**Trigger phrase change:**
1. Hold Ctrl+Alt, say "add to my tasks: pick up the kids", release.
2. Preview shows "Add Task: pick up the kids", editable.
3. Confirm → task appears in TaskFlow with source "voice-dictation".
4. Repeat with "add this to TaskFlow buy milk" and "add a task call dentist".
5. Say "add to my to-do list something" → confirm it does NOT trigger TaskFlow
   (phrase removed from defaults).

**Ctrl+Shift+Alt mode (if implemented):**
1. Hold Ctrl+Shift+Alt, say "buy milk", release.
2. Preview shows "Add Task: buy milk" — no phrase stripping needed.
3. Confirm → task appears in TaskFlow.
4. Hold Ctrl+Shift+Alt, say "call the dentist on Friday", release.
5. Preview shows "Add Task: call the dentist on Friday".
6. Confirm → task appears with no date parsing (title is literal).
7. Hold Ctrl+Alt (no Shift), say "buy milk" → confirm it goes through normal
   paste flow (no trigger match → normal paste, NOT TaskFlow).

**Regression (unchanged Ctrl+Alt flow):**
1. Hold Ctrl+Alt, say a normal sentence with no trigger phrase.
2. Confirm it pastes normally to the cursor — no TaskFlow involvement.

---

## 6. Context: what changed in TaskFlow v2 (2026-06-27)

The following was added to TaskFlow itself. Voice-dictation does not need to
implement any of this — it is for awareness only.

- **Global hotkey Ctrl+Shift+Space** (registered in the TaskFlow WPF process):
  opens a floating QuickAddWindow directly inside TaskFlow. This is a
  **separate, independent path** from voice-dictation capture. No conflict.
- Window chrome (minimize/maximize/close) bugs fixed.
- Task row layout redesigned — completion circle button, hover-reveal actions.
- Tray: right-click "New Task…", tooltip shows open/overdue count.
- Tasks added via voice-dictation appear within the 3-second live poll.
