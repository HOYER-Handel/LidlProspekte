from django.db import models

class Handzettel(models.Model):
    supermarkt = models.CharField(max_length=30)
    titel = models.CharField(max_length=100)
    datei = models.FileField(upload_to='handzettel/')
    erstellt_am = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.supermarkt} - {self.titel}"
