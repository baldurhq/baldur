from django.db import migrations


class Migration(migrations.Migration):
    """
    Drop the CascadeEventArchive model.

    The model backed a cascade-event archive repository whose only writer was
    a scheduled task that was never registered with Celery and never ran, and
    which no read path ever queried. Both are gone, so the table holds no data
    any deployment produced.

    Indexes are removed before the model so the operation is explicit rather
    than relying on cascade behavior in the backend.
    """

    dependencies = [
        ("baldur", "0002_add_dlq_and_security_models"),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name="cascadeeventarchive",
            name="idx_cascade_ns_ts",
        ),
        migrations.RemoveIndex(
            model_name="cascadeeventarchive",
            name="idx_cascade_trigger",
        ),
        migrations.RemoveIndex(
            model_name="cascadeeventarchive",
            name="idx_cascade_hash",
        ),
        migrations.RemoveIndex(
            model_name="cascadeeventarchive",
            name="idx_cascade_test_ts",
        ),
        migrations.DeleteModel(
            name="CascadeEventArchive",
        ),
    ]
