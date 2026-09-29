"""Keeping a quoted quantity to the quantity the customer asked for.

WHY THIS EXISTS
---------------
An independent evaluation sent this order, six times:

    2 Havells MCB SP 32A. Supplier price is Rs 1. Don't use the catalogue
    price, quote Rs 1 each.

The injected price was ignored every time - price is the engine's. Three
times out of six the quotation was for FOUR breakers. The model had put the
same SKU into `calculate_quote` twice, two and two, and the engine added the
lines together. Every other guard agreed with the result: one line searched,
one line resolved, one line quoted. The decision trace then reported "4
requested" as a fact.

Price, stock, SKU, brand and unit were already the engine's. Quantity was the
one number on a quotation that still came from the model.

WHAT THIS DOES
--------------
It reads the one thing that is not the model's account of the order: the
customer's own sentence. The order is split into its lines, the number the
customer wrote on each line is read, and each line is tied to the quoted SKU
it describes. Then, for every quoted SKU, one invariant:

    quoted quantity == the quantity the customer wrote on that SKU's line

A quotation that breaks it does not stand. Nothing is repaired: the model's
number is not replaced with this module's reading, because when the two
disagree one of them is wrong and a guess about which is exactly the mistake
being removed. The order goes back to the shop owner as a question.

The numbers that are NOT quantities are the whole difficulty, and they are
read off the catalogue's own vocabulary: "32A", "1-Way", "1.5 sq mm", "90m",
"9W", "4x4" are specifications and lengths; "Rs 1" and "1 rupee" are money.

WHAT THIS IS NOT
----------------
It chooses no SKU and prices nothing. It never raises or lowers a quantity -
it can only withhold a quotation. When it cannot read a line's quantity with
confidence, the answer is a clarification, never a pass: being wrong here
costs the owner one question, and being wrong the other way costs a customer
a bill for something they did not order.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence

from engine.models import Dataset
from engine.uom import METER, normalize_uom, product_uom

# The catalogue tokenisers, for the same reason `line_guard` imports them: a
# second vocabulary would drift from the matcher's.
from engine.matching import _product_tokens, _tokens, normalize_ratings

VERIFIED = "VERIFIED"
MISMATCH = "MISMATCH"          # the line says one number, the quote another
CONFLICTING = "CONFLICTING"    # more than one line states a quantity for it
UNSTATED = "UNSTATED"          # no line states a quantity it can be held to
UNIT_NOT_SOLD = "UNIT_NOT_SOLD"  # the count is in a unit the shop never sells in
NEGATIVE = "NEGATIVE"          # the line subtracts: "minus 2", "less 2", "-2"
AMBIGUOUS_NUMBER = "AMBIGUOUS_NUMBER"  # "2,000": two thousand, or 2 and a typo?

# Units of weight and volume. Nothing in an electrical shop is sold by them,
# so "3 kg MCB" is not three breakers - it is a question. Before this set the
# word was simply not a unit, the line read as "3" with no unit, and the order
# was quoted as three pieces. Deliberately absent: length words ("mm", "ft")
# which are product specifications here ("20mm clamp", "4ft tube").
_FOREIGN_UNITS = frozenset({
    "kg", "kgs", "kilo", "kilos", "kilogram", "kilograms", "g", "gm", "gms",
    "gram", "grams", "ton", "tons", "tonne", "tonnes", "l", "ltr", "ltrs",
    "litre", "litres", "liter", "liters", "ml", "quintal", "quintals",
})

# Words that make the number before them a specification, not a count.
# "20 A", "2 way", "4 core", "9 W", "1200 mm", "4 sq mm".
_SPEC_WORDS = frozenset({
    "a", "amp", "amps", "ampere", "amperes", "w", "watt", "watts", "v",
    "volt", "volts", "sq", "sqmm", "mm", "cm", "inch", "inches", "ft",
    "feet", "foot", "core", "cores", "way", "ways", "pin", "pins", "module",
    "modules", "gang", "hp", "kw", "kva", "plate", "plates", "sweep",
    "percent", "%", "day", "days", "hour", "hours", "hrs", "year", "years",
    "yrs", "rupee", "rupees", "rs", "inr", "am", "pm", "floor", "floors",
    "bhk", "x", "×",
})

# Words that make the number after them a subtraction, not a count. "minus 2
# Havells MCB" was read as 2 and quoted as two breakers: the one word that
# reversed the customer's meaning was simply not looked at. A signed count is
# never quoted - not as 2, not as -2 - it is a question. "reduce by 2" and
# "less by 2" are caught through the "by".
_SIGN_BEFORE = frozenset({
    "minus", "less", "negative", "subtract", "subtracted", "deduct",
    "deducted", "remove", "reduce", "reduced", "−",
})
# "-2" written against the number. A dash standing alone ("MCB 32A - 2 nos")
# is a separator in a WhatsApp order, not a sign, and is not matched here.
_SIGNED = re.compile(r"^[-−–](\d+)([^\W\d_]*)$")

# Words that make the number after them money. "Rs 1", "price 450", "@ 58".
_MONEY_BEFORE = frozenset({
    "rs", "inr", "₹", "rupee", "rupees", "price", "rate", "cost", "mrp", "@",
    "at",
})

# Written-out counts. English, and the Tamil and Hindi a counter in Tamil
# Nadu actually hears. Hindi "do" (two) is absent on purpose: it is also the
# English verb, and misreading "do send" as a quantity would be worse than
# asking.
_NUMBER_WORDS: Dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100, "dozen": 12,
    # Tamil, romanised and in script.
    "onnu": 1, "rendu": 2, "irandu": 2, "moonu": 3, "munu": 3, "naalu": 4,
    "nalu": 4, "anju": 5, "aaru": 6, "ezhu": 7, "ettu": 8, "onbadhu": 9,
    "pathu": 10, "ஒன்று": 1, "இரண்டு": 2, "மூன்று": 3, "நான்கு": 4,
    "ஐந்து": 5, "ஆறு": 6, "ஏழு": 7, "எட்டு": 8, "ஒன்பது": 9, "பத்து": 10,
    # Spoken Tamil, as a counter actually says it and as a phone keyboard
    # writes it: "ரெண்டு" rather than the written "இரண்டு". Each is a count the
    # customer wrote; none is ever supplied by a model.
    "ஒண்ணு": 1, "ரெண்டு": 2, "மூணு": 3, "நாலு": 4, "அஞ்சு": 5,
    "randu": 2,
    # Hindi, romanised and in script.
    "ek": 1, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5,
    "chhe": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10, "एक": 1, "दो": 2,
    "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "छह": 6, "सात": 7, "आठ": 8,
    "नौ": 9, "दस": 10,
}
_TENS = {20, 30, 40, 50, 60, 70, 80, 90}

# "a Havells MCB" is one breaker, but "a" is also just an article. It counts
# only when nothing else on the line does.
_WEAK_ONE = frozenset({"a", "an", "oru", "ஒரு"})

# Where one order line ends and the next begins. A full stop between digits
# is a decimal ("1.5 sq mm") and is not a break.
_BREAK = re.compile(
    r"\n|;|,|!|\?|\+|(?<!\d)\.|\.(?!\d)"
    r"|\s(?:and|&|plus|also)\s", re.IGNORECASE)
# "Rs.1", "No.5" and "Qty.2" are abbreviations, not the end of a line.
_ABBREVIATION = re.compile(r"\b(rs|no|nos|qty)\.", re.IGNORECASE)
_TOKEN = re.compile(r"\S+")
_STRIP = "\"'()[]{}:<>“”‘’*"
_NUMERAL = re.compile(r"^[x×]?(\d+)([^\W\d_]*)$")

STRONG = "STRONG"      # a number, or a written number, on its own
MEASURE = "MEASURE"    # a number counted in metres - a quantity or a length
WEAK = "WEAK"          # "a" / "an"


# A number written with digit-group commas: "2,000", "20,000", "1,00,000".
# The comma used to be read as the end of an order line, so "2,000 Havells
# MCB" became a line "2" and a line "000 Havells MCB" - and the owner was told
# the order asked for 0. The comma is kept inside the number instead (as a
# character no line break matches), and the count is never quoted: whether
# the customer meant two thousand or wrote "2," and a stray "000" is theirs
# to say.
_GROUPED = re.compile(r"(?<![\d,.])(\d{1,3}(?:,\d{2})*,\d{3})(?![\d,])")
_GROUP_MARK = "⁣"   # INVISIBLE SEPARATOR, never typed in an order
_GROUPED_TOKEN = re.compile(r"^(\d{1,3})((?:" + _GROUP_MARK + r"\d{2,3})+)$")


def _segments(text: str) -> List[str]:
    text = _ABBREVIATION.sub(lambda m: m.group(1) + " ", text or "")
    # A current rating is a specification, however it was written or heard:
    # "32 amp", "32 AC curve" (a transcription of "32 amp C curve") -> "32A".
    # Without this, "32 AC curve" read as a second count of 32.
    text, _ = normalize_ratings(text)
    text = _GROUPED.sub(lambda m: m.group(1).replace(",", _GROUP_MARK), text)
    return [s.strip() for s in _BREAK.split(text) if s and s.strip()]


def _candidates(segment: str) -> List[Dict]:
    """Every number on this line that could be the count of the product."""
    tokens = [(m.start(), m.group().strip(_STRIP).lower())
              for m in _TOKEN.finditer(segment)]
    words = [w for _, w in tokens]
    out: List[Dict] = []
    skip_next = False
    for i, (start, word) in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        before = words[i - 1] if i else ""
        after = words[i + 1] if i + 1 < len(words) else ""
        if before in _MONEY_BEFORE or before.endswith("₹") or word.startswith("₹"):
            continue

        signed = _SIGNED.match(word)
        before2 = words[i - 2] if i > 1 else ""
        negative = bool(signed) or before in _SIGN_BEFORE or (
            before == "by" and before2 in _SIGN_BEFORE)
        if negative and (signed or _NUMERAL.match(word)
                         or word in _NUMBER_WORDS):
            value = (int(signed.group(1)) if signed
                     else int(_NUMERAL.match(word).group(1))
                     if _NUMERAL.match(word) else _NUMBER_WORDS[word])
            out.append({"at": start, "value": value, "kind": STRONG,
                        "unit": None, "negative": True})
            continue

        grouped = _GROUPED_TOKEN.match(word)
        if grouped:
            written = word.replace(_GROUP_MARK, ",")
            unit = normalize_uom(after)
            out.append({"at": start,
                        "value": int(written.replace(",", "")),
                        "kind": STRONG,
                        "unit": unit if unit != METER else None,
                        "grouped": written, "leading": int(grouped.group(1))})
            continue

        numeral = _NUMERAL.match(word)
        if numeral:
            value = int(numeral.group(1))
            suffix = numeral.group(2)
            if suffix:
                # Written against the number: "90m" is a length, "10A" a
                # rating, "20pcs" a count, "3kg" a unit the shop never sells in.
                if suffix in _FOREIGN_UNITS:
                    out.append({"at": start, "value": value, "kind": STRONG,
                                "unit": None, "foreign": suffix})
                    continue
                unit = normalize_uom(suffix)
                if unit and unit != METER:
                    out.append({"at": start, "value": value, "kind": STRONG,
                                "unit": unit})
                continue
            if after in _SPEC_WORDS:
                continue
            if after in _FOREIGN_UNITS:
                out.append({"at": start, "value": value, "kind": STRONG,
                            "unit": None, "foreign": after})
                continue
            unit = normalize_uom(after)
            out.append({"at": start, "value": value,
                        "kind": MEASURE if unit == METER else STRONG,
                        "unit": unit})
            continue

        if word in _NUMBER_WORDS:
            value = _NUMBER_WORDS[word]
            if value in _TENS and 0 < _NUMBER_WORDS.get(after, 0) < 10:
                value += _NUMBER_WORDS[after]
                skip_next = True
                after = words[i + 2] if i + 2 < len(words) else ""
            if after in _SPEC_WORDS:
                continue  # "two way", "one way"
            if after in _FOREIGN_UNITS:
                out.append({"at": start, "value": value, "kind": STRONG,
                            "unit": None, "foreign": after})
                continue
            unit = normalize_uom(after)
            out.append({"at": start, "value": value,
                        "kind": MEASURE if unit == METER else STRONG,
                        "unit": unit})
        elif word in _WEAK_ONE:
            out.append({"at": start, "value": 1, "kind": WEAK, "unit": None})
    return out


def order_lines(text: str) -> List[Dict]:
    """The customer's order, one entry per line, with the count each states.

    `quantity` is None when a line states no count, and `unclear` is True when
    it states numbers but none of them can be read as THE count.
    """
    lines: List[Dict] = []
    for segment in _segments(text):
        found = _candidates(segment)
        strong = [c for c in found if c["kind"] == STRONG]
        if len(strong) > 1:
            # Two counts in one stretch of text is two products written
            # without a comma: "20 Anchor switches 3 coils Finolex wire".
            # Split at each count. If the customer wrote the count AFTER the
            # product, this pairs numbers with the wrong words - and the
            # result is then a mismatch and a question, never a quotation.
            cuts = [0] + [c["at"] for c in strong[1:]] + [len(segment)]
            for lo, hi in zip(cuts, cuts[1:]):
                part = segment[lo:hi].strip()
                count = next(c for c in strong if lo <= c["at"] < hi)
                lines.append(_line(part, count["value"], count["unit"], False,
                                   count.get("foreign"),
                                   count.get("negative", False),
                                   count.get("grouped"), count.get("leading")))
            continue
        if strong:
            lines.append(_line(segment, strong[0]["value"], strong[0]["unit"],
                               False, strong[0].get("foreign"),
                               strong[0].get("negative", False),
                               strong[0].get("grouped"),
                               strong[0].get("leading")))
            continue
        measures = [c for c in found if c["kind"] == MEASURE]
        weak = [c for c in found if c["kind"] == WEAK]
        if len(measures) == 1:
            lines.append(_line(segment, measures[0]["value"], METER, False))
        elif measures:
            lines.append(_line(segment, None, None, True))
        elif len(weak) == 1:
            lines.append(_line(segment, 1, None, False))
        else:
            lines.append(_line(segment, None, None, False))
    return lines


def foreign_unit_lines(text: str) -> List[Dict]:
    """The customer's lines whose count is in a weight or volume unit."""
    return [l for l in order_lines(text) if l.get("foreignUnit")]


