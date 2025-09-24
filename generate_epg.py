#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import List, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup

def get_tr_timezone():
    try:
        return ZoneInfo("Europe/Istanbul")
    except ZoneInfoNotFoundError:
        logging.warning("tz database not found; falling back to fixed UTC+03:00 offset")
        return timezone(timedelta(hours=3))


TR_TZ = get_tr_timezone()
DEFAULT_USER_AGENT = "Mozilla/5.0 (EPG Bot)"
TIME_RE = re.compile(r"^(?:[01]?\d|2[0-3]):[0-5]\d$")

DAY_SLUGS = {
    "pazartesi": 0,
    "sali": 1, "salı": 1,
    "carsamba": 2, "çarşamba": 2,
    "persembe": 3, "perşembe": 3,
    "cuma": 4,
    "cumartesi": 5,
    "pazar": 6,
}

MONTHS_TR = {
    "ocak": 1,
    "şubat": 2, "subat": 2,
    "mart": 3,
    "nisan": 4,
    "mayıs": 5, "mayis": 5,
    "haziran": 6,
    "temmuz": 7,
    "ağustos": 8, "agustos": 8,
    "eylül": 9, "eylul": 9,
    "ekim": 10,
    "kasım": 11, "kasim": 11,
    "aralık": 12, "aralik": 12,
}

NAME_OVERRIDES = {
    "beinsports": "beIN SPORTS",
    "beinsports-2": "beIN SPORTS 2",
    "beinsports-3": "beIN SPORTS 3",
    "beinsports-4": "beIN SPORTS 4",
    "bein-sports-haber": "beIN SPORTS HABER",
}

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")


@dataclass
class Channel:
    url: str
    channel_id: str
    display_name: str
    slug: str


@dataclass
class Programme:
    channel_id: str
    title: str
    start: datetime
    stop: datetime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate XMLTV guide from channel list")
    parser.add_argument("--channels-file", default="channels.md", help="Path to channel definitions (default: channels.md)")
    parser.add_argument("--output", default="dist/epg.xml", help="Combined XML output path")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="Custom User-Agent string")
    parser.add_argument("--archive", action="store_true", help="Also write a date-stamped copy alongside the main output")
    return parser.parse_args()


def parse_channels(path: str | Path) -> List[Channel]:
    raw = Path(path).read_text(encoding="utf-8")
    channels: List[Channel] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = [p.strip() for p in stripped.split("|")]
        url = parts[0]
        slug = extract_slug(url)
        if not slug:
            logging.warning("Unable to determine slug from %s; skipping", url)
            continue
        channel_id = parts[1] if len(parts) >= 2 and parts[1] else f"{slug}.tr"
        display_name = parts[2] if len(parts) >= 3 and parts[2] else infer_display_name(slug)
        channels.append(Channel(url=url, channel_id=channel_id, display_name=display_name, slug=slug))
    return channels


def extract_slug(url: str) -> str | None:
    path = urlparse(url).path
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        return None
    return segments[-2]


def infer_display_name(slug: str) -> str:
    if slug in NAME_OVERRIDES:
        return NAME_OVERRIDES[slug]
    words = slug.replace("-", " ").split()
    return " ".join(word.capitalize() for word in words)


def fetch_html(url: str, session: requests.Session, timeout: int) -> str:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text


def parse_schedule(html_text: str) -> List[tuple[str, str]]:
    soup = BeautifulSoup(html_text, "lxml")
    candidates: List[tuple[str, str]] = []

    for row in soup.select("tbody tr"):
        texts = list(row.stripped_strings)
        if len(texts) < 2:
            continue
        time_value = None
        title_value = None
        for item in texts:
            if TIME_RE.match(item):
                time_value = item
            elif time_value and not title_value:
                title_value = item
                break
        if time_value and title_value:
            candidates.append((time_value, title_value))

    if not candidates:
        time_nodes = soup.select("span[class*='time'], div[class*='time'], strong")
        for node in time_nodes:
            time_text = node.get_text(strip=True)
            if not TIME_RE.match(time_text):
                continue
            title_node = node.find_next(string=True)
            if title_node:
                title_text = title_node.strip()
                if title_text:
                    candidates.append((time_text, title_text))

    if not candidates:
        for match in re.finditer(r">(\d{1,2}:\d{2})<[^<]*>([^<>]{3,120})<", html_text):
            time_text = match.group(1)
            title_text = match.group(2).strip()
            if TIME_RE.match(time_text) and title_text:
                candidates.append((time_text, title_text))

    seen = set()
    deduped: List[tuple[str, str]] = []
    for t_value, title in candidates:
        key = (t_value, title)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((t_value, title))
    return deduped


