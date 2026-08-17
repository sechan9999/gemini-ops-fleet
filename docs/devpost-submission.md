# Devpost Submission Text

Paste-ready copy for the All Things Agentic Hackathon entry, Fortified Enterprise
Fleet track. Kept in the repo so it stays consistent with the demo script and the
README as the project changes.

Three things this copy deliberately does not claim: Memory Bank recall (wired and
provisioned, but retrieval returns nothing), semantic search (retrieval ranks by
keyword overlap today), and cross-service A2A (the card advertises the fleet, but
delegation happens in one process). Each is stated plainly in Challenges or
What's next instead.

---

## Inspiration

A fifty-person manufacturing company we know has no IT department. Sales, support,
accounting, and the owner each keep their own spreadsheets, and a support ticket that
arrives at two in the morning sits until someone opens a laptop. They are exactly the
kind of company agents should help.

They are also the kind of company that cannot survive an agent emailing a customer
something wrong, or handing a sales rep the margin memo before the quarter closes. Every
agent demo we looked at answered "how much can it do on its own?" Nobody was answering
"what is it structurally unable to do?" — which is the only question a company without a
compliance team can actually act on.

So we inverted the pitch. This project is not interesting because the agents are
autonomous. It is interesting because of what they are prevented from reaching, and
because those limits are enforced in code rather than requested in a prompt.

## What it does

Gemini Ops Fleet is a fleet of four governed back-office agents that run off a shared
event stream instead of a chat box.

- **Triage** classifies incoming tickets and assigns an owner
- **Knowledge** turns resolved tickets into searchable documents
- **Follow-up** drafts customer messages — and can only draft
- **Reconcile** checks the order book against the ledger, read-only

A business change writes its record and an `Activity` event in the same transaction. That
event goes to Pub/Sub, gets pushed to the service, and the agent that owns that event type
picks it up. Nobody is waiting at a prompt.

Three guarantees hold regardless of what anyone types:

**Roles are server-derived.** No tool accepts a role, identity, or employee id argument, so
the model has no vocabulary for claiming one. A test asserts this across every tool
signature, which means the guarantee survives future changes rather than resting on
discipline.

**Retrieval is filtered in SQL.** A sales rep asking for the Q3 margin memo gets nothing —
not a refusal message the model composed, but an empty result, because the document was
excluded by a `WHERE` clause before any row existed. Filtering is security and runs first;
ranking is quality and runs afterwards on the permitted set only.

**Nothing reaches a customer unapproved.** The follow-up agent can queue a draft and that
is the end of its reach. Approving and sending are HTTP endpoints, absent from every
agent's tool set, and `send()` refuses with a 409 if no human signed off.

Around that sit an agent registry that publishes each agent's version, scope, and
*restrictions*; an inline guardrail plugin that blocks prompt injection before the model is
called; and an audit trail plus OpenTelemetry spans that record refusals with the same
weight as successes.

## How we built it

**Gemini 3.5 Flash through Vertex AI**, pinned rather than aliased — `gemini-flash-latest`
resolves to whatever is current and proves nothing to someone reading the repo.

**Google ADK** with the A2A template. The four agents are ADK agents under a coordinator;
the deployed service publishes an A2A agent card advertising every agent and tool. A
`BasePlugin` carries the guardrail, registered on the `App` so it covers every agent at
once — a per-agent callback would leak the moment someone adds a fifth agent and forgets.

**Google Cloud**: Cloud Run with scale-to-zero, Cloud SQL Postgres for state, Pub/Sub with
an OIDC-authenticated push subscription, Secret Manager for the database password, Model
Armor for guardrails with a heuristic fallback, Cloud Trace for spans, and a Memory Bank
Agent Engine.

We kept one rule throughout: **the whole system runs offline with no credentials.** SQLite
stands in for Cloud SQL and a heuristic screen stands in for Model Armor, so all 49 tests —
including every access-control and human-gate assertion — run on a laptop with no cloud
project. A reviewer can verify the claims before deciding whether to trust the demo.

## Challenges we ran into

**We leaked a database password into Cloud Logging.** A failed connection raises with the
full URL in the exception message, and our handler logged the exception with a traceback.
We caught it while reading startup logs, rotated the password, and added a `redact()` that
every log site touching a URL now passes through. The handler logs the exception *type*
now, never the exception. It was the most useful bug of the project: we were writing an
access-control system and had a credential in plain text three layers down.

