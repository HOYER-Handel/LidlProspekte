import re
import time
import datetime
from io import BytesIO

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


class Command(BaseCommand):
    help = "Download a flyer as a PDF (supports rabatt-kompass.de and lidl.de)."

    def add_arguments(self, parser):
        parser.add_argument("baseurl", type=str, help="rabatt-kompass root OR Lidl URL containing /page/1")
        parser.add_argument("pages", type=int, help="How many pages to download (e.g., 36)")

    # --- Build the correct URL for each page depending on the site ---
    def page_url(self, baseurl: str, n: int) -> str:
        if "lidl.de" in baseurl:
            # Lidl uses /page/<n> in the path
            if re.search(r"/page/\d+", baseurl):
                return re.sub(r"/page/\d+", f"/page/{n}", baseurl)
            parts = baseurl.split("?", 1)
            path = parts[0].rstrip("/")
            query = f"?{parts[1]}" if len(parts) == 2 else ""
            return f"{path}/page/{n}{query}"
        # rabatt-kompass uses #page_<n>
        if re.search(r"#page_\d+", baseurl):
            return re.sub(r"#page_\d+", f"#page_{n}", baseurl)
        return f"{baseurl}#page_{n}"

    # --- Click cookie banner if present (Lidl uses OneTrust) ---
    def accept_cookies_if_present(self, driver):
        try:
            btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.ID, "onetrust-accept-btn-handler"))
            )
            btn.click()
            time.sleep(0.3)
        except Exception:
            pass  # fine if not there

    # --- Find one good image URL on the current page ---
    def pick_image_url(self, driver) -> str | None:
        urls = set()

        # srcset entries often include highest-res image variants
        for el in driver.find_elements(By.CSS_SELECTOR, "picture source[srcset], img[srcset], source[srcset]"):
            srcset = el.get_attribute("srcset")
            if srcset:
                for part in srcset.split(","):
                    u = part.strip().split(" ")[0]
                    if u.startswith("http"):
                        urls.add(u)

        # regular and lazy images
        for img in driver.find_elements(By.CSS_SELECTOR, "img, img[data-src]"):
            for attr in ("data-src", "src"):
                u = img.get_attribute(attr)
                if u and u.startswith("http"):
                    urls.add(u)

        # drop obvious junk
        urls = {u for u in urls if "cookielaw.org" not in u and "onetrust.com" not in u}
        urls = {u for u in urls if not u.lower().endswith(".svg")}

        if not urls:
            return None

        # simple preference: right domain + image extension + flyer-ish keywords
        def score(u: str) -> int:
            s = 0
            if any(k in u for k in ("lidl", "rabatt-kompass", "cloudfront", "cdn", "media", "assets")):
                s += 2
            if re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", u, re.I):
                s += 2
            if any(k in u.lower() for k in ("flyer", "page", "seiten", "prospekt")):
                s += 1
            if any(k in u.lower() for k in ("thumb", "icon", "small")):
                s -= 1
            return s

        return sorted(urls, key=score, reverse=True)[0]

    # --- Download one image into a PIL Image ---
    def fetch_image(self, url: str) -> Image.Image | None:
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200 and r.content:
                return Image.open(BytesIO(r.content)).convert("RGB")
        except Exception:
            pass
        return None

    def handle(self, *args, **opts):
        baseurl = opts["baseurl"]
        pages = opts["pages"]

        # 1) Start headless Chrome 
        chromedriver_path = r"C:\Users\emna.kammoun\Downloads\chromedriver-win64\chromedriver-win64\chromedriver.exe"
        chrome_options = Options()
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--window-size=1920,1080")
        driver = webdriver.Chrome(service=Service(executable_path=chromedriver_path), options=chrome_options)

        images: list[Image.Image] = []

        try:
            for i in range(1, pages + 1):
                url = self.page_url(baseurl, i)
                print(f"Loading page {i}: {url}")
                driver.get(url)

                if i == 1:
                    self.accept_cookies_if_present(driver)

                # wait for any image, then allow lazy-loading to finish
                try:
                    WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.TAG_NAME, "img")))
                except Exception:
                    pass
                time.sleep(1.5)

                img_url = self.pick_image_url(driver)
                if not img_url:
                    print("  No image found on this page.")
                    continue

                print(f"  Image: {img_url}")
                pic = self.fetch_image(img_url)
                if pic:
                    images.append(pic)
                else:
                    print("  Failed to download image.")
        finally:
            driver.quit()

        if not images:
            print("No page images found!")
            return

        # 2) Build a single PDF from all images
        pdf = BytesIO()
        images[0].save(pdf, format="PDF", save_all=True, append_images=images[1:])
        pdf.seek(0)

        # 3) Save into model
        u = baseurl.lower()
        market = (
            "lidl" if "lidl" in u else
            "aldi_nord" if "aldi-nord" in u else
            "aldi_sued" if "aldi-sued" in u else
            "kaufland" if "kaufland" in u else
            "unknown"
        )
        today = datetime.date.today().strftime("%Y-%m-%d")
        filename = f"{market}_prospekt_{today}.pdf"
        title = f"{market.capitalize()} Prospekt vom {today}"

        handzettel = Handzettel(supermarkt=market, titel=title)
        handzettel.datei.save(filename, ContentFile(pdf.read()))
        handzettel.save()

        print(f"PDF with {len(images)} pages saved as '{filename}'!")
