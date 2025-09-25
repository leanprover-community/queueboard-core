"""URL configuration for the API app."""

from django.urls import path
from . import views

urlpatterns: list = [path("", views.index, name="index")]
