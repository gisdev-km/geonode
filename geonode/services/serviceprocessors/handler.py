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

"""Remote service handling base classes and helpers."""

import logging

from django.utils.datastructures import OrderedDict

from .. import enumerations
from .wms import WmsServiceHandler
from .mapserver import MapserverServiceHandler

logger = logging.getLogger(__name__)


def get_service_handler(base_url, service_type=enumerations.AUTO):
    """Return the appropriate remote service handler for the input URL.

    If the service type is not explicitly passed in it will be guessed from

    """

    handlers = OrderedDict({
        enumerations.WMS: {"OWS": True, "handler": WmsServiceHandler},
        # enumerations.WFS: {"OWS": True, "handler": ServiceHandlerBase},
        # enumerations.TMS: {"OWS": False, "handler": ServiceHandlerBase},
        enumerations.REST: {"OWS": False, "handler": MapserverServiceHandler},
        # enumerations.CSW: {"OWS": False, "handler": ServiceHandlerBase},
        # enumerations.HGL: {"OWS": True, "handler": ServiceHandlerBase},  # TODO: verify this
        # enumerations.OGP: {"OWS": False, "handler": ServiceHandlerBase},  # TODO: verify this
    })
    if service_type in (enumerations.AUTO, enumerations.OWS):
        if service_type == enumerations.AUTO:
            to_check = handlers.keys()
        else:
            to_check = [k for k, v in handlers.items() if v["OWS"]]
        for type_ in to_check:
            logger.info("Checking {}...".format(type_))
            try:
                service = get_service_handler(base_url, type_)
            except Exception:
                pass  # move on to the next service type
            else:
                break
        else:
            raise RuntimeError("Could not parse service {!r} with any of the "
                               "available service handlers".format(base_url))
    else:
        handler = handlers.get(service_type, {}).get("handler")
        try:
            service = handler(base_url)
        except Exception:
            logger.exception(
                msg="Could not parse service {!r}".format(base_url))
            raise
    return service
