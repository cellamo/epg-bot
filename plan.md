Great use-case. Here’s a clean, production-ish way to scrape that guide and publish a valid **XMLTV** feed every day at **12:05 TRT** (+03:00).

---

# Plan (TL;DR)

1. **Scrape** the beIN page for the chosen day (e.g. `/yayin-akisi/beinsports/persembe`). The schedule is in the HTML (no JS needed). ([beinsports.com.tr][1])
2. **Build XMLTV**: `<tv><channel/><programme/></tv>` with `start`/`stop` like `YYYYMMDDHHMMSS +0300`. ([wiki.xmltv.org][2])
3. **Publish** via GitHub Actions on a **cron** that equals **12:05 TRT** (that is **09:05 UTC**, Turkey is permanently UTC+3). ([Time and Date][3])
4. Serve it with **GitHub Pages** (static URL), and also keep a `latest.xml`.

---

# 1) Python scraper → XMLTV generator

Create `epg.py`:

```python
#!/usr/bin/env python3
import argparse, re, sys, html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup

TR_TZ = ZoneInfo("Europe/Istanbul")  # UTC+3 year-round

DAY_SLUGS = {
    "pazartesi": 0, "sali": 1, "salı": 1, "carsamba": 2, "çarşamba": 2,
    "persembe": 3, "perşembe": 3, "cuma": 4, "cumartesi": 5, "pazar": 6,
}

def next_weekday(base_dt, weekday):
    delta = (weekday - base_dt.weekday()) % 7
    return (base_dt + timedelta(days=delta)).date()

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True,
                   help="e.g. https://beinsports.com.tr/yayin-akisi/beinsports/persembe")
    p.add_argument("--channel-id", default="beinsports.tr")
    p.add_argument("--channel-name", default="beIN SPORTS")
    p.add_argument("--out-prefix", default="dist/beinsports")
    return p.parse_args()

def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (EPGBot)",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text

def infer_date_from_url(url: str) -> datetime:
    """Map slug (pazartesi…pazar) to the actual date in TR timezone."""
    today_tr = datetime.now(TR_TZ)
    m = re.search(r"/(pazartesi|sali|salı|carsamba|çarşamba|persembe|perşembe|cuma|cumartesi|pazar)\b", url, re.I)
    if m:
        wd = DAY_SLUGS[m.group(1).lower()]
        return datetime.combine(next_weekday(today_tr, wd), datetime.min.time(), tzinfo=TR_TZ)
    # fallback: assume today
    return datetime(today_tr.year, today_tr.month, today_tr.day, tzinfo=TR_TZ)

def parse_schedule(html_text: str):
    """Return list of (HH:MM, title). Works with both table and plain list layouts."""
    soup = BeautifulSoup(html_text, "lxml")

    rows = []
    # 1) Table layout (like user's snippet)
    for tr in soup.select("tbody tr"):
        spans = tr.select("span")
        if len(spans) >= 2:
            t = spans[0].get_text(strip=True)
            name = spans[1].get_text(strip=True)
            if re.fullmatch(r"\d{1,2}:\d{2}", t):
                rows.append((t, name))

    # 2) Fallback: generic spans (class names often include 'time' and 'program')
    if not rows:
        times = soup.select("span[class*='time'], span:-soup-contains(':')")
        for s in times:
            t = s.get_text(strip=True)
            if re.fullmatch(r"\d{1,2}:\d{2}", t):
                # try to find the next sibling span for title
                nxt = s.find_next("span")
                if nxt:
                    name = nxt.get_text(strip=True)
                    rows.append((t, name))

    # 3) Last-chance regex over raw HTML (very permissive)
    if not rows:
        for t, name in re.findall(r'>(\d{1,2}:\d{2})<.*?>\s*([^<]{3,100})<', html_text):
            rows.append((t.strip(), name.strip()))

    # de-dup and keep order
    seen = set(); out = []
    for t, name in rows:
        key = (t, name)
        if key not in seen:
            seen.add(key)
            out.append((t, name))
    return out

def build_programmes(day_dt: datetime, items, channel_id: str):
    # Convert HH:MM → aware datetimes for the target day, and set stop = next start (or 23:59:59)
    parsed = []
    for t, title in items:
        h, m = map(int, t.split(":"))
        parsed.append((datetime(day_dt.year, day_dt.month, day_dt.day, h, m, tzinfo=TR_TZ), title))
    parsed.sort(key=lambda x: x[0])

    progs = []
    for i, (start_dt, title) in enumerate(parsed):
        if i + 1 < len(parsed):
            stop_dt = parsed[i+1][0]
        else:
            stop_dt = datetime(day_dt.year, day_dt.month, day_dt.day, 23, 59, 59, tzinfo=TR_TZ)
        progs.append((start_dt, stop_dt, title, channel_id))
    return progs

def fmt_dt(dt: datetime) -> str:
    # XMLTV wants %Y%m%d%H%M%S and a space before %z, e.g. "20250925... +0300"
    return dt.strftime("%Y%m%d%H%M%S %z")

def to_xmltv(channel_id: str, channel_name: str, programmes):
    parts = [f'<?xml version="1.0" encoding="UTF-8"?>',
             f'<tv generator-info-name="Zafer-EPG">']
    parts.append(f'  <channel id="{html.escape(channel_id)}">')
    parts.append(f'    <display-name>{html.escape(channel_name)}</display-name>')
    parts.append(f'  </channel>')
    for start_dt, stop_dt, title, ch in programmes:
        parts.append(
            f'  <programme start="{fmt_dt(start_dt)}" stop="{fmt_dt(stop_dt)}" channel="{html.escape(ch)}">'
        )
        parts.append(f'    <title lang="tr">{html.escape(title)}</title>')
        parts.append(f'  </programme>')
    parts.append('</tv>')
    return "\n".join(parts)

def main():
    args = parse_args()
    html_text = fetch_html(args.url)
    day_dt = infer_date_from_url(args.url)
    items = parse_schedule(html_text)
    if not items:
        print("No schedule items found. The page layout may have changed.", file=sys.stderr)
        sys.exit(2)
    programmes = build_programmes(day_dt, items, args.channel_id)
    xml = to_xmltv(args.channel_id, args.channel_name, programmes)

    # write files
    day_tag = day_dt.strftime("%Y%m%d")
    import os
    os.makedirs("dist", exist_ok=True)
    day_path = f"{args.out-prefix}.{day_tag}.xml"
    latest_path = f"{args.out-prefix}.latest.xml"
    open(day_path, "w", encoding="utf-8").write(xml)
    open(latest_path, "w", encoding="utf-8").write(xml)
    print("Wrote:", day_path, "and", latest_path)

if __name__ == "__main__":
    main()
```

