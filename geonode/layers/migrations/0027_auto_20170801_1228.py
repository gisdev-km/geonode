# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('layers', '26_to_27'),
    ]

    operations = [
        migrations.AlterField(
            model_name='layer',
            name='abstract_en',
            field=models.TextField(help_text='brief narrative summary of the content of the resource(s)', max_length=2000, null=True, verbose_name='abstract', blank=True),
        ),
        migrations.AlterField(
            model_name='layer',
            name='data_quality_statement_en',
            field=models.TextField(help_text="general explanation of the data producer's knowledge about the lineage of a dataset", max_length=2000, null=True, verbose_name='data quality statement', blank=True),
        ),
        migrations.AlterField(
            model_name='layer',
            name='purpose_en',
            field=models.TextField(help_text='summary of the intentions with which the resource(s) was developed', max_length=500, null=True, verbose_name='purpose', blank=True),
        ),
        migrations.AlterField(
            model_name='layer',
            name='supplemental_information_en',
            field=models.TextField(default='No information provided', max_length=2000, null=True, verbose_name='supplemental information', help_text='any other descriptive information about the dataset'),
        ),
    ]