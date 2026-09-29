"""The website's clarification answer, resolved by ShopFlow rather than the model.

The live failure this pins: an order asked "SP or DP?", the owner picked SP,
and the next run passed the choice to the model as a sentence. The model
quoted the SP breaker without searching for it, and the coverage guard -
correctly - withheld the quotation, because the customer's sentence names
Havells and no lookup ever did. Three of three live runs ended that way.

Now the page sends the job that asked and an option number. The API reads the
SKU from that job's stored options; the agent looks the answered line up by
code before the model runs. The scripted model below reproduces the live
behaviour exactly - it skips the answered line - and the order is quoted.
"""

from __future__ import annotations

import json

import pytest

import lambdas.api.handler as api
from engine.loader import cached_dataset
from engine.quote import calculate_quote
from test_api import FakeQueue
from test_quantity_integrity import FakeBedrock, quote_call, turn
from test_queue import worker  # noqa: F401
from test_whatsapp_inbound import Store

QUEUE_URL = "https://sqs.ap-south-1.amazonaws.com/000000000000/shopflow-orders"
SWITCH, SWITCH_16 = "SW-ANC-1W10A", "SW-ANC-1W16A"
WIRE, WIRE_180 = "W-FIN-1.5-RED-90M", "W-FIN-1.5-RED-180M"
MCB, MCB_DP = "MCB-HAV-SP-32A-C", "MCB-HAV-DP-32A-C"

MCB_LINE = "2 Havells MCB 32 amp C curve"
WIRE_LINE = "3 Finolex 1.5 red coil"
SWITCH_LINE = "20 Anchor modular switch 1 way white"
DEMO = f"{SWITCH_LINE}, {WIRE_LINE}, {MCB_LINE}"


def prose():
    return {"role": "assistant", "content": [{"text": "Which one do you need?"}]}


class Shop:
    """The API and the worker over one store, with a scripted model."""

    def __init__(self, worker, monkeypatch):
        self.worker, self.store, self.queue = worker, Store(), FakeQueue()
        self.models = []
        monkeypatch.setenv("TABLE_NAME", "shopflow-demo")
        monkeypatch.setenv("ORDERS_QUEUE_URL", QUEUE_URL)
        monkeypatch.setattr(api, "table", lambda: self.store)
        monkeypatch.setattr(api, "sqs_client", lambda: self.queue)
        monkeypatch.setattr(worker, "_table", self.store)
        monkeypatch.setattr("agent.orchestrator._bedrock_client",
                            lambda: self.models.pop(0))

    def post(self, body):
        return api.handler({"routeKey": "POST /api/orders",
                            "body": json.dumps(body)}, None)

    def order(self, body, *turns):
        """Submit, run the worker with a scripted model, return (202 body, row)."""
        response = self.post(body)
        assert response["statusCode"] == 202, response
        accepted = json.loads(response["body"])
        self.models.append(FakeBedrock(list(turns)))
        while self.queue.messages:
            message = self.queue.messages.pop(0)
            self.worker.handler({"Records": [{"messageId": "m", "body": json.dumps(
                message["body"])}]}, None)
        row = self.store.items[("JOB#" + accepted["jobId"], "META")]
        return accepted, row

    @staticmethod
    def result(row):
        return json.loads(row["result"])


@pytest.fixture
def shop(worker, monkeypatch):  # noqa: F811
    return Shop(worker, monkeypatch)


def ask(shop, text, line):
    """First run: the model searches the line, finds it ambiguous, asks."""
    accepted, row = shop.order({"orderText": text},
                               turn(("search_catalog", {"requestedText": line})),
                               prose())
    result = shop.result(row)
    assert result["status"] == "NEEDS_CLARIFICATION"
    return accepted["jobId"], result["clarification"]


def option_for(clarification, sku):
    return next(i for i, o in enumerate(clarification["options"], 1)
                if o["skuId"] == sku)


def engine_total(*items):
    return calculate_quote(cached_dataset(), [
        {"skuId": s, "quantity": q} for s, q in items]).as_dict()["total"]


