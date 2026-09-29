# Canonical order — live check

**Script:** `python scripts/live/canonical_check.py [runs]` · **Credentials:** none ·
**Reproducible:** yes, any time against the deployed site.

**Input:** `20 Anchor modular switches 1-Way 10A White, 3 coils Finolex 1.5 sq mm FR wire red 90m, 2 Havells MCB SP 32A C-curve`

**Expected, every run:** `QUOTED`; lines exactly 20 × `SW-ANC-1W10A`,
3 × `W-FIN-1.5-RED-90M`, 2 × `MCB-HAV-SP-32A-C`; subtotal ₹22,306.48,
GST ₹4,015.16, total ₹26,321.64; the switch and wire lines not fully in stock,
the MCB line in stock. A clarification counts as a failure.

**Actual — 2026-09-29, deployment of commit `b0bac8b`:**

```
PASS  run 1: QUOTED total=22306.48 gst=4015.16 grand=26321.64
PASS  run 2: QUOTED total=22306.48 gst=4015.16 grand=26321.64
PASS  run 3: QUOTED total=22306.48 gst=4015.16 grand=26321.64
PASS  run 4: QUOTED total=22306.48 gst=4015.16 grand=26321.64
PASS  run 5: QUOTED total=22306.48 gst=4015.16 grand=26321.64
{"script": "canonical_check", "base": "https://d3m3lwn03zb2eu.cloudfront.net", "startedAt": "2026-09-29T07:53:17+00:00", "finishedAt": "2026-09-29T07:53:41+00:00", "passed": 5, "failed": 0, "failedChecks": []}
```

The on-hand counts behind "in stock" (14 / 1 / 5, shortages 6 / 2 / 0) are
owner data and are not in the customer view this script reads; they are pinned
offline in `tests/test_remediation.py`.
