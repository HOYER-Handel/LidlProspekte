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
import selenium  # for version-based quirks if ever needed
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
            # text
            "baseurl",  # direct viewr URL --> the page to start from
            type=str,
            help=(
                "rabatt-kompass overview OR Lidl URL. "
                "For Lidl include /page/1 or the base; for rabatt-kompass an overview like "
                ".../aldi-sued-prospekt is fine."
            ),
        )
        parser.add_argument(
            # number
            "pages",
            type=int,
            help="How many pages to download (e.g., 36)",  # how many pages to fetch
        )
        # auto(default),overview(use the last part of the input URL),viewer(use the viewer id lik prospekt-123456-0)
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
        # which flyer to choose on a rabatt kompass overview page
        parser.add_argument(
            "--rk-pick",
            choices=["current", "upcoming", "latest"],
            default=os.getenv("RK_PICK", "current"),
            help="On overview pages, pick 'current' (default), 'upcoming' (Vorschau), or 'latest' uploaded id.",
        )

    # simple logger: Simple console logging like [INFO 12:34:56]
    def _log(self, level: str, *parts):
        print(f"[{level} {time.strftime('%H:%M:%S')}]", " ".join(str(p) for p in parts))

    # ---------- Chrome / Cloudflare helpers ----------
    # Builds a set of settings for Chrome so Selenium can run without a window and look like a normal desktop browser
    def _configure_chrome_options(self) -> Options:
        opts = Options()
        opts.add_argument("--headless=new")  # no visible window
        opts.add_argument(
            "--window-size=1920,1080"
        )  # many sites load images based on size
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

    # Small trick so sites don t easily detect Selenium -->It makes Selenium look less like a bot by hiding the flag navigator.webdriver
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

    # waits if cloudflare shows "just a moment" challenges

    # it pauses script while Cloudflare is showing the "just a moment"bot check page, then contines once the real page is ready
    # This function waits just long enough so the real content can load
    def _wait_cloudflare(self, driver, max_wait=25):
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

    # --- Build the correct URL for each page depending on the site ---
    def page_url(self, baseurl: str, n: int) -> str:
        # lidl paths use /page/<n>
        if "lidl.de" in baseurl:
            if re.search(r"/page/\d+", baseurl):
                return re.sub(r"/page/\d+", f"/page/{n}", baseurl)
            parts = baseurl.split("?", 1)
            path = parts[0].rstrip("/")
            query = f"?{parts[1]}" if len(parts) == 2 else ""
            return f"{path}/page/{n}{query}"
        # rabatt Kompass uses #pages_<n>
        if re.search(r"#page_\d+", baseurl):
            return re.sub(r"#page_\d+", f"#page_{n}", baseurl)
        return f"{baseurl}#page_{n}"

    # --- Slug helpers Creates a safe slug from the last path segment of a URL --> used in filenames/Runs of bad characters collapse to a single -
    def url_slug(self, url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        seg = (path.split("/")[-1] or "prospekt").lower()
        return re.sub(r"[^a-z0-9_-]+", "-", seg)

    # If the URL looks like /prospekt-<id>-0, returns that stable slug (e.g. prospekt-2898320-0).
    def viewer_slug(self, url: str) -> str | None:
        m = re.search(r"/prospekt-(\d+)-0", url)
        return f"prospekt-{m.group(1)}-0" if m else None

    # --- Cookie banner ---Clicks common cookie “accept” buttons if they exist. Ignores errors.
    def accept_cookies_if_present(self, driver):
        for locator in [
            (By.ID, "onetrust-accept-btn-handler"),
            (By.XPATH, "//button[contains(., 'Alle akzeptieren')]"),
            (By.XPATH, "//button[contains(., 'Akzeptieren')]"),
            (By.XPATH, "//button[contains(., 'Zustimmen')]"),
        ]:
            # it loops over these options 1 by 1, wait up to 4 seconds unntil the button is clickable
            try:
                WebDriverWait(driver, 4).until(
                    EC.element_to_be_clickable(locator)
                    # if it becomes clickable
                ).click()  # click it
                time.sleep(0.3)  # wait 0.3s
                break
            except Exception:
                pass  # if the button isn t there,it ignore the error and tries the next option

    # Waits until images are present and then does a quick scroll down & up to trigger lazy-loading
    # Many pages don’t load images until scroll.If it starts scraping too early, you’ll miss the images.
    # This function makes sure the images are present (or at least likely to be), before the next steps run
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

    # Looks inside a text(already lowcrase) and tries to find German sytle dates,it retirns a start date and end date as dateime.date objects
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
            except Exception:  # if it can t find any dates it returns (None,None)
                return None, None
        # Returns (start_date, end_date)
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

    # It reads some text and adds or subtracts points when it sees ceratin words.It helps pick the best flyer
    def _retailer_phrase_bonus(
        self, blob: str, retailer_hint: str, mode: str
    ) -> tuple[int, list[str]]:
        b, why = 0, []
        is_aktuell = any(
            w in blob for w in ("aktuell", "aktueller", "diese woche", "gültig bis")
        )
        is_vorschau = any(
            w in blob
            for w in (
                "vorschau",
                "nächste woche",
                "kommende woche",
                "ab nächster woche",
            )
        )
        is_kw = bool(re.search(r"\bkw\s*\d{1,2}\b", blob))
        if mode == "current":  # this week
            if is_aktuell:
                b += 350
                why.append("phrase:aktuell+350")
            if is_kw:
                b += 60
                why.append("phrase:kw+60")
            if is_vorschau:
                b -= 450
                why.append("phrase:vorschau-450")
        elif mode == "upcoming":  # next week
            if is_vorschau:
                b += 350
                why.append("phrase:vorschau+350")
            if is_aktuell:
                b -= 150
                why.append("phrase:aktuell-150")
        else:
            if is_aktuell:  # neutral
                b += 40
                why.append("phrase:aktuell+40")
            if is_vorschau:
                b += 40
                why.append("phrase:vorschau+40")
            if is_kw:
                b += 20
                why.append("phrase:kw+20")

        if retailer_hint == "lidl" and "lidl" in blob:
            b += 25
            why.append("retailer:lidl+25")
        elif retailer_hint == "aldi" and "aldi" in blob:
            b += 25
            why.append("retailer:aldi+25")

        return b, why

    # scoring helper
    def _id_bias(self, href: str, mode: str) -> tuple[int, str]:
        # find ID in URL like /prospekt-123456-0
        m = re.search(r"/prospekt-(\d+)-0", href)
        if not m:
            # if no id --> return (0, "")
            return 0, ""
        # If mode == "latest" (prefer newest uploads)
        pid = int(m.group(1))
        if mode == "latest":
            add = min(2000, (pid // 10) % 3000)
            return add, f"id(latest):{pid}(+{add})"
        else:
            add = (pid % 13) - 6
            return add, f"id(light):{pid}(+{add})"

    # Inputs: a: the link element form Selenium,retailer_hint:which store lidl or aldi,mode:"current","upcoming"or "latest"
    # this function adds points for good signs (right store, prospekt words, “this week”/“next week”, valid dates, newer ID) and subtracts for past dates. The highest score wins
    def _score_rk_viewer_link(
        self, a, retailer_hint: str, mode: str
    ) -> tuple[int, str, str]:
        href = a.get_attribute("href") or ""
        title = (a.get_attribute("title") or "").strip()
        text = (a.text or "").strip()
        try:
            card = a.find_element(
                By.XPATH,
                "ancestor-or-self::*[self::article or self::li or self::div][1]",
            ).text.strip()
        except Exception:
            card = ""
        blob = " ".join([title, text, card]).lower()

        s, reasons = 0, []
        if retailer_hint and retailer_hint in blob:
            s += 10
            reasons.append("retailer+10")
        if "prospekt" in blob:
            s += 8
            reasons.append("prospekt+8")
        if any(k in blob for k in ("woche", "wochenprospekt", "angebote", "aktuell")):
            s += 6
            reasons.append("woche/aktuell+6")

        start, end = self._extract_date_range(blob)
        today = datetime.date.today()
        date_bonus = 0
        if start and end:
            if start <= today <= end:
                date_bonus += 300
                reasons.append("overview:heute+300")
            elif today < start:
                days = (start - today).days
                b = max(0, 250 - 10 * days)
                date_bonus += b
                reasons.append(f"overview:future+{b}")
            else:
                days = (today - end).days
                m = 40 + 3 * min(days, 30)
                date_bonus -= m
                reasons.append(f"overview:past-{m}")
        elif start and not end:
            if today >= start:
                date_bonus += 180
                reasons.append("overview:ab+180")
            else:
                days = (start - today).days
                b = max(0, 160 - 8 * days)
                date_bonus += b
                reasons.append(f"overview:ab_future+{b}")

        s += date_bonus

        phrase_b, why = self._retailer_phrase_bonus(blob, retailer_hint, mode)
        s += phrase_b
        reasons.extend(why)

        id_b, why_id = self._id_bias(href, mode)
        s += id_b
        reasons.append(why_id)

        return int(s), href, ", ".join(reasons)

    # open 1 flyer page and decide status:current(this week),upcoming(next),or unknown
    # exple: Text shows: “01.09.–07.09.2025” → current, +2000,Text shows: “ab 10.09.2025” → upcoming, about +500..+1200 (closer date = higher)
    #   Text shows: “Vorschau – nächste Woche” and no dates → upcoming, +900,Text shows: “01.08.–07.08.2025” → past, −700
    def _inspect_viewer_details(self, driver, href: str):
        # open the page href-->make sure it shows page1
        status, bonus, reason = "unknown", 0, []
        # wait a moment(firstimage)
        try:
            driver.get(href if "#page_1" in href else href + "#page_1")
            self._wait_cloudflare(driver)
            try:
                WebDriverWait(driver, 6).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "img[src*='/public/gimg/']")
                    )
                )
            except Exception:
                pass
            # read the page html +<h1>/<h2>text, all in lowcrase
            html = (driver.page_source or "").lower()
            # include visible text around leaflet title if present
            try:
                head = driver.find_element(By.CSS_SELECTOR, "h1, h2").text.lower()
                html = head + "\n" + html
            except Exception:
                pass

            start, end = self._extract_date_range(html)
            today = datetime.date.today()
            is_vorschau = any(
                w in html for w in ("vorschau", "nächste woche", "kommende woche")
            )

            if start and end:
                if start <= today <= end:
                    status = "current"
                    b = 2000
                    bonus += b
                    reason.append(f"viewer:current+{b}")
                elif today < start:
                    status = "upcoming"
                    days = (start - today).days
                    b = max(600, 1400 - 20 * days)
                    bonus += b
                    reason.append(f"viewer:upcoming+{b}")
                else:
                    status = "past"
                    bonus += -700
                    reason.append("viewer:past-700")
            elif start and not end:
                if today >= start:
                    status = "current"
                    bonus += 1600
                    reason.append("viewer:ab_current+1600")
                else:
                    status = "upcoming"
                    days = (start - today).days
                    b = max(500, 1200 - 18 * days)
                    bonus += b
                    reason.append(f"viewer:ab_upcoming+{b}")
            else:
                if is_vorschau:
                    status = "upcoming"
                    bonus += 900
                    reason.append("viewer:phrase_vorschau+900")

            return {
                "href": href,
                "start": start,
                "end": end,
                "is_vorschau": is_vorschau,
                "status": status,
                "bonus": int(bonus),
                "reason": ", ".join(reason),
            }
        except Exception as e:
            return {
                "href": href,
                "start": None,
                "end": None,
                "is_vorschau": False,
                "status": "unknown",
                "bonus": 0,
                "reason": f"viewer:error {e}",
            }

    # --- Find one good image URL on the current page,if it findes one it returns the URL.If not it returns None
    # The goal of pick_image_url is to find the single best URL of the flyer page image on the current webpage so the script can download it and later build the PDF
    def pick_image_url(self, driver) -> str | None:
        # detect site--> checks if we re on RB.de
        on_rk = "rabatt-kompass.de" in (driver.current_url or "")
        # collect all possible images via a small javaScript.It returns a list of object like:{ src: "...", w: <naturalWidth>, h: <naturalHeight>, vis: <visible_pixels> }
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
        # Skip known tracking / analytics / cookie images — they’re not our flyer pages

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
            # Normalize values from the JS result
            u = (it.get("src") or "").strip()
            w = int(it.get("w") or 0)
            h = int(it.get("h") or 0)
            vis = int(it.get("vis") or 0)
            if not u.startswith("http"):  # Filter 1: must be an absolute http(s) URL
                continue
            if any(b in u for b in EXCLUDE):  # Filter 2: drop trackers/analytics
                continue
            if u.lower().endswith(
                ".svg"
            ):  # Filter 3: skip SVGs (usually icons/logos, not flyer pages)
                continue

            if on_rk:
                # On rabatt-kompass we *require* the known flyer path and a minimum size
                if "/public/gimg/" not in u or w < 600 or h < 800:
                    continue
                # Score: visibility and size matter, plus a big base bonus for /public/gimg/
                score = vis * 5 + w * h // 20 + 800_000
            else:
                # On other sites (e.g., lidl.de), be more heuristic
                score = vis * 4 + w * h // 25
                if re.search(r"\.(jpe?g|png|webp)(\?|$)", u, re.I):
                    score += 250_000
                    # Bonus if the URL name hints it's a flyer page
                if any(k in u.lower() for k in ("flyer", "page", "seiten", "prospekt")):
                    score += 80_000
                    # Penalty if it looks like a thumbnail or icon
                if any(k in u.lower() for k in ("thumb", "icon", "small")):
                    score -= 200_000
            # Keep (score, url, dimensions, visibility) for comparison
            cands.append((score, u, w, h, vis))
        # Pick the single best candidate
        if cands:
            cands.sort(key=lambda t: t[0], reverse=True)
            self._log("DBG", f"Collected candidate visuals: {len(cands)}")
            self._log("DBG", "TOP CANDIDATES:")
            for score, src, w, h, vis in cands[:8]:
                extra = "rk_gimg+8e5" if on_rk and "/public/gimg/" in src else ""
                # avoid float formatting error on some Python versions
                print(f"    {score:8.0f}  [img]  {src}  :: {w}x{h}, vis={vis}, {extra}")
            chosen = cands[0][1]
            self._log("DBG", "Chosen image URL:", chosen)
            return chosen
        return None

    # This function grabs the best possible screenshot from the page (as PNG bytes) so we can still build the PDF.
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

    # --- Page numbering helpers & total detection ---
    def _current_page_from_url(self, url: str) -> int | None:
        m = re.search(r"(?:#page_|/page/)(\d+)", url or "")
        return int(m.group(1)) if m else None

    def _detect_total_pages(self, driver) -> int | None:
        try:
            html = driver.page_source or ""
        except Exception:
            html = ""

        m = re.search(r"(?:Seite|Page)\s*\d+\s*/\s*(\d{1,3})", html, re.I)
        if m:
            return int(m.group(1))

        for key in ("pageCount", "totalPages", "pagesTotal", "numPages"):
            m = re.search(rf'"{key}"\s*:\s*(\d{{1,3}})', html)
            if m:
                return int(m.group(1))

        try:
            thumbs = driver.find_elements(By.CSS_SELECTOR, "a[href*='#page_']")
            nums = []
            for a in thumbs:
                href = a.get_attribute("href") or ""
                mm = re.search(r"#page_(\d+)", href)
                if mm:
                    nums.append(int(mm.group(1)))
            if nums:
                return max(nums)
        except Exception:
            pass

        return None

    # --- Perceptual hash (dHash) for visual-duplicate stop ---
    def _img_dhash(self, img: Image.Image) -> int:
        """8x8 perceptual dHash (64-bit)."""
        # Pillow keeps Image.LANCZOS for backward compat; if using PIL>=10, Resampling.LANCZOS also works.
        small = img.convert("L").resize((9, 8), Image.LANCZOS)
        px = list(small.getdata())
        bits = 0
        for r in range(8):
            row = r * 9
            for c in range(8):
                bits = (bits << 1) | (1 if px[row + c] > px[row + c + 1] else 0)
        return bits

    def _ham(self, a: int, b: int) -> int:
        """Hamming distance between two 64-bit integers."""
        x = a ^ b
        cnt = 0
        while x:
            x &= x - 1
            cnt += 1
        return cnt

    # --- Direct image fetch (kept for completeness) ---
    def fetch_image(self, url: str) -> Image.Image | None:
        try:
            # simple request get
            r = requests.get(url, timeout=20)
            if r.status_code == 200 and r.content:
                # returns a PIL image or None
                return Image.open(BytesIO(r.content)).convert("RGB")
        except Exception:
            pass
        return None

    # Turn a market name into the SharePoint folder name we use
    def brand_folder_name(self, market: str) -> str:
        # a small dictionary
        mapping = {
            "lidl": "LIDL",
            "aldi_nord": "ALDI_NORD",
            "aldi_sued": "ALDI_SUED",
        }
        return mapping.get((market or "").lower(), "MISC")

    # main flow
    def handle(self, *args, **opts):
        # decide modes:1.starting URL 2.how many pages to fetch 3.how to build filename 4.which flyer to prefer on overview
        baseurl = opts["baseurl"]
        pages = opts["pages"]
        filename_mode = opts.get("filename_mode", "auto")
        rk_pick_mode = opts.get("rk_pick", "current")
        # Log a short job header so we see what was passed in
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
        # Try to guess the retailer from the input URL
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

        #  --- 2) Rabatt-Kompass OVERVIEW → resolve to a specific viewer -----------
        # If we were given a RK overview page (not a /prospekt-<id>-0 viewer),
        # open a temporary Chrome, collect candidate viewer links, score them,
        # and pick the best one according to rk_pick_mode ("current" / "upcoming" / "latest").
        resolved_slug = None
        if "rabatt-kompass.de" in baseurl and "/prospekt-" not in baseurl:
            try:
                tmp_driver = webdriver.Chrome(options=self._configure_chrome_options())
                self._apply_basic_stealth(tmp_driver)
                try:
                    tmp_driver.get(baseurl)
                    self._wait_cloudflare(tmp_driver)
                    # dismiss cookie banner if present
                    try:
                        WebDriverWait(tmp_driver, 3).until(
                            EC.element_to_be_clickable(
                                (By.ID, "onetrust-accept-btn-handler")
                            )
                        ).click()
                        time.sleep(0.2)
                    except Exception:
                        pass

                    # a) collect unique links that look like prospekt viewers
                    links = tmp_driver.find_elements(
                        By.CSS_SELECTOR, "a[href*='/prospekt-'][href$='-0']"
                    )
                    hrefs = []
                    seen = set()
                    for a in links:
                        h = a.get_attribute("href") or ""
                        if h and h not in seen:
                            hrefs.append(a)
                            seen.add(h)
                    self._log("DBG", f"RK overview → found viewer links: {len(hrefs)}")

                    # b) overview score
                    overview_scored = []
                    for a in hrefs:
                        sc, href, why = self._score_rk_viewer_link(
                            a, retailer_hint, rk_pick_mode
                        )
                        overview_scored.append((sc, href, why))
                    overview_scored.sort(key=lambda t: t[0], reverse=True)

                    top = overview_scored[:6]
                    if not top:
                        # Regex fallback when DOM is empty
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

                        # Helper: pick the best item by a given status
                        def pick_by_status(desired):
                            c = [v for v in verified if v["status"] == desired]
                            return max(c, key=lambda v: v["total"]) if c else None

                        choice = None
                        # Choose final target depending on rk_pick_mode
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
                                # prefer the earliest start date when available
                                upcoming.sort(
                                    key=lambda v: (
                                        v["start"] or datetime.date.max,
                                        -v["total"],
                                    )
                                )
                                choice = upcoming[0]
                            else:
                                choice = pick_by_status("upcoming") or pick_by_status(
                                    "current"
                                )
                        else:
                            verified.sort(
                                key=lambda v: int(
                                    re.search(r"/prospekt-(\d+)-0", v["href"]).group(1)
                                ),
                                reverse=True,
                            )
                            choice = verified[0] if verified else None

                        if choice:
                            baseurl = choice["href"]
                            resolved_slug = self.viewer_slug(baseurl)
                            self._log(
                                "INFO",
                                "Redirecting to viewer (regex fallback):",
                                baseurl,
                            )
                    else:
                        dump = json.dumps(
                            [[sc, href] for sc, href, _ in top],
                            indent=2,
                            ensure_ascii=False,
                        )
                        self._log("DBG", "RK candidates (top 6):", f"\n{dump}")

                        # open each top viewer and extract *real* dates/status
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

                        # Helper: pick best item by status using 'total'
                        def pick_by_status(desired: str):
                            cands = [v for v in verified if v["status"] == desired]
                            return (
                                max(cands, key=lambda v: v["total"]) if cands else None
                            )

                        choice = None
                        # choose final target based on rk_pick_mode
                        if rk_pick_mode == "current":
                            choice = (
                                pick_by_status("current")
                                or pick_by_status("unknown")
                                or pick_by_status("upcoming")
                            )
                        elif rk_pick_mode == "upcoming":
                            # for upcoming prefer earliest start date if available
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
                                choice = pick_by_status("upcoming") or pick_by_status(
                                    "current"
                                )
                        else:  # latest
                            # latest by id, but still include total score to break ties
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
                finally:
                    tmp_driver.quit()
            except Exception as e:
                self._log("DBG", "overview resolver error:", e)

        #  Start headless Chrome (main)
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
        found_total_pages: int | None = None
        last_fp: int | None = None  # perceptual fingerprint of previous page (dHash)

        # loop through pages
        try:
            for i in range(1, pages + 1):
                # Stop if we already know the true total count
                if found_total_pages and i > found_total_pages:
                    self._log(
                        "INFO",
                        f"Stopping at page {found_total_pages} (detected total).",
                    )
                    break

                url = self.page_url(baseurl, i)
                self._log("INFO", f"Loading page {i}:", url)
                driver.get(url)
                self._wait_cloudflare(driver)
                self._log("DBG", "current_url after GET:", driver.current_url or url)

                # If viewer clamped (e.g., requested 46, showing 45)
                try:
                    actual = self._current_page_from_url(driver.current_url or url)
                    if actual is not None and actual < i:
                        found_total_pages = actual
                        self._log("INFO", f"Viewer clamped at page {actual}. Stopping.")
                        break
                except Exception:
                    pass

                if i == 1:
                    self.accept_cookies_if_present(driver)
                    self._wait_first_page_ready(driver)

                    # filename slug
                    current = driver.current_url or baseurl
                    candidate_viewer = resolved_slug or self.viewer_slug(current)
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

                    # Try to detect the real total pages from page 1
                    try:
                        detected = self._detect_total_pages(driver)

                        MIN_TRUSTED_TOTAL = 46
                        if detected and detected > MIN_TRUSTED_TOTAL:
                            found_total_pages = detected
                            self._log(
                                "INFO",
                                f"Detected total pages (trusted): {found_total_pages}",
                            )
                        else:
                            self._log(
                                "INFO",
                                f"Ignoring small total =: {detected};continuing to scan pages.",
                            )
                    except Exception:
                        pass

                try:
                    WebDriverWait(driver, 8).until(
                        EC.presence_of_element_located((By.TAG_NAME, "img"))
                    )
                except Exception:
                    pass
                time.sleep(1.0)

                img_url = self.pick_image_url(driver)

                # If no direct image URL -> try visual fallback before deciding to stop
                if not img_url:
                    self._log("INFO", "No direct image URL; trying visual fallback…")
                    png_bytes = self._capture_best_visual(driver)
                    if png_bytes:
                        try:
                            img = Image.open(BytesIO(png_bytes)).convert("RGB")
                            fp = self._img_dhash(img)
                            if (
                                i > 1
                                and last_fp is not None
                                and self._ham(fp, last_fp) <= 2
                            ):
                                found_total_pages = i - 1
                                self._log(
                                    "INFO",
                                    f"Visually same as page {found_total_pages}. Stopping.",
                                )
                                break
                            last_fp = fp
                            images.append(img)
                            self._log(
                                "INFO",
                                f"Page {i}: added fallback screenshot. Total now {len(images)}",
                            )
                            continue
                        except Exception:
                            self._log("INFO", "Could not decode fallback visual.")
                            self._log(
                                "INFO",
                                f"No usable visual content on page {i}. Stopping.",
                            )
                            break
                    else:
                        self._log(
                            "INFO",
                            f"No visual content captured on page {i}. Stopping here.",
                        )
                        break

                # We have an image URL → try high-res and GET
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
                        # Perceptual duplicate check (visual)
                        try:
                            img = Image.open(BytesIO(r.content)).convert("RGB")
                        except Exception:
                            img = None
                        if img is not None:
                            fp = self._img_dhash(img)
                            if (
                                i > 1
                                and last_fp is not None
                                and self._ham(fp, last_fp) <= 2
                            ):
                                found_total_pages = i - 1
                                self._log(
                                    "INFO",
                                    f"Visually same as page {found_total_pages}. Stopping.",
                                )
                                break
                            last_fp = fp

                            # optional MD5 for debugging logs
                            md5 = hashlib.md5(r.content[:20000]).hexdigest()[:12]
                            self._log(
                                "DBG", "Downloaded bytes:", len(r.content), "md5=", md5
                            )

                            images.append(img)
                            self._log(
                                "INFO",
                                f"Page {i}: added image. Total now {len(images)}",
                            )
                            continue
                except Exception as e:
                    self._log("DBG", "Direct GET failed:", e)

                # Direct GET failed or empty -> try visual fallback
                self._log("INFO", "Direct download failed; trying visual fallback…")
                png_bytes = self._capture_best_visual(driver)
                if png_bytes:
                    try:
                        img = Image.open(BytesIO(png_bytes)).convert("RGB")
                        fp = self._img_dhash(img)
                        if (
                            i > 1
                            and last_fp is not None
                            and self._ham(fp, last_fp) <= 2
                        ):
                            found_total_pages = i - 1
                            self._log(
                                "INFO",
                                f"Visually same as page {found_total_pages}. Stopping.",
                            )
                            break
                        last_fp = fp
                        images.append(img)
                        self._log(
                            "INFO",
                            f"Page {i}: added fallback screenshot. Total now {len(images)}",
                        )
                        continue
                    except Exception:
                        self._log("INFO", "Could not decode fallback visual.")
                else:
                    self._log(
                        "INFO",
                        f"No visual content captured on page {i}. Stopping here.",
                    )
                    break
        finally:
            try:
                driver.quit()
                self._log("DBG", "Chrome closed.")
            except Exception:
                pass

        if not images:
            self._log("INFO", "No page images found!")
            return

        #  Build a single PDF from all images
        pdf = BytesIO()
        images[0].save(pdf, format="PDF", save_all=True, append_images=images[1:])
        pdf.seek(0)
        self._log("INFO", f"PDF built with {len(images)} pages.")

        #  Save into model Handzettel
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

        filename = f"{filename_prefix}_KW{iso_week:02d}.pdf"
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
            [client_id, client_secret, tenant_id, sharepoint_site, sharepoint_folder]
        ):
            return die("ERROR: SharePoint/Azure configuration missing!")

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
                return die(
                    "ERROR: Could not get access token:",
                    token_result.get("error_description"),
                    token_result,
                )
            token = token_result["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            self._log("INFO", "OK: Got access token")
        except Exception as e:
            return die("MSAL token error", e)

        try:
            site_info = requests.get(
                f"https://graph.microsoft.com/v1.0/sites/{sharepoint_site}",
                headers=headers,
            )
            self._log("DBG", "GET site status:", site_info.status_code)
            if site_info.status_code != 200:
                print("GET site body:", site_info.text[:800])
                return die(
                    "cannot read site.Check sharepoint site and permissions",
                    site_info.text,
                )
            site_json = site_info.json()
            site_id = site_json.get("id")
            self._log("INFO", "OK site id =", site_id)
            if not site_id:
                return die("site ID missing in response", site_json)
        except Exception as e:
            return die("site lookup failed", e)

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
                    return die(
                        "cannot list drives. Permissions missing!", drives_resp.text
                    )
                drives = drives_resp.json().get("value", [])
                self._log(
                    "DBG",
                    "Available drives:",
                    [(d.get("name"), d.get("id")) for d in drives],
                )
                if not drives:
                    return die("No drives found on the site.")

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
            return die("Drive lookup failed", e)

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
                        f"/{parent_path.rsplit('/',1)[0]}" if "/" in parent_path else ""
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

        subpath = "/".join(sharepoint_folder.split("/")[1:])
        year_folder = f"{today.year}"
        brand_folder = self.brand_folder_name(market)
        nested_subpath = f"{subpath}/{brand_folder}/{year_folder}"

        self._log("INFO", "Ensuring nested path (drive root-relative):", nested_subpath)

        try:
            if nested_subpath:
                ensure_folder_path(drive_id, nested_subpath)
                self._log("INFO", "OK: Folder path ensured:", nested_subpath)
        except Exception as e:
            return die("Creating/checking folder path failed", e)

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
                return die("Error uploading to SharePoint:", resp.status_code)
        except Exception as e:
            return die("Upload exception", e)

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