def determine_schedule_date(slug: str, html_text: str, now: datetime) -> date:
    parsed = parse_date_from_html(html_text)
    if parsed:
        return parsed
    weekday = DAY_SLUGS.get(slug.lower())
    if weekday is None:
        return now.date()
    delta = (weekday - now.weekday()) % 7
    return (now + timedelta(days=delta)).date()


def parse_date_from_html(html_text: str) -> date | None:
    expr = re.compile(r"(\d{1,2})\s+(ocak|şubat|subat|mart|nisan|mayıs|mayis|haziran|temmuz|ağustos|agustos|eylül|eylul|ekim|kasım|kasim|aralık|aralik)\s+(\d{4})", re.IGNORECASE)
    match = expr.search(html_text)
    if not match:
        return None
    day = int(match.group(1))
    month_key = match.group(2).lower()
    year = int(match.group(3))
    month = MONTHS_TR.get(month_key)
    if not month:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def build_programmes(channel_id: str, schedule_date: date, items: Sequence[tuple[str, str]]) -> List[Programme]:
    programmes: List[Programme] = []
    last_start: datetime | None = None
    for raw_time, title in items:
        hour, minute = map(int, raw_time.split(":"))
        start_dt = datetime.combine(schedule_date, time(hour=hour, minute=minute), tzinfo=TR_TZ)
        if last_start and start_dt <= last_start:
            while start_dt <= last_start:
                start_dt += timedelta(days=1)
        if programmes:
            programmes[-1].stop = start_dt
        programmes.append(Programme(channel_id=channel_id, title=title, start=start_dt, stop=start_dt + timedelta(hours=1)))
        last_start = programmes[-1].start
    if programmes:
        programmes[-1].stop = programmes[-1].start + timedelta(hours=1)
    return programmes


def to_xmltv(channels: Sequence[Channel], programmes: Sequence[Programme]) -> str:
    from xml.etree.ElementTree import Element, SubElement, ElementTree

    tv = Element("tv", attrib={
        "generator-info-name": "epg-bot",
        "source-info-name": "beinsports.com.tr",
    })
    for channel in channels:
        channel_el = SubElement(tv, "channel", attrib={"id": channel.channel_id})
        display = SubElement(channel_el, "display-name")
        display.text = channel.display_name

    for programme in sorted(programmes, key=lambda item: item.start):
        attrs = {
            "start": format_xmltv_datetime(programme.start),
            "stop": format_xmltv_datetime(programme.stop),
            "channel": programme.channel_id,
        }
        programme_el = SubElement(tv, "programme", attrib=attrs)
        title_el = SubElement(programme_el, "title", attrib={"lang": "tr"})
        title_el.text = programme.title

    tree = ElementTree(tv)
    try:
        from xml.etree.ElementTree import indent  # type: ignore
        indent(tv, space="  ")  # type: ignore
    except Exception:
        pass
    import io
    buffer = io.BytesIO()
    tree.write(buffer, encoding="utf-8", xml_declaration=True)
    return buffer.getvalue().decode("utf-8")


def format_xmltv_datetime(moment: datetime) -> str:
    return moment.strftime("%Y%m%d%H%M%S %z")


def main() -> None:
    args = parse_args()
    channels = parse_channels(args.channels_file)
    if not channels:
        logging.error("No channels configured. Check %s", args.channels_file)
        sys.exit(1)

    session = requests.Session()
    session.headers.update({"User-Agent": args.user_agent, "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8"})

    now = datetime.now(TR_TZ)
    all_programmes: List[Programme] = []
    included_channels: List[Channel] = []

    for channel in channels:
        logging.info("Fetching %s", channel.url)
        try:
            html_text = fetch_html(channel.url, session, timeout=args.timeout)
        except requests.RequestException as exc:
            logging.error("Failed to fetch %s: %s", channel.url, exc)
            continue

        schedule_items = parse_schedule(html_text)
        if not schedule_items:
            logging.warning("No schedule entries found for %s", channel.channel_id)
        schedule_date = determine_schedule_date(channel.slug, html_text, now)
        programmes = build_programmes(channel.channel_id, schedule_date, schedule_items)
        all_programmes.extend(programmes)
        included_channels.append(channel)
        logging.info("Added %d programmes for %s (%s)", len(programmes), channel.display_name, schedule_date.isoformat())

    if not included_channels:
        logging.error("No channel data could be fetched. Aborting.")
        sys.exit(2)

    xml_text = to_xmltv(included_channels, all_programmes)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(xml_text, encoding="utf-8")
    logging.info("Wrote combined guide to %s", output_path)

    if args.archive:
        stamp = now.strftime("%Y%m%d")
        archive_path = output_path.with_name(f"{output_path.stem}-{stamp}{output_path.suffix}")
        archive_path.write_text(xml_text, encoding="utf-8")
        logging.info("Wrote archive copy to %s", archive_path)


if __name__ == "__main__":
    main()
