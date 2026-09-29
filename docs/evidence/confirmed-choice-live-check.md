# Confirmed clarification choice — live check

**Script:** `python scripts/live/confirmed_choice_check.py [--remediation]` ·
**Credentials:** none · **Reproducible:** yes.

## What it tests

When ShopFlow asks which variant was meant, the page answers with
`choice: {jobId, option}` — the option **number** on the job that asked. The
server reads the SKU from that job's stored options; the agent then looks the
answered line up by code before the model runs (commit `b0bac8b`).

- **FULL** — `20 Anchor modular switch 1 way 10A white, 3 Finolex 1.5 red 90m coil, 2 Havells MCB single pole 32 amp C curve`.
  Before `b0bac8b` this failed 3/3 after the SP choice: the model skipped
  searching the confirmed line and the coverage guard withheld the quotation.
- **DEMO** — `20 Anchor modular switch 1 way white, 3 Finolex 1.5 red coil, 2 Havells MCB 32 amp C curve`.
  Three questions: pole, length, rating.

**Expected:** every run `QUOTED` after at least one question, exactly the
canonical lines, ₹22,306.48 / ₹4,015.16 / ₹26,321.64. An out-of-range or string
option and changed order text → 400; an unknown job → asked again.

## Actual — 2026-09-29, deployment of commit `b0bac8b`

```
PASS  FULL run 1: answered ['MCB-HAV-SP-32A-C'] -> QUOTED 22306.48 / 26321.64
PASS  FULL run 2: answered ['MCB-HAV-SP-32A-C'] -> QUOTED 22306.48 / 26321.64
PASS  FULL run 3: answered ['MCB-HAV-SP-32A-C'] -> QUOTED 22306.48 / 26321.64
PASS  DEMO run 1: answered ['MCB-HAV-SP-32A-C', 'W-FIN-1.5-RED-90M', 'SW-ANC-1W10A'] -> QUOTED 22306.48 / 26321.64
PASS  DEMO run 2: answered ['MCB-HAV-SP-32A-C', 'W-FIN-1.5-RED-90M', 'SW-ANC-1W10A'] -> QUOTED 22306.48 / 26321.64
PASS  DEMO run 3: answered ['MCB-HAV-SP-32A-C', 'W-FIN-1.5-RED-90M', 'SW-ANC-1W10A'] -> QUOTED 22306.48 / 26321.64
PASS  a question was asked: ['MCB-HAV-DP-32A-C', 'MCB-HAV-SP-32A-C']
PASS  option out of range: 400 choice.option must be between 1 and 2
PASS  option as a string: 400 choice.option must be a positive whole number
PASS  changed order text: 400 the choice belongs to a different order text
PASS  unknown job is asked again, not answered: 202 {'applied': False, 'reason': 'EXPIRED'}
{"script": "confirmed_choice_check", ..., "startedAt": "2026-09-29T07:53:42+00:00", "finishedAt": "2026-09-29T07:55:02+00:00", "passed": 11, "failed": 0, "failedChecks": []}
```

## `--remediation` — pending deployment

These four checks cover the remediation in the working tree: `/api/orders`
refuses a client-supplied SKU (in `clarifications` or inside `choice`) and any
price or total field. Run against the same deployment, which predates it, they
fail — which is the correct result and shows the checks are real:

```
FAIL  client SKU in clarifications refused: 202 None
FAIL  SKU inside choice refused: 202 None
FAIL  price on an order refused: 202 None
FAIL  total on an order refused: 202 None
{"script": "confirmed_choice_check", ..., "startedAt": "2026-09-29T07:57:29+00:00", "finishedAt": "2026-09-29T07:58:51+00:00", "passed": 11, "failed": 4, ...}
```

(The 202s were already harmless on that deployment: a `clarifications` SKU was
catalogue-checked and only a hint to the model, and price fields were ignored.
The remediation makes the refusal explicit.) Re-run with `--remediation` after
deploying it; until then it is **not live-verified**.