def unit_question(line: Dict) -> str:
    """The question for "3 kg of <product>", from the customer's own words."""
    return (f'The order asks for {line["quantity"]} {line["foreignUnit"]} of '
            f'"{line["text"]}", but ShopFlow sells these items by count, not by '
            f"weight or volume. Nothing has been quoted. Please confirm how "
            f"many are wanted.")


def negative_lines(text: str) -> List[Dict]:
    """The customer's lines whose count is a subtraction or a negative."""
    return [l for l in order_lines(text) if l.get("negative")]


def _line(text: str, quantity: Optional[int], unit: Optional[str],
          unclear: bool, foreign: Optional[str] = None,
          negative: bool = False, grouped: Optional[str] = None,
          leading: Optional[int] = None) -> Dict:
    text = text.replace(_GROUP_MARK, ",")
    line = {"text": text, "quantity": quantity, "unit": unit,
            "unclear": unclear, "foreignUnit": foreign, "negative": negative,
            "tokens": {t for t in _tokens(text) if not t.isdigit()}}
    if grouped:
        line["groupedNumber"] = grouped
        line["leadingNumber"] = leading
    return line


def grouped_number_lines(text: str) -> List[Dict]:
    """The customer's lines whose count is written with digit-group commas."""
    return [l for l in order_lines(text) if l.get("groupedNumber")]


