"""Factory Boy factories for testapp models.

Mirrors shopping/tests/factories.py patterns so existing tests
can switch imports with minimal changes.
"""

from decimal import Decimal

import factory
from factory.django import DjangoModelFactory

from tests.testapp.models import TestOrder, TestUser

DEFAULT_PASSWORD = "testpass123"


class TestUserFactory(DjangoModelFactory):
    class Meta:
        model = TestUser
        django_get_or_create = ("username",)
        skip_postgeneration_save = True

    username = factory.Sequence(lambda n: f"testuser{n}")
    email = factory.LazyAttribute(lambda obj: f"{obj.username}@test.com")
    is_active = True

    @factory.post_generation
    def password(obj, create, extracted, **kwargs):
        if not create:
            return
        obj.set_password(extracted or DEFAULT_PASSWORD)
        obj.save()


class TestOrderFactory(DjangoModelFactory):
    class Meta:
        model = TestOrder

    user = factory.SubFactory(TestUserFactory)
    total_amount = Decimal("10000.00")
    status = "pending"
