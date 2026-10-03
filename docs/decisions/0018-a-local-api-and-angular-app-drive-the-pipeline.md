# 0018. A local API and an Angular app drive the pipeline, and compute nothing

Status: Accepted

## Context

The engine has two surfaces. The CLI drives every step: `fbe refresh`,
`fbe report`, `fbe dashboard`, `fbe journal add`. The dashboard is one static
HTML file that `fbe dashboard` writes from a finished report. Neither lets the
owner start a run or journal a trade from a browser, and the owner asked for a
web app that does both, looks modern, and is easy to extend. Proposal #340,
approved by the owner on 2026-10-03.

Three facts constrain where and how it runs.

- **A cloud host loses data.** The RBNZ refuses every data-centre network at
  the TLS handshake, so NZD loses its 2-year yield and with it the monetary
  pillar, and Stooq serves automated clients a challenge page with HTTP 200
  (`docs/data-sources.md`, 0013). From the owner's home connection both work.
- **The journal is a private financial record** and is git-ignored for that
  reason (`CLAUDE.md`, "The `data/` directory"). A hosted app would need its own
  private storage and its own backup story for the one file no rerun recreates.
- **Part of the run lives in the CLI.** `fbe report` builds its `BiasReport`
  through private helpers in `src/fbe/cli.py` (`_daily_calendar`, `_filtered`,
  `_score_notes`, `_bias_notes`, `_coverage_collapsed`), and `fbe journal add`
  builds its record the same way (`_entry_view`, `_engine_view`,
  `_entry_blackout`, `_pillar_snapshot`, `_realised_money`,
  `_checked_journal_pair`). A second front end that calls the library directly
  would either duplicate them or skip them.

## Decision

**A FastAPI JSON API in `src/fbe/api/` and an Angular app in `frontend/` run on
the owner's own machine. The API drives and renders the pipeline. It computes
nothing, and neither does the Angular app.**

- **One code path.** Before any web code lands, the CLI helpers above move into
  library functions: `src/fbe/pipeline.py` (`refresh`, `build_run`) and
  `build_record` in `src/fbe/journal.py`. The CLI and the API both call them.
  The CLI tests passing unchanged is the evidence nothing moved.
- **Renderers compute nothing, on both sides.** An API route calls the library
  and returns its result. An Angular component shows the fields the API
  returns. A number a screen needs and the API lacks is added upstream, in the
  engine, the same rule `CLAUDE.md` sets for `report.py` and the dashboard.
- **Local by default.** `fbe serve` binds `127.0.0.1` and runs one worker. Phone
  access, when it comes, goes through a private network between the owner's own
  devices and never through a forwarded router port.
- **A token even on localhost.** The app refuses to start without
  `BIAS_WEB_TOKEN`, and every API route returns 401 without the login cookie.
  The name carries no `FBE_` prefix because `config.py` rejects any `FBE_`
  variable that names no setting. Requiring it from the first version means
  exposing the app later changes nothing about its security.
- **One job at a time, in process.** A run is one background thread behind a
  lock. A second start returns 409. Status is written atomically to
  `data/jobs/current.json`, which is git-ignored and safe to delete. A status
  left `running` by a dead process becomes `interrupted` at startup and is
  never resumed. The web layer adds no retries and no schedule: the base
  client's retry policy is the only one.
- **The API schema is the contract.** Pydantic response models in
  `src/fbe/api/schemas.py` describe what the API returns, FastAPI publishes them
  as OpenAPI, and the Angular client is generated from that, never hand-written.
  A test validates every committed `data/reports/*.json` against the models, so
  the engine and the contract cannot drift apart without a red test.
- **An optional extra.** FastAPI and uvicorn sit in a `web` extra, so a plain
  install stays CLI-only and the core dependencies do not grow.
- **A second language enters the repository.** TypeScript and Node.js tooling
  come with Angular, with their own CI job (`ng lint`, `ng test`, `ng build`)
  beside the Python one. `src/fbe/types.py` is not touched.
- **The look comes from the Beezhub design system.** Its tokens become CSS custom
  properties and theme Angular Material. A gap in the system, today a dark theme
  and a diverging palette for signed values, is closed in the system first
  rather than invented in the app, so the two do not disagree.

## Alternatives considered

**A cloud host.** The owner's first preference. Rejected for now because it
costs real data on every run, NZD's monetary pillar and Stooq's prices, and
moves the private journal onto someone else's disk. Reversible: nothing in this
decision prevents a hosted deployment later, and the token requirement already
holds.

**Server-rendered pages, Jinja with HTMX.** One language, no Node.js, and the
project already renders HTML with Jinja. Rejected because the owner wants a
modern interactive app that grows by feature, and a typed client generated from
the API contract keeps the two halves honest in a way template variables do not.

**React instead of Angular.** Comparable. Angular chosen by the owner. Its
built-in router, forms, HTTP client and dependency injection mean fewer choices
per feature, which suits the "easy to add" requirement.

**Call the library from the API without extracting the CLI helpers.** Rejected:
the browser would produce a different report from the terminal on the same
inputs, which is the one failure this layer cannot be allowed.

**A task queue (Celery, a separate worker).** Rejected as more machinery than
one owner starting one run a day needs. The lock and the status file cover it.

## Consequences

- Adding a feature follows one recipe: logic in `src/fbe/`, one route file in
  `src/fbe/api/routes/`, regenerate the client, one folder in
  `frontend/src/app/features/`.
- The web app only works while the owner's machine is on. That is the price of
  every source working and the journal staying private.
- Reports written from the web app land in `data/reports/` exactly as
  `fbe report` writes them, and are committed the same way.
- `fbe journal review` is still a Phase 6 stub. The journal screen shows what
  the library already computes and never fills the gap with numbers of its own.
- Contributors need Node.js to work on `frontend/`. Python-only work does not.
- A route or component that computes a figure is a defect against this record.

## Status

Accepted, 2026-10-03. Approved on #340.
