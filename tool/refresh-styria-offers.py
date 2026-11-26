#!/usr/bin/env python3
"""Refresh the Styria offer feed from public offers verified in regional flyers."""

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html import unescape
import json
from pathlib import Path
import re
import unicodedata
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pymupdf

RETAILERS = ("billa", "spar", "interspar")
VIENNA = ZoneInfo("Europe/Vienna")
JSON_LD = re.compile(r'<script type="application/ld\+json">(.+?)</script>', re.S)
DATES = re.compile(r'(?<!\d)(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{4}|\d{2})?(?!\d)')
AMOUNT = re.compile(r'\b(\d+(?:[,.]\d+)?)\s*[- ]?\s*(kilogramm|gramm|liter|kg|g|ml|cl|l)\b', re.I)
RANGE = re.compile(
    r'\b(\d+(?:[,.]\d+)?)(?:\s*[- ]?\s*(?:kg|g|ml|l))?\s*[–—−-]\s*'
    r'(\d+(?:[,.]\d+)?)\s*[- ]?\s*(kg|g|ml|l)\b', re.I)
UNIT = re.compile(r'\b(\d+[,.]\d{2})\s*€\s*/\s*(kg|l)\b', re.I)
LIMIT = 40 * 1024 * 1024


@dataclass(frozen=True)
class Flyer:
    url: str
    start: date
    end: date
    pdf: bytes


@dataclass(frozen=True)
class Candidate:
    name: str
    description: str
    start: date
    end: date
    cents: int
    quantity: int
    each: int
    regular_each: int
    unit: str | None


@dataclass(frozen=True)
class Block:
    rect: pymupdf.Rect
    text: str


def fetch(url):
    request = Request(url, headers={"User-Agent": "Checker-App weekly offer feed"})
    with urlopen(request, timeout=35) as response:
        if urlsplit(response.url).scheme != "https":
            raise ValueError("Expected an HTTPS retailer source")
        payload = response.read(LIMIT + 1)
        if len(payload) > LIMIT:
            raise ValueError("Retailer response exceeded the download limit")
        return payload


def editions(retailer, today):
    # The regional viewers identify editions by the calendar week and cover date.
    thursday = today - timedelta(days=(today.weekday() - 3) % 7)
    for offset in (7, 0, -7):
        start = thursday + timedelta(days=offset)
        year, week, _ = start.isocalendar()
        if retailer == "billa":
            yield f"https://view.publitas.com/billa-at/billa_fb_kw{week:02}_{year}_steiermark/"
        elif retailer == "spar":
            yield f"https://flugblatt.spar.at/steiermark/spar/{start:%y%m%d}-1-flugblatt-kw-{week:02}/"
        else:
            yield f"https://flugblatt.interspar.at/steiermark/steiermark_kw{week:02}/"


def edition_dates(value, today):
    matches = list(DATES.finditer(value))
    if len(matches) < 2:
        raise ValueError("The flyer did not publish a date range")
    first, last = matches[:2]

    def year(match, default):
        value = match[3]
        return default if value is None else int(value) + (2000 if len(value) == 2 else 0)

    end = date(year(last, today.year), int(last[2]), int(last[1]))
    beginning = date(year(first, end.year), int(first[2]), int(first[1]))
    if not first[3] and beginning > end:
        beginning = beginning.replace(year=end.year - 1)
    if not timedelta(0) <= end - beginning <= timedelta(days=35):
        raise ValueError("Unexpected flyer date range")
    return beginning, end


def get_flyer(retailer, url, today):
    html = fetch(url).decode("utf-8")
    if retailer == "billa":
        title = re.search(r'"publicationOriginalTitle":"([^"]+)"', html)
        download = re.search(r'"downloadPdfUrl":("[^"]+")', html)
        if not title or "steiermark" not in title[1].lower() or not download:
            raise ValueError("BILLA did not return a Styrian edition")
        pdf_url = json.loads(download[1])
        if urlsplit(pdf_url).hostname != "view.publitas.com":
            raise ValueError("Unexpected BILLA PDF host")
        pdf = fetch(pdf_url)
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            text = doc[0].get_text()
        title = re.search(r'ANGEBOT(?:E)?\s+GÜLTIG\s+(?:VON|AB)\s+([\s\S]{0,90})', text, re.I)
        if not title:
            raise ValueError("BILLA cover dates could not be verified")
        start, end = edition_dates(title[1], today)
    else:
        canonical = re.search(r'rel="canonical"\s+href="([^"]+)"', html)
        title = re.search(r'"pageTitle":("[^"]+")', html)
        if not canonical or canonical[1].rstrip("/") != url.rstrip("/") or not title:
            raise ValueError("SPAR viewer did not confirm the Styrian edition")
        start, end = edition_dates(json.loads(title[1]), today)
        if end < today or start > today + timedelta(days=7):
            return None
        pdf = fetch(url + "ViewPdf.ashx")
    if end < today or start > today + timedelta(days=7):
        return None
    return Flyer(url, start, end, pdf)


