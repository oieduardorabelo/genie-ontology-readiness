# Genie Ontology Readiness

A readiness assessment that helps a customer **prepare for Genie Ontology**.
Run it as a Databricks App for the interactive experience, or as a CI job to save
assessment reports. The app will:

- **Assess** the live environment and score maturity across the readiness pillars
  Genie Ontology depends on (Unity Catalog, metadata, relationships, metrics /
  metric views, Genie Agents, domains, adoption).
- **Explain** each capability from a **technical** and a **business** standpoint, with
  accurate GA / preview status.
- **Recommend** best practices for both technical enablement and business adoption.
- **Generate** a tailored, sequenced enablement + adoption plan and answer questions
  via an AI assistant — powered by the customer's **own Foundation Model API**.

The assessment is **read-only** and degrades gracefully when a signal isn't available.

> Genie Ontology is the *learned* enterprise context layer (gated preview) built on the
> customer's *governed* UC Business Semantics foundation (largely GA). Preparing for it =
> maturing that foundation. See `CLAUDE.md` for the product framing and full deploy steps.

## Walkthrough

The app has three tabs. Each walkthrough below is a short, sped-up screen capture.

### Assess — the 7-pillar readiness scorecard

Run a read-only assessment that scores your workspace across seven pillars, rolls up to a
0–100 readiness score and maturity stage, and expands each pillar to its signals and gaps.

![Assess tab walkthrough: running the readiness assessment, viewing the overall score and pillar-maturity radar, and expanding a pillar to see its signals and gaps.](assets/assess-cuj.gif)

### Plan — a tailored action plan

Generate a prioritized, tactical action plan from a saved assessment — grounded in your real
scores and gaps and powered by your workspace's own Foundation Model API.

![Plan tab walkthrough: selecting an assessment and generating an AI action plan with prioritized recommendations, a suggested sequence, and example Genie use cases.](assets/plan-cuj.gif)

### Learn — enablement and accelerators

Explore each capability from a technical and a business angle, with best practices, downloadable
guides, and the public Databricks accelerators that raise each pillar's score.