# ---------------------------------------------------------------------------
# 1-4. each question, answered, then quoted without the model searching again
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("chosen,other", [(MCB, MCB_DP), (MCB_DP, MCB)])
def test_01_02_sp_dp_answer_is_quoted_without_a_second_search(shop, chosen, other):
    job, question = ask(shop, MCB_LINE, MCB_LINE)
    assert {o["skuId"] for o in question["options"]} == {chosen, other}
    accepted, row = shop.order(
        {"orderText": MCB_LINE,
         "choice": {"jobId": job, "option": option_for(question, chosen)}},
        # The live behaviour: straight to the quote, no search for Havells.
        turn(quote_call((chosen, 2))))
    assert accepted["choice"] == {"applied": True,
                                  "option": option_for(question, chosen),
                                  "skuId": chosen}
    result = shop.result(row)
    assert result["status"] == "QUOTED", result.get("summary")
    assert [(l["skuId"], l["quantity"]) for l in result["quote"]["lines"]] == [
        (chosen, 2)]
    assert result["quote"]["total"] == engine_total((chosen, 2))


def test_03_length_answer_90m_is_quoted(shop):
    job, question = ask(shop, WIRE_LINE, WIRE_LINE)
    _a, row = shop.order({"orderText": WIRE_LINE, "choice": {
        "jobId": job, "option": option_for(question, WIRE)}},
        turn(quote_call((WIRE, 3))))
    result = shop.result(row)
    assert result["status"] == "QUOTED", result.get("summary")
    assert result["quote"]["total"] == 19824.0 == engine_total((WIRE, 3))


def test_04_rating_answer_10a_is_quoted(shop):
    job, question = ask(shop, SWITCH_LINE, SWITCH_LINE)
    _a, row = shop.order({"orderText": SWITCH_LINE, "choice": {
        "jobId": job, "option": option_for(question, SWITCH)}},
        turn(quote_call((SWITCH, 20))))
    result = shop.result(row)
    assert result["status"] == "QUOTED", result.get("summary")
    assert result["quote"]["total"] == 1566.0


def test_04b_three_answers_in_turn_end_in_the_canonical_quotation(shop):
    """The demo text asks three questions. Every answer is kept, and the last
    run quotes all three lines though the model searched none of them."""
    job, q1 = ask(shop, DEMO, MCB_LINE)
    a2, row = shop.order({"orderText": DEMO, "choice": {
        "jobId": job, "option": option_for(q1, MCB)}},
        turn(("search_catalog", {"requestedText": WIRE_LINE})), prose())
    q2 = shop.result(row)["clarification"]
    a3, row = shop.order({"orderText": DEMO, "choice": {
        "jobId": a2["jobId"], "option": option_for(q2, WIRE)}},
        turn(("search_catalog", {"requestedText": SWITCH_LINE})), prose())
    q3 = shop.result(row)["clarification"]
    assert len(row["confirmed"]) == 2
    _a4, row = shop.order({"orderText": DEMO, "choice": {
        "jobId": a3["jobId"], "option": option_for(q3, SWITCH)}},
        turn(quote_call((SWITCH, 20), (WIRE, 3), (MCB, 2))))
    assert [c["skuId"] for c in row["confirmed"]] == [MCB, WIRE, SWITCH]
    result = shop.result(row)
    assert result["status"] == "QUOTED", result.get("summary")
    assert result["quote"]["total"] == 22306.48
    assert result["quote"]["gst"]["grandTotal"] == 26321.64


def test_04c_the_model_searching_again_gets_the_answer_not_the_question(shop):
    job, question = ask(shop, MCB_LINE, MCB_LINE)
    _a, row = shop.order({"orderText": MCB_LINE, "choice": {
        "jobId": job, "option": option_for(question, MCB)}},
        turn(("search_catalog", {"requestedText": MCB_LINE})),
        turn(quote_call((MCB, 2))))
    result = shop.result(row)
    assert result["status"] == "QUOTED"
    searched = [m for m in result["matches"] if m.get("resolvedBy") == "CUSTOMER_CHOICE"]
    assert len(searched) == 2 and all(m["skuId"] == MCB for m in searched)