**Run locally** (example for Thursday feed):

```bash
python epg.py \
  --url https://beinsports.com.tr/yayin-akisi/beinsports/persembe \
  --channel-id beinsports.tr \
  --channel-name "beIN SPORTS"
```

This outputs `dist/beinsports.YYYYMMDD.xml` and `dist/beinsports.latest.xml`.

---

# 2) Automate daily at 12:05 (TRT) with GitHub Actions

Create `.github/workflows/epg.yml`:

```yaml
name: Generate EPG (beIN Sports)

on:
  schedule:
    # GitHub cron is UTC. 12:05 TRT = 09:05 UTC.
    - cron: "5 9 * * *"
  workflow_dispatch: {}

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: write    # allow pushing (via GITHUB_TOKEN)
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.x" }
      - run: pip install requests beautifulsoup4 lxml
      - run: python epg.py --url https://beinsports.com.tr/yayin-akisi/beinsports/persembe --channel-id beinsports.tr --channel-name "beIN SPORTS"
      - name: Publish to gh-pages
        uses: peaceiris/actions-gh-pages@v3
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          publish_dir: dist
          # dist will contain beinsports.YYYYMMDD.xml and beinsports.latest.xml
```

Then enable **Pages → Source: `gh-pages`**. Your feed URLs will be:

* `https://<github-username>.github.io/<repo>/beinsports.latest.xml`
* `https://<github-username>.github.io/<repo>/beinsports.YYYYMMDD.xml`

Point your IPTV player to either one.

---

# 3) Optional: Vercel Cron + on-demand endpoint

Since you’re on Vercel, you can expose an endpoint that returns **fresh XML** and also schedule a daily warm-build:

* Next.js API route `/api/epg` (fetch + return XML with `Content-Type: application/xml`).
* Add Vercel Cron (UTC): `"5 9 * * *"` to hit `/api/epg` daily (or a `/api/rebuild-epg` that writes to Vercel Blob, then serve via `/api/epg-latest`).

*(If you want this version, say the word and I’ll drop the exact TS code + `vercel.json`.)*

---

## Notes & gotchas

* **HTML changes**: I made the parser tolerant to layout/class changes, but if beIN changes markup hard, adjust selectors quickly. The data is present server-side in current pages. ([beinsports.com.tr][1])
* **Time zone**: We hardcode `Europe/Istanbul` (UTC+3 permanently). No DST flips. ([Time and Date][3])
* **XMLTV**: Many players *expect* `stop` times; we derive `stop` from the next programme, and set the last one to `23:59:59`. ([help.cesbo.com][4])
* **Legality/ToS**: Use this for personal EPG; be polite with requests (one fetch/day).