![Learn tab walkthrough: browsing a capability's technical and business value, best practices, accelerators, and the downloadable AI-ready-semantics handbook.](assets/learn-cuj.gif)

## Quick start

**Get the code.** For a **stable build**, clone the latest tagged
[release](https://github.com/databricks-solutions/genie-ontology-readiness/releases)
— this is the recommended source for customer deployments. For a **staging build**
with the newest, unreleased changes, clone `main` directly.

```bash
# Stable — latest release (recommended)
tag=$(gh release view --repo databricks-solutions/genie-ontology-readiness --json tagName -q .tagName)
git clone --branch "$tag" https://github.com/databricks-solutions/genie-ontology-readiness.git
# (no gh? browse the Releases page above and: git clone --branch <tag> <repo-url>)

# Staging — latest main (newest, unreleased)
git clone https://github.com/databricks-solutions/genie-ontology-readiness.git
```

Then build and deploy:

```bash
cd app/frontend && npm install && npm run build && cd ../..
databricks bundle deploy -t dev --profile <p> --var="warehouse_id=<id>"
DATABRICKS_PROFILE=<p> TARGET=dev WAREHOUSE_ID=<id> python3 scripts/post_deploy.py
```

The `-t <target>` selects the environment and fixes the app name (`dev`, the
default → `genie-ontology-readiness-dev`, `stg` → `…-stg`, `prod` → the bare
`genie-ontology-readiness`). There is no `--var app_name` — the name is pinned
per target so a deploy can never rename or delete another environment's app. Keep
`TARGET` in step 2 matching the `-t` in step 1. **For a production install**, use
`-t prod` (with `TARGET=prod`) to get the unsuffixed `genie-ontology-readiness`.

See **[CLAUDE.md](./CLAUDE.md)** for prerequisites, the service-principal grants the
assessment needs, local development, optional Lakebase history, and branding.

## Run an assessment in CI

The Python CLI runs the same seven-pillar assessment as the App and saves its
results as JSON, Markdown, and a branded PDF. It calls your Databricks workspace
and SQL warehouse directly. You do not need an App deployment, frontend build,
Lakebase database, or Foundation Model API access.

For local execution, install the backend dependencies in a virtual environment
and configure a Databricks CLI profile:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r app/requirements.txt
export DATABRICKS_CLI_PROFILE=<your-profile>
export DATABRICKS_WAREHOUSE_ID=<warehouse-id>
PYTHONPATH=app python -m server.assessment.cli \
  --catalogs gold,silver \
  --workspace-ids <workspace-id> \
  --output-dir reports
```

`--workspace-mode exclude` excludes the given workspace IDs from activity signals.
`--title "Customer readiness"` sets the report title. Run the CLI with `--help` to
see its arguments. Authentication also supports `DATABRICKS_HOST` with
`DATABRICKS_TOKEN`, or the SDK's unattended OAuth authentication described below.

### GitHub Actions setup

Enable Actions in your fork, then configure these values under **Settings →
Secrets and variables → Actions**:

| Kind | Name | Value |
| --- | --- | --- |
| Variable | `DATABRICKS_HOST` | Workspace URL |
| Variable | `DATABRICKS_WAREHOUSE_ID` | Existing SQL warehouse ID |
| Variable, optional | `ASSESS_CATALOGS` | Comma-separated catalog names |
| Variable, optional | `ASSESS_WORKSPACE_IDS` | Comma-separated workspace IDs |
| Secret | `DATABRICKS_CLIENT_ID` | CI service principal application ID |
| Secret | `DATABRICKS_CLIENT_SECRET` | Databricks OAuth secret for that principal |

Assign the CI service principal to the workspace and grant it `CAN USE` on the
warehouse and the catalog/system-table permissions in the table below. The
workflow uses [Databricks OAuth machine-to-machine authentication](https://docs.databricks.com/aws/en/dev-tools/auth/oauth-m2m)
with `DATABRICKS_AUTH_TYPE=oauth-m2m`. Every read uses the CI identity's permissions.
The existing engine calls that identity "App service principal" in some report
labels, even when it runs outside an App.

The workflow file must be present on the repository's default branch for the
manual trigger to appear. Open **Actions → Readiness assessment → Run workflow**.
You can override catalogs and workspace IDs for each run, choose include or
exclude mode, and enable partial results. Blank catalog/workspace inputs use the
repository variables. The workflow runs on an Ubuntu runner with Python 3.12 and
a 30-minute job timeout. A workspace with private network access requires a
runner that can reach its endpoints, such as a self-hosted runner.

Download `readiness-<run-id>-<attempt>` from the workflow run's artifacts. It contains:

- `assessment.json`, the full scorecard, findings, metrics, and source queries.
- `readiness.md`, the assessment report with scores, gaps, and recommended practices.
- `readiness.pdf`, the same report rendered with the App's PDF styling.

Artifacts remain available for 14 days. They contain workspace metadata, so
choose the fork's visibility and access permissions accordingly. The default
local `reports/` directory is gitignored. This workflow is manual only. It does
not deploy resources or generate the separate AI action plan.

### Scope and incomplete results

With no catalog or workspace selection, the engine assesses visible catalogs
and unfiltered activity signals. Activity can cover multiple workspaces in the
system tables. An include filter also lets the engine derive catalog scope from
workspace bindings when readable; explicit catalogs take precedence. Metadata
signals remain catalog/metastore based rather than workspace specific.

The CLI checks authentication, warehouse access with a read-only `SELECT 1`, and
report dependencies before running the probes. Exit code `0` means success,
`1` means an assessment/report failure or an unavailable pillar, and `2` means
configuration or preflight failure. A low readiness score alone does not fail
the job.

Unavailable pillars retain the engine's existing scoring behavior and contribute
zero to the overall score. They appear as unavailable in the report. By default,
the CLI saves the reports and fails the job so that an incomplete assessment
does not pass unnoticed. Use `--allow-partial`, or the workflow's partial-results
option, when this is expected. This option does not suppress preflight,
execution, or report-generation errors. Availability reflects the existing
probe results; an available pillar can still use fallback sources or have
individual signals missing.

The workflow attempts artifact upload even after a failed assessment. JSON and
Markdown survive a PDF-generation failure; a preflight failure produces no new
reports. The CLI replaces reports in its output directory, so use a different
directory when you want to keep earlier runs. Lakebase history is not used.

## Permissions required

The app reads only **metadata** (`information_schema`, tags) and **system tables**
(`system.access.*`, `system.query.*`) — never your actual table data. Every signal
defaults to **on-behalf-of-user (OBO)** with an automatic **SP fallback**:

- **Interactive** — a person viewing the app. With **on-behalf-of-user (OBO)**
  authorization enabled, **every signal runs as the viewing user** (their own Unity
  Catalog + system-table grants), so the assessment reflects exactly what *you* can
  see. If a given read fails because you lack a grant the app SP holds (system
  tables are the common case), that read **falls back to the app SP** rather than
  dropping the signal.
- **Scheduled / unattended** — snapshot history or any background run with no user
  present (also local dev). With no forwarded token every read runs as the app
  **service principal (SP)**, so the SP must hold the grants below.

A few reads are **always** the SP (they can't run OBO): the Genie REST API (not
covered by the `sql` user scope) and Lakebase credential minting.

### Who needs what

| Assessment area | Reads from | Grant needed (held by the **viewer** under OBO, or by the **SP** when unattended) |
|---|---|---|
| Run any query | SQL warehouse | `CAN USE` on the warehouse |
| UC Foundation · Metadata · Relationships · Metrics · Domains (tags) | catalog / `system.information_schema` | `BROWSE` on each assessed catalog (metadata-only, least privilege) — or `USE CATALOG`+`USE SCHEMA`+`SELECT` |
| "Not in Unity Catalog" coverage | `hive_metastore.information_schema` | read on `hive_metastore` (if legacy access is enabled) |
| Genie Agents (count + activity) | `system.access.audit` (`aibiGenie` events) | `USE`+`SELECT` on `system.access` |
| Adoption & Activity | `system.access.audit`, `system.query.history` | `USE`+`SELECT` on `system.access` and `system.query` |
| Top‑10 most‑accessed + certified | `system.access.table_lineage` + `information_schema.table_tags` | `USE`+`SELECT` on `system.access` + catalog metadata |
| Plan / Assistant (LLM) | Foundation Model API | model serving / FMAPI enabled for the workspace |

> [!NOTE]
> **SP fallback.** Under OBO each signal is attempted **as the viewer first**. If
> that read fails — most commonly because the viewer lacks a grant the app SP holds
> (system tables such as `system.access` / `system.query`) — the app **transparently
> retries the same read as the service principal** instead of dropping the signal.
> This means a signal can appear in the assessment even when the viewer can't read
> it directly, *provided the SP is granted*. If neither the viewer nor the SP holds
> the grant, the signal degrades to "not available." The fallback is per-read and
> lives in `app/server/sql_client.py` (`execute_sql`); the SP-only reads that never
> attempt OBO (Genie REST, Lakebase) are opted out with `force_sp=True`.
>
> **SP-only mode.** To disable OBO entirely at deploy time, set the `FORCE_SP=true`
> env (in `app.yml`, or `export FORCE_SP=true` before `post_deploy.py`). Every read
> then runs as the app SP regardless of any forwarded viewer token — useful when you
> want consistent, workspace-wide system-table signals (e.g. Adoption) that don't
> vary by who's viewing. The SP must hold the grants below. Default is `false`.

`scripts/setup_app_permissions.py` (run by `post_deploy.py`) applies the SP grants; see
**[CLAUDE.md](./CLAUDE.md)** for the exact statements and the OBO details.

## Stack

FastAPI + React/Vite + Tailwind, deployed as a Databricks App via Databricks
Asset Bundles.

## License

Provided under the **Databricks License** — see [`LICENSE.md`](./LICENSE.md) and
[`NOTICE.md`](./NOTICE.md). Third-party dependencies are subject to their own licenses,
declared in the respective package manifests.

## Support

This project is a Databricks **Field Engineering solutions example**, published as a
demonstration accelerator. It is **not** an official Databricks product and is **not**
covered by any Databricks Support agreement, SLA, or warranty.

- **Provided AS-IS**, with no warranties or conditions of any kind. There are **no SLAs**
  and no commitment to maintenance, bug fixes, or future updates.
- **Community / field-maintained** on a best-effort basis by the owner below — not
  staffed by Databricks Support or Engineering.
- **Not a substitute for official guidance.** Validate GA / preview status against the
  official [Databricks documentation](https://docs.databricks.com/) before making
  decisions.
- To report a security issue, see [`SECURITY.md`](./SECURITY.md).

For questions or issues, **open an issue on this repository** or contact the maintainer:
**Allan Cao** (`allan.cao@databricks.com`). See [`NOTICE.md`](./NOTICE.md) for the full
disclaimer.
