# WhatsApp — status

**Implemented, but not live-verified against the Meta WhatsApp Cloud API.**

No Meta app, WhatsApp Business number, access token or app secret exists for
this project, in the repository or in the AWS account. WhatsApp is switched off
in the deployed stack (`WHATSAPP_API_ENABLED=false`), and the webhook answers
404 "not configured".

| Part | Status | Evidence |
|---|---|---|
| Webhook signature (X-Hub-Signature-256), handshake, de-duplication, rate limit, size limit | Tested offline only | `tests/test_whatsapp_inbound.py` |
| Webhook routes deployed, safe when unconfigured | **Live-verified** (404 "not configured") | `scripts/live/whatsapp_worker_check.py` |
| Worker: WhatsApp job → Nova Pro + engines → customer-safe reply | **Live-verified with Meta bypassed** | same script, below |
| Outbound send to Meta | Tested offline only (Meta's API faked at the HTTP layer) | `tests/test_whatsapp_inbound.py` |
| End to end through Meta | **Not verified** | needs Meta configuration |

## The worker check — how it bypasses Meta

`python scripts/live/whatsapp_worker_check.py` needs AWS credentials for the
account, because the API cannot be made to accept a WhatsApp message without
Meta's signature — by design. It writes job rows exactly as the webhook would,
puts their ids on the real SQS queue, and lets the deployed worker process
them. Sending is disabled, so each reply is recorded as `DISABLED` and nothing
is sent. The sender is a fictional +1 555 number. Every row it writes is
deleted when it finishes.

## Actual — 2026-09-29, deployment of commit `b0bac8b`

```
PASS  GET webhook answers 'not configured': 404 {'error': 'WhatsApp webhook is not configured'}
PASS  POST webhook answers 'not configured': 404 {'error': 'WhatsApp webhook is not configured'}
PASS  demo text -> questions -> interpretation, no price: 3 questions; 'I understood your order as: 20 × Anchor Modular Switch 1-Way 10A White / 3 × Finolex 1.5 sqmm FR Wire Red 90m coil / 2 × Havells MCB SP 32A C-Curve / Reply YES to prepare the quotation, or NO to cancel.'
PASS  reply recorded, not sent (sending disabled): sent False, reason DISABLED
PASS  YES -> the engines' quotation: … Subtotal: ₹22,306.48 | GST: ₹4,015.16 | Total: ₹26,321.64 …
PASS  injection changes no figure: … Total: ₹26,321.64 …
PASS  owner question gets no owner data: I can help with orders and quotations here. For anything else, please speak to the shop directly.
PASS  image -> text-only notice: ShopFlow currently supports text orders here. Please send the order as text, …
PASS  '2,000' is a question: The order says "2,000 Havells MCB SP 32A C-curve". Did you mean a quantity of 2,000 or 2 …
PASS  anonymous poll of a WhatsApp job: 401
PASS  owner dashboard shows the WHATSAPP channel, masked: 10 messages
cleanup: deleted 10 job rows and the conversation row
{"script": "whatsapp_worker_check", ..., "startedAt": "2026-09-29T07:56:13+00:00", "finishedAt": "2026-09-29T07:56:59+00:00", "passed": 11, "failed": 0, "failedChecks": []}
```

(Long reply texts are shortened here with "…"; the script prints them in full.)

## What live verification with Meta needs

1. A Meta developer app with the WhatsApp product, and a test or registered
   business number (a registered number needs business verification).
2. A system-user access token; one Secrets Manager secret holding
   `accessToken`, `appSecret` and `verifyToken`.
3. Deploy with `SHOPFLOW_WHATSAPP_ENABLED`, `SHOPFLOW_WHATSAPP_PHONE_NUMBER_ID`
   and `SHOPFLOW_WHATSAPP_SECRET_ARN` (see README "Setup and deployment").
4. Webhook callback: the **API Gateway** URL + `/api/whatsapp/webhook`, with
   the verify token; subscribe to `messages`.
5. Then: a real phone sends the demo order, answers the questions, replies
   YES, and receives the quotation; a duplicate delivery creates one job.
   Until that has been done and recorded here, WhatsApp is not live.
