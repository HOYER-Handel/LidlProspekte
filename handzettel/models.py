from django.db import models


class Handzettel(models.Model):
    SUPERMARKT_CHOICES = [
        ("lidl", "LIDL"),
        ("aldi_sued", "ALDI SÜD"),
        ("aldi_nord", "ALDI_NORD"),
    ]
    supermarkt = models.CharField(max_length=30, choices=SUPERMARKT_CHOICES)
    titel = models.CharField(max_length=100)
    datei = models.FileField(upload_to="handzettel/")
    erstellt_am = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.supermarkt} - {self.titel}"
