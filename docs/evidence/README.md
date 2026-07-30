# Evidence

Raw output from runs against a deployed stack. [`../WALKTHROUGH.md`](../WALKTHROUGH.md)
draws on these; this directory is where the unedited originals live.

**Raw, not retold.** Paste the output verbatim, including timings and any warnings. A
transcript tidied for the page is no longer evidence of anything — and this repo's whole
argument is that its claims can be checked, which requires something to check them
against.

**No real PII, ever** — the same rule as everywhere else here. Subjects are fabricated and
`subject_ref` is a pseudonymous handle, so a walkthrough transcript is safe by
construction. Read anything captured from CloudWatch or a participant before committing
it, rather than assuming.

Account identifiers, stack ARNs and region names are fine to keep: they are not secrets,
and a redacted ARN makes a transcript harder to trust for no gain. Credentials and tokens
are not, and nothing here should ever contain one.

## What belongs here

| File | From | Feeds |
|---|---|---|
| `walkthrough.txt` | `make walkthrough` | WALKTHROUGH.md §3, §4.1 |
| `threads.txt` | `make threads`, while paused at approval | §4.1 — the saga waiting with nothing running |
| `policy-deny.txt` | the Cedar deny line from the injection corpus | §4.2 |
| `tools-list.json` | the `tools/list` response | §4.2 — the tool was never offered |
| `verify-residuals.txt` | `make integration` / the certificate | §4.3 — PARTIAL, disclosed |
| `cost.md` | Cost Explorer: one run, and 24h idle | §5 |

§4.2 needs **both** files. The deny line proves the request was refused; the tool list
proves the tool was never in the model's choices to begin with. Those are different
claims, and the second is the stronger one.
