# Download Rabatt-Kompass flyer pages -> PDF -> Django model -> SharePoint
import re, time, datetime, json, urllib.parse
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
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from handzettel.models import Handzettel

import os
from dotenv import load_dotenv
import msal

load_dotenv()


class Command(BaseCommand):
    help = "Download a flyer as a PDF (Rabatt-Kompass uniquement)."

    def add_arguments(self, parser):
        parser.add_argument(
            "baseurl",
            type=str,
            help="Rabatt-Kompass overview ou viewer (ex: …/lidl-prospekte/lidl-prospekt ou …/prospekt-<id>-0).",
        )
        parser.add_argument("pages", type=int, help="Nombre max de pages à essayer.")
        parser.add_argument(
            "--filename-mode",
            choices=["auto", "overview", "viewer"],
            default=os.getenv("FILENAME_MODE", "auto"),
        )
        parser.add_argument(
            "--rk-pick",
            choices=["current", "upcoming", "latest"],
            default=os.getenv("RK_PICK", "current"),
            help="Sur une page overview: choisir ‘current’ (défaut), ‘upcoming’ (Vorschau) ou ‘latest’.",
        )

    # ----- tiny logger -----
    def _log(self, level: str, *parts):
        print(f"[{level} {time.strftime('%H:%M:%S')}]", " ".join(str(p) for p in parts))

    # ----- Chrome / CF -----
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
                self._log("DBG", "CF challenge… wait")
                time.sleep(2)
                continue
            try:
                if driver.find_elements(
                    By.ID, "challenge-stage"
                ) or driver.find_elements(By.CSS_SELECTOR, ".cf-browser-verification"):
                    self._log("DBG", "CF DOM… wait")
                    time.sleep(2)
                    continue
            except Exception:
                pass
            break

    # ----- URL helpers (Rabatt-Kompass only) -----
    def page_url(self, baseurl: str, n: int) -> str:
        if re.search(r"#page_\d+", baseurl):
            return re.sub(r"#page_\d+", f"#page_{n}", baseurl)
        return f"{baseurl}#page_{n}"

    def url_slug(self, url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        seg = (path.split("/")[-1] or "prospekt").lower()
        return re.sub(r"[^a-z0-9_-]+", "-", seg)

    def viewer_slug(self, url: str) -> str | None:
        m = re.search(r"/prospekt-(\d+)-0", url)
        return f"prospekt-{m.group(1)}-0" if m else None

    # ----- Consent / ready -----
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

    def _wait_first_page_ready(self, driver):
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "img[src*='/public/gimg/']")
                )
            )
        except Exception:
            pass
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.5)
            driver.execute_script("window.scrollTo(0, 0);")
        except Exception:
            pass

    # ----- Overview scoring (idem) -----
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
        if mode == "current":
            if is_aktuell:
                b += 350
                why.append("phrase:aktuell+350")
            if is_kw:
                b += 60
                why.append("phrase:kw+60")
            if is_vorschau:
                b -= 450
                why.append("phrase:vorschau-450")
        elif mode == "upcoming":
            if is_vorschau:
                b += 350
                why.append("phrase:vorschau+350")
            if is_aktuell:
                b -= 150
                why.append("phrase:aktuell-150")
        else:
            if is_aktuell:
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

    def _id_bias(self, href: str, mode: str) -> tuple[int, str]:
        m = re.search(r"/prospekt-(\d+)-0", href)
        if not m:
            return 0, ""
        pid = int(m.group(1))
        if mode == "latest":
            add = min(2000, (pid // 10) % 3000)
            return add, f"id(latest):{pid}(+{add})"
        else:
            add = (pid % 13) - 6
            return add, f"id(light):{pid}(+{add})"

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

    def _inspect_viewer_details(self, driver, href: str):
        status, bonus, reason = "unknown", 0, []
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
            html = (driver.page_source or "").lower()
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

    # ----- Visual capture & hashing -----
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
            return driver.get_screenshot_as_png()
        except Exception:
            return None

    def _img_dhash(self, img: Image.Image) -> int:
        small = img.convert("L").resize((9, 8), Image.LANCZOS)
        px = list(small.getdata())
        bits = 0
        for r in range(8):
            row = r * 9
            for c in range(8):
                bits = (bits << 1) | (1 if px[row + c] > px[row + c + 1] else 0)
        return bits

    def _ham(self, a: int, b: int) -> int:
        x = a ^ b
        cnt = 0
        while x:
            x &= x - 1
            cnt += 1
        return cnt

    # ----- Viewer context & nav -----
    def _enter_rk_viewer_frame(self, driver) -> bool:
        driver.switch_to.default_content()
        for idx, f in enumerate(driver.find_elements(By.CSS_SELECTOR, "iframe")):
            try:
                driver.switch_to.frame(f)
                ok = driver.execute_script(
                    "return !!document.querySelector(\"img[src*='/public/gimg/'], a[href*='#page_']\");"
                )
                if ok:
                    self._log("DBG", f"Entered viewer iframe #{idx}")
                    return True
            except Exception:
                pass
            finally:
                driver.switch_to.default_content()
        return False

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

    def _goto_rk_page_in_context(
        self, driver, n: int, hard_url: str | None = None
    ) -> None:
        thumbs = driver.find_elements(By.CSS_SELECTOR, f"a[href$='#page_{n}']")
        if thumbs:
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'})", thumbs[0]
                )
                thumbs[0].click()
                return
            except Exception:
                pass
        try:
            driver.execute_script(
                f"if (location.hash !== '#page_{n}') location.hash = '#page_{n}';"
            )
            driver.execute_script(
                "window.dispatchEvent(new HashChangeEvent('hashchange'));"
            )
            return
        except Exception:
            pass
        if hard_url:
            try:
                driver.switch_to.default_content()
                driver.get(hard_url)
            except Exception:
                pass
        if n > 1:
            try:
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ARROW_RIGHT)
            except Exception:
                pass

    def _current_main_img_src(self, driver) -> str | None:
        best, best_area = None, 0
        for el in driver.find_elements(By.CSS_SELECTOR, "img[src*='/public/gimg/']"):
            if not el.is_displayed():
                continue
            try:
                w = el.size.get("width", 0)
                h = el.size.get("height", 0)
                area = max(0, w) * max(0, h)
                src = el.get_attribute("currentSrc") or el.get_attribute("src") or ""
                if src and area > best_area:
                    best, best_area = src, area
            except Exception:
                continue
        return best

    def _wait_main_img_src_changed(
        self, driver, old_src: str | None, timeout: float = 10.0
    ) -> str | None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            new_src = self._current_main_img_src(driver)
            if new_src and new_src != old_src:
                return new_src
            time.sleep(0.25)
        return self._current_main_img_src(driver)

    def _make_session_from_browser(self, driver, referer: str) -> requests.Session:
        s = requests.Session()
        try:
            for c in driver.get_cookies():
                s.cookies.set(
                    c.get("name"),
                    c.get("value"),
                    domain=c.get("domain"),
                    path=c.get("path"),
                )
        except Exception:
            pass
        try:
            ua = driver.execute_script("return navigator.userAgent") or ""
            if ua:
                s.headers["User-Agent"] = ua
        except Exception:
            pass
        s.headers["Referer"] = referer
        return s

    # ----- SharePoint helpers -----
    def brand_folder_name(self, market: str) -> str:
        mapping = {"lidl": "LIDL", "aldi_nord": "ALDI_NORD", "aldi_sued": "ALDI_SUED"}
        return mapping.get((market or "").lower(), "MISC")

    # ----- Main -----
    def handle(self, *args, **opts):
        baseurl = opts["baseurl"]
        pages = opts["pages"]
        filename_mode = opts.get("filename_mode", "auto")
        rk_pick_mode = opts.get("rk_pick", "current")

        if "rabatt-kompass.de" not in baseurl:
            self._log("INFO", "this command support only rabatt-kompass.de")
            raise SystemExit(1)

        self._log(
            "INFO",
            "START baseurl=",
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

        # ---- Resolve overview -> viewer
        resolved_slug = None
        if "/prospekt-" not in baseurl:
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
                    hrefs, seen = [], set()
                    for a in links:
                        h = a.get_attribute("href") or ""
                        if h and h not in seen:
                            hrefs.append(a)
                            seen.add(h)
                    self._log("DBG", f"overview → {len(hrefs)} viewer links")
                    overview_scored = []
                    for a in hrefs:
                        sc, href, why = self._score_rk_viewer_link(
                            a, retailer_hint, rk_pick_mode
                        )
                        overview_scored.append((sc, href, why))
                    overview_scored.sort(key=lambda t: t[0], reverse=True)
                    top = overview_scored[:6]
                    if not top:
                        html = tmp_driver.page_source or ""
                        raw = re.findall(
                            r'https://rabatt-kompass\.de/[^"\'\s]*/prospekt-\d+-0', html
                        )
                        raw = list(dict.fromkeys(raw))
                        self._log("DBG", f"regex → {len(raw)} candidates")
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

                        def pick_by_status(desired):
                            c = [v for v in verified if v["status"] == desired]
                            return max(c, key=lambda v: v["total"]) if c else None

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
                                        (v["start"] or datetime.date.max),
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
                            self._log("INFO", "Redirect → viewer (regex):", baseurl)
                    else:
                        dump = json.dumps(
                            [[sc, href] for sc, href, _ in top],
                            indent=2,
                            ensure_ascii=False,
                        )
                        self._log("DBG", "candidates(top 6):\n" + dump)
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

                        def pick_by_status(desired: str):
                            cands = [v for v in verified if v["status"] == desired]
                            return (
                                max(cands, key=lambda v: v["total"]) if cands else None
                            )

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
                                        (v["start"] or datetime.date.max),
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
                        self._log("INFO", "Redirect → viewer:", best_href)
                        baseurl = best_href
                        resolved_slug = self.viewer_slug(best_href)
                finally:
                    tmp_driver.quit()
            except Exception as e:
                self._log("DBG", "overview resolver error:", e)

        # ---- Launch Chrome (main)
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
        last_fp: int | None = None

        try:
            # Open viewer once
            driver.get(
                baseurl if "/prospekt-" in baseurl else self.page_url(baseurl, 1)
            )
            self._wait_cloudflare(driver)
            self.accept_cookies_if_present(driver)
            self._wait_first_page_ready(driver)

            # Promote iframe (CI) if requested
            if os.getenv("RK_FORCE_TOPLEVEL", "0") not in ("0", "", "false", "False"):
                try:
                    iframe = driver.find_element(By.CSS_SELECTOR, "iframe[src]")
                    src = (iframe.get_attribute("src") or "").strip()
                    if src:
                        if not src.startswith("http"):
                            src = urllib.parse.urljoin(
                                driver.current_url or baseurl, src
                            )
                        if "#page_" not in src:
                            src = src + ("#page_1" if "#" not in src else "")
                        self._log("INFO", "CI mode: open iframe src directly:", src)
                        driver.switch_to.default_content()
                        driver.get(src)
                        self._wait_cloudflare(driver)
                        self._wait_first_page_ready(driver)
                except Exception as e:
                    self._log("DBG", "No promotable iframe:", e)

            # Enter viewer context
            self._switch_to_viewer_context(driver)

            # filename slug
            current = driver.current_url or baseurl
            candidate_viewer = resolved_slug or self.viewer_slug(current)
            overview_slug = self.url_slug(original_input)
            slug_for_filename = (
                candidate_viewer
                if (
                    opts.get("filename_mode") in ("auto", "viewer") and candidate_viewer
                )
                else overview_slug
            )
            self._log("INFO", "Filename slug:", slug_for_filename)

            session = self._make_session_from_browser(driver, referer=current)
            prev_src = None
            viewer_base = re.sub(r"#page_\d+$", "", current)

            for i in range(1, pages + 1):
                self._log("INFO", f"→ Page {i}")
                self._switch_to_viewer_context(driver)
                self._goto_rk_page_in_context(driver, i)
                new_src = self._wait_main_img_src_changed(
                    driver, prev_src, timeout=10.0
                )

                if not new_src or (prev_src and new_src == prev_src):
                    try:
                        driver.execute_script("window.scrollBy(0, 200)")
                        time.sleep(0.3)
                        driver.execute_script("window.scrollBy(0, -200)")
                    except Exception:
                        pass
                    new_src = self._wait_main_img_src_changed(
                        driver, prev_src, timeout=4.0
                    )

                if not new_src or (prev_src and new_src == prev_src):
                    hard = viewer_base.rstrip("#") + f"#page_{i}"
                    self._goto_rk_page_in_context(driver, i, hard_url=hard)
                    new_src = self._wait_main_img_src_changed(
                        driver, prev_src, timeout=6.0
                    )

                if i > 1 and (not new_src or new_src == prev_src):
                    self._log("INFO", f"Fin détectée à la page {i-1}.")
                    break

                if not new_src:
                    self._log("INFO", "Pas d'image principale → fallback screenshot")
                    png_bytes = self._capture_best_visual(driver)
                    if not png_bytes:
                        self._log("INFO", f"Pas de visuel à la page {i}. Stop.")
                        break
                    try:
                        img = Image.open(BytesIO(png_bytes)).convert("RGB")
                        fp = self._img_dhash(img)
                        if (
                            i > 1
                            and last_fp is not None
                            and self._ham(fp, last_fp) <= 2
                        ):
                            self._log("INFO", f"Identique à la page {i-1}. Stop.")
                            break
                        last_fp = fp
                        images.append(img)
                        prev_src = f"fp:{fp}"
                        self._log(
                            "INFO", f"Page {i}: screenshot ajouté. Total {len(images)}"
                        )
                        continue
                    except Exception:
                        self._log("INFO", "Screenshot illisible.")
                        break

                # Download the page image
                prev_src = new_src
                hi_url = (
                    re.sub(
                        r"-(\d{3,4})-(\d+)\.(jpe?g|png|webp)$",
                        r"-2000-\2.\3",
                        new_src,
                        flags=re.I,
                    )
                    or new_src
                )
                self._log("DBG", "Image URL:", hi_url)
                try:
                    r = session.get(hi_url, timeout=20)
                    self._log(
                        "DBG",
                        "GET",
                        hi_url,
                        "→",
                        r.status_code,
                        "len=",
                        len(r.content) if r.ok else 0,
                    )
                    if r.status_code == 200 and r.content:
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
                                self._log("INFO", f"Identique à la page {i-1}. Stop.")
                                break
                            last_fp = fp
                            images.append(img)
                            self._log(
                                "INFO", f"Page {i}: image ajoutée. Total {len(images)}"
                            )
                            continue
                except Exception as e:
                    self._log("DBG", "GET failed:", e)

                # Fallback screenshot
                self._log("INFO", "Download KO → fallback screenshot…")
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
                            self._log("INFO", f"Identique à la page {i-1}. Stop.")
                            break
                        last_fp = fp
                        images.append(img)
                        self._log(
                            "INFO", f"Page {i}: screenshot ajouté. Total {len(images)}"
                        )
                        continue
                    except Exception:
                        self._log("INFO", "Screenshot illisible.")
                        break
        finally:
            try:
                driver.quit()
                self._log("DBG", "Chrome closed.")
            except Exception:
                pass

        if not images:
            self._log("INFO", "Aucune page trouvée.")
            return

        # ---- Build PDF
        pdf = BytesIO()
        images[0].save(pdf, format="PDF", save_all=True, append_images=images[1:])
        pdf.seek(0)
        self._log("INFO", f"PDF construit ({len(images)} pages).")

        # ---- Save model
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
        filename_prefix = {
            "lidl": "LidlProspekt",
            "aldi_nord": "AldiNordProspekt",
            "aldi_sued": "AldiSuedProspekt",
            "kaufland": "KauflandProspekt",
            "edeka": "EdekaProspekt",
        }.get(market, "Prospekt")
        filename = f"{filename_prefix}_KW{iso_week:02d}.pdf"
        title = f"{market.replace('_',' ').upper()} – {slug} – {today:%Y-%m-%d}"

        handzettel = Handzettel(supermarkt=market, titel=title)
        handzettel.datei.save(filename, ContentFile(pdf.read()))
        handzettel.save()
        self._log("INFO", f"PDF sauvegardé dans le modèle: {filename}")

        # ---- SharePoint upload
        self._log("INFO", "Upload vers SharePoint…")
        client_id = os.getenv("AZURE_CLIENT_ID")
        client_secret = os.getenv("AZURE_CLIENT_SECRET")
        tenant_id = os.getenv("AZURE_TENANT_ID")
        sharepoint_site = os.getenv("SHAREPOINT_SITE")
        sharepoint_folder = os.getenv("SHAREPOINT_FOLDER")
        sharepoint_drive_id = os.getenv("SHAREPOINT_DRIVE_ID")

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
            self._log("INFO", "OK: access token")
        except Exception as e:
            return die("MSAL token error", e)

        try:
            site_info = requests.get(
                f"https://graph.microsoft.com/v1.0/sites/{sharepoint_site}",
                headers=headers,
            )
            if site_info.status_code != 200:
                return die("cannot read site. Check site & permissions", site_info.text)
            site_id = site_info.json().get("id")
            if not site_id:
                return die("site ID missing")
            self._log("INFO", "Site id =", site_id)
        except Exception as e:
            return die("site lookup failed", e)

        def _norm(name: str) -> str:
            return (name or "").lower().replace(" ", "").replace("_", "")

        try:
            if sharepoint_drive_id:
                drive_id = sharepoint_drive_id.strip()
                self._log("INFO", "Using drive by ID:", drive_id)
            else:
                drives_resp = requests.get(
                    f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives",
                    headers=headers,
                )
                if drives_resp.status_code != 200:
                    return die("cannot list drives", drives_resp.text)
                drives = drives_resp.json().get("value", [])
                if not drives:
                    return die("No drives on site")
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
                ) or drives[0]
                drive_id = drive["id"]
                self._log("INFO", "Using drive:", drive.get("name"), drive_id)
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
                            f"create '{part}' failed: {cr.status_code} {cr.text}"
                        )
                elif r.status_code != 200:
                    raise RuntimeError(
                        f"Folder check failed for '{parent_path}': {r.status_code} {r.text}"
                    )

        subpath = "/".join(sharepoint_folder.split("/")[1:])
        year_folder = f"{today.year}"
        brand_folder = self.brand_folder_name(market)
        nested_subpath = f"{subpath}/{brand_folder}/{year_folder}"

        try:
            if nested_subpath:
                ensure_folder_path(drive_id, nested_subpath)
        except Exception as e:
            return die("Creating/checking folder path failed", e)

        try:
            upload_path = "/".join([nested_subpath, filename])
            upload_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{upload_path}:/content"
            file_bytes = handzettel.datei.read()
            resp = requests.put(upload_url, headers=headers, data=file_bytes)
            if resp.status_code not in (200, 201):
                return die(
                    "Upload error",
                    resp.status_code,
                    resp.text[:400] if resp.text else "",
                )
            # optional: fetch webUrl
            try:
                weburl = resp.json().get("webUrl")
            except Exception:
                weburl = None
            self._log("INFO", "Uploaded to:", weburl or upload_path)
        except Exception as e:
            return die("Upload exception", e)

        # quick search (debug)
        try:
            q = urllib.parse.quote(filename)
            r = requests.get(
                f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root/search(q='{q}')",
                headers=headers,
            )
            if r.status_code == 200:
                hits = r.json().get("value", [])
                self._log("INFO", f"Search hits '{filename}':", len(hits))
                for it in hits[:5]:
                    print("-", it.get("name"), "| webUrl:", it.get("webUrl"))
        except Exception:
            pass

        self._log("INFO", "DONE.")