**ADK's session store and our repository code cannot share a driver.** The session service
runs on SQLAlchemy's asyncio extension and rejects a synchronous driver outright; our own
code is synchronous. The fix is two URLs against the same database — `asyncpg` for
sessions, `pg8000` for everything else — and they differ in a way that is easy to miss:
asyncpg wants the socket *directory*, pg8000 wants the socket *file*.

**Cloud Run's IAM layer eats the `Authorization` header.** We were passing employee tokens
there, and behind an authenticated ingress every governance endpoint returned 401 while
working perfectly in tests. Identity moved to `X-Fleet-Token`.

**Memory Bank ingests but does not recall.** This one we did not solve. The service
resolves to `VertexAiMemoryBankService`, the Agent Engine exists with memory topics
configured, `add_session_to_memory()` succeeds and logs an ingest — and `memories.list()`
and `search_memory()` both return nothing. We are reporting it as unresolved rather than
describing the wiring as if it were working, because the difference matters to anyone
evaluating this.

Smaller ones worth naming: a Pub/Sub push subscription is created successfully and then
fails every delivery with 401 unless Pub/Sub's own service agent has
`serviceAccountTokenCreator` on the push identity; and a project with conditional IAM
bindings refuses `add-iam-policy-binding` non-interactively without an explicit
`--condition=None`.

## Accomplishments that we're proud of

**We proved persistence instead of assuming it.** We wrote an event, replaced the entire
Cloud Run revision — new container, new filesystem — and read it back. It was still there.
That is the difference between "we configured Cloud SQL" and "state survives."

**The demo shows refusals.** Four of the eleven cuts in our video end in a failure that is
supposed to happen: a guardrail blocking before the model, a tool refusing a department, a
409 on an unapproved send, a denial in the audit log. Anyone can film an agent succeeding.

**The registry publishes what each agent may not do.** Version, owning department,
capabilities, and restrictions — plus an autonomy grade of `autonomous`, `drafts_only`, or
`read_only`. A test asserts the follow-up agent is registered as `drafts_only` and that no
tool with "send" in its name is listed, so the catalogue cannot advertise a capability the
code does not permit.

**Denials are first-class in telemetry.** A refused call sets `fleet.access_denied` on its
span, so refusals are findable in Cloud Trace without constructing a filter. Background
dispatch opens its own root span — without it, the asynchronous half of the system has no
incoming request to attach to and would be invisible.

## What we learned

**Filtering and ranking must be separate things.** Once we split them, the security
property stopped depending on retrieval quality. We can swap keyword matching for pgvector
embeddings tomorrow without touching the boundary, because the boundary is a SQL predicate
that runs first.

**A guarantee the model is asked for is not a guarantee.** We wrote the restrictions into
every agent's instruction *and* enforced them in Python. When we tested the injection
attempt, the instruction was irrelevant — the guardrail stopped it before the model was
called, and the tool ACL would have stopped it after. The instruction is a courtesy; the
code is the contract.

**Silent fallbacks are worse than failures.** Our session service degrades to in-process
when Cloud SQL is unreachable. That is correct behaviour, but for a while it degraded
*quietly*, and we thought persistence was working when it was not. Every fallback now says
so in the log. A fleet that quietly forgets is worse than one that admits it cannot
remember.

**Write the offline path first.** Making the entire system runnable without credentials was
not a testing convenience — it forced every cloud dependency behind a port, which is why
swapping the LLM provider, the vector store, and the guardrail backend each turned out to
be a one-file change.

## What's next for Gemini Ops Fleet

**Finish Memory Bank.** Our leading hypothesis is a scope-key mismatch between what
ingestion writes and what retrieval queries. Until it recalls, we would rather ship
durable Cloud SQL sessions and say so plainly.

**Split an agent across a process boundary.** All four currently run as sub-agents in one
service. The A2A card advertises them, but nothing crosses a network hop yet. Deploying
reconcile as its own A2A service would make the fleet claim literal rather than structural
— and would let accounting own its agent's deployment independently.

**pgvector for retrieval.** The security boundary is already the right shape for it; only
the ranking function changes.

**Approval UI.** The queue is an API. The people who need it are not going to curl it.

---

## Prior art disclosure

Per the hackathon rules, all code in this repository was newly written during the
Submission Period. The design draws on an earlier personal project by the same author,
`unified-ops-ax` (built 2026-07-30/31, in the sechan9999/splunk_hec repository), which
explored the same domain on a different stack — self-hosted LLM providers, no Google
Cloud, no ADK. No code was copied; this is a ground-up reimplementation on the Gemini
Enterprise Agent Platform. The same disclosure appears at the top of the README.
