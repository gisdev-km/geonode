# -*- coding: utf-8 -*-
#########################################################################
#
# Copyright (C) 2017 OSGeo
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################

from django import forms


GEOMETRY_TYPES = (
    ('Point', 'Points'),
    ('LineString', 'Lines'),
    ('Polygon', 'Polygons'),
)


class NewLayerForm(forms.Form):
    """
    A form to create an empty layer in PostGIS.
    """
    name = forms.CharField(label='Layer name', max_length=255)
    title = forms.CharField(label='Layer title', max_length=255)
    geometry_type = forms.ChoiceField(choices=GEOMETRY_TYPES)

    permissions = forms.CharField(
        widget=forms.HiddenInput(
            attrs={
                'name': 'permissions',
                'id': 'permissions'}),
        required=True)

    attributes = forms.CharField(
        widget=forms.HiddenInput(
            attrs={
                'name': 'attributes',
                'id': 'attributes'}),
        required=True)


    def clean_title(self):
        data = self.cleaned_data['title']

        return data.title()