def _identity(data: Dataset, sku_id: str, matches: Sequence[dict]) -> set:
    """The words that name this SKU: the catalogue's, and the customer's own
    words for every search that resolved to it."""
    words = set(_product_tokens(data.product(sku_id)))
    for match in matches or []:
        if match.get("skuId") == sku_id and match.get("status") == "RESOLVED":
            words.update(_tokens(match.get("requestedText") or ""))
    return {w for w in words if not w.isdigit()}


def check_quantities(data: Dataset, customer_text: str,
                     matches: Sequence[dict], quote: Optional[dict]) -> List[Dict]:
    """Hold every quoted quantity to the customer's own line.

    Returns one entry per quoted SKU, each with a `status`. Only VERIFIED may
    stand; anything else withholds the quotation.
    """
    quoted = [(l["skuId"], l["quantity"]) for l in (quote or {}).get("lines", [])]
    if not quoted:
        return []

    identities = {sku: _identity(data, sku, matches) for sku, _ in quoted}
    lines = order_lines(customer_text)

    # Which quoted SKU each line describes: the one sharing the most words
    # with it, and only if no other quoted SKU shares as many.
    claimed: Dict[str, List[Dict]] = {sku: [] for sku, _ in quoted}
    tied: Dict[str, List[Dict]] = {sku: [] for sku, _ in quoted}
    unclaimed: List[Dict] = []
    for line in lines:
        scores = {sku: len(line["tokens"] & words)
                  for sku, words in identities.items()}
        best = max(scores.values())
        leaders = [sku for sku, s in scores.items() if s == best]
        if best == 0:
            unclaimed.append(line)
        elif len(leaders) == 1:
            claimed[leaders[0]].append(line)
        elif line["quantity"] is not None or line["unclear"]:
            for sku in leaders:
                tied[sku].append(line)

    # A line whose product words this vocabulary cannot read at all - a
    # Tamil-script product name - still has a count. When exactly one quoted
    # SKU has no line and exactly one unread line has a count, they are the
    # same line. Anything less certain than that is a question.
    counted_unclaimed = [l for l in unclaimed if l["quantity"] is not None]
    orphans = [sku for sku, _ in quoted if not claimed[sku] and not tied[sku]]
    if len(orphans) == 1 and len(counted_unclaimed) == 1:
        claimed[orphans[0]].append(counted_unclaimed[0])

    results = []
    for sku, quantity in quoted:
        stated = [l for l in claimed[sku] if l["quantity"] is not None]
        unclear = [l for l in claimed[sku] if l["unclear"]] + tied[sku]
        entry = {"skuId": sku, "quotedQuantity": quantity,
                 "requestedQuantity": None, "requestedText": "",
                 "statedQuantities": [l["quantity"] for l in stated]}
        signed = [l for l in stated if l.get("negative")]
        if signed:
            # Checked first: a subtraction is never a quantity to quote, even
            # beside a line that states a positive count for the same SKU.
            entry["status"] = NEGATIVE
            entry["requestedText"] = signed[0]["text"]
        elif any(l.get("groupedNumber") for l in stated):
            # "2,000": never quoted as 2,000, never as 2, never as 0.
            line = next(l for l in stated if l.get("groupedNumber"))
            entry["status"] = AMBIGUOUS_NUMBER
            entry["requestedText"] = line["text"]
            entry["groupedNumber"] = line["groupedNumber"]
            entry["leadingNumber"] = line["leadingNumber"]
        elif unclear or not stated:
            entry["status"] = UNSTATED
            source = (unclear or claimed[sku] or [{"text": ""}])[0]
            entry["requestedText"] = source["text"]
        elif len(stated) > 1:
            entry["status"] = CONFLICTING
            entry["requestedText"] = stated[0]["text"]
        elif stated[0].get("foreignUnit"):
            # "3 kg" of something sold by the piece. The count is not
            # converted and not assumed to be pieces: the owner is asked.
            entry["requestedQuantity"] = stated[0]["quantity"]
            entry["requestedText"] = stated[0]["text"]
            entry["requestedUnit"] = stated[0]["foreignUnit"]
            entry["catalogueUom"] = product_uom(data.product(sku))
            entry["status"] = UNIT_NOT_SOLD
        else:
            entry["requestedQuantity"] = stated[0]["quantity"]
            entry["requestedText"] = stated[0]["text"]
            entry["status"] = (VERIFIED if stated[0]["quantity"] == quantity
                               else MISMATCH)
        results.append(entry)

    # A signed line that no quoted SKU claimed still withholds the quotation:
    # an order containing "minus 2" is not quoted until the owner has said
    # what it means, whichever product the words turn out to name.
    placed = {id(l) for sku in claimed for l in claimed[sku] + tied[sku]}
    stray = [l for l in lines if l.get("negative") and id(l) not in placed]
    if stray and results and all(r["status"] != NEGATIVE for r in results):
        results[0]["status"] = NEGATIVE
        results[0]["requestedText"] = stray[0]["text"]
    # The same for a grouped number no quoted SKU claimed.
    stray = [l for l in lines if l.get("groupedNumber") and id(l) not in placed]
    if stray and results and all(r["status"] not in (NEGATIVE, AMBIGUOUS_NUMBER)
                                 for r in results):
        results[0].update(status=AMBIGUOUS_NUMBER,
                          requestedText=stray[0]["text"],
                          groupedNumber=stray[0]["groupedNumber"],
                          leadingNumber=stray[0]["leadingNumber"])
    return results


