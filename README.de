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
   cd LidlProspekte

2. **Install dependencies**
pip install Django requests beautifulsoup4 selenium pillow

3. **Run migrations and start server**
python manage.py makemigrations
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver


4. **Download and save a brochure as PDF**
python manage.py download_lidl_prospekt "URL" PAGES
