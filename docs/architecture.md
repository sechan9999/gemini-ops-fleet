# Architecture

Three views. The first shows what runs where, the second shows how a request is
constrained, and the third shows how work happens with nobody watching. The
second and third are the ones that matter for the Fortified Enterprise Fleet
track — the first is just the map.

---

## 1. System

```mermaid
flowchart TB
    subgraph client["Callers"]
        emp["Employee<br/>X-Fleet-Token"]
        ps["Pub/Sub push<br/>event envelope"]
        a2a["Other agents<br/>A2A / JSON-RPC"]
    end

    subgraph run["Cloud Run · gemini-ops-fleet · scale-to-zero"]
        api["FastAPI<br/>/fleet/* governance surface"]
        card["A2A agent card<br/>/a2a/app/.well-known"]
        guard["GuardrailPlugin<br/>injection + PII screen"]
        coord["fleet_coordinator"]
        subgraph agents["Fleet"]
            tri["triage_agent<br/>autonomous"]
            kno["knowledge_agent<br/>autonomous"]
            fol["followup_agent<br/>drafts only"]
            rec["reconcile_agent<br/>read only"]
        end
        tools["tools.py<br/>identity check · audit"]
        worker["worker.py<br/>outbox drain"]
    end

    subgraph google["Google Cloud"]
        gem["Vertex AI<br/>gemini-3.5-flash"]
        armor["Model Armor"]
        sql[("Cloud SQL · Postgres<br/>fleet data + ADK sessions")]
        mb[("Memory Bank<br/>Agent Engine")]
        sm["Secret Manager<br/>DB password"]
        trace["Cloud Trace<br/>OTel spans"]
    end

    emp --> api
    ps --> api
    a2a --> card
    api --> coord
    card --> coord
    coord --> guard
    guard --> tri & kno & fol & rec
    tri & kno & fol & rec --> tools
    coord --> gem
    tri & kno & fol & rec --> gem
    guard -.screens.-> armor
    tools --> sql
    api --> worker
    worker --> sql
    coord -.recall / store.-> mb
    run -.password at boot.-> sm
    run -.every decision.-> trace
```

Two details in that picture are easy to miss and both were bugs first:

- **Cloud SQL is reached by two drivers.** ADK's session store runs on
  SQLAlchemy's asyncio extension and rejects a synchronous driver; the fleet's
  own repository code is synchronous. Same database, `asyncpg` for sessions and
  `pg8000` for everything else.
- **The employee token is not in `Authorization`.** Cloud Run's IAM layer
  consumes that header before the request reaches the app, so identity travels
  in `X-Fleet-Token`.

---

## 2. What makes a request safe

The interesting claim is not that the agents can act. It is what they cannot
reach. Every one of these gates is Python, not prompt text.

```mermaid
flowchart TB
    req["Request arrives"] --> ident

    subgraph server["Server-side, before the model"]
        ident["Resolve identity from token<br/>role read from the database"]
        block{"Guardrail:<br/>injection or PII?"}
    end

    ident --> block
    block -->|blocked| stop1["Refused before the model<br/>fleet.guardrail_blocked"]
    block -->|clean| model["Gemini 3.5 Flash<br/>decides which tool to call"]

    model --> acl{"May this role<br/>call this tool?"}
    acl -->|no| deny["denied<br/>fleet.access_denied = true"]
    acl -->|yes| scope

    subgraph data["Inside the tool"]
        scope["SQL predicate restricts rows<br/>to the caller's scope"]
        work["Read or append"]
    end

    scope --> work
    work --> audit["Audit row + span attribute<br/>allowed and denied alike"]
    deny --> audit

    work -.->|customer message| queue["Approval queue"]
    queue --> human{"Human approves?"}
    human -->|no| held["Held. send() raises 409"]
    human -->|yes| sent["Delivered"]

    style stop1 fill:#7f1d1d,color:#fff
    style deny fill:#7f1d1d,color:#fff
    style held fill:#78350f,color:#fff
    style sent fill:#14532d,color:#fff
```

Read the diagram from the failure paths, because those are the product:

| Gate | Where it lives | Why it holds |
|---|---|---|
| Identity | `identity.py` | No tool takes a role, identity, or employee id argument. A test asserts this over every tool signature, so the model has no vocabulary for claiming a role. |
| Tool ACL | `identity.check_tool_access` | `reconcile_accounting` is restricted to accounting and management. A refusal is recorded, not swallowed. |
| Row scope | `retrieval.permitted_documents`, `identity.visible_customers` | A SQL `WHERE` clause, evaluated before rows exist. Filtering is security; ranking runs afterwards on the permitted set and is free to change. |
| Guardrail | `guardrails.GuardrailPlugin` | Registered on the `App`, so it covers every agent at once. A per-agent callback would leak the moment someone adds an agent. |
| Human gate | `approvals.py` | Not a tool. No agent has a path to `approve` or `send`, and `send` re-checks rather than trusting the caller. |

---

## 3. How work happens with nobody watching

An agent that waits for a prompt is a chatbot. The fleet consumes an event
stream instead.

```mermaid
sequenceDiagram
    autonumber
    participant Biz as Business change
    participant DB as Cloud SQL
    participant PS as Pub/Sub
    participant W as Outbox drain
    participant A as Owning agent
    participant Q as Approval queue
    participant H as Human

    Biz->>DB: write record + Activity event<br/>(one transaction)
    Note over DB: The event cannot promise<br/>work that never happened
    PS->>W: push envelope
    W->>DB: claim event (dispatched = true)
    Note over W: Idempotent. A crash costs<br/>one redelivery, not a duplicate
    W->>A: route by event kind
    A->>DB: read within caller scope
    A->>Q: draft only, if outbound
    Note over A,Q: No agent can send
    H->>Q: approve
    Q->>H: delivered, approver recorded
```

Routing is a table, not a judgement call:

| Event | Owning agent | Autonomy |
|---|---|---|
| `as.opened` | `triage_agent` | autonomous |
| `as.resolved` | `knowledge_agent` | autonomous |
| `delivery.done` | `followup_agent` | drafts only |
| `transaction.posted` | `reconcile_agent` | read only |

An event with no owner is marked handled and logged rather than retried, so one
unrouted message cannot bury the queue behind it.

---

## What the traces carry

ADK already emits the execution skeleton. `app/tracing.py` adds the layer that
answers a different question — not *did it run* but *what was it allowed to do*:

| Attribute | Meaning |
|---|---|
| `fleet.caller_role` | Which department the call ran as |
| `fleet.authorization` | `allowed` or `denied` |
| `fleet.access_denied` | Set only on refusals, so they are findable without a filter |
| `fleet.guardrail_blocked` / `fleet.guardrail_reason` | What was stopped and why |
| `fleet.event_kind` / `fleet.routed_to` | Which agent picked up which event |
| `fleet.human_actor` | Who approved an outbound message |

Background dispatch opens its own root span. Without it, the asynchronous half
of the system has no incoming request to hang off and would be invisible.