def money(value):
    try:
        amount = Decimal(str(value).replace(",", ".")) * 100
        if amount.is_finite() and 0 < amount == int(amount) <= 100_000:
            return int(amount)
    except (InvalidOperation, ValueError):
        pass
    return None


def parse_candidate(product, retailer):
    offer = product.get("offers")
    name, description = product.get("name"), product.get("description")
    if not isinstance(offer, dict) or not isinstance(name, str) or not isinstance(description, str):
        return None
    if offer.get("seller", {}).get("name", "").casefold() != retailer:
        return None
    if offer.get("priceCurrency") != "EUR" or offer.get("availability") != "https://schema.org/InStoreOnly":
        return None
    match = re.search(r'f[üu]r .*?, je [\wäöü. ]+ €\s*(\d+,\d{2})', description, re.I)
    quantity = offer.get("eligibleQuantity", {}).get("value", 1)
    cents = money(offer.get("price"))
    regular = re.search(r'statt €\s*(\d+,\d{2})', description)
    if not isinstance(quantity, int) or not 1 <= quantity <= 30 or not cents:
        return None
    regular_cents = money(regular[1]) if regular else None
    if not regular_cents or regular_cents <= cents:
        return None
    each = cents if quantity == 1 else money(match[1]) if match else None
    if not each or abs(cents - each * quantity) > (quantity + 1) // 2:
        return None
    if re.search(r'\b(jö|joker|gutschein|coupon|onlinepreis|mit .*?app)\b', description, re.I):
        return None
    try:
        start = date.fromisoformat(offer["validFrom"])
        end = date.fromisoformat(offer["validThrough"])
    except (KeyError, TypeError, ValueError):
        return None
    unit = UNIT.search(description)
    return Candidate(name.strip(), description, start, end, cents, quantity, each,
                     round(regular_cents / quantity),
                     f"{unit[1]} €/{unit[2]}" if unit else None)


def listing_candidates(html, today):
    # The paginated public page has prices and conditions in its offer cards.
    for card in re.findall(r'<a class="z"\s.*?</a>', html, re.S):
        title = re.search(r'<span class="n">([^<]+)</span>', card)
        details = re.search(r'<span class="m">([^<]+)</span>', card)
        price = re.search(r'<span class="schild">([^<]+)</span>', card)
        regular = re.search(r'<s>\s*€\s*([^<]+)</s>', card)
        if not all((title, details, price, regular)):
            continue
        details = unescape(details[1]).strip()
        if re.search(r'\b(jö|joker|app|coupon|gutschein|online|bald|ab\s+\d{1,2}\.)\b', details, re.I):
            continue
        expiration = re.search(r'gültig bis (\d{1,2})\.(\d{1,2})\.', details, re.I)
        cents = money(price[1])
        regular_cents = money(regular[1])
        if not expiration or not cents or not regular_cents or regular_cents <= cents:
            continue
        day, month = map(int, expiration.groups())
        try:
            end = date(today.year + (today.month == 12 and month == 1), month, day)
        except ValueError:
            continue
        if not today <= end <= today + timedelta(days=35):
            continue
        required = re.search(r'\bab\s+(\d{1,2})\s+\w', details, re.I)
        per_pack = re.search(r'\bje\s+(\d+,\d{2})\s*€', unescape(card))
        quantity = int(required[1]) if required else 1
        if (per_pack is None) != (quantity == 1):
            continue
        each = money(per_pack[1]) if per_pack else cents
        if not 1 <= quantity <= 30 or not each or abs(cents - each * quantity) > (quantity + 1) // 2:
            continue
        metadata = details.split(" · ", 1)[0]
        name = unescape(title[1]).strip()
        if metadata and not re.search(r'[\d€]|sorten|gültig|\bab\b', metadata, re.I):
            if normalize(metadata) not in normalize(name):
                name = f"{metadata} {name}"
        unit = UNIT.search(unescape(card))
        yield Candidate(name, details, today, end, cents, quantity, each,
                        round(regular_cents / quantity),
                        f"{unit[1]} €/{unit[2]}" if unit else None)


