# Live checks — reproducible from this repository

Every "verified live" claim in the README now comes from a script in
[`scripts/live/`](../../scripts/live/). Each one calls the **deployed**
application, judges the answer, prints `PASS`/`FAIL` lines and one JSON
summary line, and exits non-zero if anything failed. Nothing is replayed or
stubbed.

```bash
python scripts/live/canonical_check.py          # 5 runs by default
python scripts/live/confirmed_choice_check.py   # add --remediation after deploying it
python scripts/live/hardening_check.py
python scripts/live/supplier_reply_check.py
python scripts/live/whatsapp_worker_check.py    # needs AWS credentials; see below
```

`SHOPFLOW_BASE_URL` points them at another deployment; `SHOPFLOW_FORCE_IPV4=1`
helps on networks where IPv6 stalls.

**What a run writes.** Only the application's own job rows (orders,
counter-offer drafts, supplier-reply readings), which expire after 24 hours
under the table's TTL. No supplier cost is confirmed and no permanent record is
changed. `whatsapp_worker_check.py` writes WhatsApp job rows directly and
deletes every one of them, and the conversation row, when it finishes.

## Latest run — 2026-09-29, 07:53–07:58 UTC

Against `https://d3m3lwn03zb2eu.cloudfront.net`, whose backend is the code of
commit `b0bac8b`. The remediation in the working tree at the time (client-SKU
refusal, supplier price plausibility, ratings in words) was **not deployed**.

| Script | Result | Needs credentials | Detail |
|---|---|---|---|
| `canonical_check.py` | **5 / 5** | no | [canonical-live-check.md](canonical-live-check.md) |
| `confirmed_choice_check.py` | **11 / 11** | no | [confirmed-choice-live-check.md](confirmed-choice-live-check.md) |
| `confirmed_choice_check.py --remediation` | **11 / 15** — the 4 remediation checks fail, as they must, against a deployment without it | no | [confirmed-choice-live-check.md](confirmed-choice-live-check.md) |
| `hardening_check.py` | **17 / 17** | no | [security-live-check.md](security-live-check.md) |
| `supplier_reply_check.py` | **7 / 7** | no (demo gate header only) | [supplier-reply-live-check.md](supplier-reply-live-check.md) |
| `whatsapp_worker_check.py` | **11 / 11** — **Meta not involved** | AWS (DynamoDB, SQS) | [whatsapp-status.md](whatsapp-status.md) |

Earlier results in the README (20/20 canonical runs on 2026-09-27; the P1 and
P2 suites) come from `scripts/smoke_test_p1_regressions.py`,
`scripts/smoke_test_p2_regressions.py` and runs of the canonical check with
more iterations.

## Not live-verified

| What | Why |
|---|---|
| Supplier price plausibility (EXTREME_CHANGE, the price ceiling) | Not deployed at the time of the run. Covered by `tests/test_remediation.py`. |
| Client SKU / price fields refused on `/api/orders` | Not deployed at the time of the run; `--remediation` shows the deployment still accepted them. Covered by `tests/test_confirmed_choice.py`. |
| Ratings spoken in words ("thirty two amp") | Not deployed; no audio clip of it exists. Covered by unit tests only. |
| WhatsApp via Meta | No Meta app, number or token exists. See [whatsapp-status.md](whatsapp-status.md). |
| Speech-recognition accuracy | The two clips are text-to-speech, not people. They prove the pipeline, not accuracy. |
