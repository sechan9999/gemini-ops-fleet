# Gemini Ops Fleet

A governed fleet of back-office agents for a small manufacturing/sales company — built for the
[All Things Agentic Hackathon](https://allthingsagentichackathon.devpost.com/),
**The Fortified Enterprise Fleet** track.

Ticket triage, knowledge capture, customer follow-up, and accounting reconciliation agents run
asynchronously off a shared event stream on Google Cloud, powered by Gemini through Vertex AI.
Every agent is cataloged in a registry and runs under role-based access control, inline guardrails,
and an audit trail — so a sales rep's agent cannot reach accounting documents, and no customer
message leaves the system without human approval.

Built with the A2A protocol on Google ADK. Agent generated with `agents-cli` version `0.5.0`.

## Prior Art Disclosure

Per the hackathon rules, all code in this repository was newly written during the Submission Period
(2026-08-03 — 2026-08-31).

The **design** of this system draws on an earlier personal project by the same author,
`unified-ops-ax` (built 2026-07-30/31, in the
[sechan9999/splunk_hec](https://github.com/sechan9999/splunk_hec) repository), which explored the
same domain on a different stack — self-hosted LLM providers, no Google Cloud, no ADK. That project
is cited here as prior art. No code was copied from it; this repository is a ground-up
reimplementation on the Gemini Enterprise Agent Platform.

## Project Structure

```
gemini-ops-fleet/
├── app/
│   ├── agent.py         # Root agent, identity seeding, memory callback
│   ├── fleet.py         # The four fleet members + coordinator
│   ├── tools.py         # Eight tools; every call audited, denials included
│   ├── identity.py      # Server-derived roles, row-level customer ownership
│   ├── retrieval.py     # Role filtering (SQL) kept separate from ranking
│   ├── approvals.py     # The human gate — unreachable from any agent
│   ├── guardrails.py    # Model Armor plugin + offline heuristic fallback
│   ├── registry.py      # Agent catalogue: version, scope, restrictions
│   ├── worker.py        # Idempotent outbox drain
│   ├── tracing.py       # Governance attributes on the OTel spans
│   ├── memory.py        # Cloud SQL sessions + Memory Bank
│   ├── routes.py        # Approval queue, registry, audit, Pub/Sub push
│   ├── domain.py        # Event stream and business records
│   └── store.py         # Engine, event emission, demo seed
├── demo.py              # End-to-end proof of the five claims
├── tests/unit/          # 48 tests, no credentials required
└── deployment/terraform/
```

## How It Works

A single `Activity` stream is the spine. Business changes write an event in the
same transaction, and agents consume that stream rather than a chat box:

```
order/ticket change ──▶ Activity (outbox) ──▶ Pub/Sub ──▶ /fleet/trigger/pubsub
                                                                │
                                    EVENT_ROUTES ───────────────┤
                                                                ▼
                        triage · knowledge · follow-up · reconcile
                                                                │
                                          tools ── identity check ── audit
                                                                │
                                        follow-up ──▶ approval queue ──▶ human
```

Three properties are enforced in code rather than asked of the model:

1. **Roles are server-derived.** No tool takes a role, identity, or employee id
   argument, so nothing the model produces can widen its own access. A test
   asserts this over every tool signature.
2. **Retrieval is filtered in SQL.** `retrieval.permitted_documents()` restricts
   the query by role before rows exist. Ranking runs afterwards, on the
   permitted set only.
3. **Nothing reaches a customer unapproved.** The follow-up agent can only queue
   drafts. `approvals.send()` refuses anything without a recorded human
   sign-off, and no tool exposes that path.

## Requirements

- **uv** — [install](https://docs.astral.sh/uv/getting-started/installation/)
- **agents-cli** — `uv tool install google-agents-cli`
- **Google Cloud SDK** — [install](https://cloud.google.com/sdk/docs/install)

## Run It Locally

Everything below works with **no credentials and no cloud project**. SQLite
stands in for Cloud SQL and a heuristic screen stands in for Model Armor, so the
governance behaviour is exercised offline.

```bash
uv sync --group dev
uv run pytest tests/unit -q
```

48 tests, covering the access-control and human-gate claims above.

To exercise the whole system against the real model, authenticate first:

```bash
gcloud auth application-default login
gcloud config set project <your-project-id>
```

Then run the proof script — it seeds a small demo company and walks the five
claims end to end:

```bash
GOOGLE_CLOUD_PROJECT=<your-project-id> \
GOOGLE_CLOUD_LOCATION=global \
GOOGLE_GENAI_USE_VERTEXAI=True \
uv run python demo.py
```

Expect: a shared policy retrieved with a citation; the same sales caller refused
an accounting document; accounting given that same document; an injection
attempt blocked before the model; and a customer follow-up left sitting unsent
in the approval queue.

## Deploy It

```bash
gcloud services enable \
  aiplatform.googleapis.com run.googleapis.com sqladmin.googleapis.com \
  pubsub.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com \
  --project=<your-project-id>
```

```bash
agents-cli deploy --project <your-project-id> --region us-central1 --min-instances 0
```

`--min-instances 0` lets the service scale to zero, so an idle deployment costs
nothing.

### Optional: durable state

Without this the service runs on SQLite in the container filesystem, which is
fine for a demo but resets when an instance is recycled.

```bash
gcloud sql instances create fleet-db \
  --database-version=POSTGRES_15 --tier=db-f1-micro \
  --region=us-central1 --storage-size=10 --storage-type=HDD --no-backup \
  --project=<your-project-id>
```

Store the password in Secret Manager rather than an environment variable, grant
the runtime service account `roles/cloudsql.client` and
`roles/secretmanager.secretAccessor`, then redeploy with the instance attached.
`app/config.py` builds the connection string from the Cloud SQL unix socket when
`INSTANCE_CONNECTION_NAME` is set, so no host, port, or password appears in any
plain environment variable.

Memory Bank needs nothing else — `app/memory.py` creates the backing Agent
Engine on first boot and reuses it by display name afterwards.

> If your project's IAM policy contains conditional bindings, `gcloud projects
> add-iam-policy-binding` refuses to run non-interactively without an explicit
> `--condition=None`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FLEET_MODEL` | `gemini-3.5-flash` | Pinned, not aliased, so the version is verifiable |
| `DATABASE_URL` | `sqlite:///fleet.db` | Overrides the Cloud SQL socket URL |
| `INSTANCE_CONNECTION_NAME` | — | Cloud SQL instance; switches on the socket URL |
| `DB_USER` / `DB_NAME` | `fleet` | Cloud SQL credentials |
| `DB_PASSWORD` | — | Injected from Secret Manager |
| `MODEL_ARMOR_TEMPLATE_ID` | — | Enables Model Armor; heuristics used when unset |
| `AGENT_ENGINE_ID` | — | Skips Memory Bank provisioning if you already have one |
| `FLEET_DEV_TOKEN` | — | Local only: attaches an employee identity to a session |

Demo employee tokens, one per department: `tok-sales`, `tok-support`,
`tok-accounting`, `tok-manager`.

## The Governance API

| Endpoint | Purpose |
|---|---|
| `GET /fleet/registry` | Agent catalogue, filterable by department or tag |
| `GET /fleet/approvals` | Drafts waiting on a human |
| `POST /fleet/approvals/{id}/approve` | Record a person's sign-off |
| `POST /fleet/approvals/{id}/send` | Deliver — 409 without sign-off |
| `GET /fleet/audit` | Tool calls, including refused ones |
| `GET /fleet/events` | The event stream and what is still pending |
| `POST /fleet/trigger/pubsub` | Pub/Sub push endpoint |

Approval and sending are HTTP-only by design. They are absent from every agent's
tool set.

## Commands

| Command              | Description                                                                                 |
| -------------------- | ------------------------------------------------------------------------------------------- |
| `agents-cli install` | Install dependencies using uv                                                         |
| `agents-cli playground` | Launch local development environment                                                  |
| `agents-cli lint`    | Run code quality checks                                                               |
| `agents-cli eval`    | Evaluate agent behavior (generate, grade, analyze, and more — see `agents-cli eval --help`) |
| `uv run pytest tests/unit tests/integration` | Run unit and integration tests                                                        |
| `agents-cli deploy`  | Deploy agent to Cloud Run                                                                   |
| [A2A Inspector](https://github.com/a2aproject/a2a-inspector) | Launch A2A Protocol Inspector                                                        |

## 🛠️ Project Management

| Command | What It Does |
|---------|--------------|
| `agents-cli scaffold enhance` | Add CI/CD pipelines and Terraform infrastructure |
| `agents-cli infra cicd` | One-command setup of entire CI/CD pipeline + infrastructure |
| `agents-cli scaffold upgrade` | Auto-upgrade to latest version while preserving customizations |

---

## Development

Edit your agent logic in `app/agent.py` and test with `agents-cli playground` - it auto-reloads on save.

## Deployment

```bash
gcloud config set project <your-project-id>
agents-cli deploy
```

To add CI/CD and Terraform, run `agents-cli scaffold enhance`.
To set up your production infrastructure, run `agents-cli infra cicd`.

## Observability

Built-in telemetry exports to Cloud Trace, BigQuery, and Cloud Logging.

## A2A Inspector

This agent supports the [A2A Protocol](https://a2a-protocol.org/). Use the [A2A Inspector](https://github.com/a2aproject/a2a-inspector) to test interoperability.
See the [A2A Inspector docs](https://github.com/a2aproject/a2a-inspector) for details.