def candidates(retailer, today, limit=4):
    offers = {}
    for number in range(1, limit + 1):
        suffix = "" if number == 1 else f"?seite={number}"
        url = f"https://ledice.ai/aktionen/{retailer}{suffix}"
        try:
            html = fetch(url).decode("utf-8")
        except HTTPError as error:
            if number > 1 and error.code == 404:
                break
            raise
        if number > 1:
            for candidate in listing_candidates(html, today):
                offers[candidate] = candidate
        for markup in JSON_LD.findall(html):
            data = json.loads(markup)
            if not isinstance(data, dict) or data.get("@type") != "ItemList":
                continue
            for entry in data.get("itemListElement", []):
                item = entry.get("item", {})
                candidate = parse_candidate(item, retailer)
                if candidate and candidate.end >= today:
                    offers[candidate] = candidate
    if not offers:
        raise ValueError("No structured offer candidates")
    return list(offers)


def normalize(value):
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", value))


def pack_size(text):
    match = AMOUNT.search(text)
    if match is None:
        return None
    amount = Decimal(match[1].replace(",", "."))
    unit = match[2].lower()
    if unit in ("kg", "kilogramm", "liter", "l"):
        amount *= 1000
    if unit == "cl":
        amount *= 10
    measure = "g" if unit in ("kg", "kilogramm", "g", "gramm") else "ml"
    return amount, measure


def advertised_pack(text):
    if match := RANGE.search(text):
        start, end, unit = match.groups()
        return f"{start}–{end} {unit}", [pack_size(f"{n} {unit}") for n in (start, end)]
    if match := AMOUNT.search(text):
        return f"{match[1]} {match[2]}", [pack_size(match[0])]
    return None, []


def card_sizes(text):
    sizes = {pack_size(match[0]) for match in AMOUNT.finditer(text)}
    for match in RANGE.finditer(text):
        sizes.update(advertised_pack(match[0])[1])
    return sizes


def page_blocks(page):
    return [Block(pymupdf.Rect(*item[:4]), item[4].strip())
            for item in page.get_text("blocks", flags=pymupdf.TEXTFLAGS_BLOCKS & ~pymupdf.TEXT_PRESERVE_IMAGES)
            if item[4].strip()]


def price_on_card(block, cents):
    euros, remainder = divmod(cents, 100)
    regular = rf'{euros}[,.]\s*{remainder:02}'
    display = rf'0?{euros}\s*{remainder:02}'
    return any(re.fullmatch(rf'(?i)\s*(?:nur\s+)?(?:€\s*)?(?:{regular}|{display})\s*€?\s*', line)
               for line in block.text.splitlines())


def title_matches(text, name):
    return f" {name} " in f" {normalize(text)} "


def local_card(rect, name, price):
    return pymupdf.Rect(
        min(name.rect.x0, price.rect.x0) - rect.width * .014,
        min(name.rect.y0, price.rect.y0) - rect.height * .022,
        max(name.rect.x1, price.rect.x1) + rect.width * .027,
        max(name.rect.y1, price.rect.y1) + rect.height * .034,
    )


def near_names(blocks, label, page_rect):
    for block in blocks:
        if title_matches(block.text, label):
            yield block
            continue
        # Large and bold PDF headings can be split into separate text blocks.
        if len(label.split()) < 2 or normalize(block.text).split()[:1] != label.split()[:1]:
            continue
        for right in blocks:
            if right.rect.y0 < block.rect.y0 or right.rect.y0 > block.rect.y1 + page_rect.height * .016:
                continue
            if abs(right.rect.x0 - block.rect.x0) > page_rect.width * .045 or right == block:
                continue
            combined = block.text + " " + right.text
            if title_matches(combined, label):
                yield Block(block.rect | right.rect, combined)
                break