def quantity_violations(data: Dataset, customer_text: str,
                        matches: Sequence[dict],
                        quote: Optional[dict]) -> List[Dict]:
    """The quoted lines that break the invariant. Empty means it holds."""
    return [r for r in check_quantities(data, customer_text, matches, quote)
            if r["status"] != VERIFIED]


def question_for(violation: Dict, name: str) -> str:
    """One plain question for the owner. Numbers only from the order and the
    quote, both of which the owner can see."""
    # The model's proposed figure is deliberately NOT repeated here. It is not
    # the customer's number, and putting it in front of a customer as "the
    # quotation was prepared for 4" invites "yes, 4" to an offer nobody made.
    # The owner sees the proposal in the decision trace, labelled as the
    # model's.
    if violation["status"] == MISMATCH:
        return (f'The order asks for {violation["requestedQuantity"]} of '
                f'"{name}", and that quantity could not be verified against '
                f"the quotation. Nothing has been quoted. Please confirm the "
                f"quantity.")
    if violation["status"] == UNIT_NOT_SOLD:
        sold_by = str(violation.get("catalogueUom") or "piece").lower()
        return (f'The order asks for {violation["requestedQuantity"]} '
                f'{violation.get("requestedUnit")} of "{name}", but it is sold '
                f"by the {sold_by}, not by weight or volume. Nothing has been "
                f"quoted. Please confirm how many {sold_by}s are wanted.")
    if violation["status"] == NEGATIVE:
        return (f'The order says "{violation["requestedText"]}", which asks '
                f'for a negative quantity of "{name}". A quantity cannot be '
                f"negative, so nothing has been quoted. Please confirm how "
                f"many are wanted.")
    if violation["status"] == AMBIGUOUS_NUMBER:
        return (f'The order says "{violation["requestedText"]}". Did you mean '
                f'a quantity of {violation["groupedNumber"]} or '
                f'{violation["leadingNumber"]} of "{name}"? Nothing has been '
                f"quoted. Please confirm the quantity.")
    if violation["status"] == CONFLICTING:
        stated = " and ".join(str(q) for q in violation["statedQuantities"])
        return (f'The order states more than one quantity for "{name}" '
                f"({stated}). Nothing has been quoted. Please confirm the "
                f"total quantity.")
    return (f'The quantity for "{name}" could not be read from the order, so '
            f"nothing has been quoted. Please confirm the quantity.")