If you want me to adapt this to **multiple beIN channels** (e.g., `bein-sports-haber` etc.) or add **descriptions/icons**, I can extend the scraper and emit multiple `<channel>` blocks in one XML.

[1]: https://beinsports.com.tr/yayin-akisi/beinsports/persembe?utm_source=chatgpt.com "beinsports Yayın Akışı"
[2]: https://wiki.xmltv.org/index.php/XMLTVFormat?utm_source=chatgpt.com "XMLTVFormat"
[3]: https://www.timeanddate.com/news/time/turkey-scraps-dst-2016.html?utm_source=chatgpt.com "Turkey Scraps Daylight Saving Time"
[4]: https://help.cesbo.com/misc/articles/format/xmltv/?utm_source=chatgpt.com "XMLTV - Cesbo Docs"

Absolutely. GitHub Actions + Pages can handle **many channels** just fine. You’ve got two clean patterns:

# Option A — One combined XMLTV for all channels

Your IPTV player reads a single URL. The workflow scrapes each channel page, then writes one `all.latest.xml` that contains multiple `<channel>` blocks and their `<programme>` entries.

**epg\_multi.py** (key bits; drop-in replacement for your script):

```python
#!/usr/bin/env python3
import re, html, sys, os, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup

TR_TZ = ZoneInfo("Europe/Istanbul")

DAY_SLUGS = {
    "pazartesi":0,"sali":1,"salı":1,"carsamba":2,"çarşamba":2,"persembe":3,"perşembe":3,
    "cuma":4,"cumartesi":5,"pazar":6,
}

CHANNELS = [
  {
    "url": "https://beinsports.com.tr/yayin-akisi/beinsports/persembe",
    "id": "beinsports.tr", "name": "beIN SPORTS"
  },
  {
    "url": "https://beinsports.com.tr/yayin-akisi/beinsportshaber/persembe",
    "id": "beinsportshaber.tr", "name": "beIN SPORTS HABER"
  },
  # add as many as you want...
]

def next_weekday(base, wd):
    delta = (wd - base.weekday()) % 7
    return (base + timedelta(days=delta)).date()

def infer_date_from_url(url):
    today = datetime.now(TR_TZ)
    m = re.search(r"/(pazartesi|sali|salı|carsamba|çarşamba|persembe|perşembe|cuma|cumartesi|pazar)\b", url, re.I)
    if not m: return datetime(today.year, today.month, today.day, tzinfo=TR_TZ)
    wd = DAY_SLUGS[m.group(1).lower()]
    d  = next_weekday(today, wd)
    return datetime(d.year, d.month, d.day, tzinfo=TR_TZ)

def fetch(url):
    r = requests.get(url, headers={"User-Agent":"EPGBot/1.0"}, timeout=30)
    r.raise_for_status()
    return r.text

def parse(html_text):
    soup = BeautifulSoup(html_text, "lxml")
    rows = []
    for tr in soup.select("tbody tr"):
        spans = tr.select("span")
        if len(spans) >= 2:
            t = spans[0].get_text(strip=True)
            name = spans[1].get_text(strip=True)
            if re.fullmatch(r"\d{1,2}:\d{2}", t):
                rows.append((t, name))
    if not rows:
        for t, name in re.findall(r'>(\d{1,2}:\d{2})<.*?>\s*([^<]{3,100})<', html_text):
            rows.append((t.strip(), name.strip()))
    # de-dup keep order
    out, seen = [], set()
    for it in rows:
        if it not in seen:
            seen.add(it); out.append(it)
    return out

def build_programmes(day_dt, items, channel_id):
    from datetime import datetime as dt
    parsed = []
    for t, title in items:
        h, m = map(int, t.split(":"))
        parsed.append((dt(day_dt.year, day_dt.month, day_dt.day, h, m, tzinfo=TR_TZ), title))
    parsed.sort(key=lambda x: x[0])
    progs = []
    for i, (start_dt, title) in enumerate(parsed):
        stop_dt = parsed[i+1][0] if i+1 < len(parsed) else start_dt.replace(hour=23, minute=59, second=59)
        progs.append((start_dt, stop_dt, title, channel_id))
    return progs

def fmt_dt(dt): return dt.strftime("%Y%m%d%H%M%S %z")

def main():
    os.makedirs("dist", exist_ok=True)
    all_channels = []
    all_programmes = []
    day_tag = datetime.now(TR_TZ).strftime("%Y%m%d")

    for ch in CHANNELS:
        html_text = fetch(ch["url"])
        day_dt = infer_date_from_url(ch["url"])
        items = parse(html_text)
        if not items: 
            print(f"[warn] No schedule items for {ch['id']}", file=sys.stderr)
            continue
        progs = build_programmes(day_dt, items, ch["id"])
        all_channels.append(ch)
        all_programmes.extend(progs)

        # optional: per-channel xml too
        xml = tv_xml([ch], progs)
        open(f"dist/{ch['id']}.{day_tag}.xml","w",encoding="utf-8").write(xml)
        open(f"dist/{ch['id']}.latest.xml","w",encoding="utf-8").write(xml)

    # combined XML
    combined = tv_xml(all_channels, all_programmes)
    open(f"dist/all.{day_tag}.xml","w",encoding="utf-8").write(combined)
    open("dist/all.latest.xml","w",encoding="utf-8").write(combined)
    print("Done.")

def tv_xml(channels, programmes):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>','<tv generator-info-name="Zafer-EPG">']
    for ch in channels:
        parts.append(f'  <channel id="{html.escape(ch["id"])}">')
        parts.append(f'    <display-name>{html.escape(ch["name"])}</display-name>')
        parts.append( '  </channel>')
    for st, sp, title, ch_id in sorted(programmes, key=lambda x:x[0]):
        parts.append(f'  <programme start="{fmt_dt(st)}" stop="{fmt_dt(sp)}" channel="{html.escape(ch_id)}">')
        parts.append(f'    <title lang="tr">{html.escape(title)}</title>')
        parts.append( '  </programme>')
    parts.append('</tv>')
    return "\n".join(parts)

if __name__ == "__main__":
    main()
```