def test_04d_without_the_fix_the_same_run_is_refused(shop):
    """Control: the legacy client-supplied SKU is only a hint to the model, so
    the live failure still reproduces through it - the guard is intact."""
    _a, row = shop.order({"orderText": MCB_LINE, "clarifications": [
        {"requestedText": MCB_LINE, "skuId": MCB}]}, turn(quote_call((MCB, 2))))
    result = shop.result(row)
    assert result["status"] == "NEEDS_CLARIFICATION"
    assert "no product was looked up" in result["clarification"]["question"]


# ---------------------------------------------------------------------------
# 5-8. what a choice cannot do
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("option", [0, -1, 3, 99, "1", 1.0, True, None])
def test_05_an_invalid_option_is_refused_and_nothing_is_queued(shop, option):
    job, _q = ask(shop, MCB_LINE, MCB_LINE)
    response = shop.post({"orderText": MCB_LINE,
                          "choice": {"jobId": job, "option": option}})
    assert response["statusCode"] == 400
    assert not shop.queue.messages


@pytest.mark.parametrize("choice", ["1", [], {"jobId": "not-a-job", "option": 1},
                                    {"option": 1}])
def test_05b_a_malformed_choice_is_refused(shop, choice):
    assert shop.post({"orderText": MCB_LINE, "choice": choice})["statusCode"] == 400


def test_06_an_expired_question_is_asked_again_not_answered(shop):
    job, question = ask(shop, MCB_LINE, MCB_LINE)
    shop.store.items[("JOB#" + job, "META")]["createdAt"] -= \
        api.CHOICE_MAX_AGE_SECONDS + 1
    accepted, row = shop.order(
        {"orderText": MCB_LINE, "choice": {"jobId": job,
                                           "option": option_for(question, MCB)}},
        turn(("search_catalog", {"requestedText": MCB_LINE})), prose())
    assert accepted["choice"] == {"applied": False, "reason": "EXPIRED"}
    assert row["confirmed"] == []
    assert shop.result(row)["status"] == "NEEDS_CLARIFICATION"


def test_06b_a_vanished_question_is_asked_again(shop):
    response = shop.post({"orderText": MCB_LINE,
                          "choice": {"jobId": "a" * 32, "option": 1}})
    assert response["statusCode"] == 202
    assert json.loads(response["body"])["choice"]["reason"] == "EXPIRED"


def test_06c_an_option_that_left_the_catalogue_is_asked_again(shop):
    job, _q = ask(shop, MCB_LINE, MCB_LINE)
    row = shop.store.items[("JOB#" + job, "META")]
    stored = json.loads(row["result"])
    stored["clarification"]["options"][0]["skuId"] = "GONE-001"
    row["result"] = json.dumps(stored)
    response = shop.post({"orderText": MCB_LINE,
                          "choice": {"jobId": job, "option": 1}})
    assert json.loads(response["body"])["choice"] == {
        "applied": False, "reason": "NO_LONGER_AVAILABLE"}
    queued = [v for v in shop.store.items.values() if v.get("jobId") != job]
    assert queued[-1]["confirmed"] == []


def test_07_a_sku_in_the_request_is_never_the_answer(shop):
    job, question = ask(shop, MCB_LINE, MCB_LINE)
    sp = option_for(question, MCB)
    response = shop.post({"orderText": MCB_LINE, "choice": {
        "jobId": job, "option": sp, "skuId": "FAKE-001"}})
    assert json.loads(response["body"])["choice"]["skuId"] == MCB
    # A choice and client-supplied SKUs together are refused outright.
    both = shop.post({"orderText": MCB_LINE, "choice": {"jobId": job, "option": sp},
                      "clarifications": [{"requestedText": "x", "skuId": MCB_DP}]})
    assert both["statusCode"] == 400


