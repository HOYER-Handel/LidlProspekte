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

import os #for reading environment variables
from dotenv import load_dotenv
import msal #microsoft authentification library

#Load envt variables for Azure credentials
load_dotenv()


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
     
        imgs = driver.execute_script("""
            return Array.from(document.images).map(img => ({
                src: img.currentSrc || img.src || '',
                w: img.naturalWidth || 0,
                h: img.naturalHeight || 0
            }));
        """) or []

        EXCLUDE_HOST_BITS = [
            "bat.bing.com", "cookielaw.org", "onetrust.com",
            "googletagmanager", "google-analytics", "doubleclick",
            "facebook", "hotjar", "adservice", "analytics"
        ]

        candidates = []
        for it in imgs:
            u = (it.get("src") or "").strip()
            w = int(it.get("w") or 0)
            h = int(it.get("h") or 0)
            if not u.startswith("http"):
                continue
            if any(bad in u for bad in EXCLUDE_HOST_BITS):
                continue
            if u.lower().endswith(".svg"):
                continue

            # Score: prioritize big images; boost likely flyer/CDN URLs + proper extensions
            area = w * h
            score = area
            if any(k in u for k in ("leaflets", "schwarz", "lidl", "rabatt-kompass", "cloudfront", "cdn", "media", "assets")):
                score += 500_000
            if re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", u, re.I):
                score += 250_000
            if any(k in u.lower() for k in ("flyer", "page", "seiten", "prospekt")):
                score += 100_000
            if any(k in u.lower() for k in ("thumb", "icon", "small")):
                score -= 200_000

            candidates.append((score, u))

        if not candidates:
            return None

        candidates.sort(reverse=True)
        return candidates[0][1]

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
        
        #Azure sharePoint upload
        print("uploading PDF to SharePoint")
        #Read sharePoint/Azure config from envt variables
        client_id = os.getenv("AZURE_CLIENT_ID")
        client_secret = os.getenv("AZURE_CLIENT_SECRET")
        tenant_id = os.getenv("AZURE_TENANT_ID")
        sharepoint_site = os.getenv("SHAREPOINT_SITE")
        sharepoint_folder = os.getenv("SHAREPOINT_FOLDER")
        
        #ensure that all required config values are set
        if not all([client_id,client_secret,tenant_id,sharepoint_site,sharepoint_folder]):
            print("ERROR: SahrePoint/Azure configuration missing!")
            return
        
        #Use msal to get a token for the graph API
        app = msal.ConfidentialClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=client_secret
        )
        token_result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in token_result:
            print("ERROR: Could not get access token:",token_result.get("error_description"))
            return
        token = token_result["access_token"]
        headers ={'Authorization' : f'Bearer {token}'}
        
        #Get sharePoint site ID
        site_info = requests.get(f"https://graph.microsoft.com/v1.0/sites/{sharepoint_site}", headers=headers).json()
        site_id = site_info.get('id')
        if not site_id:
            print("ERROR: SharePoint Site ID nicht gefunden!", site_info)
            return
        
        #Upload the PDF to the correct sharePoint folder via the Graph API
        upload_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root:/{sharepoint_folder}/{filename}:/content"
        with open(handzettel.datei.path, "rb") as f:
            resp = requests.put(upload_url, headers=headers, data=f)
        if resp.status_code in (200, 201):
            print("PDF successfully uploaded to SharePoint!")
        else:
            print("Error uploading to SharePoint:", resp.status_code, resp.text)
        

        
        

            
        
        
        