**Workflow** `.github/workflows/epg.yml`:

```yaml
name: Generate multi-channel EPG
on:
  schedule:
    - cron: "5 9 * * *"  # 12:05 TRT
  workflow_dispatch: {}
jobs:
  build:
    runs-on: ubuntu-latest
    permissions: { contents: write }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.x" }
      - run: pip install requests beautifulsoup4 lxml
      - run: python epg_multi.py
      - name: Publish to gh-pages
        uses: peaceiris/actions-gh-pages@v3
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          publish_dir: dist
```

You’ll get:

* One **combined** feed:
  `https://<user>.github.io/<repo>/all.latest.xml`
* Also **per-channel** files:
  `.../beinsports.tr.latest.xml`, `.../beinsportshaber.tr.latest.xml`, and date-stamped archives.

# Option B — Matrix job, one XML per channel then merge

If you prefer strict isolation, use a **matrix** to build each channel to an artifact, then a merge step to concatenate into one XML. It’s slightly more boilerplate, not necessary unless you want per-channel logs/artifacts.

```yaml
jobs:
  build-each:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        channel:
          - { url: "https://.../beinsports/persembe", id: "beinsports.tr", name: "beIN SPORTS" }
          - { url: "https://.../beinsportshaber/persembe", id: "beinsportshaber.tr", name: "beIN SPORTS HABER" }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.x" }
      - run: pip install requests beautifulsoup4 lxml
      - run: |
          python epg.py --url "${{ matrix.channel.url }}" \
            --channel-id "${{ matrix.channel.id }}" \
            --channel-name "${{ matrix.channel.name }}"
      - uses: actions/upload-artifact@v4
        with:
          name: epg-${{ matrix.channel.id }}
          path: dist/*.xml

  merge-and-publish:
    needs: build-each
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@v4
        with: { path: dist }
      # run a small merge script that reads all per-channel XMLs and writes all.latest.xml
      - run: python merge_xmltv.py dist
      - name: Publish
        uses: peaceiris/actions-gh-pages@v3
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          publish_dir: dist
```

# Which to pick

* **Option A** is simpler and fast. One script, one job, both **combined** and **per-channel** outputs. Recommended.
* **Option B** only if you want channel isolation or plan to scale to dozens of sources with different parsers.

# Practical tips

* Give each channel a stable `channel id` that matches your M3U `tvg-id`.
* If you ever need 2-day EPG, run the script for both day slugs and concatenate programmes.
* If any page is temporarily empty, keep last good file as `*.latest.xml` and log a warning rather than failing the job.
* Be polite with scraping frequency. One fetch per day per channel is trivial.

If you tell me the exact list of beIN channel paths you want, I’ll prefill the `CHANNELS` array for you.
