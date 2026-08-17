---
title: "Your agent's guardrails are in the wrong place"
published: false
description: "Building a governed multi-agent fleet on Gemini 3.5 Flash and ADK — and why every guarantee that mattered had to live in Python, not in the prompt."
tags: googlecloud, ai, python, showdev
---

*I built this project for the Google Cloud [All Things Agentic Hackathon](https://allthingsagentichackathon.devpost.com/), and I wrote this post for the purpose of entering that hackathon.*

---

Every agent demo I looked at while planning this answered the same question: how much can it do on its own?

That is the wrong question for the company I had in mind. Fifty people, manufacturing and sales, no IT department. They would love agents. What they cannot survive is an agent emailing a customer something wrong, or handing a sales rep the margin memo two weeks before the quarter closes.

For them the only useful question is the inverse: **what is this thing structurally unable to do?**

That reframing changed almost every implementation decision. Here are the three that transfer to any agent you are building, and the bugs that cost me the most time.

## Pattern 1: the model has no vocabulary for the thing you don't want it to say

The obvious way to scope an agent is to tell it: *you are the support agent, you may not access accounting documents.*

This fails the moment someone types "ignore all previous instructions." Not because the model is weak — because you put the constraint somewhere the input can reach.

So I removed the ability to express it. Every tool in the fleet looks like this:

```python
def search_knowledge(query: str, limit: int, tool_context: ToolContext) -> dict:
    who = caller(tool_context)          # resolved from the session, server-side
    check_tool_access(who, "search_knowledge")
```

There is no `role` parameter. No `identity`, no `employee_id`, no `as_role`. The model cannot emit a function call that claims a department, because no such argument exists in any schema it is shown.

The part I am most pleased with is that this is enforced by a test, not by discipline:

```python
def test_identity_cannot_be_supplied_as_a_tool_argument():
    for tool in tools.FLEET_TOOLS:
        params = set(inspect.signature(tool).parameters)
        assert not params & {"role", "identity", "employee_id", "as_role"}
```

Six months from now, someone adds a tool and slips a `role` argument in for convenience. The suite fails. The guarantee outlives whoever wrote it.

## Pattern 2: separate filtering from ranking, and never let them merge

This is the one I would tell everyone building RAG.

Retrieval usually does two things at once: find relevant documents, and don't show documents this person shouldn't see. When those live in the same function, your security property depends on your relevance function — and relevance functions change constantly.

So I split them:

```python
def permitted_documents(role: Role):
    """SELECT restricted to documents this role may read."""
    return select(Document).where(Document.allowed_roles.like(f"%,{role.value},%"))


def search(session, role, query, limit):
    candidates = session.scalars(permitted_documents(role)).all()   # security
    ...                                                            # relevance
```

**Filtering is a SQL predicate that runs first.** Out-of-scope documents never enter the process, so they cannot be ranked, summarised, logged, or leaked through a stack trace. Ranking then runs over whatever survived, and is free to be keyword overlap today and pgvector embeddings tomorrow. The boundary does not move.

A small storage detail mattered here. I originally stored `allowed_roles` as a JSON column — nicer to read, and it quietly pushed the filter into Python on SQLite. That would have made "filtered in SQL" a lie in local development. A comma-delimited string with sentinel commas keeps one portable predicate working identically on SQLite and Postgres:

```
",sales,support,accounting,manager,"   →   LIKE '%,sales,%'
```

Ugly. Correct in both places. I'll take it.

## Pattern 3: the dangerous action isn't a tool at all

The follow-up agent drafts customer messages. It cannot send them — and not because its instruction says so.

`approve()` and `send()` live in a module no agent imports. They are HTTP endpoints. The agent's entire reach ends at writing a row:

```python
def send(session, approval_id: int) -> dict:
    approval = session.get(Approval, approval_id)
    if not approval.approved:
        raise NotApproved(f"approval {approval_id} has no human sign-off; refusing to send")
```

Note that `send` re-checks rather than trusting that the caller already did. It is the last checkpoint, so it does not delegate the decision upward.

And again, a test rather than a promise:

```python
def test_an_agent_cannot_reach_the_send_path():
    tool_names = {tool.__name__ for tool in tools.FLEET_TOOLS}
    assert not any("send" in name for name in tool_names)
```

## The bugs

### I put a database password in Cloud Logging

The worst one, and the most instructive.

A failed database connection raises with the full connection URL in the exception message. My handler did the reasonable thing:

```python
except Exception:
    logger.warning("could not open the session database", exc_info=True)
```

`exc_info=True` writes the traceback. The traceback contains the exception message. The exception message contains `postgresql+pg8000://fleet:<the actual password>@/fleet`.

I found it reading startup logs for an unrelated reason. Rotated the password, then fixed the class of bug rather than the instance:

```python
def redact(url: str) -> str:
    scheme, _, rest = url.partition("//")
    _, _, host = rest.partition("@")
    return f"{scheme}//***:***@{host}"
```

Every log site that touches a connection string passes through it, and the handler now logs `type(exc).__name__` instead of the exception. I was three days into building an access-control system and had a credential sitting in plain text one layer below it.

### An API that accepts your data and does nothing with it

This one took an afternoon and is the reason I am writing this section.

ADK's `add_session_to_memory()` posts a session to Memory Bank. It returned success. The log said `Ingest events request triggered`. And the memory bank stayed empty — `list()` returned nothing, `search_memory()` returned nothing, and a second session recalled nothing.

I chased the wrong thing first. Added memory topics (`EXPLICIT_INSTRUCTIONS`, `USER_PREFERENCES`, `KEY_CONVERSATION_DETAILS`), because a bank with no topics has nothing to extract *toward*. Still zero. Then added a generation trigger rule, because ingested events sit inert until something tells the extractor to run:

```python
generation_config=...GenerationConfig(
    generation_trigger_config=MemoryGenerationTriggerConfig(
        generation_rule=...GenerationTriggerRule(event_count=1)
    )
)
```

Still zero.

What finally worked was skipping the ingest path entirely and calling generation directly:

```python
client.agent_engines.memories.generate(
    name=engine,
    direct_contents_source={"events": events},
    scope={"app_name": session.app_name, "user_id": session.user_id},
    config={"wait_for_completion": True},
)
```

The operation came back with `action=CREATED` and a memory resource. Reading it back gave me the model's own compression of what I had said: *"I want Acme Machining tooling issues to always be escalated to high severity."* `list()` found it, `retrieve()` found it, and ADK's own `search_memory()` found it at the same `{app_name, user_id}` scope.

Two lessons. **`wait_for_completion` matters more than it looks** — generation is a long-running operation, and returning early means the next session asks a question the bank cannot answer yet, which is indistinguishable from memory being broken. And **an accepted request is not a completed one.** I spent an afternoon trusting a 200.

I will be honest about where this still stands: running my full demo end to end still leaves the bank empty even though each individual write logs success, and I suspect consolidation across repeated same-scope writes. The mechanism is proven; the integration is not finished.

### Cloud Run eats the `Authorization` header

Employee tokens went in `Authorization: Bearer`. Every test passed. Deployed behind an authenticated ingress, every governance endpoint returned 401.

Cloud Run's IAM layer consumes that header for its own identity token before your app sees it. Identity moved to `X-Fleet-Token`, with `Authorization` kept as a fallback so local runs are unchanged.

### A Pub/Sub subscription that succeeds and then never delivers

Creating a push subscription to a private Cloud Run service works. Every delivery then fails with 401, and the subscription itself looks perfectly healthy.

The missing piece is that Pub/Sub's own service agent needs permission to mint tokens as your push identity:

```
serviceAccount:service-PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com
  → roles/iam.serviceAccountTokenCreator on the push service account
```

I put that in a Terraform comment, because the failure gives you nothing to search for.

### Project id and project number are not interchangeable

A late one that wasted half an hour. I wrote memories under
`projects/my-project-id/...` and listed them under `projects/123456789/...`.
Both are valid resource paths. They did not show each other's data. Pick one
form and use it everywhere.

## Proving it rather than claiming it

Two things I would repeat.

**The whole system runs offline.** SQLite stands in for Cloud SQL, a regex screen stands in for Model Armor. All 49 tests — including every access-control assertion — run with no credentials and no cloud project. That was not a testing convenience; it forced every cloud dependency behind a port, which is why swapping the model provider and the guardrail backend each turned out to be a one-file change.

**I proved persistence instead of assuming it.** Wrote an event, replaced the entire Cloud Run revision — new container, new filesystem — read it back. Still there. "We configured Cloud SQL" and "state survives" are different sentences, and only one of them is evidence.

## The takeaway

If you remember one thing: **write the constraint where the input cannot reach it.**

Instructions are a courtesy to the model. Function signatures, SQL predicates, and module boundaries are the contract. When I tested the injection attempt, the carefully written restriction in the agent's instruction was irrelevant — the guardrail stopped it before the model was called, and the tool ACL would have stopped it after.

Code: [github.com/sechan9999/gemini-ops-fleet](https://github.com/sechan9999/gemini-ops-fleet)

*Built on Gemini 3.5 Flash, Google ADK, Cloud Run, Cloud SQL, and Pub/Sub. I created this post for the purpose of entering the Google Cloud All Things Agentic Hackathon.*
