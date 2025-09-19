# This Python class downloads flyer images from websites, creates a PDF from the images, saves the PDF
# to a model, and uploads the PDF to SharePoint using the Microsoft Graph API.

import re, time, datetime, json, urllib.parse, hashlib
from io import BytesIO
from urllib.parse import urlparse

import requests
from PIL import Image

from django.core.management.base import BaseCommand
from django.core.files.base import ContentFile

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from handzettel.models import Handzettel

import os  # for reading environment variables
from dotenv import load_dotenv
import msal  # microsoft authentication library

# Load env variables for Azure credentials
load_dotenv()


# standard django container --> it s a Django management command, it downloads a flyer from a website and makes PDF
class Command(BaseCommand):
    help = "Download a flyer as a PDF (supports rabatt-kompass.de and lidl.de)."

    # define command line inputs
    def add_arguments(self, parser):
        parser.add_argument(
            "baseurl",  # direct viewr URL --> the page to start from
            type=str,
            help=(
                "rabatt-kompass overview OR Lidl URL. "
                "For Lidl include /page/1 or the base; for rabatt-kompass an overview like "
                ".../aldi-sued-prospekt is fine."
            ),
        )
        parser.add_argument(
            "pages",
            type=int,
            help="How many pages to download (e.g., 36)",  # how many pages to fetch
        )
        parser.add_argument(
            "--filename-mode",
            choices=["auto", "overview", "viewer"],
            default=os.getenv("FILENAME_MODE", "auto"),
            help=(
                "How to build the filename slug: "
                "'auto' = prefer viewer id if available (default), "
                "'overview' = use the last path segment of the input URL, "
                "'viewer' = force viewer id."
            ),
        )
        parser.add_argument(
            "--rk-pick",
            choices=["current", "upcoming", "latest"],
            default=os.getenv("RK_PICK", "current"),
            help="On overview pages, pick 'current' (default), 'upcoming' (Vorschau), or 'latest' uploaded id.",
        )

    # simple logger--> helps to see what the script is doing step by step
    def _log(self, level: str, *parts):
        print(f"[{level} {time.strftime('%H:%M:%S')}]", " ".join(str(p) for p in parts))

    # ---------- Chrome / Cloudflare helpers --> Runs Chrome without a window (headless)
    def _configure_chrome_options(self) -> Options:
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        )
        opts.add_argument(f"--user-agent={ua}")
        return opts

    # make selenium look less like a bot--> Hides the 'navigator.webdriver' flag that some sites use to detect bots
    def _apply_basic_stealth(self, driver):
        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                },
            )
        except Exception:
            pass

    # Detects when Cloudflare is showing a challenge page and waits a bit
    # Continues as soon as the real page is available
    def _wait_cloudflare(self, driver, max_wait=25):
        """Wait through Cloudflare 'Just a moment…' challenge."""
        start = time.time()
        while time.time() - start < max_wait:
            url = driver.current_url or ""
            title = (driver.title or "").lower()
            if (
                ("__cf_chl_" in url)
                or ("just a moment" in title)
                or ("cloudflare" in title)
            ):
                self._log("DBG", "CF challenge detected; waiting…")
                time.sleep(2)
                continue
            try:
                if driver.find_elements(
                    By.ID, "challenge-stage"
                ) or driver.find_elements(By.CSS_SELECTOR, ".cf-browser-verification"):
                    self._log("DBG", "CF challenge DOM present; waiting…")
                    time.sleep(2)
                    continue
            except Exception:
                pass
            break

    # --- Build the correct URL---
    def page_url(self, baseurl: str, n: int) -> str:
        if re.search(r"#page_\d+", baseurl):
            return re.sub(r"#page_\d+", f"#page_{n}", baseurl)
        return f"{baseurl}#page_{n}"

    # --- Slug helpers:Takes the last part of the path and keeps only letters, numbers, '-' and '_'.
    # Useful for building filenames
    def url_slug(self, url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        seg = (path.split("/")[-1] or "prospekt").lower()
        return re.sub(r"[^a-z0-9_-]+", "-", seg)

    # If the URL contains '/prospekt-<id>-0', returns that exact piece
    def viewer_slug(self, url: str) -> str | None:
        m = re.search(r"/prospekt-(\d+)-0", url)
        return f"prospekt-{m.group(1)}-0" if m else None

    # --- Cookie banner: Like closing a cookie pop-up so you can see the page content
    def accept_cookies_if_present(self, driver):
        for locator in [
            (By.ID, "onetrust-accept-btn-handler"),
            (By.XPATH, "//button[contains(., 'Alle akzeptieren')]"),
            (By.XPATH, "//button[contains(., 'Akzeptieren')]"),
            (By.XPATH, "//button[contains(., 'Zustimmen')]"),
        ]:
            try:
                WebDriverWait(driver, 4).until(
                    EC.element_to_be_clickable(locator)
                ).click()
                time.sleep(0.3)
                break
            except Exception:
                pass

    # Wait for the first page to actually load images, then trigger lazy-loading
    def _wait_first_page_ready(self, driver):
        if "rabatt-kompass.de" in (driver.current_url or ""):
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "img[src*='/public/gimg/']")
                    )
                )
            except Exception:
                pass
        else:
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "img"))
                )
            except Exception:
                pass
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.5)
            driver.execute_script("window.scrollTo(0, 0);")
        except Exception:
            pass

    # ---- Date extraction ----
    # _extract_date_range("Angebote 01.09.–07.09.") on 2025-09-02  → (2025-09-01, 2025-09-07)
    def _extract_date_range(self, blob_lower: str):
        today = datetime.date.today()
        year = today.year

        m = re.search(
            r"(\d{1,2})\.(\d{1,2})\.\s*[–-]\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?",
            blob_lower,
        )
        if m:
            d1, m1, d2, m2, y2 = m.groups()
            y = int(y2) if y2 else year
            try:
                start = datetime.date(year if not y2 else y, int(m1), int(d1))
                end = datetime.date(y, int(m2), int(d2))
                if end < start and not y2:
                    end = datetime.date(year + 1, int(m2), int(d2))
                return start, end
            except Exception:
                return None, None

        m = re.search(
            r"(gültig\s+bis|bis)\s+(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", blob_lower
        )
        if m:
            d2, m2, y2 = m.group(2), m.group(3), m.group(4)
            y = int(y2) if y2 else year
            try:
                end = datetime.date(y, int(m2), int(d2))
                return None, end
            except Exception:
                return None, None

        m = re.search(r"\bab\s+(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", blob_lower)
        if m:
            d1, m1, y1 = m.groups()
            y = int(y1) if y1 else year
            try:
                start = datetime.date(y, int(m1), int(d1))
                return start, None
            except Exception:
                return None, None

        return None, None

    #  Score a viewer link found on an overview page.Returns score
    def _score_rk_viewer_link(self, a) -> tuple[int, str, str]:
        # a.Reads text around the link (title + nearby card text).
        href = a.get_attribute("href") or ""
        title = (a.get_attribute("title") or "").strip()
        text = (a.text or "").strip()
        blob = " ".join([title, text]).lower()

        # b.Extracts dates → gives positive score if 'current', future score if 'upcoming',negative score if 'past'.
        start, end = self._extract_date_range(blob)
        today = datetime.date.today()

        if start and end:
            if start <= today <= end:
                status = "current"
                score = 3
            elif today < start:
                status = "upcoming"
                score = 2
            else:
                status = "past"
                score = 0
        elif start and not end:
            status = "current" if today >= start else "upcoming"
            score = 3 if status == "current" else 2
        else:
            status = "unknown"
            score = 1
        return score, href, status

    def _inspect_viewer_details(self, driver, href: str):

        try:
            # --- 1) Open the viewer page (force #page_1 so images/text are loaded) ---
            driver.get(href if "#page_1" in href else href + "#page_1")
            self._wait_cloudflare(driver)
            # Small wait until a flyer image element appears
            try:
                WebDriverWait(driver, 6).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "img[src*='/public/gimg/']")
                    )
                )
            except Exception:
                pass  # don't fail just because the image didn't show fast enough
            # --- 2) Read page text (headline + full DOM HTML) ---
            html = (driver.page_source or "").lower()
            try:
                # Many pages include useful dates in <h1> or <h2>
                head = driver.find_element(By.CSS_SELECTOR, "h1, h2").text.lower()
                html = head + "\n" + html
            except Exception:
                pass
            # --- 3) Extract dates from the text (your existing helper parses DE formats) ---
            start, end = self._extract_date_range(html)
            today = datetime.date.today()

            #  --- 4) Decide the flyer status with simple, explicit rules ---
            if start and end:
                if start <= today <= end:
                    status = "current"  # valid now
                elif today < start:
                    status = "upcoming"  # starts in the future
                else:
                    status = "past"  # already ended
            elif start and not end:
                # If only a start date is known, treat as current once we reach that date
                status = "current" if today >= start else "upcoming"
            else:
                # No dates found: if the page looks like a preview, mark as  unknown

                status = "unknown"
            # --- 5) Return the standardized structure used by the caller ---
            return {
                "href": href,
                "start": start,
                "end": end,
                "status": status,
                "bonus": 0,  # always zero
                "reason": f"status={status}; start={start}; end={end}",
            }
        except Exception as e:
            # On any unexpected error, return a safe, neutral structure
            return {
                "href": href,
                "start": None,
                "end": None,
                "is_vorschau": False,
                "status": "unknown",
                "bonus": 0,
                "reason": f"viewer:error {e}",
            }

    # ---- Visual picking :Find the single best image URL for the current flyer page
    def pick_image_url(self, driver) -> str | None:
        on_rk = "rabatt-kompass.de" in (driver.current_url or "")
        imgs = (
            driver.execute_script(
                """
            const Vw = window.innerWidth, Vh = window.innerHeight;
            function visArea(el){
              const r = el.getBoundingClientRect();
              const x = Math.max(0, Math.min(r.right, Vw) - Math.max(r.left, 0));
              const y = Math.max(0, Math.min(r.bottom, Vh) - Math.max(r.top, 0));
              return Math.floor(x*y);
            }
            const list = [];
            for (const img of Array.from(document.images)) {
                const src = img.currentSrc || img.src || '';
                const w = img.naturalWidth || 0, h = img.naturalHeight || 0;
                const vis = visArea(img);
                if (src) list.push({src, w, h, vis});
                const ss = img.getAttribute('srcset') || img.getAttribute('data-srcset');
                if (ss) ss.split(',').forEach(part => {
                    const u = part.trim().split(' ')[0];
                    if (u) list.push({src: u, w, h, vis});
                });
                const lazy = img.getAttribute('data-src');
                if (lazy) list.push({src: lazy, w, h, vis});
            }
            const els = Array.from(document.querySelectorAll('*'));
            for (const el of els) {
                const st = getComputedStyle(el);
                const bg = st.backgroundImage || '';
                const m = bg.match(/url\\(["']?(.*?)["']?\\)/);
                if (m && m[1]) {
                    const r = el.getBoundingClientRect();
                    const w = Math.max(0, r.width)|0, h = Math.max(0, r.height)|0;
                    const vis = Math.floor(Math.max(0, Math.min(r.right, Vw) - Math.max(r.left, 0)) *
                                           Math.max(0, Math.min(r.bottom, Vh) - Math.max(r.top, 0)));
                    list.push({src: m[1], w, h, vis});
                }
            }
            return list;
            """
            )
            or []
        )

        EXCLUDE = (
            "bat.bing.com",
            "cookielaw.org",
            "onetrust.com",
            "googletagmanager",
            "google-analytics",
            "doubleclick",
            "facebook",
            "hotjar",
            "adservice",
            "analytics",
        )

        cands = []
        for it in imgs:
            u = (it.get("src") or "").strip()
            w = int(it.get("w") or 0)
            h = int(it.get("h") or 0)
            vis = int(it.get("vis") or 0)
            if not u.startswith("http"):
                continue
            if any(b in u for b in EXCLUDE):
                continue
            if u.lower().endswith(".svg"):
                continue

            if on_rk:
                if "/public/gimg/" not in u or w < 600 or h < 800:
                    continue
                score = vis * 5 + w * h // 20 + 800_000
            else:
                score = vis * 4 + w * h // 25
                if re.search(r"\.(jpe?g|png|webp)(\?|$)", u, re.I):
                    score += 250_000
                if any(k in u.lower() for k in ("flyer", "page", "seiten", "prospekt")):
                    score += 80_000
                if any(k in u.lower() for k in ("thumb", "icon", "small")):
                    score -= 200_000
            cands.append((score, u, w, h, vis))

        if cands:
            cands.sort(key=lambda t: t[0], reverse=True)
            self._log("DBG", f"Collected candidate visuals: {len(cands)}")
            self._log("DBG", "TOP CANDIDATES:")
            for score, src, w, h, vis in cands[:8]:
                extra = "rk_gimg+8e5" if on_rk and "/public/gimg/" in src else ""
                print(f"    {score:8.0f}  [img]  {src}  :: {w}x{h}, vis={vis}, {extra}")
            chosen = cands[0][1]
            self._log("DBG", "Chosen image URL:", chosen)
            return chosen
        return None

    # If images are drawn on a canvas (no URL), we still capture what to see
    def _capture_best_visual(self, driver) -> bytes | None:
        try:
            best, area = None, 0
            for el in driver.find_elements(By.TAG_NAME, "img"):
                if not el.is_displayed():
                    continue
                sz = el.size or {}
                a = max(0, sz.get("width", 0)) * max(0, sz.get("height", 0))
                if a > area and a >= 400 * 500:
                    best, area = el, a
            if best:
                return best.screenshot_as_png
        except Exception:
            pass
        try:
            el = driver.execute_script(
                """
                const all = Array.from(document.querySelectorAll('*'));
                const cand = [];
                for (const el of all) {
                    const bg = getComputedStyle(el).backgroundImage || '';
                    if (bg.includes('url(')) {
                        const r = el.getBoundingClientRect();
                        const area = Math.max(0,r.width)*Math.max(0,r.height);
                        if (area > 10000) cand.push([area, el]);
                    }
                }
                cand.sort((a,b)=>b[0]-a[0]);
                return cand.length ? cand[0][1] : null;
                """
            )
            if el:
                return el.screenshot_as_png
        except Exception:
            pass
        try:
            el = driver.execute_script(
                """
                const cs = Array.from(document.querySelectorAll('canvas'));
                cs.sort((a,b)=>(b.width*b.height)-(a.width*a.height));
                return cs.length ? cs[0] : null;
                """
            )
            if el:
                return el.screenshot_as_png
        except Exception:
            pass
        try:
            return driver.get_screenshot_as_png()
        except Exception:
            return None

    #  Converts image to 9x8 grayscale and compares neighboring pixels.
    # Result is a 64-bit integer; similar-looking images produce similar hashes.
    def _img_dhash(self, img: Image.Image) -> int:
        small = img.convert("L").resize((9, 8), Image.LANCZOS)
        px = list(small.getdata())
        bits = 0
        for r in range(8):
            row = r * 9
            for c in range(8):
                bits = (bits << 1) | (1 if px[row + c] > px[row + c + 1] else 0)
        return bits

    # Hamming distance between two integer hashes: _ham(0b1010, 0b1000) → 1 (only one bit different).
    def _ham(self, a: int, b: int) -> int:
        x = a ^ b
        cnt = 0
        while x:
            x &= x - 1
            cnt += 1
        return cnt

    # Enter the rabatt-kompass <iframe> that contains the flyer image.
    def _enter_rk_viewer_frame(self, driver) -> bool:
        driver.switch_to.default_content()
        for f in driver.find_elements(By.CSS_SELECTOR, "iframe"):
            try:
                driver.switch_to.frame(f)
                ok = driver.execute_script(
                    "return !!document.querySelector(\"img[src*='/public/gimg/'], a[href*='#page_']\");"
                )
                if ok:
                    return True
            except Exception:
                pass
            finally:
                driver.switch_to.default_content()
        return False

    # Ensure Selenium is currently inside the right context to see the flyer image
    def _switch_to_viewer_context(self, driver) -> None:
        try:
            ok = driver.execute_script(
                "try { return !!document.querySelector(\"img[src*='/public/gimg/']\"); } catch(e){ return false; }"
            )
            if ok:
                return
        except Exception:
            pass
        self._enter_rk_viewer_frame(driver)

    # --- Download one image from a URL and return it as a PIL Image.--
    def fetch_image(self, url: str) -> Image.Image | None:
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200 and r.content:
                return Image.open(BytesIO(r.content)).convert("RGB")
        except Exception:
            pass
        return None

    # map brand to SharePoint folder name
    def brand_folder_name(self, market: str) -> str:
        mapping = {
            "lidl": "LIDL",
            "aldi_nord": "ALDI_NORD",
            "aldi_sued": "ALDI_SUED",
        }
        return mapping.get(
            (market or "").lower(), "MISC"
        )  # Returns 'MISC' if the retailer is unknown.

    # main flow
    def handle(self, *args, **opts):
        baseurl = opts["baseurl"]
        pages = opts["pages"]
        filename_mode = opts.get("filename_mode", "auto")
        rk_pick_mode = opts.get("rk_pick", "current")

        self._log(
            "INFO",
            "START job baseurl=",
            baseurl,
            "pages=",
            pages,
            "filename_mode=",
            filename_mode,
        )
        original_input = baseurl
        u_low_in = original_input.lower()

        retailer_hint = (
            "lidl"
            if "lidl" in u_low_in
            else (
                "aldi"
                if "aldi" in u_low_in
                else (
                    "kaufland"
                    if "kaufland" in u_low_in
                    else ("edeka" if "edeka" in u_low_in else "")
                )
            )
        )
        self._log("DBG", "retailer_hint:", retailer_hint)

        # --- rabatt-kompass OVERVIEW → resolve to specific viewer(s) ---
        resolved_slug = None
        if "rabatt-kompass.de" in baseurl and "/prospekt-" not in baseurl:
            target_viewers: list[str] = []
            try:
                tmp_driver = webdriver.Chrome(options=self._configure_chrome_options())
                self._apply_basic_stealth(tmp_driver)
                try:
                    tmp_driver.get(baseurl)
                    self._wait_cloudflare(tmp_driver)
                    try:
                        WebDriverWait(tmp_driver, 3).until(
                            EC.element_to_be_clickable(
                                (By.ID, "onetrust-accept-btn-handler")
                            )
                        ).click()
                        time.sleep(0.2)
                    except Exception:
                        pass

                    links = tmp_driver.find_elements(
                        By.CSS_SELECTOR, "a[href*='/prospekt-'][href$='-0']"
                    )
                    href_elems = []
                    seen = set()
                    for a in links:
                        h = a.get_attribute("href") or ""
                        if h and h not in seen:
                            href_elems.append(a)
                            seen.add(h)
                    self._log(
                        "DBG", f"RK overview → found viewer links: {len(href_elems)}"
                    )

                    overview_scored = []
                    for a in href_elems:
                        sc, href, why = self._score_rk_viewer_link(a)
                        overview_scored.append((sc, href, why))
                    overview_scored.sort(key=lambda t: t[0], reverse=True)

                    top = overview_scored[:6]
                    if not top:
                        # Regex fallback when DOM had no anchors
                        html = tmp_driver.page_source or ""
                        raw = re.findall(
                            r'https://rabatt-kompass\.de/[^"\'\s]*/prospekt-\d+-0', html
                        )
                        raw = list(dict.fromkeys(raw))
                        self._log("DBG", f"RK overview regex → {len(raw)} candidates")

                        verified = []
                        for href in raw[:12]:
                            det = self._inspect_viewer_details(tmp_driver, href)
                            verified.append(
                                {
                                    "href": href,
                                    "total": det["bonus"],
                                    "status": det["status"],
                                    "start": det["start"],
                                    "end": det["end"],
                                    "why": det["reason"],
                                }
                            )

                        current_items = [
                            v for v in verified if v["status"] == "current"
                        ]
                        if current_items:

                            def _pid(v):
                                m = re.search(r"/prospekt-(\d+)-0", v["href"])
                                return int(m.group(1)) if m else 0

                            current_items.sort(
                                key=lambda v: (v["total"], _pid(v)), reverse=True
                            )
                            target_viewers = [v["href"] for v in current_items]
                            self._log(
                                "INFO",
                                f"Found {len(target_viewers)} current viewers (regex path).",
                            )
                        else:
                            # single-choice fallback
                            def pick_by_status(desired):
                                c = [v for v in verified if v["status"] == desired]
                                return max(c, key=lambda v: v["total"]) if c else None

                            choice = None
                            if rk_pick_mode == "current":
                                choice = (
                                    pick_by_status("current")
                                    or pick_by_status("unknown")
                                    or pick_by_status("upcoming")
                                )
                            elif rk_pick_mode == "upcoming":
                                upcoming = [
                                    v for v in verified if v["status"] == "upcoming"
                                ]
                                if upcoming and any(v["start"] for v in upcoming):
                                    upcoming.sort(
                                        key=lambda v: (
                                            v["start"] or datetime.date.max,
                                            -v["total"],
                                        )
                                    )
                                    choice = upcoming[0]
                                else:
                                    choice = pick_by_status(
                                        "upcoming"
                                    ) or pick_by_status("current")
                            else:  # latest
                                verified.sort(
                                    key=lambda v: int(
                                        re.search(
                                            r"/prospekt-(\d+)-0", v["href"]
                                        ).group(1)
                                    ),
                                    reverse=True,
                                )
                                choice = verified[0] if verified else None

                            if choice:
                                baseurl = choice["href"]
                                resolved_slug = self.viewer_slug(baseurl)
                                target_viewers = [baseurl]
                                self._log(
                                    "INFO",
                                    "Redirecting to viewer (regex fallback):",
                                    baseurl,
                                )
                    else:
                        # Have DOM 'top' candidates → verify
                        dump = json.dumps(
                            [[sc, href] for sc, href, _ in top],
                            indent=2,
                            ensure_ascii=False,
                        )
                        self._log("DBG", "RK candidates (top 6):", f"\n{dump}")

                        verified = []
                        for sc, href, why in top:
                            detail = self._inspect_viewer_details(tmp_driver, href)
                            total = sc + detail["bonus"]
                            verified.append(
                                {
                                    "href": href,
                                    "overview_score": sc,
                                    "viewer_bonus": detail["bonus"],
                                    "total": total,
                                    "status": detail["status"],
                                    "why": why + " | " + detail["reason"],
                                    "start": detail["start"],
                                    "end": detail["end"],
                                }
                            )

                        current_items = [
                            v for v in verified if v["status"] == "current"
                        ]
                        if current_items:

                            def _pid(v):
                                m = re.search(r"/prospekt-(\d+)-0", v["href"])
                                return int(m.group(1)) if m else 0

                            current_items.sort(
                                key=lambda v: (v["total"], _pid(v)), reverse=True
                            )
                            target_viewers = [v["href"] for v in current_items]
                            self._log(
                                "INFO",
                                f"Found {len(target_viewers)} current viewers (normal path).",
                            )
                        else:
                            # single-choice fallback
                            def pick_by_status(desired: str):
                                cands = [v for v in verified if v["status"] == desired]
                                return (
                                    max(cands, key=lambda v: v["total"])
                                    if cands
                                    else None
                                )

                            choice = None
                            if rk_pick_mode == "current":
                                choice = (
                                    pick_by_status("current")
                                    or pick_by_status("unknown")
                                    or pick_by_status("upcoming")
                                )
                            elif rk_pick_mode == "upcoming":
                                upcoming = [
                                    v for v in verified if v["status"] == "upcoming"
                                ]
                                if upcoming and any(v["start"] for v in upcoming):
                                    upcoming.sort(
                                        key=lambda v: (
                                            v["start"] or datetime.date.max,
                                            -v["total"],
                                        )
                                    )
                                    choice = upcoming[0]
                                else:
                                    choice = pick_by_status(
                                        "upcoming"
                                    ) or pick_by_status("current")
                            else:  # latest
                                verified.sort(
                                    key=lambda v: (
                                        int(
                                            re.search(
                                                r"/prospekt-(\d+)-0", v["href"]
                                            ).group(1)
                                        ),
                                        v["total"],
                                    ),
                                    reverse=True,
                                )
                                choice = verified[0]
                            best_href = choice["href"] if choice else top[0][1]
                            self._log("INFO", "Redirecting to viewer:", best_href)
                            baseurl = best_href
                            resolved_slug = self.viewer_slug(best_href)
                            target_viewers = [best_href]
                finally:
                    tmp_driver.quit()
            except Exception as e:
                self._log("DBG", "overview resolver error:", e)

        # Default list if overview didn't populate
        if not locals().get("target_viewers"):
            target_viewers = [baseurl]
        multiple = len(target_viewers) > 1

        # === Process each selected viewer (download → PDF → upload) ===
        for baseurl in target_viewers:
            self._log("INFO", "Processing viewer:", baseurl)

            #  Start headless Chrome (main) for this viewer
            chrome_options = self._configure_chrome_options()
            chromedriver_path = os.getenv("CHROMEDRIVER", "").strip()
            try:
                if chromedriver_path:
                    driver = webdriver.Chrome(
                        service=Service(chromedriver_path), options=chrome_options
                    )
                else:
                    driver = webdriver.Chrome(options=chrome_options)
                    self._log("DBG", "Launched Chrome via Selenium Manager fallback.")
            except Exception:
                driver = webdriver.Chrome(options=chrome_options)
                self._log("DBG", "Launched Chrome via Selenium Manager fallback.")

            self._apply_basic_stealth(driver)

            images: list[Image.Image] = []
            slug_for_filename: str | None = None
            prev_img_url = None
            last_fp = None

            # loop through pages (site-aware)
            try:
                if "rabatt-kompass.de" in baseurl:
                    driver.get(
                        baseurl
                        if "/prospekt-" in baseurl
                        else self.page_url(baseurl, 1)
                    )
                    self._wait_cloudflare(driver)
                    self.accept_cookies_if_present(driver)
                    self._wait_first_page_ready(driver)

                    self._switch_to_viewer_context(driver)

                    viewer_base = (driver.current_url or baseurl).split("#", 1)[0]
                    last_fp = None

                    for i in range(1, pages + 1):
                        target = f"{viewer_base}#page_{i}"
                        self._log("INFO", f"[RK] go page {i}: {target}")
                        driver.get(target)
                        self._wait_cloudflare(driver)
                        self._switch_to_viewer_context(driver)

                        try:
                            WebDriverWait(driver, 12).until(
                                lambda d: d.execute_script(
                                    "return !!document.querySelector(\"img[src*='/public/gimg/']\");"
                                )
                            )
                        except Exception:
                            pass

                        img_url = self.pick_image_url(driver)

                        if not img_url:
                            self._log("INFO", "No direct image; trying screenshot…")
                            png = self._capture_best_visual(driver)
                            if not png:
                                if i > 1:
                                    self._log(
                                        "INFO", f"Reached end at page {i-1}. Stopping."
                                    )
                                    break
                                self._log("INFO", "No visual on page 1. Stopping.")
                                break
                            try:
                                img = Image.open(BytesIO(png)).convert("RGB")
                            except Exception:
                                self._log("INFO", "Screenshot decode failed.")
                                break
                        else:
                            hi_url = (
                                re.sub(
                                    r"-(\d{3,4})-(\d+)\.(jpe?g|png|webp)$",
                                    r"-2000-\2.\3",
                                    img_url,
                                    flags=re.I,
                                )
                                if "/public/gimg/" in img_url
                                else img_url
                            )

                            img = None
                            try:
                                r = requests.get(hi_url, timeout=20)
                                if r.status_code == 200 and r.content:
                                    img = Image.open(BytesIO(r.content)).convert("RGB")
                            except Exception as e:
                                self._log("DBG", "Direct GET failed:", e)

                            if img is None:
                                self._log(
                                    "INFO",
                                    "Direct download failed; using screenshot fallback…",
                                )
                                png = self._capture_best_visual(driver)
                                if not png:
                                    if i > 1:
                                        self._log(
                                            "INFO",
                                            f"Reached end at page {i-1}. Stopping.",
                                        )
                                        break
                                    self._log("INFO", "No visual on page 1. Stopping.")
                                    break
                                try:
                                    img = Image.open(BytesIO(png)).convert("RGB")
                                except Exception:
                                    self._log("INFO", "Screenshot decode failed.")
                                    break

                        fp = self._img_dhash(img)
                        if (
                            i > 1
                            and last_fp is not None
                            and self._ham(fp, last_fp) <= 2
                        ):
                            self._log("INFO", f"Visually same as page {i-1}. Stopping.")
                            break
                        last_fp = fp

                        images.append(img)
                        self._log("INFO", f"Page {i}: added. Total now {len(images)}")

                else:
                    for i in range(1, pages + 1):
                        url = self.page_url(baseurl, i)
                        self._log("INFO", f"Loading page {i}:", url)
                        driver.get(url)
                        self._wait_cloudflare(driver)
                        self._log(
                            "DBG", "current_url after GET:", driver.current_url or url
                        )

                        if i == 1:
                            self.accept_cookies_if_present(driver)
                            self._wait_first_page_ready(driver)

                            current = driver.current_url or baseurl
                            candidate_viewer = resolved_slug or self.viewer_slug(
                                current
                            )
                            overview_slug = self.url_slug(original_input)
                            if filename_mode == "viewer":
                                slug_for_filename = candidate_viewer or overview_slug
                            elif filename_mode == "overview":
                                slug_for_filename = overview_slug
                            else:
                                slug_for_filename = candidate_viewer or overview_slug
                            self._log(
                                "INFO",
                                "Using slug for filename (mode:",
                                filename_mode,
                                "):",
                                slug_for_filename,
                            )

                        try:
                            WebDriverWait(driver, 8).until(
                                EC.presence_of_element_located((By.TAG_NAME, "img"))
                            )
                        except Exception:
                            pass
                        time.sleep(1.0)

                        img_url = self.pick_image_url(driver)

                        if not img_url and i == 1:
                            self._log("INFO", "No image on page 1 yet; retrying…")
                            time.sleep(1.0)
                            driver.get(url)
                            self._wait_cloudflare(driver)
                            self._wait_first_page_ready(driver)
                            img_url = self.pick_image_url(driver)

                        if not img_url:
                            self._log("INFO", "No image found on this page.")
                            continue

                        hi_url = (
                            re.sub(r"-\d{3,4}-", "-2000-", img_url, count=1)
                            if "/public/gimg/" in img_url
                            else img_url
                        )
                        self._log("DBG", "Chosen (possibly hi-res) image:", hi_url)

                        try:
                            r = requests.get(hi_url, timeout=20)
                            self._log(
                                "DBG",
                                "GET",
                                hi_url,
                                "->",
                                r.status_code,
                                "lenHdr=",
                                len(r.content) if r.ok else 0,
                            )
                            if r.status_code == 200 and r.content:
                                images.append(
                                    Image.open(BytesIO(r.content)).convert("RGB")
                                )
                                md5 = hashlib.md5(r.content[:20000]).hexdigest()[:12]
                                self._log(
                                    "DBG",
                                    "Downloaded bytes:",
                                    len(r.content),
                                    "md5=",
                                    md5,
                                )
                                self._log(
                                    "INFO",
                                    f"Page {i}: added image. Total now {len(images)}",
                                )
                                continue
                        except Exception as e:
                            self._log("DBG", "Direct GET failed:", e)

                        self._log(
                            "INFO", "Direct download failed; trying visual fallback…"
                        )
                        png_bytes = self._capture_best_visual(driver)
                        if png_bytes:
                            try:
                                images.append(
                                    Image.open(BytesIO(png_bytes)).convert("RGB")
                                )
                                self._log(
                                    "INFO",
                                    f"Page {i}: added fallback screenshot. Total now {len(images)}",
                                )
                            except Exception:
                                self._log("INFO", "Could not decode fallback visual.")
                        else:
                            self._log(
                                "INFO", "No visual content captured on this page."
                            )
            finally:
                try:
                    driver.quit()
                    self._log("DBG", "Chrome closed.")
                except Exception:
                    pass

            if not images:
                self._log("INFO", "No page images found!")
                continue

            # Build PDF
            pdf = BytesIO()
            images[0].save(pdf, format="PDF", save_all=True, append_images=images[1:])
            pdf.seek(0)
            self._log("INFO", f"PDF built with {len(images)} pages.")

            # Save into model Handzettel
            u = original_input.lower() if original_input else baseurl.lower()
            market = (
                "lidl"
                if "lidl" in u
                else (
                    "aldi_nord"
                    if ("aldi-nord" in u or "aldi_nord" in u)
                    else (
                        "aldi_sued"
                        if ("aldi-sued" in u or "aldi_sued" in u)
                        else (
                            "edeka"
                            if "edeka" in u
                            else ("kaufland" if "kaufland" in u else "unknown")
                        )
                    )
                )
            )
            today = datetime.date.today()
            iso_week = today.isocalendar()[1]
            slug = slug_for_filename or self.url_slug(baseurl)
            brand_folder = self.brand_folder_name(market)
            filename_prefix = {
                "lidl": "LidlProspekt",
                "aldi_nord": "AldiNordProspekt",
                "aldi_sued": "AldiSuedProspekt",
                "kaufland": "KauflandProspekt",
                "edeka": "EdekaProspekt",
            }.get(market, "Prospekt")

            viewer_id = (
                (self.viewer_slug(baseurl) or self.url_slug(baseurl))
                .replace("prospekt-", "")
                .replace("-0", "")
            )
            filename = f"{filename_prefix}_KW{iso_week:02d}.pdf"
            if multiple:
                filename = f"{filename_prefix}_KW{iso_week:02d}_{viewer_id}.pdf"
            title = f"{market.replace('_',' ').upper()} – {slug} – {today:%Y-%m-%d}"
            self._log(
                "DBG", f"Model filename: {filename} | title: {title} | market: {market}"
            )
            handzettel = Handzettel(supermarkt=market, titel=title)
            handzettel.datei.save(filename, ContentFile(pdf.read()))
            handzettel.save()
            self._log("INFO", f"PDF saved to model as '{filename}'.")

            # Azure SharePoint upload
            self._log("INFO", "Uploading PDF to SharePoint…")

            client_id = os.getenv("AZURE_CLIENT_ID")
            client_secret = os.getenv("AZURE_CLIENT_SECRET")
            tenant_id = os.getenv("AZURE_TENANT_ID")
            sharepoint_site = os.getenv("SHAREPOINT_SITE")
            sharepoint_folder = os.getenv("SHAREPOINT_FOLDER")
            sharepoint_drive_id = os.getenv("SHAREPOINT_DRIVE_ID")

            self._log(
                "DBG",
                "SITE=",
                sharepoint_site,
                " | FOLDER=",
                sharepoint_folder,
                " | DRIVE_ID=",
                sharepoint_drive_id or "(auto)",
            )

            def die(msg, *extra):
                print("ERROR", msg, *extra)
                return

            if not all(
                [
                    client_id,
                    client_secret,
                    tenant_id,
                    sharepoint_site,
                    sharepoint_folder,
                ]
            ):
                die("ERROR: SharePoint/Azure configuration missing!")
                continue

            try:
                app = msal.ConfidentialClientApplication(
                    client_id,
                    authority=f"https://login.microsoftonline.com/{tenant_id}",
                    client_credential=client_secret,
                )
                token_result = app.acquire_token_for_client(
                    scopes=["https://graph.microsoft.com/.default"]
                )
                if "access_token" not in token_result:
                    die(
                        "ERROR: Could not get access token:",
                        token_result.get("error_description"),
                        token_result,
                    )
                    continue
                token = token_result["access_token"]
                headers = {"Authorization": f"Bearer {token}"}
                self._log("INFO", "OK: Got access token")
            except Exception as e:
                die("MSAL token error", e)
                continue

            try:
                site_info = requests.get(
                    f"https://graph.microsoft.com/v1.0/sites/{sharepoint_site}",
                    headers=headers,
                )
                self._log("DBG", "GET site status:", site_info.status_code)
                if site_info.status_code != 200:
                    print("GET site body:", site_info.text[:800])
                    die(
                        "cannot read site.Check sharepoint site and permissions",
                        site_info.text,
                    )
                    continue
                site_json = site_info.json()
                site_id = site_json.get("id")
                self._log("INFO", "OK site id =", site_id)
                if not site_id:
                    die("site ID missing in response", site_json)
                    continue
            except Exception as e:
                die("site lookup failed", e)
                continue

            def _norm(name: str) -> str:
                return (name or "").lower().replace(" ", "").replace("_", "")

            try:
                if sharepoint_drive_id:
                    drive_id = sharepoint_drive_id.strip()
                    self._log("INFO", "OK: Using drive by ID:", drive_id)
                else:
                    drives_resp = requests.get(
                        f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives",
                        headers=headers,
                    )
                    self._log("DBG", "GET drives status:", drives_resp.status_code)
                    if drives_resp.status_code != 200:
                        die(
                            "cannot list drives. Permissions missing!", drives_resp.text
                        )
                        continue
                    drives = drives_resp.json().get("value", [])
                    self._log(
                        "DBG",
                        "Available drives:",
                        [(d.get("name"), d.get("id")) for d in drives],
                    )
                    if not drives:
                        die("No drives found on the site.")
                        continue

                    library_name = sharepoint_folder.split("/")[0].strip()
                    drive = (
                        next((d for d in drives if d.get("name") == library_name), None)
                        or next(
                            (
                                d
                                for d in drives
                                if (d.get("name") or "").lower() == library_name.lower()
                            ),
                            None,
                        )
                        or next(
                            (
                                d
                                for d in drives
                                if _norm(d.get("name")) == _norm(library_name)
                            ),
                            None,
                        )
                    )
                    if not drive:
                        print(
                            f"WARN: Library '{library_name}' not found. Using first drive as fallback."
                        )
                        drive = drives[0]

                    drive_id = drive["id"]
                    self._log("INFO", "OK: Using drive:", drive.get("name"), drive_id)
            except Exception as e:
                die("Drive lookup failed", e)
                continue

            def ensure_folder_path(drive_id: str, folder_path: str):
                parts = [p for p in folder_path.split("/") if p.strip()]
                parent_path = ""
                for part in parts:
                    parent_path = f"{parent_path}/{part}" if parent_path else part
                    r = requests.get(
                        f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{parent_path}",
                        headers=headers,
                    )
                    self._log("DBG", "Check folder", parent_path, "->", r.status_code)
                    if r.status_code == 404:
                        parent_parent = (
                            f"/{parent_path.rsplit('/',1)[0]}"
                            if "/" in parent_path
                            else ""
                        )
                        create_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:{parent_parent}:/children"
                        cr = requests.post(
                            create_url,
                            headers={**headers, "Content-Type": "application/json"},
                            json={
                                "name": part,
                                "folder": {},
                                "@microsoft.graph.conflictBehavior": "rename",
                            },
                        )
                        if cr.status_code not in (200, 201):
                            raise RuntimeError(
                                f"Failed to create '{part}': {cr.status_code} {cr.text}"
                            )
                    elif r.status_code != 200:
                        raise RuntimeError(
                            f"Folder check failed for '{parent_path}': {r.status_code} {r.text}"
                        )

            subpath = "/".join(
                sharepoint_folder.split("/")[1:]
            )  # strip the library ("Documents")
            year_folder = f"{today.year}"
            brand_folder = self.brand_folder_name(market)
            nested_subpath = f"{subpath}/{brand_folder}/{year_folder}"

            self._log(
                "INFO", "Ensuring nested path (drive root-relative):", nested_subpath
            )

            try:
                if nested_subpath:
                    ensure_folder_path(drive_id, nested_subpath)
                    self._log("INFO", "OK: Folder path ensured:", nested_subpath)
            except Exception as e:
                die("Creating/checking folder path failed", e)
                continue

            try:
                upload_path = "/".join([nested_subpath, filename])

                self._log("INFO", "Uploading to path:", upload_path)
                upload_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{upload_path}:/content"
                with open(handzettel.datei.path, "rb") as f:
                    resp = requests.put(upload_url, headers=headers, data=f)
                self._log("INFO", "PUT upload status:", resp.status_code)

                data = None
                try:
                    data = resp.json()
                except Exception:
                    pass

                if resp.status_code in (200, 201):
                    weburl = (data or {}).get("webUrl")
                    if not weburl:
                        meta = requests.get(
                            f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{upload_path}",
                            headers=headers,
                        )
                        if meta.status_code == 200:
                            weburl = meta.json().get("webUrl")
                    self._log("INFO", "SharePoint webUrl:", weburl or "(not returned)")
                    self._log("INFO", "PDF successfully uploaded to SharePoint!")
                else:
                    print("Response body (truncated):", (resp.text or "")[:500])
                    die("Error uploading to SharePoint:", resp.status_code)
                    continue
            except Exception as e:
                die("Upload exception", e)
                continue

            def search_in_drive(name: str):
                try:
                    q = urllib.parse.quote(name)
                    r = requests.get(
                        f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root/search(q='{q}')",
                        headers=headers,
                    )
                    if r.status_code == 200:
                        hits = r.json().get("value", [])
                        self._log("INFO", f"Search hits for '{name}':", len(hits))
                        for it in hits[:5]:
                            print("-", it.get("name"), "| webUrl:", it.get("webUrl"))
                    else:
                        print("Search failed:", r.status_code, r.text[:400])
                except Exception as e:
                    print("Search exception:", e)

            search_in_drive(filename)

        self._log("INFO", "DONE.")
