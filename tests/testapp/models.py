"""Minimal Django models for baldur integration tests.

Replaces shopping.models (Order, User) so baldur tests
can run without host-app dependencies.
"""

from django.contrib.auth.models import AbstractUser
from django.db import models


class TestUser(AbstractUser):
    """Test-only User model (replaces shopping.User)."""

    class Meta:
        app_label = "testapp"


class TestOrder(models.Model):
    """Test-only Order model (replaces shopping.Order)."""

    user = models.ForeignKey(TestUser, on_delete=models.CASCADE)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "testapp"
