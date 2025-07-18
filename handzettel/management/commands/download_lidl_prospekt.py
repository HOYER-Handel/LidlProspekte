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
    help = 'Download flyer from rabatt-kompass.de as PDF'

    def add_arguments(self, parser):
        # Command line arguments: URL and number of pages
        parser.add_argument('baseurl', type=str, help='e.g. https://rabatt-kompass.de/aldi-sued-prospekte/aldi-sued-prospekt')
        parser.add_argument('seiten', type=int, help='Number of pages, e.g. 36')

    def handle(self, *args, **options):
        baseurl = options['baseurl']
        seiten = options['seiten']

        # === Set up Selenium ChromeDriver ===
     
        chromedriver_path = r"C:\Users\emna.kammoun\Downloads\chromedriver-win64\chromedriver-win64\chromedriver.exe"
        chrome_options = Options()
        chrome_options.add_argument("--headless=new")  # Run Chrome in headless mode 
        chrome_options.add_argument("--window-size=1920,1080")
        service = Service(executable_path=chromedriver_path)
        driver = webdriver.Chrome(service=service, options=chrome_options)

        pil_images = []
        # Loop through each requested flyer page
        for i in range(1, seiten+1):
            page_url = f"{baseurl}#page_{i}"
            print(f"Loading page {i}: {page_url}")
            driver.get(page_url)
            time.sleep(2.5)  # Wait for the page and images to load

            # Try to find the correct flyer image on the page
            imgs = driver.find_elements("css selector", "img.swiper-lazy, img[loading='lazy']")
            img_url = None
            for img in imgs:
                url = img.get_attribute("data-src") or img.get_attribute("src")
                # Only accept correct images from rabatt-kompass, matching "seiten" or "flyer"
                if url and "rabatt-kompass" in url and ("seiten" in url or "flyer" in url):
                    img_url = url
                    break
            # Fallback: If not found, use the biggest image on the page
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
            # Download the image and add it to our list
            if img_url:
                print(f"  Image: {img_url}")
                r = requests.get(img_url)
                if r.status_code == 200:
                    im = Image.open(BytesIO(r.content)).convert("RGB")
                    pil_images.append(im)
                else:
                    print(f"  Error downloading image")
            else:
                print("  No image found!")

        driver.quit()  # Close the browser when done

        # If no images were found, abort and print a message
        if not pil_images:
            print("No page images found!")
            return

        # === Create PDF from all downloaded images ===
        pdf_bytes = BytesIO()
        pil_images[0].save(pdf_bytes, format="PDF", save_all=True, append_images=pil_images[1:])
        pdf_bytes.seek(0)
        
        # === Determine supermarket from URL for naming ===
        supermarkt = "unknown"
        if "lidl" in baseurl:
            supermarkt = "lidl"
        elif "aldi-nord" in baseurl:
            supermarkt = "aldi_nord"
        elif "aldi-sued" in baseurl:
            supermarkt = "aldi_sued"
        elif "kaufland" in baseurl:
            supermarkt = "kaufland"

        # === Generate automatic filename with date and supermarket name ===
        datum = datetime.date.today().strftime('%Y-%m-%d')
        dateiname = f"{supermarkt}_prospekt_{datum}.pdf"
        titel = f"{supermarkt.capitalize()} Prospekt vom {datum}"

        # === Save the PDF into the Django model ===
        handzettel = Handzettel(supermarkt=supermarkt, titel=titel)
        handzettel.datei.save(dateiname, ContentFile(pdf_bytes.read()))
        handzettel.save()
        print(f"PDF with {len(pil_images)} pages saved as '{dateiname}'!")
