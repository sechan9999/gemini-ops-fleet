# Scope: Healthcare Domain on the Fleet

Sizing exercise for re-pointing this fleet at a payer/clinical domain — prior
authorization, claims, and member outreach instead of tickets, orders, and
customers.

Written before committing to the work, so the decision rests on a file-level
estimate rather than a feeling about how big it looks.

**Status: scoped, not started.** Sequencing recommendation is in §5.

---

## 1. What transfers unchanged

The governance machinery does not know what domain it is governing. That is the
finding this scope exists to record — roughly sixty percent of the system is
untouched by the swap.

| File | Why it does not change |
|---|---|
| `worker.py` | The outbox drain routes by event kind. It never reads a payload field. |
| `tracing.py` | `fleet.caller_role` carries a string. The span does not know what a role means. |
| `approvals.py` | The gate does not know what it is gating. A draft is a draft. |
| `routes.py` | Endpoint shapes are identical; only the nouns in the docstrings move. |
| `config.py`, `memory.py`, `fast_api_app.py` | Infrastructure, not domain. |
| `deployment/terraform/` | Cloud Run, Cloud SQL, Pub/Sub, the push identity — all reusable as-is. |
| `guardrails.py` | The **mechanism** is unchanged. Patterns get extended (see §3). |

Practical consequence: deployment, the event bus, durable state, and telemetry
carry over with zero rework. Every hour of this estimate goes into domain code
and evidence, none into infrastructure.

## 2. What gets swapped

| File | Work | Size |
|---|---|:---:|
| `domain.py` | `Customer`→`Member`, `Order`→`Claim`, `Transaction`→`Remittance`, `Ticket`→`PriorAuthRequest`. `Document` survives as-is — payer policy and clinical guidance are already exactly that shape. | M |
| `identity.py` | Role enum becomes `CARE_TEAM`, `UTILIZATION_MGMT`, `REVENUE_CYCLE`, `COMPLIANCE`. Row-scope rules redefined against the new ownership model. | M |
| `store.py` | Seed data replaced: payer policies, clinical guidance, members, claims. The public/restricted document pair that makes the access-control demo work needs a healthcare equivalent. | M |
| `tools.py` | Eight tools renamed and re-aimed — `get_customer_360`→`get_member_360`, `reconcile_accounting`→`reconcile_claims`. Signatures and the identity-check pattern stay. | M |
| `fleet.py` | Four agent instructions rewritten. Structure identical. | S |
| `registry.py` | Restrictions and autonomy grades rewritten. | S |
| `retrieval.py` | **Logic unchanged.** Only the values inside the scope predicate differ. | XS |
| `tests/unit/` ×4 | 49 assertions re-pointed. The test *structure* is right and stays; every subject changes. | **L** |
| `demo.py` | Six scenarios rewritten. | M |
| `README`, `architecture.md`, `demo-script.md`, `devpost-submission.md` | Full revision. | **L** |

## 3. What healthcare adds that manufacturing never needed

This is the real work, and the reason a healthcare version is more than a
find-and-replace.

**① Minimum necessary (HIPAA §164.502(b)).** Current row-level security is
binary: you can see the record or you cannot. Healthcare needs *different fields
of the same record* per role — a billing analyst sees diagnosis codes and not
clinical notes. That is field-level masking, and it does not exist yet. It also
strengthens the core claim: the demo becomes "same record, two roles, different
columns," which is harder to fake than an empty result.

**② Break-the-glass.** Emergency access that exceeds normal scope, permitted but
*mandatorily recorded*. Today the system can deny and it can allow; it has no
"allow, and make the access itself an event someone will answer for." The audit
trail and the approval queue are both already the right shape to host it.

**③ The guardrail stops being decorative.** In manufacturing, PII screening on
tool output was good hygiene. Here, PHI leaking through a tool result is the
violation itself. Patterns extend to MRN, member id, and date of birth — and
**Model Armor moves from optional to required**, because shipping a healthcare
demo on a regex fallback would misrepresent the guarantee.

**④ Audit retention.** Six years. `AuditEntry` has no retention policy today.

①–③ are roughly half a day each and are what a judge in this track would
actually look for.

## 4. Estimate

| Phase | Effort |
|---|---|
| Domain swap (§2) | 1.5 d |
| Healthcare-specific features (§3 ①②③) | 1.5 d |
| Live re-verification — **all seven verified claims, again** | 0.5 d |
| Four documents + re-shoot the video | 1.0 d |
| **Total** | **~4.5 d** |

## 5. Sequencing — do not start this before the video

D-13 remains, so the arithmetic fits. Two things argue against starting now.

**The seven verified claims all become invalid.** Live Gemini call, the sales
denial, the injection block, the 409, registry discovery, real Pub/Sub delivery,
and state surviving a revision swap — every one is domain-specific evidence that
would need re-running and re-filming. The verification history is the most
expensive asset this project has; a domain swap discards it.

**The video is still unshot.** Rewriting finished work while the only remaining
mandatory requirement is outstanding inverts the risk order.

Recommended order:

1. **Now** — film and submit with the manufacturing domain. R7 is the only gap.
2. **After submission** — build healthcare on a `feature/healthcare` branch. If
   it lands before the deadline it can replace the entry or stand as a second
   one. If it does not, nothing is lost.

This has no downside. The reverse order puts the submission itself at risk the
moment a 4.5-day estimate turns into six.

## 6. Is healthcare actually the better entry?

For this track, yes — meaningfully. "Compliance, data sovereignty, and security
policies" is in the track's own wording, and HIPAA is a regime every judge
recognises without explanation. A fifty-person manufacturer is a weaker story
than a payer handling PHI.

So the conclusion is not that healthcare is wrong. It is that the order is:
secure the submission, then upgrade it. Done that way, both are available.