def verified_card(page_rect, blocks, candidate):
    label = normalize(candidate.name)
    if label in {"kombiaktion", "aktion", "angebot", "gutschein", "rabatt"}:
        return None
    labels = [label]
    shorter = re.sub(r'^(billa|spar|interspar)\s+', '', label)
    if len(shorter.split()) >= 2 and shorter != label:
        labels.append(shorter)
    candidates = [p for p in blocks if price_on_card(p, candidate.each)]
    _, expected_sizes = advertised_pack(candidate.description.split(" bei ", 1)[0])
    windows = []
    for label in labels:
        for name in near_names(blocks, label, page_rect):
            for price in candidates:
                # A badge entirely to the left belongs to a different flyer card.
                if price.rect.x1 < name.rect.x0:
                    continue
                dx = abs(name.rect.x0 - price.rect.x0) / page_rect.width
                dy = abs(name.rect.y0 - price.rect.y0) / page_rect.height
                if dx > .25 or dy > .14 or dx + dy > .26:
                    continue
                card = local_card(page_rect, name, price)
                local = [b for b in blocks if card.contains((b.rect.tl + b.rect.br) / 2)]
                text = " ".join(b.text for b in local)
                if expected_sizes and not set(expected_sizes).issubset(card_sizes(text)):
                    continue
                thresholds = set(int(n) for n in re.findall(
                    r'\b(?:ab|bei)\s+(\d{1,2})\s*(?:stk|ds|be|fl|gl|st[üu]ck|dosen|pkg|pack|kist)',
                    text, re.I))
                if candidate.quantity > 1 and candidate.quantity not in thresholds:
                    continue
                if candidate.quantity == 1 and thresholds:
                    continue
                euros, remainder = divmod(candidate.regular_each, 100)
                regular = rf'(?<![\d,.]){euros}[,.]\s*{remainder:02}(?!\d)'
                if candidate.quantity == 1:
                    if not re.search(rf'\bstatt\s*€?\s*{regular}', text, re.I):
                        continue
                elif not re.search(regular, text) or re.search(
                    rf'\b(?:ab|bei)\s+{candidate.quantity}\s*(?:kist|karton|tray)', text, re.I
                ):
                    continue
                if re.search(r'\b(jö|joker|gutschein|coupon|spar-app|fr\s*&\s*sa|freitag\s+(?:und|&)\s+samstag|zusätzlich\s*-|verfügbar\s+nur|nur\s+online|online\s+gültig|stattpreis\s+pro)\b', text, re.I):
                    continue
                if DATES.search(text):
                    continue
                windows.append((dx + dy, text))
    return min(windows, default=(0, None))[1]


def austrian_holidays(year):
    # Gregorian Easter Sunday, followed by Austria's movable store holidays.
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    weekday = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * weekday) // 451
    month, day = divmod(h + weekday - 7 * m + 114, 31)
    easter = date(year, month, day + 1)
    return {easter + timedelta(days=n) for n in (1, 39, 50, 60)} | {
        date(year, month, day) for month, day in
        ((1, 1), (1, 6), (5, 1), (8, 15), (10, 26), (11, 1), (12, 8), (12, 25), (12, 26))
    }


def verified_offers(retailer, flyer, products, today):
    with pymupdf.open(stream=flyer.pdf, filetype="pdf") as doc:
        for page in doc:
            blocks = page_blocks(page)
            for product in products:
                start, end = max(product.start, flyer.start), min(product.end, flyer.end)
                if end < today or start > end:
                    continue
                card = verified_card(page.rect, blocks, product)
                if not card:
                    continue
                prefix = product.description.split(" bei ", 1)[0]
                pack, _ = advertised_pack(prefix)
                row = {
                    "retailer": retailer, "name": product.name, "cents": product.cents,
                    "from": start.isoformat(), "until": end.isoformat(),
                    "source": flyer.url + (f"page/{page.number + 1}" if retailer == "billa" else f"#{page.number + 1}"),
                    "automatic": True,
                }
                if pack:
                    row["pack"] = pack
                if product.quantity > 1:
                    row["quantity"] = product.quantity
                    row["condition"] = f"Ab {product.quantity} Stück"
                elif re.search(r'\b(?:pro|per|je)\s+(?:kg|100\s*g)\b', prefix, re.I):
                    row["condition"] = "Gewichtsware"
                if product.unit:
                    row["unit"] = product.unit
                if retailer == "billa":
                    holidays = austrian_holidays(start.year) | austrian_holidays(end.year)
                    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
                    row["excludedDays"] = [day.isoformat() for day in days
                                           if day.weekday() == 6 or day in holidays]
                yield row


