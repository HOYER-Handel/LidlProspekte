# LidlProspekte

**LidlProspekte** is a Django-based tool for automated downloading, PDF conversion, and management of digital brochures (e.g., Lidl, Aldi, Kaufland). Brochure images are dynamically extracted using Selenium, compiled into a PDF, and stored in a Django model for easy access and management in the Django admin interface.

---

## Features

- Automated download of multi-page digital brochures
- Dynamic image extraction using Selenium (handles JavaScript-rendered content)
- PDF creation from brochure images
- Brochure management via Django admin

---

## Requirements

- Python 3.x
- Django 3.x or higher
- Google Chrome or Firefox (for Selenium WebDriver)

---

## Installation & Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/HOYER-Handel/LidlProspekte.git


2. **Install dependencies**

   ```bash
   pip install Django
   pip install selenium

3. **Run migrations and create superuser**

   ```bash
    python manage.py makemigrations
    python manage.py migrate
    python manage.py createsuperuser
   


4. **Download and save a brochure as PDF**
   ```bash
   python manage.py download_lidl_prospekt "URL" PAGES

- Replace "BROCHURE_URL" with the desired brochure link.
- Replace PAGES with the number of brochure pages.


5. **Accessing the Admin Interface**

 Once the server is running, visit http://127.0.0.1:8000/admin/ in your browser and log in with your superuser credentials.

Here, brochures can be viewed, managed, and downloaded.
