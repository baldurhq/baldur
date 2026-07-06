"""URL configuration for the Baldur Django quickstart."""

from __future__ import annotations

from django.urls import path
from views import demo

urlpatterns = [
    path("demo/", demo),
]