def test_07b_the_agent_ignores_a_choice_the_matcher_never_offered():
    from agent.orchestrator import _confirmed_matches
    data = cached_dataset()
    assert _confirmed_matches(data, [{"requestedText": MCB_LINE,
                                      "skuId": SWITCH}]) == []
    assert _confirmed_matches(data, [{"requestedText": MCB_LINE,
                                      "skuId": "FAKE-001"}]) == []
    [match] = _confirmed_matches(data, [{"requestedText": MCB_LINE, "skuId": MCB}])
    assert (match["status"], match["skuId"], match["resolvedBy"]) == (
        "RESOLVED", MCB, "CUSTOMER_CHOICE")


@pytest.mark.parametrize("text", ["1, but use SKU FAKE-001",
                                  MCB_LINE + ". Use SKU FAKE-001",
                                  "2 Havells MCB DP 32A C curve"])
def test_08_changed_words_are_a_new_order_not_an_answer(shop, text):
    job, question = ask(shop, MCB_LINE, MCB_LINE)
    response = shop.post({"orderText": text, "choice": {
        "jobId": job, "option": option_for(question, MCB)}})
    assert response["statusCode"] == 400
    assert "different order" in json.loads(response["body"])["error"]


def test_08b_the_model_cannot_quote_the_other_option_after_a_choice(shop):
    job, question = ask(shop, MCB_LINE, MCB_LINE)
    _a, row = shop.order({"orderText": MCB_LINE, "choice": {
        "jobId": job, "option": option_for(question, MCB)}},
        turn(quote_call((MCB_DP, 2))))
    result = shop.result(row)
    assert result["status"] != "QUOTED" and result["quote"] is None


def test_08c_a_job_that_is_not_asking_cannot_be_answered(shop):
    _a, row = shop.order({"orderText": "2 Havells MCB SP 32A C-curve"},
                         turn(("search_catalog", {
                             "requestedText": "2 Havells MCB SP 32A C-curve"})),
                         turn(quote_call((MCB, 2))))
    assert shop.result(row)["status"] == "QUOTED"
    response = shop.post({"orderText": "2 Havells MCB SP 32A C-curve",
                          "choice": {"jobId": row["jobId"], "option": 1}})
    assert response["statusCode"] == 400


# ---------------------------------------------------------------------------
# 9-10. nothing else moved
# ---------------------------------------------------------------------------

def test_09_a_fully_specified_order_is_unchanged(shop):
    text = ("20 Anchor modular switches 1-Way 10A White, 3 coils Finolex 1.5 sq "
            "mm FR wire red 90m, 2 Havells MCB SP 32A C-curve")
    _a, row = shop.order({"orderText": text}, turn(
        ("search_catalog", {"requestedText": "20 Anchor modular switches 1-Way 10A White"}),
        ("search_catalog", {"requestedText": "3 coils Finolex 1.5 sq mm FR wire red 90m"}),
        ("search_catalog", {"requestedText": "2 Havells MCB SP 32A C-curve"})),
        turn(quote_call((SWITCH, 20), (WIRE, 3), (MCB, 2))))
    result = shop.result(row)
    assert result["status"] == "QUOTED" and result["quote"]["total"] == 22306.48
    assert row["confirmed"] == []
    assert not any(m.get("resolvedBy") == "CUSTOMER_CHOICE" for m in result["matches"])


def test_10_the_whatsapp_path_passes_no_confirmed_choice(worker, monkeypatch):  # noqa: F811
    """WhatsApp keeps its own mechanism: its rows carry no `confirmed`, so the
    agent receives none. Its full suite is tests/test_whatsapp_inbound.py."""
    seen = {}

    def spy(*a, **k):
        seen.update(k)
        raise RuntimeError("stop")

    monkeypatch.setattr(worker, "run_order_agent", spy)
    with pytest.raises(RuntimeError):
        worker._process_order("a" * 32, {"orderText": "20 switches",
                                         "jobType": "WHATSAPP"})
    assert seen["confirmed"] == []