def refresh_retailer(retailer, today):
    products = candidates(retailer, today)
    offers = []
    confirmed_editions = set()
    for url in editions(retailer, today):
        try:
            flyer = get_flyer(retailer, url, today)
        except HTTPError as error:
            if error.code != 404:
                print(f"{retailer}: {error.code} loading {url}")
            continue
        except (OSError, ValueError, pymupdf.FileDataError) as error:
            print(f"{retailer}: {error} loading {url}")
            continue
        if flyer:
            rows = list(verified_offers(retailer, flyer, products, today))
            if rows:
                offers.extend(rows)
                confirmed_editions.add(flyer.url)
    if not offers:
        raise ValueError(f"{retailer}: none of {len(products)} candidates could be verified")
    return offers, confirmed_editions


def offer_identity(row):
    name = re.sub(r'\s+(versch|verschiedene|div)\s+sorten$', '', normalize(row["name"]))
    pack = row.get("pack", "")
    sizes = advertised_pack(pack)[1]
    return (row["retailer"], name, tuple(sizes) or normalize(pack),
            row.get("quantity", 1), row["cents"])


def offer_key(row):
    return (*offer_identity(row), row["from"], row["until"])


def edition_url(row):
    return row.get("source", "").split("#", 1)[0].split("/page/", 1)[0].rstrip("/") + "/"


def add_offer(combined, row):
    identity = offer_identity(row)

    def same_product(key):
        return (key[:2] == identity[:2] and key[3:-2] == identity[3:]
                and (not key[2] or not identity[2] or key[2] == identity[2]))

    same = [(key, other) for key, other in combined.items()
            if same_product(key) and other["from"] <= row["until"] and row["from"] <= other["until"]]
    for key, other in same:
        if other["until"] >= row["until"] and other["from"] <= row["from"]:
            return
    for key, other in same:
        if other.get("automatic") and row["until"] >= other["until"] and row["from"] <= other["from"]:
            del combined[key]
    combined[offer_key(row)] = row


def refresh(feed, today):
    previous = {offer_key(row): row for row in feed.get("offers", [])
                if row["until"] >= today.isoformat()}
    combined = dict(previous)
    succeeded = 0
    with ProcessPoolExecutor(max_workers=len(RETAILERS)) as pool:
        jobs = {retailer: pool.submit(refresh_retailer, retailer, today)
                for retailer in RETAILERS}
        for retailer, job in jobs.items():
            try:
                rows, confirmed_editions = job.result()
            except (ValueError, OSError, TypeError, KeyError, pymupdf.FileDataError) as error:
                print(f"Refresh failed for {retailer}: {error}")
                continue
            succeeded += 1
            combined = {key: row for key, row in combined.items()
                        if not row.get("automatic") or edition_url(row) not in confirmed_editions}
            for row in rows:
                add_offer(combined, row)
    if succeeded == 0:
        raise ValueError("All retailer source checks failed; preserving the published feed")
    offers = sorted(combined.values(), key=lambda row: (
        row["retailer"], row["until"], row["name"], row["cents"], row["from"]))
    print("Offers by retailer:", dict(Counter(row["retailer"] for row in offers)))
    if combined == previous and len(previous) == len(feed.get("offers", [])):
        return feed
    return {"region": "AT-6", "checkedAt": datetime.now(timezone.utc).isoformat(),
            "complete": False, "offers": offers}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feed", type=Path, help="The existing Styrian JSON feed.")
    args = parser.parse_args()
    feed = json.loads(args.feed.read_text(encoding="utf-8")) if args.feed.exists() else {}
    if feed and feed.get("region") != "AT-6":
        parser.error("Expected a feed for AT-6")
    today = datetime.now(VIENNA).date()
    updated = refresh(feed, today)
    if updated != feed:
        staging = args.feed.with_suffix(".json.tmp")
        staging.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staging.replace(args.feed)


if __name__ == "__main__":
    main()
