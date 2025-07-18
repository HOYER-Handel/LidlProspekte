import time
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from django.core.management.base import BaseCommand
from handzettel.models import Handzettel
from django.core.files.base import ContentFile
from PIL import Image
from io import BytesIO
import datetime

class Command(BaseCommand):
    help = 'Download Prospekt from rabatt-kompass.de as PDF'

    def add_arguments(self, parser):
        # Command line arguments: base URL and number of pages to download
        parser.add_argument('baseurl', type=str, help='e.g. https://rabatt-kompass.de/aldi-sued-prospekte/aldi-sued-prospekt')
        parser.add_argument('seiten', type=int, help='Number of pages, e.g. 36')

    def handle(self, *args, **options):
        baseurl = options['baseurl']
        seiten = options['seiten']

        # === SETUP SELENIUM (Chrome in headless mode) ===
        chromedriver_path = r"C:\Users\emna.kammoun\Downloads\chromedriver-win64\chromedriver-win64\chromedriver.exe"
        chrome_options = Options()
        chrome_options.add_argument("--headless=new")  # Run Chrome without UI
        chrome_options.add_argument("--window-size=1920,1080")
        service = Service(executable_path=chromedriver_path)
        driver = webdriver.Chrome(service=service, options=chrome_options)

        pil_images = []  # Will hold all PIL images for PDF later
        for i in range(1, seiten + 1):
            # Build page URL for each flyer/prospekt page
            page_url = f"{baseurl}#page_{i}"
            print(f"Loading page {i}: {page_url}")
            driver.get(page_url)
            time.sleep(2.5)  # Wait for images to load

            # Find main flyer image on the page
            imgs = driver.find_elements("css selector", "img.swiper-lazy, img[loading='lazy']")
            img_url = None
            for img in imgs:
                url = img.get_attribute("data-src") or img.get_attribute("src")
                if url and "rabatt-kompass" in url and ("seiten" in url or "flyer" in url):
                    img_url = url
                    break

            # If no image found, pick the largest image (fallback)
            if not img_url:
                imgs2 = driver.find_elements("css selector", "img")
                biggest = (0, None)
                for img in imgs2:
                    try:
                        w = int(img.get_attribute("width") or 0)
                        if w > biggest[0]:
                            biggest = (w, img)
                    except Exception:
                        continue
                if biggest[1]:
                    img_url = biggest[1].get_attribute("src")

            # Download and store the image if found
            if img_url:
                print(f"  Image: {img_url}")
                r = requests.get(img_url)
                if r.status_code == 200:
                    im = Image.open(BytesIO(r.content)).convert("RGB")
                    pil_images.append(im)
                else:
                    print("  Error downloading image")
            else:
                print("  No image found!")

        driver.quit()  # Close Selenium browser

        if not pil_images:
            print("No page images found!")
            return

        # === CREATE PDF FROM ALL IMAGES ===
        pdf_bytes = BytesIO()
        pil_images[0].save(pdf_bytes, format="PDF", save_all=True, append_images=pil_images[1:])
        pdf_bytes.seek(0)

        # === Determine supermarket type from URL ===
        supermarkt = "unknown"
        if "lidl" in baseurl:
            supermarkt = "lidl"
        elif "aldi-nord" in baseurl:
            supermarkt = "aldi_nord"
        elif "aldi-sued" in baseurl:
            supermarkt = "aldi_sued"
        elif "kaufland" in baseurl:
            supermarkt = "kaufland"

        # === Automatically generate filename with date and supermarket ===
        datum = datetime.date.today().strftime('%Y-%m-%d')
        dateiname = f"{supermarkt}_prospekt_{datum}.pdf"
        titel = f"{supermarkt.capitalize()} Prospekt vom {datum}"

        # === Save PDF to Django Model ===
        handzettel = Handzettel(supermarkt=supermarkt, titel=titel)
        handzettel.datei.save(dateiname, ContentFile(pdf_bytes.read()))
        handzettel.save()
        print(f"PDF with {len(pil_images)} pages saved as '{dateiname}'!")
