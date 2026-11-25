<a id="readme-top"></a>

<div align="center">
  <h1>Once Sport Timeline MCP</h1>
  <p>A local MCP bridge for human-reviewed timelines, with an optional persistent video-workflow workspace.</p>
</div>

<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white&style=for-the-badge">
  <img alt="MCP stdio" src="https://img.shields.io/badge/MCP-stdio-5B5BD6?style=for-the-badge">
  <img alt="FFmpeg" src="https://img.shields.io/badge/FFmpeg-media%20tools-007808?logo=ffmpeg&logoColor=white&style=for-the-badge">
  <img alt="Pillow" src="https://img.shields.io/badge/Pillow-image%20processing-8CAAE6?style=for-the-badge">
</p>

---

<details>
  <summary>Table of contents</summary>

- [What it does](#section-what-it-does)
- [Status](#section-status)
- [Install and launch](#section-install-and-launch)
- [Configuration](#section-configuration)
- [Default timeline tools](#section-default-timeline-tools)
- [Optional video workflow](#section-optional-video-workflow)
- [Optional native Once export](#section-optional-native-once-export)
- [Optional target tracking](#section-optional-target-tracking)
- [Time stop (hold)](#section-time-stop-hold)
- [Formation drawings](#section-formation-drawings)
- [Statistics, playlists, drawing kinds, speed](#section-more-analysis)
- [Proposal and review gate](#section-proposal-and-review-gate)
- [CLI reference and recovery](#section-cli-reference-and-recovery)
- [CSV mapping and native Once limits](#section-csv-mapping-and-native-once-limits)
- [Privacy, effects, and checks](#section-privacy-effects-and-checks)
- [References](#section-references)
</details>

---

## What it does
<a id="section-what-it-does"></a>

This package exposes four JSON-only timeline tools by default. They validate, filter, summarize, and provisionally export human-supplied events; they do not watch a match, infer tactics, identify players, or operate Once Sport.

An explicitly configured workflow adds a persistent local workspace, queue/control tools, host-authored analysis proposals, and a separate media worker. The host agent may inspect extracted evidence and draft a proposal; an operator must separately review the exact proposal digest before an export is queued. The project does not run an LLM, authenticate the reviewer, write Once Sport's database, or control the Once desktop.

The optional media workflow may process a permitted local video or one YouTube video. Only that opt-in worker handles media acquisition/processing; the default timeline tools do not download footage. Keep source media in a dedicated permitted-source directory, not in this repository.

<div align="right"><a href="#readme-top">↖ Back to top</a></div>

---

## Status
<a id="section-status"></a>

**Pre-release (`0.1.0a1`).** Interfaces may change. Tested on Linux only, with FFmpeg 7+ and Python 3.13.

- Native Once export fails closed for holds, speed segments, text, flags and the newer drawing kinds; use the custom renderer for them.
- Tracking is seeded by the host, single-target per job. There is no re-identification, occlusion recovery or automatic player detection.
- Not implemented: magnifier, player vision, chroma key, image import, field mapping and field tracking.
- Opening native output in the Once GUI project list, XLSX in a desktop spreadsheet, and Windows/macOS are untested.
- Rendering 1080p costs roughly 0.1-0.25 s per output frame; keep clips short.
- Reviewer identity is an operator assertion, not authentication.

<div align="right"><a href="#readme-top">↖ Back to top</a></div>

---

## Install and launch
<a id="section-install-and-launch"></a>

Prerequisites:

- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).
- FFmpeg and ffprobe available on `PATH` for video workflow processing.
- yt-dlp available on `PATH` only when using a permitted YouTube source.

From the package root, install the locked project environment and start the stdio MCP server:

```sh
uv sync
uv run once-sport-mcp
```

The command starts an MCP stdio server, not an interactive shell. Connect it to an MCP client. No Hermes configuration is changed by this project. The generic client configuration below is a template: replace every path with an absolute path on the machine that launches the server, and adapt the outer keys to the MCP client. It does not configure a client automatically. This client template enables the optional workflow; omit the workspace and source-directory variables to retain only the four default tools.

```json
{
  "mcpServers": {
    "once-sport": {
      "command": "/absolute/path/to/once-sport-mcp/.venv/bin/once-sport-mcp",
      "args": [],
      "env": {
        "ONCE_SPORT_OUTPUT_DIR": "/absolute/path/to/dedicated-output",
        "ONCE_SPORT_WORKSPACE": "/absolute/path/to/dedicated-workspace",
        "ONCE_SPORT_ALLOWED_SOURCE_DIRS": "[\"/absolute/path/to/permitted-source\"]"
      }
    }
  }
}
```

<div align="right"><a href="#readme-top">↖ Back to top</a></div>

---

## Configuration
<a id="section-configuration"></a>

Environment variables are read when the MCP server starts. The default timeline service and opt-in workflow have separate storage settings:

| Variable | Default / format | Effect |
|---|---|---|
| `ONCE_SPORT_OUTPUT_DIR` | `.data/output`, relative to the server's working directory | Destination for newly generated legacy CSV files. Set an absolute, trusted local directory for a stable location. |
| `ONCE_SPORT_WORKSPACE` | Unset | Unset means only the default four timeline tools are registered. Set a dedicated absolute directory to opt into persistent workflow tools and state. `/`, the home directory, and paths containing `..` are rejected. |
| `ONCE_SPORT_ALLOWED_SOURCE_DIRS` | If unset while workflow is enabled, defaults to `workspace/inputs` | A JSON array of permitted absolute source directories, for example `["/absolute/path/to/source"]`. It restricts local source selection. Set the variable to a JSON-encoded array string. |
| `ONCE_SPORT_NATIVE_EXECUTABLE` | Unset | Optional trusted absolute path to the tested Once executable. Enables the headless native tool only with a workspace. The worker must separately receive `--native-once-executable PATH`; startup configuration does not prove the executable is available. |
| `ONCE_SPORT_TRACKING_ENABLED` | Unset / `0` | Exact `1` enables seeded target tracking with a workspace; exact `0` disables it. Other values are rejected. Install the optional `tracking` extra and separately give the worker `--enable-tracking`. |

Example shell values (replace the placeholders; do not use the example text literally):

```sh
export ONCE_SPORT_OUTPUT_DIR="/absolute/path/to/dedicated-output"
export ONCE_SPORT_WORKSPACE="/absolute/path/to/dedicated-workspace"
export ONCE_SPORT_ALLOWED_SOURCE_DIRS='["/absolute/path/to/permitted-source"]'
```

The workspace and source directory should be separate, dedicated locations. The MCP server persists workflow state in the workspace; it does not use Once Sport's project database. CLI and worker processes receive workspace/source directories as explicit arguments rather than relying on the MCP environment variables.

<div align="right"><a href="#readme-top">↖ Back to top</a></div>

---

## Default timeline tools
<a id="section-default-timeline-tools"></a>

The default server registers these four tools and accepts supplied JSON; it does not parse Once Sport project files:

- `validate_timeline`: validate an `events` array and return accepted records or diagnostics.
- `generate_once_import_csv`: validate events and create a new CSV in `ONCE_SPORT_OUTPUT_DIR`; it never overwrites an existing destination and returns `import_verified: false`.
- `search_events`: filter by optional period, phase, team, action substring, and player. Text matching is case-insensitive; team and player matching is exact.
- `summarize_phases`: return counts for all eight accepted phase labels and the supplied event records. Counts are not tactical findings.

An event has `event_id`, `period`, `start_seconds`, `end_seconds`, `team`, `phase`, and `action`; optional `players` is an array of names and `notes` is a string. Times are video offsets in seconds, not period-relative or match-clock values. Bounds must be finite, nonnegative, and ordered, with a maximum timestamp of `1e12` seconds; event start times must be nondecreasing. Boolean timestamps, unknown fields, control characters, and invalid Unicode are rejected. Equal starts, overlapping and zero-length intervals are allowed. Periods 1–4 are range-checked, but period progression, video duration, event truth, and tactical phase transitions are not inferred. Accepted phase labels are `defending`, `transition_to_attack`, `attacking`, `transition_to_defend`, `set_piece_attack`, `set_piece_defend`, `goal`, and `goal_conceded`.

The synthetic example is at [`examples/timeline.synthetic.json`](examples/timeline.synthetic.json). Keep the original JSON: legacy CSV contains only start time, end time, and an action-name JSON array of `[team, phase, action]`; event ID, period, players, and notes are not represented in those columns.

CSV filenames accept only letters, digits, underscores, and hyphens followed by `.csv`. The client supplies a filename, not an arbitrary destination directory.

Timeline content is not deliberately logged by the legacy server, but clients and their hosts may retain inputs and outputs. Restrict access to the server and output directory and avoid sensitive data.

<div align="right"><a href="#readme-top">↖ Back to top</a></div>

---

## Optional video workflow
<a id="section-optional-video-workflow"></a>

Setting `ONCE_SPORT_WORKSPACE` opts the MCP server into persistent local project/job state and additional workflow tools. The host agent authors all events, drawings, and report text from inspected evidence. The separate worker performs media ingestion, evidence extraction and custom export by default. An explicitly configured native backend instead starts the tested Once CLI for rendering; neither backend performs football analysis. A queued job is not a completed job.

Workflow MCP controls are:

- `analysis_capabilities` reports local capabilities and the native boundary.
- `create_analysis_project`, `get_analysis_project`, `get_analysis_job`, and `list_analysis_jobs` create and inspect persisted project/job state.
- `cancel_analysis_job` requests cancellation; `retry_analysis_job` explicitly creates a new retry job ID for eligible failures/interruption/cancellation.
- `request_analysis_evidence` queues bounded extraction of source-timestamped frames. Evidence sampling is not proof of continuous match coverage.
- `submit_analysis_proposal` stores host-authored events, drawings, an analysis-only Markdown report, and evidence job IDs. `get_analysis_proposal` reads the saved proposal, digest, and review history.
- `review_analysis_proposal` records an explicit operator assertion for one exact digest. It is not automatic approval and does not authenticate the reviewer.
- `request_analysis_export` queues an export only when the exact current proposal digest has been explicitly approved.
- `list_analysis_artifacts` and `get_analysis_artifact` verify and return local artifact metadata and paths. They do not stream artifact binaries through MCP.

### Dedicated-workspace quickstart

Use a dedicated absolute workspace and a permitted-source directory; substitute your real local paths. Do not put footage in the repository. These are command templates, not recorded successful command outputs:

```sh
export WORKSPACE="/absolute/path/to/dedicated-workspace"
export SOURCE_DIR="/absolute/path/to/permitted-source"
export VIDEO="$SOURCE_DIR/match-video.mp4"

uv run once-sport-workflow --root "$WORKSPACE" --allow-source-dir "$SOURCE_DIR" capabilities
uv run once-sport-workflow --root "$WORKSPACE" --allow-source-dir "$SOURCE_DIR" create-project --title "Match review" --source "$VIDEO"
```

For a YouTube source, set `VIDEO_URL` to an actual permitted single-video HTTPS watch or short URL (not a playlist). Use its exact video ID; extra playlist, timestamp, or tracking query parameters are rejected. This queues a real network download only when the separate worker claims the job:

```sh
uv run once-sport-workflow --root "$WORKSPACE" create-project --title "URL review" --source "$VIDEO_URL"
```

The selected downloadable format determines source quality; a low-resolution source is not true HD and is not improved by relabeling it.

The CLI prints one JSON envelope. Read `project_id` and the initial ingest `job_id` from that actual result; do not guess IDs. Start the worker independently in another terminal, using the same workspace and allowed source directory:

```sh
uv run python -m once_sport_mcp.worker --root "$WORKSPACE" --allow-source-dir "$SOURCE_DIR"
```

The worker runs independently of an MCP client connection. It is a separate process, not an installed daemon or a host-reboot service. `--once` may be added to process one available job and exit. Check job state explicitly; queued is not running or complete:

```sh
uv run once-sport-workflow --root "$WORKSPACE" --allow-source-dir "$SOURCE_DIR" get-job "JOB_ID_FROM_CREATE_RESULT"
uv run once-sport-workflow --root "$WORKSPACE" --allow-source-dir "$SOURCE_DIR" get-project "PROJECT_ID_FROM_CREATE_RESULT"
```

After ingest completes, choose a real source-video interval within its duration. Queue evidence and run the separate worker again or leave it running; then inspect the returned evidence job until it is completed:

```sh
uv run once-sport-workflow --root "$WORKSPACE" --allow-source-dir "$SOURCE_DIR" evidence "PROJECT_ID_FROM_CREATE_RESULT" --start SOURCE_START_SECONDS --end SOURCE_END_SECONDS --interval 2
uv run once-sport-workflow --root "$WORKSPACE" --allow-source-dir "$SOURCE_DIR" get-job "EVIDENCE_JOB_ID_FROM_RESULT"
```

Replace the proposal values only after examining the evidence. This JSON shape has exactly the required proposal keys; the placeholder text and empty arrays are not analysis or a successful example:

```json
{
  "events": [],
  "drawings": [],
  "report_markdown": "REPLACE WITH AN ANALYSIS-ONLY REPORT GROUNDED IN REVIEWED EVIDENCE",
  "evidence_job_ids": ["REPLACE WITH A COMPLETED EVIDENCE JOB ID"]
}
```

Each event uses the timeline fields documented above, with source-video offsets. Unlike the default JSON-only validator, the exportable workflow requires positive-duration clips within the probed video bounds. Drawing points use normalized coordinates from 0 to 1; a drawing belongs to its named event and must stay within that event's time window. Each drawing uses the timed renderer schema illustrated in [`tests/test_workflow_mcp.py`](tests/test_workflow_mcp.py): `id`, `event_id`, `kind`, `start_seconds`, `end_seconds`, `points`, and optional ordered `keyframes` containing `time_seconds` and `points`, plus optional custom-renderer `hold_seconds` for a time stop (see [Time stop](#section-time-stop-hold)). A keyframe's points are supplied by the host and positions are interpolated between keyframes. The optional seeded tracker can propose measured keyframes as review-required evidence; there is no automatic player detection or identity recognition. These are canonical render instructions. The default custom export does not create native Once drawings; the explicitly selected native backend accepts only its documented subset.

Submit the completed proposal and retrieve it to inspect the exact digest. Then STOP for a human review. The proposal author and the operator's explicit review are separate steps; do not approve automatically. The reviewer label is an operator assertion, not authenticated identity. Only after the operator has reviewed the saved proposal and intentionally decided to approve it, use that exact digest for both review and export:

```sh
uv run once-sport-workflow --root "$WORKSPACE" --allow-source-dir "$SOURCE_DIR" submit-analysis "PROJECT_ID_FROM_CREATE_RESULT" --proposal "/absolute/path/to/proposal.json"
uv run once-sport-workflow --root "$WORKSPACE" --allow-source-dir "$SOURCE_DIR" get-analysis "ANALYSIS_ID_FROM_SUBMIT_RESULT"

```

**Human review pause.** Stop here and inspect the saved proposal and digest. Only an intentional operator decision permits the next commands; do not run them automatically.

```sh
uv run once-sport-workflow --root "$WORKSPACE" --allow-source-dir "$SOURCE_DIR" review-analysis "PROJECT_ID_FROM_CREATE_RESULT" "ANALYSIS_ID_FROM_SUBMIT_RESULT" --digest "EXACT_DIGEST_FROM_GET_ANALYSIS" --reviewer "OPERATOR_LABEL" --approve
uv run once-sport-workflow --root "$WORKSPACE" --allow-source-dir "$SOURCE_DIR" export "PROJECT_ID_FROM_CREATE_RESULT" "ANALYSIS_ID_FROM_SUBMIT_RESULT" --digest "EXACT_APPROVED_DIGEST"
```

Export is queued, not complete, until the separate worker processes it. Inspect the export job, list artifacts for the project, then retrieve metadata for an artifact ID from that list:

```sh
uv run once-sport-workflow --root "$WORKSPACE" --allow-source-dir "$SOURCE_DIR" get-job "EXPORT_JOB_ID_FROM_RESULT"
uv run once-sport-workflow --root "$WORKSPACE" --allow-source-dir "$SOURCE_DIR" list-artifacts "PROJECT_ID_FROM_CREATE_RESULT"
uv run once-sport-workflow --root "$WORKSPACE" --allow-source-dir "$SOURCE_DIR" get-artifact "ARTIFACT_ID_FROM_LIST"
```

The returned local path is not remote binary delivery. An approved proposal change requires a new review of its new digest. Use rejected review instead of approval when declining a proposal.

<div align="right"><a href="#readme-top">↖ Back to top</a></div>

---

## Optional native Once export
<a id="section-optional-native-once-export"></a>

For the tested Once Sport **4.9.6** build, the optional native backend invokes the application's genuine batch-export CLI without mouse movement, clicks, an open window, a display or the Cursor editor. It is a version-pinned local CLI/file adapter, not a public vendor API. Native startup requires Linux Bubblewrap and an explicitly trusted `ONCE_SPORT_NATIVE_EXECUTABLE` in the MCP environment; the separately started worker needs the equivalent `--native-once-executable PATH` global flag.

With tracking disabled, a workspace but no native configuration has 18 tools. Explicit native configuration adds `request_native_analysis_export(project_id, analysis_id, expected_digest)`, for 19 tools. The default no-workspace service remains four tools. Native export reuses the exact-digest review gate and persisted export queue; it never chooses an executable from a tool payload or silently falls back to the custom backend.

The CLI equivalent is `export-native PROJECT_ID ANALYSIS_ID --digest EXACT_APPROVED_DIGEST`, with the global native flag supplied before the subcommand. The native projection supports timed arrows and ellipses, including supplied ellipse position keyframes with fixed radii; it rejects unsupported fields before queueing. The nine-file export bundle preserves the report/proposal/review/CSV/metadata and includes the raw native MP4, H.264 compatibility MP4, native XML and exact drawing sidecar. Moving the bundle does not copy its referenced source video.

See the headless native workflow for both process commands, supported subset, output roles, sandbox and verification limits, and the [generic native MCP client template](examples/mcp.native.example.json) for startup configuration. Native file/model save/reopen and genuine export are distinct from normal GUI project-list import, which remains unverified. Existing live projects are not rewritten by this workflow.

<div align="right"><a href="#readme-top">↖ Back to top</a></div>

---

## Optional target tracking
<a id="section-optional-target-tracking"></a>

Install with `uv sync --frozen --extra tracking` and opt in through `ONCE_SPORT_TRACKING_ENABLED=1` for MCP, or trusted global `--enable-tracking` for CLI/worker. Keep `--extra tracking` on subsequent project commands so an environment sync does not remove OpenCV. The separate worker processes one manually seeded visual target with CPU CSRT; no GPU, downloaded weights, Cursor editor, display or pointer control is required.

`request_analysis_tracking(project_id, start_seconds, end_seconds, initial_bbox)` takes a normalized XYWH seed chosen from actual source-frame evidence. The CLI equivalent is `track-target PROJECT_ID --start SECONDS --end SECONDS --bbox X Y W H`. Requests are limited to 12 seconds / 600 source frames and supported constant-cadence, square-pixel footage. Tracking-only workspace mode has 19 tools; tracking plus native export has 20. Tracking disabled preserves the previous 4/18/19 discovery modes.

The two verified artifacts are `tracking.json` and an unapproved diagnostic `tracking.preview.mp4`. Observed source-time keyframes and foot anchors remain **candidates**: include their completed job as proposal evidence and review the exact proposal digest before export. Detected loss/cuts/inconsistent motion produce an explicit reseed interval instead of frozen or extrapolated positions. Consistency scores do not establish identity; crossings, occlusion recovery and automatic re-identification are not guaranteed.

Both custom and native export can consume the same reviewed keys. Native `PlayerCircle2` preserves Once's 30-frame entrance animation; the custom ellipse is the option for a ring visible immediately without that native introduction. A [combined tracking/native MCP template](examples/mcp.tracking-native.example.json) is provided; it does not install or configure a client automatically.

<div align="right"><a href="#readme-top">↖ Back to top</a></div>

---

## Time stop (hold)
<a id="section-time-stop-hold"></a>

A custom-rendered clip can pause on a frame, keep the reviewed drawings visible, then resume from the next source frame. Add optional `hold_seconds` to the drawing whose `start_seconds` marks the stop, for example a label or formation drawing:

```json
{"id": "formation-stop", "event_id": "EVENT_ID", "kind": "label", "start_seconds": 3.0, "end_seconds": 7.0, "points": [[0.03, 0.05]], "text": "Defensive shape", "hold_seconds": 2.0}
```

`hold_seconds` must be finite and in `(0, 10]`. An event can contain one hold. The first output frame at or after the drawing start is painted once and copied for `round(hold_seconds × output fps)` additional frames (at least one), so the manifest clip `frame_count`, `reel_end_seconds` and every later clip move by the hold duration. The clip manifest gains `hold` with the drawing ID, source time, hold length and reel interval. Source audio is silent during the hold and resumes from the same source time.

Use ordinary `export` / `request_analysis_export`; the hold is part of the reviewed proposal digest. The native `export-native` backend rejects the plan instead of guessing, because only an observed native fixed pause for static equal-time marks exists and it is not configurable here. A hold has not been tested on crossings, long clips beyond the stated bounds, or the Once GUI. A tracked drawing during a hold is intentionally frozen with that frame; it does not continue tracking.

<div align="right"><a href="#readme-top">↖ Back to top</a></div>

---

## Formation drawings (multi-target)
<a id="section-formation-drawings"></a>

With tracking enabled, `build_formation_drawings` (CLI: `formation`) composes 2-11 completed single-target tracking jobs into editable drawings: lines between targets (default: left-to-right polyline, or explicit `links`, or `closed`), one ring per target, and an optional label with `hold_seconds` for a time stop. It is read-only: it creates no proposal, approval, job or artifact. Put all job IDs in `evidence_job_ids`, use the returned `suggested_event` times, then submit and approve as usual.

The trackers are independent and do not re-identify players. The composer uses only frames where every target has a result and stops before any pair's boxes overlap (IoU >= 0.15 or centres closer than 0.75 box width), reporting `limited_by: possible_overlap_or_identity_swap`, `conflict` and `needs_reseed`. Custom renderer only; native export rejects holds.

<div align="right"><a href="#readme-top">↖ Back to top</a></div>

---

## Statistics, playlists, drawing kinds and speed segments
<a id="section-more-analysis"></a>

- **Event metadata:** optional `flag` (`good`, `bad`, `favourite`) and `playlists` (names) on events, next to `players` and `notes`. Native export rejects `flag`; other fields are preserved.
- **Statistics:** after exact-digest approval, `request_analysis_statistics` (CLI `statistics PROJECT_ID ANALYSIS_ID --digest DIGEST`) writes `statistics.xlsx`, `statistics.html` (self-contained, no scripts) and `statistics.json`: counts and seconds by phase, team, action and period, goals, players, flags, playlists. It needs no video file.
- **Playlist export:** `request_analysis_export(..., playlist="NAME")` or `export --playlist NAME` renders only events listing that playlist; an unknown name fails at queue time.
- **New custom drawing kinds:** `polyline` (optional `arrow`), `curve` (3-point arrow), `area` (`opacity` 0.05-0.8, keyframes), `spotlight` (`style` halo or ground), `stopwatch`, and `speed` (a time segment: `speed` 0.25, 0.5, 2 or 4, no points). Slow motion repeats frames, fast forward drops frames; audio is silent inside the segment and a hold cannot start inside it. `build_formation_drawings` also accepts `area` and `spotlights`.
- Native export keeps failing closed for these features.

<div align="right"><a href="#readme-top">↖ Back to top</a></div>

---

## Proposal and review gate
<a id="section-proposal-and-review-gate"></a>

A proposal JSON file has exactly these four top-level keys: `events`, `drawings`, `report_markdown`, and `evidence_job_ids`. The CLI rejects missing/extra keys, duplicate object keys, malformed JSON, and non-finite constants. Events and timed drawings must refer to the source video timeline, not a match clock. The report is analysis-only Markdown; provider/model identity, production details, provenance hashes, and source inventories belong in internal workflow records/manifests, not the report body. Never invent player identities or tactical findings to fill a report.

The digest binds review to the exact proposal. Review requires exactly one of `--approve` or `--reject`, plus `--digest` and `--reviewer`. Export also requires `--digest` and fails closed unless that exact proposal has approval. The reviewer value is descriptive only: it is not a credential or proof of human identity. Rejected, stale, or changed proposals must not be exported as approved.

<div align="right"><a href="#readme-top">↖ Back to top</a></div>

---

## CLI reference and recovery
<a id="section-cli-reference-and-recovery"></a>

The installed entry points are `once-sport-workflow` and `python -m once_sport_mcp.workflow_cli`. Global `--root ROOT` is required; repeat `--allow-source-dir DIR` for each permitted source directory. Add trusted global `--native-once-executable PATH` only for native configuration and `--enable-tracking` only for tracking. Put these global flags before the subcommand. `--allowed-source-dir` is an alias.

Available commands and their positional/required arguments:

| Command | Purpose |
|---|---|
| `capabilities` | Report configured paths, available media tools, and optional native descriptor without claiming GUI project import. |
| `create-project --title TITLE --source SOURCE` | Queue ingestion of a permitted local path or one HTTPS YouTube video. |
| `get-project PROJECT_ID`; `get-job JOB_ID`; `list-jobs [--project-id PROJECT_ID]` | Inspect persisted project/job state and history. |
| `evidence PROJECT_ID --start SECONDS --end SECONDS [--interval SECONDS]` | Queue bounded source-frame extraction. |
| `track-target PROJECT_ID --start SECONDS --end SECONDS --bbox X Y W H` | Queue review-required seeded tracking; requires global `--enable-tracking` and a tracking-enabled worker. |
| `submit-analysis PROJECT_ID --proposal FILE`; `get-analysis ANALYSIS_ID` | Submit/read a proposal and its current digest/review. |
| `review-analysis PROJECT_ID ANALYSIS_ID --digest DIGEST --reviewer LABEL (--approve or --reject)` | Explicit review of one exact digest; the decision flags are mutually exclusive. |
| `export PROJECT_ID ANALYSIS_ID --digest DIGEST` | Queue export for the reviewed exact digest. |
| `export-native PROJECT_ID ANALYSIS_ID --digest DIGEST` | Queue the reviewed exact digest using the explicitly configured native backend; unsupported native plans fail closed. |
| `list-artifacts PROJECT_ID`; `get-artifact ARTIFACT_ID` | List or verify local artifact metadata and path. |
| `cancel-job JOB_ID`; `retry-job JOB_ID` | Request cancellation or create a new explicit retry ID. |

CLI success prints `{"ok": true, "result": ...}` and exits 0. A workflow/domain or local I/O failure prints `{"ok": false, "error": {"code": ..., "message": ...}}` and exits 1. Argparse usage/argument errors exit 2 and are not workflow JSON results. Treat the envelope and exit status together. Workflow MCP domain failures similarly return `ok: false`. The default timeline validation/query tools instead report `valid: false`, and legacy CSV export reports `ok: false`; MCP `isError` alone is not a domain-success test. Missing arguments or unknown tools may yield `isError: true`.

On disconnect, reconnect with a fresh MCP client or start a new CLI process against the same workspace and read the saved state. A job can remain queued until a worker claims it. Inspect status rather than assuming completion. Use cancellation deliberately; after an eligible failure, cancellation, or interruption, request an explicit retry and record its new job ID. A single-worker lock prevents concurrent worker claims. A process interruption is not a successful job. On an explicit worker restart, interrupted jobs become retryable and eligible owned residue is cleaned under the worker lock; they are not automatically requeued. Host-reboot service management is not included.

If a result reports `cleanup_pending` or a cleanup warning, inspect the workspace and referenced state; do not blindly remove data or retry by duplicating a committed artifact. Committed artifacts and protected source media must be preserved. Legacy CSV staging recovery is separate: ordinary pre-publication failure cleans its owned staging file and permits retrying the same filename if no destination exists; a forced termination can leave `.once-*.tmp` files. Inspect before manual cleanup and never remove a live export's staging file. CSV staging uses private mode-0600 files and atomic same-directory publication without overwriting existing files or symlinks; the output filesystem must support the required POSIX directory operations and hard links.

<div align="right"><a href="#readme-top">↖ Back to top</a></div>

---

## CSV mapping and native Once limits
<a id="section-csv-mapping-and-native-once-limits"></a>

The emitted legacy CSV is UTF-8 and comma-delimited, with the exact ordered headers `start time`, `end time`, and `action name`. Bounds are numeric seconds. Action names are Unicode-preserving JSON arrays `[team, phase, action]` serialized with standard CSV quoting, so commas and quotes remain recoverable. Treat spreadsheet imports as data, not trusted formulas. This is a provisional representation, not a confirmed Once Sport schema. Officially described import concepts are start time, end time, and action name, but the exact header, timestamp format, mapping, and label interpretation remain unverified. In a disposable Once project with permitted footage, manually import a test CSV, map fields if offered, and compare every label and time against the video, including period transitions. Record the app version and observed behavior. Do not treat CSV creation or a successful import attempt as verified native integration.

No general parser for arbitrary native Once XML/XLSX/HTML projects is included. The optional native exporter emits a narrowly supported XML projection validated against the pinned Once 4.9.6 loader/renderer. This does not validate the legacy CSV mapping or expose arbitrary desktop project editing.

The default `export` creates a custom FFmpeg reel with editable JSON sidecar data; that JSON does not make its drawings native or change an already burned-in MP4. The optional `export-native` instead retains a genuine Once-generated MP4 plus native XML and a compatibility transcode. Copy artifacts out before editing: modifying registered workspace artifacts invalidates their hashes. Submit a new proposal, review its new digest and export again for changes. Native file/model save/reopen and headless export have engineering-fixture evidence; normal GUI project-list import and full desktop editability remain separate unverified boundaries. No live Once database is edited.

<div align="right"><a href="#readme-top">↖ Back to top</a></div>

---

## Privacy, effects, and checks
<a id="section-privacy-effects-and-checks"></a>

The legacy read-only timeline tools are advertised as read-only/idempotent; CSV creation is a local non-destructive write, not an idempotent request. MCP annotations are planning hints, not security controls. The server suppresses diagnostics during stdio operation and restores the prior process logging threshold when it exits; this is not a general concurrency-safe logging-isolation API. Restrict client access to timelines, the workspace, source media, and exports.

The opt-in workflow uses local processing and persistent workspace state. It does not control the user's desktop, edit the live Once database, call a private online Once API, or provide a daemon/reboot guarantee. Native rendering is opt-in, build-pinned and sandboxed; custom export remains the default. No footage belongs in the repository. YouTube ingestion is a separate opt-in worker capability requiring yt-dlp; local-file support alone does not establish URL ingestion.

From the package root, run checks with:

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

To exercise the optional tracker tests, use `uv run --frozen --extra tracking pytest -q`. Base operation does not require OpenCV; real tracking tests skip when its optional dependencies are absent.

<div align="right"><a href="#readme-top">↖ Back to top</a></div>

---

## References
<a id="section-references"></a>

- [Synthetic legacy timeline example](examples/timeline.synthetic.json)

<div align="right"><a href="#readme-top">↖ Back to top</a></div>
