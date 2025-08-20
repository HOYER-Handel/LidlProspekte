# This Python class downloads flyer images from websites, creates a PDF from the images, saves the PDF
# to a model, and uploads the PDF to SharePoint using the Microsoft Graph API.
import re, time, datetime
from io import BytesIO
import json, urllib.parse
from pathlib import Path
from typing import Optional, List

import requests
from PIL import Image

from django.core.management.base import BaseCommand
from django.core.files.base import ContentFile

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from selenium.webdriver import Remote


from handzettel.models import Handzettel

import os  # for reading environment variables
from dotenv import load_dotenv
import msal  # microsoft authentification library

# Load envt variables for Azure credentials
load_dotenv()


class Command(BaseCommand):
    help = "Download a flyer as a PDF (supports rabatt-kompass.de and lidl.de)."

    def add_arguments(self, parser):
        parser.add_argument(
            "baseurl",
            type=str,
            help="rabatt-kompass root OR Lidl URL containing /page/1",
        )
        parser.add_argument(
            "pages", type=int, help="How many pages to download (e.g., 36)"
        )

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

    # --- Click cookie banner if present ---
    # Lidl uses OneTrust cookie banners. This tries to click "Accept" if it appears.
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
        imgs = (
            driver.execute_script(
                """
            return Array.from(document.images).map(img => ({
                src: img.currentSrc || img.src || '',
                w: img.naturalWidth || 0,
                h: img.naturalHeight || 0
            }));
        """
            )
            or []
        )

        EXCLUDE_HOST_BITS = [
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

            # Score: prioritize big images; boost likely flyer + proper extensions
            area = w * h
            score = area
            if any(k in u for k in ("lidl", "rabatt-kompass")):
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
            r = requests.get(
                url,
                timeout=20,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/123.0.0.0 Safari/537.36"
                    )
                },
            )
            if r.status_code == 200 and r.content:
                img = Image.open(BytesIO(r.content))
                if img.mode != "RGB":
                    img = img.convert("RGB")
                return img
        except Exception:
            pass
        return None

    # ---Github Actions trigger ---
    def trigger_github_action(self, baseurl, pages) -> bool:
        github_token = os.getenv("GITHUB_TOKEN")
        repo_owner = os.getenv("GITHUB_REPO_OWNER")
        repo_name = os.getenv("GITHUB_REPO_NAME")
        workflow_id = os.getenv("GITHUB_WORKFLOW_ID", "selenium-flyer.yml")
        github_ref = os.getenv("GITHUB_REF", "main")

        if not all([github_token, repo_owner, repo_name]):
            raise RuntimeError(
                "GitHub configuration missing: GITHUB_TOKEN / GITHUB_REPO_OWNER / GITHUB_REPO_NAME"
            )

        url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/actions/workflows/{workflow_id}/dispatches"
        headers = {
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        }

        data = {"ref": github_ref, "inputs": {"url": baseurl, "pages": str(pages)}}

        response = requests.post(url, headers=headers, json=data, timeout=30)

        if response.status_code == 204:
            self.stdout.write(self.style.SUCCESS("GitHub Actions workflow triggered"))
            return True

        self.stdout.write(
            self.style.ERROR(
                f"GitHub trigger failed {response.status_code}: {response.text[:400]}"
            )
        )
        if response.status_code == 404:
            self.stdout.write("Check GITHUB_WORKFLOW_ID (filename) and branch ref.")
        if response.status_code in (401, 403):
            self.stdout.write("Check PAT scopes: need repo + workflow/actions.")
        return False

    # --Main--
    def handle(self, *args, **opts):
        baseurl = opts["baseurl"]
        pages = opts["pages"]

        use_github_actions = os.getenv("USE_GITHUB_ACTIONS", "true").lower() == "true"

        if use_github_actions:
            print("using github actions for selenium processing")
            success = self.trigger_github_action(baseurl, pages)
            if success:
                print("Flyer processing job submitted to github actions")
            else:
                print("Failed to submit job to Github Actioons")
            return

        print("Using local Selenuim processing")
        chrome_options = Options()
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")

        remote_url = os.getenv("SELENIUM_REMOTE_URL")
        driver = None
        try:
            if remote_url:
                driver = Remote(command_executor=remote_url, options=chrome_options)
            else:
                # This will usually fail on PythonAnywhere — that’s expected
                driver = webdriver.Chrome(options=chrome_options)
        except Exception as e:
            raise Exception(
                f"Selenium not available locally: {e}\n"
                "Set USE_GITHUB_ACTIONS=true to run via GitHub Actions."
            )

        images: list[Image.Image] = []
        # loop through pages
        try:
            for i in range(1, pages + 1):
                # build url
                url = self.page_url(baseurl, i)
                print(f"Loading page {i}: {url}")
                driver.get(url)
                # accept cookies: first page only
                if i == 1:
                    self.accept_cookies_if_present(driver)

                # wait for any image, then allow lazy-loading to finish
                try:
                    WebDriverWait(driver, 8).until(
                        EC.presence_of_element_located((By.TAG_NAME, "img"))
                    )
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
            "lidl"
            if "lidl" in u
            else (
                "aldi_nord"
                if "aldi-nord" in u
                else (
                    "aldi_sued"
                    if "aldi-sued" in u
                    else "kaufland" if "kaufland" in u else "unknown"
                )
            )
        )
        today = datetime.date.today().strftime("%Y-%m-%d")
        filename = f"{market}_prospekt_{today}.pdf"
        title = f"{market.capitalize()} Prospekt vom {today}"
        # save in django model
        handzettel = Handzettel(supermarkt=market, titel=title)
        handzettel.datei.save(filename, ContentFile(pdf.read()))
        handzettel.save()

        print(f"PDF with {len(images)} pages saved as '{filename}'!")

        # Azure sharePoint upload
        print("uploading PDF to SharePoint")
        # Read sharePoint/Azure config from envt variables
        client_id = os.getenv("AZURE_CLIENT_ID")
        client_secret = os.getenv("AZURE_CLIENT_SECRET")
        tenant_id = os.getenv("AZURE_TENANT_ID")
        sharepoint_site = os.getenv("SHAREPOINT_SITE")
        sharepoint_folder = os.getenv("SHAREPOINT_FOLDER")

        def die(msg, *extra):
            print("ERROR", msg, *extra)
            return

        # ensure that all required config values are set
        if not all(
            [client_id, client_secret, tenant_id, sharepoint_site, sharepoint_folder]
        ):
            return die("ERROR: SahrePoint/Azure configuration missing!")

        # Use msal to get a token for the graph API
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
            print("OK: Got access token")
        except Exception as e:
            return die("MSAL token error", e)

        # Get sharePoint site ID
        try:
            site_info = requests.get(
                f"https://graph.microsoft.com/v1.0/sites/{sharepoint_site}",
                headers=headers,
            )
            print("get site:", site_info.status_code)
            if site_info.status_code != 200:
                print("GET site body:", site_info.text[:800])
                return die(
                    "cannot read site.Check sharepoint site and permissions",
                    site_info.text,
                )
            site_json = site_info.json()
            site_id = site_json.get("id")
            print("OK site id =", site_id)
            if not site_id:
                return die("site ID missing in response", site_json)

        except Exception as e:
            return die("site lookup failed", e)

        # 3) List drives (document libraries) in the site : Lists all document libraries on the site.Matches the first part of SHAREPOINT_FOLDER to the library name.
        try:
            drives_resp = requests.get(
                f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives",
                headers=headers,
            )
            print("get drives", drives_resp.status_code)
            if drives_resp.status_code != 200:
                return die("cannot list drives.Permissions missing!", drives_resp.text)
            drives = drives_resp.json().get("value", [])
            print("Available drives:", [d.get("name") for d in drives])
            if not drives:
                return die("No drives found on the site.")
            library_name = sharepoint_folder.split("/")[0].strip()
            drive = next((d for d in drives if d.get("name") == library_name), None)
            if not drive:
                print(
                    f"WARN: Library '{library_name}' not found. Using first drive as fallback."
                )
                drive = drives[0]
            drive_id = drive["id"]
            print("OK: Using drive:", drive.get("name"), drive_id)
        except Exception as e:
            return die("Drive lookup failed", e)

        # 4) Ensure subfolders exist
        def ensure_folder_path(drive_id: str, folder_path: str):
            parts = [p for p in folder_path.split("/") if p.strip()]
            parent_path = ""
            for part in parts:
                parent_path = f"{parent_path}/{part}" if parent_path else part
                # Check if folder exists
                r = requests.get(
                    f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{parent_path}",
                    headers=headers,
                )
                if r.status_code == 404:
                    # Create folder
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
                    print(f"CREATE folder '{part}':", cr.status_code)
                    if cr.status_code not in (200, 201):
                        raise RuntimeError(
                            f"Failed to create '{part}': {cr.status_code} {cr.text}"
                        )
                elif r.status_code != 200:
                    raise RuntimeError(
                        f"Folder check failed for '{parent_path}': {r.status_code} {r.text}"
                    )

        subpath = "/".join(sharepoint_folder.split("/")[1:])
        try:
            if subpath:
                ensure_folder_path(drive_id, subpath)
                print("OK: Folder path ensured:", subpath)
        except Exception as e:
            return die("Creating/checking folder path failed", e)

        # Upload the PDF to the correct sharePoint folder via the Graph API
        try:
            upload_path = f"{sharepoint_folder}/{filename}"
            upload_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{upload_path}:/content"

            pdf_content = handzettel.datei.read()
            resp = requests.put(upload_url, headers=headers, data=pdf_content)
            print("put upload status:", resp.status_code)
            if resp.status_code in (200, 201):
                print("PDF successfully uploaded to SharePoint!")
            else:
                return die(
                    "Error uploading to SharePoint:", resp.status_code, resp.text
                )
        except Exception as e:
            return die("Upload exception", e)
