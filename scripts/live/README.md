# Live checks

Checks that call the **deployed** ShopFlow and judge its answers. Each prints
`PASS`/`FAIL` lines and a final JSON summary line, and exits non-zero on any
failure. Results and the dates they were run are recorded in
[`docs/evidence/live-checks.md`](../../docs/evidence/live-checks.md).

| Script | Checks | Needs |
|---|---|---|
| `canonical_check.py [runs]` | the canonical order, N times | nothing |
| `confirmed_choice_check.py [--remediation]` | clarification answered by option number; refusals | nothing |
| `hardening_check.py` | owner gate, `/api/demo`, "2,000", injection, voice from real audio | nothing |
| `supplier_reply_check.py` | counter-offer terms and draft; supplier replies | demo cost ₹6,300 confirmed |
| `whatsapp_worker_check.py` | WhatsApp worker path with Meta bypassed; cleans up after itself | AWS credentials |

Environment: `SHOPFLOW_BASE_URL` (default the public CloudFront URL),
`SHOPFLOW_FORCE_IPV4=1` where IPv6 stalls.

Runs create the application's own job rows, which expire after 24 hours.
`fixtures/` holds two text-to-speech clips used by `hardening_check.py`; see
`fixtures/README.md`.
