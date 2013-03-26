from geonode.core.models import AUTHENTICATED_USERS, ANONYMOUS_USERS
from geonode.maps.models import Map, Layer, MapLayer, Contact, ContactRole,Role, get_csw, Thumbnail
from geonode.maps.gs_helpers import fixup_style, cascading_delete, delete_from_postgis
from geonode import geonetwork
import geoserver
from geoserver.resource import FeatureType, Coverage
import base64
from django import forms
from django.contrib.auth import authenticate, get_backends as get_auth_backends
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist
from django.core.urlresolvers import reverse
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render_to_response, get_object_or_404
from django.conf import settings
from django.template import RequestContext, loader
from django.utils.translation import ugettext as _
from django.views.decorators.cache import never_cache
from django.core.cache import cache
from django.utils import simplejson as json
import math
import httplib2 
from owslib.csw import CswRecord, namespaces
from owslib.util import nspath
import re
from urllib import urlencode
from urlparse import urlparse
import uuid
import unicodedata
from django.forms.models import inlineformset_factory
from django.db.models import Q
import logging
import traceback    
from django.utils.html import escape
import taggit
from geonode.maps.utils import forward_mercator

logger = logging.getLogger("geonode.maps.views")

_user, _password = settings.GEOSERVER_CREDENTIALS

DEFAULT_TITLE = ""
DEFAULT_ABSTRACT = ""


def default_map_config(request):
    _DEFAULT_MAP_CENTER = forward_mercator(settings.DEFAULT_MAP_CENTER)

    _default_map = Map(
        title=DEFAULT_TITLE, 
        abstract=DEFAULT_ABSTRACT,
        projection="EPSG:900913",
        center_x=_DEFAULT_MAP_CENTER[0],
        center_y=_DEFAULT_MAP_CENTER[1],
        zoom=settings.DEFAULT_MAP_ZOOM
    )
    def _baselayer(lyr, order):
        return MapLayer.objects.from_viewer_config(
            map = _default_map,
            layer = lyr,
            source = lyr["source"],
            ordering = order
        )

    DEFAULT_BASE_LAYERS = [_baselayer(lyr, ord) for ord, lyr in enumerate(settings.MAP_BASELAYERS)]
    auth = request and request.user.is_authenticated() or False
    DEFAULT_MAP_CONFIG = _default_map.viewer_json(added_layers=DEFAULT_BASE_LAYERS, authenticated=auth)

    return DEFAULT_MAP_CONFIG, DEFAULT_BASE_LAYERS



def bbox_to_wkt(x0, x1, y0, y1, srid="4326"):
    return 'SRID='+srid+';POLYGON(('+x0+' '+y0+','+x0+' '+y1+','+x1+' '+y1+','+x1+' '+y0+','+x0+' '+y0+'))'
class ContactForm(forms.ModelForm):
    keywords = taggit.forms.TagField()
    class Meta:
        model = Contact
        exclude = ('user',)

class LayerForm(forms.ModelForm):
    date = forms.DateTimeField(widget=forms.SplitDateTimeWidget)
    date.widget.widgets[0].attrs = {"class":"date"}
    date.widget.widgets[1].attrs = {"class":"time"}
    temporal_extent_start = forms.DateField(required=False,widget=forms.DateInput(attrs={"class":"date"}))
    temporal_extent_end = forms.DateField(required=False,widget=forms.DateInput(attrs={"class":"date"}))
    
    poc = forms.ModelChoiceField(empty_label = "Person outside GeoNode (fill form)",
                                 label = "Point Of Contact", required=False,
                                 queryset = Contact.objects.exclude(user=None))

    metadata_author = forms.ModelChoiceField(empty_label = "Person outside GeoNode (fill form)",
                                             label = "Metadata Author", required=False,
                                             queryset = Contact.objects.exclude(user=None))
    keywords = taggit.forms.TagField(required=False)
    abstract = forms.CharField(required=False)

    class Meta:
        model = Layer
        exclude = ('contacts','workspace', 'store', 'name', 'uuid', 'storeType', 'typename')

class RoleForm(forms.ModelForm):
    class Meta:
        model = ContactRole
        exclude = ('contact', 'layer')

class PocForm(forms.Form):
    contact = forms.ModelChoiceField(label = "New point of contact",
                                     queryset = Contact.objects.exclude(user=None))


class MapForm(forms.ModelForm):
    keywords = taggit.forms.TagField(required=False)
    abstract = forms.CharField(required=False)
    class Meta:
        model = Map
        exclude = ('contact', 'zoom', 'projection', 'center_x', 'center_y', 'owner', 'portal_params', 'tools_params')
        widgets = {
            'abstract': forms.Textarea(attrs={'cols': 40, 'rows': 10}),
        }



MAP_LEV_NAMES = {
    Map.LEVEL_NONE  : _('No Permissions'),
    Map.LEVEL_READ  : _('Read Only'),
    Map.LEVEL_WRITE : _('Read/Write'),
    Map.LEVEL_ADMIN : _('Administrative')
}
LAYER_LEV_NAMES = {
    Layer.LEVEL_NONE  : _('No Permissions'),
    Layer.LEVEL_READ  : _('Read Only'),
    Layer.LEVEL_WRITE : _('Read/Write'),
    Layer.LEVEL_ADMIN : _('Administrative')
}

def maps(request, mapid=None):
    if request.method == 'GET':
        return render_to_response('maps.html', RequestContext(request))
    elif request.method == 'POST':
        if not request.user.is_authenticated():
            return HttpResponse(
                'You must be logged in to save new maps',
                mimetype="text/plain",
                status=401
            )
        else:
            try:
                map = Map(owner=request.user, zoom=0, center_x=0, center_y=0)
                map.save()
                map.set_default_permissions()
                map.update_from_viewer(request.raw_post_data)
                response = HttpResponse('', status=201)
                response['Location'] = map.id
                return response
            except json.JSONDecodeError:
                return HttpResponse(status=400)

def mapJSON(request, mapid):
    if request.method == 'GET':
        map = get_object_or_404(Map,pk=mapid) 
        if not request.user.has_perm('maps.view_map', obj=map):
            return HttpResponse(loader.render_to_string('401.html', 
                RequestContext(request, {})), status=401)
    	return HttpResponse(json.dumps(map.viewer_json(authenticated=request.user.is_authenticated())))
    elif request.method == 'PUT':
        if not request.user.is_authenticated():
            return HttpResponse(
                _("You must be logged in to save this map"),
                status=401,
                mimetype="text/plain"
            )
        map = get_object_or_404(Map, pk=mapid)
        if not request.user.has_perm('maps.change_map', obj=map):
            return HttpResponse("You are not allowed to modify this map.", status=403)
        try:
            map.update_from_viewer(request.raw_post_data)

            return HttpResponse(
                "Map successfully updated.", 
                mimetype="text/plain",
                status=204
            )
        except Exception, e:
            return HttpResponse(
                "The server could not understand the request." + str(e),
                mimetype="text/plain",
                status=400
            )

def newmap_config(request):
    '''
    View that creates a new map.  
    
    If the query argument 'copy' is given, the inital map is
    a copy of the map with the id specified, otherwise the 
    default map configuration is used.  If copy is specified
    and the map specified does not exist a 404 is returned.
    '''
    DEFAULT_MAP_CONFIG, DEFAULT_BASE_LAYERS = default_map_config(request)

    if request.method == 'GET' and 'copy' in request.GET:
        mapid = request.GET['copy']
        map = get_object_or_404(Map,pk=mapid)
        
        if not request.user.has_perm('maps.view_map', obj=map):
            return HttpResponse(loader.render_to_string('401.html', 
                RequestContext(request, {'error_message': 
                    _("You are not permitted to view or copy this map.")})), status=401)

        map.abstract = DEFAULT_ABSTRACT
        map.title = DEFAULT_TITLE
        if request.user.is_authenticated(): map.owner = request.user
        config = map.viewer_json(authenticated=request.user.is_authenticated())
        del config['id']
    else:
        if request.method == 'GET':
            params = request.GET
        elif request.method == 'POST':
            params = request.POST
        else:
            return HttpResponse(status=405)
        
        if 'layer' in params:
            bbox = None
            map = Map(projection="EPSG:900913")
            layers = []
            for layer_name in params.getlist('layer'):
                try:
                    layer = Layer.objects.get(typename=layer_name)
                except ObjectDoesNotExist:
                    # bad layer, skip 
                    continue

                if not request.user.has_perm('maps.view_layer', obj=layer):
                    # invisible layer, skip inclusion
                    continue
                    
                layer_bbox = layer.resource.latlon_bbox
                # assert False, str(layer_bbox)
                if bbox is None and layer_bbox:
                    bbox = list(layer_bbox[0:4])
                else:
                    bbox[0] = min(bbox[0], layer_bbox[0])
                    bbox[1] = max(bbox[1], layer_bbox[1])
                    bbox[2] = min(bbox[2], layer_bbox[2])
                    bbox[3] = max(bbox[3], layer_bbox[3])
                
                layers.append(MapLayer(
                    map = map,
                    name = layer.typename,
                    ows_url = settings.GEOSERVER_BASE_URL + "wms",
                    visibility = True
                ))

            if bbox is not None:
                minx, maxx, miny, maxy = [float(c) for c in bbox]
                x = (minx + maxx) / 2
                y = (miny + maxy) / 2

                center = forward_mercator((x, y))
                if center[1] == float('-inf'):
                    center = (center[0], 0)

                if maxx == minx:
                    width_zoom = 15
                else:
                    width_zoom = math.log(360 / (maxx - minx), 2)
                if maxy == miny:
                    height_zoom = 15
                else:
                    height_zoom = math.log(360 / (maxy - miny), 2)

                map.center_x = center[0]
                map.center_y = center[1]
                map.zoom = math.ceil(min(width_zoom, height_zoom))

            
            config = map.viewer_json(added_layers=(DEFAULT_BASE_LAYERS + layers), authenticated=request.user.is_authenticated())
            config['fromLayer'] = True
        else:
            config = DEFAULT_MAP_CONFIG
    return json.dumps(config)

def newmap(request):
    config = newmap_config(request)
    if isinstance(config, HttpResponse):
        return config
    else:
        return render_to_response('maps/view.html', RequestContext(request))

def newmapJSON(request):
    config = newmap_config(request)
    if isinstance(config, HttpResponse):
        return config
    else:
        return HttpResponse(config)

h = httplib2.Http()
h.add_credentials(_user, _password)
h.add_credentials(_user, _password)
_netloc = urlparse(settings.GEOSERVER_BASE_URL).netloc
h.authorizations.append(
    httplib2.BasicAuthentication(
        (_user, _password), 
        _netloc,
        settings.GEOSERVER_BASE_URL,
        {},
        None,
        None, 
        h
    )
)


@login_required
def map_download(request, mapid):
    """ 
    Download all the layers of a map as a batch
    XXX To do, remove layer status once progress id done 
    This should be fix because 
    """ 
    mapObject = get_object_or_404(Map,pk=mapid)
    if not request.user.has_perm('maps.view_map', obj=mapObject):
        return HttpResponse(_('Not Permitted'), status=401)

    map_status = dict()
    if request.method == 'POST': 
        url = "%srest/process/batchDownload/launch/" % settings.GEOSERVER_BASE_URL

        def perm_filter(layer):
            return request.user.has_perm('maps.view_layer', obj=layer)

        mapJson = mapObject.json(perm_filter)

        resp, content = h.request(url, 'POST', body=mapJson)

        if resp.status not in (400, 404, 417):
            map_status = json.loads(content)
            request.session["map_status"] = map_status
        else: 
            pass # XXX fix

    if request.method == 'GET':
        if "map_status" in request.session and type(request.session["map_status"]) == dict:
            msg = "You already started downloading a map"
        else: 
            msg = "You should download a map" 

    locked_layers = []
    remote_layers = []
    downloadable_layers = []

    for lyr in mapObject.layer_set.all():
        if lyr.group != "background":
            if not lyr.local():
                remote_layers.append(lyr)
            else:
                ownable_layer = Layer.objects.get(typename=lyr.name)
                if not request.user.has_perm('maps.view_layer', obj=ownable_layer):
                    locked_layers.append(lyr)
                else:
                    downloadable_layers.append(lyr)

    return render_to_response('maps/download.html', RequestContext(request, {
         "map_status" : map_status,
         "map" : mapObject,
         "locked_layers": locked_layers,
         "remote_layers": remote_layers,
         "downloadable_layers": downloadable_layers,
         "geoserver" : settings.GEOSERVER_BASE_URL,
         "site" : settings.SITEURL
    }))
    

def check_download(request):
    """
    this is an endpoint for monitoring map downloads
    """
    try:
        layer = request.session["map_status"] 
        if type(layer) == dict:
            url = "%srest/process/batchDownload/status/%s" % (settings.GEOSERVER_BASE_URL,layer["id"])
            resp,content = h.request(url,'GET')
            status= resp.status
            if resp.status == 400:
                return HttpResponse(content="Something went wrong",status=status)
        else: 
            content = "Something Went wrong" 
            status  = 400 
    except ValueError:
        # TODO: Is there any useful context we could include in this log?
        logger.warn("User tried to check status, but has no download in progress.")
    return HttpResponse(content=content,status=status)


def batch_layer_download(request):
    """
    batch download a set of layers
    
    POST - begin download
    GET?id=<download_id> monitor status
    """

    # currently this just piggy-backs on the map download backend 
    # by specifying an ad hoc map that contains all layers requested
    # for download. assumes all layers are hosted locally.
    # status monitoring is handled slightly differently.
    
    if request.method == 'POST':
        layers = request.POST.getlist("layer")
        layers = Layer.objects.filter(typename__in=list(layers))

        def layer_son(layer):
            return {
                "name" : layer.typename, 
                "service" : layer.service_type, 
                "metadataURL" : "",
                "serviceURL" : ""
            } 

        readme = """This data is provided by GeoNode.

Contents:
"""
        def list_item(lyr):
            return "%s - %s.*" % (lyr.title, lyr.name)

        readme = "\n".join([readme] + [list_item(l) for l in layers])

        fake_map = {
            "map": { "readme": readme },
            "layers" : [layer_son(lyr) for lyr in layers]
        }

        url = "%srest/process/batchDownload/launch/" % settings.GEOSERVER_BASE_URL
        resp, content = h.request(url,'POST',body=json.dumps(fake_map))
        return HttpResponse(content, status=resp.status)

    
    if request.method == 'GET':
        # essentially, this just proxies back to geoserver
        download_id = request.GET.get('id', None)
        if download_id is None:
            return HttpResponse(status=404)

        url = "%srest/process/batchDownload/status/%s" % (settings.GEOSERVER_BASE_URL, download_id)
        resp,content = h.request(url,'GET')
        return HttpResponse(content, status=resp.status)

def set_layer_permissions(layer, perm_spec):
    if "authenticated" in perm_spec:
        layer.set_gen_level(AUTHENTICATED_USERS, perm_spec['authenticated'])
    if "anonymous" in perm_spec:
        layer.set_gen_level(ANONYMOUS_USERS, perm_spec['anonymous'])
    users = [n for (n, p) in perm_spec['users']]
    layer.get_user_levels().exclude(user__username__in = users + [layer.owner]).delete()
    for username, level in perm_spec['users']:
        user = User.objects.get(username=username)
        layer.set_user_level(user, level)

def set_map_permissions(m, perm_spec):
    if "authenticated" in perm_spec:
        m.set_gen_level(AUTHENTICATED_USERS, perm_spec['authenticated'])
    if "anonymous" in perm_spec:
        m.set_gen_level(ANONYMOUS_USERS, perm_spec['anonymous'])
    users = [n for (n, p) in perm_spec['users']]
    m.get_user_levels().exclude(user__username__in = users + [m.owner]).delete()
    for username, level in perm_spec['users']:
        user = User.objects.get(username=username)
        m.set_user_level(user, level)

def ajax_layer_permissions(request, layername):
    layer = get_object_or_404(Layer, typename=layername)

    if not request.method == 'POST':
        return HttpResponse(
            'You must use POST for editing layer permissions',
            status=405,
            mimetype='text/plain'
        )

    if not request.user.has_perm("maps.change_layer_permissions", obj=layer):
        return HttpResponse(
            'You are not allowed to change permissions for this layer',
            status=401,
            mimetype='text/plain'
        )

    permission_spec = json.loads(request.raw_post_data)
    set_layer_permissions(layer, permission_spec)

    return HttpResponse(
        "Permissions updated",
        status=200,
        mimetype='text/plain'
    )

def ajax_map_permissions(request, mapid):
    map = get_object_or_404(Map, pk=mapid)

    if not request.user.has_perm("maps.change_map_permissions", obj=map):
        return HttpResponse(
            'You are not allowed to change permissions for this map',
            status=401,
            mimetype='text/plain'
        )

    if not request.method == 'POST':
        return HttpResponse(
            'You must use POST for editing map permissions',
            status=405,
            mimetype='text/plain'
        )

    spec = json.loads(request.raw_post_data)
    set_map_permissions(map, spec)

    # _perms = {
    #     Layer.LEVEL_READ: Map.LEVEL_READ,
    #     Layer.LEVEL_WRITE: Map.LEVEL_WRITE,
    #     Layer.LEVEL_ADMIN: Map.LEVEL_ADMIN,
    # }

    # def perms(x):
    #     return _perms.get(x, Map.LEVEL_NONE)

    # if "anonymous" in spec:
    #     map.set_gen_level(ANONYMOUS_USERS, perms(spec['anonymous']))
    # if "authenticated" in spec:
    #     map.set_gen_level(AUTHENTICATED_USERS, perms(spec['authenticated']))
    # users = [n for (n, p) in spec["users"]]
    # map.get_user_levels().exclude(user__username__in = users + [map.owner]).delete()
    # for username, level in spec['users']:
    #     user = User.objects.get(username = username)
    #     map.set_user_level(user, perms(level))

    return HttpResponse(
        "Permissions updated",
        status=200,
        mimetype='text/plain'
    )


@login_required
def deletemap(request, mapid):
    ''' Delete a map, and its constituent layers. '''
    map = get_object_or_404(Map,pk=mapid) 

    if not request.user.has_perm('maps.delete_map', obj=map):
        return HttpResponse(loader.render_to_string('401.html', 
            RequestContext(request, {'error_message': 
                _("You are not permitted to delete this map.")})), status=401)

    if request.method == 'GET':
        return render_to_response("maps/map_remove.html", RequestContext(request, {
            "map": map
        }))
    elif request.method == 'POST':
        layers = map.layer_set.all()
        for layer in layers:
            layer.delete()
        map.delete()

        return HttpResponseRedirect(reverse("geonode.maps.views.maps"))

def mapdetail(request,mapid): 
    '''
    The view that show details of each map
    '''
    map = get_object_or_404(Map,pk=mapid)
    if not request.user.has_perm('maps.view_map', obj=map):
        return HttpResponse(loader.render_to_string('401.html', 
            RequestContext(request, {'error_message': 
                _("You are not allowed to view this map.")})), status=401)
     
    config = map.viewer_json(authenticated=request.user.is_authenticated())
    #config["tools"] = False;
    config = json.dumps(config)
    # build unique set based on name and ows_url
    layers = {}
    for layer in MapLayer.objects.filter(map=map.id):
        layers[(layer.ows_url,layer.name)] = layer
    
    return render_to_response("maps/mapinfo.html", RequestContext(request, {
        'config': config, 
        'map': map,
        'layers': layers.values(),
        'permissions_json': json.dumps(_perms_info(map, MAP_LEV_NAMES))
    }))

@login_required
def describemap(request, mapid):
    '''
    The view that displays a form for
    editing map metadata
    '''
    map = get_object_or_404(Map,pk=mapid) 
    if not request.user.has_perm('maps.change_map', obj=map):
        return HttpResponse(loader.render_to_string('401.html', 
                            RequestContext(request, {'error_message': 
                            _("You are not allowed to modify this map's metadata.")})),
                            status=401)

    if request.method == "POST":
        # Change metadata, return to map info page
        map_form = MapForm(request.POST, instance=map, prefix="map")
        if map_form.is_valid():
            map = map_form.save(commit=False)
            if map_form.cleaned_data["keywords"]:
                map.keywords.add(*map_form.cleaned_data["keywords"])
            else:
                map.keywords.clear()
            map.save()

            return HttpResponseRedirect(reverse('geonode.maps.views.map_controller', args=(map.id,)))
    else:
        # Show form
        map_form = MapForm(instance=map, prefix="map")

    return render_to_response("maps/map_describe.html", RequestContext(request, {
        "map": map,
        "map_form": map_form
    }))


def map_controller(request, mapid):
    '''
    main view for map resources, dispatches to correct 
    view based on method and query args. 
    '''
    if 'remove' in request.GET: 
        return deletemap(request, mapid)
    if 'describe' in request.GET:
        return describemap(request, mapid)
    if 'thumbnail' in request.GET:
        return _handleThumbNail(request, Map.objects.get(pk=mapid))
    else:
        return mapdetail(request, mapid)

def view(request, mapid):
    """  
    The view that returns the map composer opened to
    the map with the given map ID.
    """
    map = get_object_or_404(Map, pk=mapid)
    if not request.user.has_perm('maps.view_map', obj=map):
        return HttpResponse(loader.render_to_string('401.html', 
            RequestContext(request, {'error_message': 
                _("You are not allowed to view this map.")})), status=401)    
    
    return render_to_response('maps/view.html', RequestContext(request,{
        'map' : map
    }))

def embed(request, mapid=None):
    if mapid is None:
        DEFAULT_MAP_CONFIG, DEFAULT_BASE_LAYERS = default_map_config(request)
        config = DEFAULT_MAP_CONFIG
    else:
        map = get_object_or_404(Map, pk=mapid)
        if not request.user.has_perm('maps.view_map', obj=map):
            return HttpResponse(_("Not Permitted"), status=401, mimetype="text/plain")
        
        config = map.viewer_json(authenticated=request.user.is_authenticated())
    return render_to_response('maps/embed.html', RequestContext(request, {
        'config': json.dumps(config),
        'map' : map
    }))


def data(request):
    return render_to_response('data.html', RequestContext(request, {
        'GEOSERVER_BASE_URL':settings.GEOSERVER_BASE_URL
    }))

def view_js(request, mapid):
    map = Map.objects.get(pk=mapid)
    if not request.user.has_perm('maps.view_map', obj=map):
        return HttpResponse(_("Not Permitted"), status=401, mimetype="text/plain")
    config = map.viewer_json(authenticated=request.user.is_authenticated())
    return HttpResponse(json.dumps(config), mimetype="application/javascript")

def fixdate(str):
    return " ".join(str.split("T"))

class LayerDescriptionForm(forms.Form):
    title = forms.CharField(300)
    abstract = forms.CharField(1000, widget=forms.Textarea, required=False)
    keywords = taggit.forms.TagField(required=False)

@login_required
def layer_metadata(request, layername):
    layer = get_object_or_404(Layer, typename=layername)
    if request.user.is_authenticated():
        if not request.user.has_perm('maps.change_layer', obj=layer):
            return HttpResponse(loader.render_to_string('401.html', 
                RequestContext(request, {'error_message': 
                    _("You are not permitted to modify this layer's metadata")})), status=401)
        
        poc = layer.poc
        metadata_author = layer.metadata_author
        poc_role = ContactRole.objects.get(layer=layer, role=layer.poc_role)
        metadata_author_role = ContactRole.objects.get(layer=layer, role=layer.metadata_author_role)

        if request.method == "POST":
            layer_form = LayerForm(request.POST, instance=layer, prefix="layer")
        else:
            layer_form = LayerForm(instance=layer, prefix="layer")

        if request.method == "POST" and layer_form.is_valid():
            new_poc = layer_form.cleaned_data['poc']
            new_author = layer_form.cleaned_data['metadata_author']
            new_keywords = layer_form.cleaned_data['keywords']

            if new_poc is None:
                poc_form = ContactForm(request.POST, prefix="poc")
                if poc_form.has_changed and poc_form.is_valid():
                    new_poc = poc_form.save()

            if new_author is None:
                author_form = ContactForm(request.POST, prefix="author")
                if author_form.has_changed and author_form.is_valid():
                    new_author = author_form.save()

            if new_poc is not None and new_author is not None:
                the_layer = layer_form.save(commit=False)
                the_layer.poc = new_poc
                the_layer.metadata_author = new_author
                the_layer.keywords.add(*new_keywords)
                the_layer.save()
                return HttpResponseRedirect("/data/" + layer.typename)

        if poc.user is None:
            poc_form = ContactForm(instance=poc, prefix="poc")
        else:
            layer_form.fields['poc'].initial = poc.id
            poc_form = ContactForm(prefix="poc")
            poc_form.hidden=True

        if metadata_author.user is None:
            author_form = ContactForm(instance=metadata_author, prefix="author")
        else:
            layer_form.fields['metadata_author'].initial = metadata_author.id
            author_form = ContactForm(prefix="author")
            author_form.hidden=True

        return render_to_response("maps/layer_describe.html", RequestContext(request, {
            "layer": layer,
            "layer_form": layer_form,
            "poc_form": poc_form,
            "author_form": author_form,
        }))
    else: 
        return HttpResponse("Not allowed", status=403)

def layer_remove(request, layername):
    layer = get_object_or_404(Layer, typename=layername)
    if request.user.is_authenticated():
        if not request.user.has_perm('maps.delete_layer', obj=layer):
            return HttpResponse(loader.render_to_string('401.html', 
                RequestContext(request, {'error_message': 
                    _("You are not permitted to delete this layer")})), status=401)
        
        if (request.method == 'GET'):
            return render_to_response('maps/layer_remove.html',RequestContext(request, {
                "layer": layer
            }))
        if (request.method == 'POST'):
            layer.delete()
            return HttpResponseRedirect(reverse("data_home"))
        else:
            return HttpResponse("Not allowed",status=403) 
    else:  
        return HttpResponse("Not allowed",status=403)

def layer_style(request, layername):
    layer = get_object_or_404(Layer, typename=layername)
    if request.user.is_authenticated():
        if not request.user.has_perm('maps.change_layer', obj=layer):
            return HttpResponse(loader.render_to_string('401.html', 
                RequestContext(request, {'error_message': 
                    _("You are not permitted to modify this layer")})), status=401)
        
        if (request.method == 'POST'):
            style_name = request.POST.get('defaultStyle')

            # would be nice to implement
            # better handling of default style switching
            # in layer model or deeper (gsconfig.py, REST API)

            old_default = layer.default_style
            if old_default.name == style_name:
                return HttpResponse("Default style for %s remains %s" % (layer.name, style_name), status=200)

            # This code assumes without checking
            # that the new default style name is included
            # in the list of possible styles.

            new_style = (style for style in layer.styles if style.name == style_name).next()

            layer.default_style = new_style
            layer.styles = [s for s in layer.styles if s.name != style_name] + [old_default]
            layer.save()
            return HttpResponse("Default style for %s changed to %s" % (layer.name, style_name),status=200)
        else:
            return HttpResponse("Not allowed",status=403)
    else:  
        return HttpResponse("Not allowed",status=403)
    
def layer_data(req, layername):
    if req.method != 'POST':
        return HttpResponse('Invalid Request', status = 400)
    layer = get_object_or_404(Layer, typename=layername)
    if not req.user.has_perm('maps.change_layer', obj=layer):
        return HttpResponse(loader.render_to_string('401.html',
            RequestContext(req, {'error_message':
                _("You are not permitted to modify this layer")})), status=401)
    json.loads(req.POST.raw_post_data)
    layer.map_config = req.POST.raw_post_data
    layer.save()
    return HttpResponse(status=204)
                

def layer_detail(request, layername):
    layer = get_object_or_404(Layer, typename=layername)
    if not request.user.has_perm('maps.view_layer', obj=layer):
        return HttpResponse(loader.render_to_string('401.html', 
            RequestContext(request, {'error_message': 
                _("You are not permitted to view this layer")})), status=401)
    
    metadata = None
    if settings.USE_GEONETWORK:
        metadata = layer.metadata_csw()
    
    map_config = layer.map_config
    
    if not map_config:
        vs_url = settings.GEOSERVER_BASE_URL + '%s/%s/wms' % tuple(layer.typename.split(':'))
        maplayer = MapLayer(name = layer.typename.split(':')[1], ows_url=vs_url)

        # center/zoom don't matter; the viewer will center on the layer bounds
        map = Map(projection="EPSG:900913")
        DEFAULT_BASE_LAYERS = default_map_config(request)[1]
        map_config = json.dumps(map.viewer_json(added_layers=(DEFAULT_BASE_LAYERS + [maplayer])))
        # we could save the config, but for now...

    return render_to_response('maps/layer.html', RequestContext(request, {
        "layer": layer,
        "metadata": metadata,
        "viewer": map_config,
        "permissions_json": _perms_info_json(layer, LAYER_LEV_NAMES),
        "GEOSERVER_BASE_URL": settings.GEOSERVER_BASE_URL
    }))
    
def layer_thumbnail(req, layername):
    return _handleThumbNail(req, get_object_or_404(Layer, typename=layername))

def _handleThumbNail(req, obj):
    # object will either be a map or a layer, one or the other permission must apply
    if not req.user.has_perm('maps.change_map', obj=obj) and not req.user.has_perm('maps.change_layer', obj=obj):
        return HttpResponse(loader.render_to_string('401.html',
            RequestContext(req, {'error_message':
                _("You are not permitted to modify this object")})), status=401)
    if req.method == 'GET':
        return HttpResponseRedirect(obj.get_thumbnail_url())
    elif req.method == 'POST':
        spec = _fixup_ows_url(req.raw_post_data)
        thumb = obj.set_thumbnail(spec)
        return HttpResponseRedirect(thumb.get_thumbnail_url())
    
def _fixup_ows_url(thumb_spec):
    #@HACK - for whatever reason, a map's maplayers ows_url contains only /geoserver/wms
    # so rendering of thumbnails fails - replace those uri's with full geoserver URL
    import re
    gspath = '"/geoserver/wms' # this should be in img src attributes
    repl = '"' + settings.GEOSERVER_BASE_URL + "/wms" 
    return re.sub(gspath, repl, thumb_spec)

GENERIC_UPLOAD_ERROR = _("There was an error while attempting to upload your data. \
Please try again, or contact and administrator if the problem continues.")

@login_required
def upload_layer(request):
    if request.method == 'GET':
        return render_to_response('maps/layer_upload.html',
                                  RequestContext(request, {}))
    elif request.method == 'POST':
        from geonode.maps.forms import NewLayerUploadForm
        from geonode.maps.utils import save
        from django.utils.html import escape
        import os, shutil
        form = NewLayerUploadForm(request.POST, request.FILES)
        tempdir = None
        if form.is_valid():
            try:
                tempdir, base_file = form.write_files()
                name, __ = os.path.splitext(form.cleaned_data["base_file"].name)
                saved_layer = save(name, base_file, request.user, 
                        overwrite = False,
                        abstract = form.cleaned_data["abstract"],
                        title = form.cleaned_data["layer_title"],
                        permissions = form.cleaned_data["permissions"]
                        )
                return HttpResponse(json.dumps({
                    "success": True,
                    "redirect_to": reverse('data_metadata', args=[saved_layer.typename])}))
            except Exception, e:
                logger.exception("Unexpected error during upload.")
                return HttpResponse(json.dumps({
                    "success": False,
                    "errors": ["Unexpected error during upload: " + escape(str(e))]}))
            finally:
                if tempdir is not None:
                    shutil.rmtree(tempdir)
    else:
        errors = []
        for e in form.errors.values():
            errors.extend([escape(v) for v in e])
        return HttpResponse(json.dumps({ "success": False, "errors": errors}))

@login_required
def layer_replace(request, layername):
    layer = get_object_or_404(Layer, typename=layername)
    if not request.user.has_perm('maps.change_layer', obj=layer):
        return HttpResponse(loader.render_to_string('401.html', 
            RequestContext(request, {'error_message': 
                _("You are not permitted to modify this layer")})), status=401)
    if request.method == 'GET':
        cat = Layer.objects.gs_catalog
        info = cat.get_resource(layer.name)
        is_featuretype = info.resource_type == FeatureType.resource_type
        
        return render_to_response('maps/layer_replace.html',
                                  RequestContext(request, {'layer': layer,
                                                           'is_featuretype': is_featuretype}))
    elif request.method == 'POST':
        from geonode.maps.forms import LayerUploadForm
        from geonode.maps.utils import save
        from django.utils.html import escape
        import os, shutil

        form = LayerUploadForm(request.POST, request.FILES)
        tempdir = None

        if form.is_valid():
            try:
                if settings.USE_UPLOADER:
                    from geonode.maps.upload import save
                    if 'import_session' in request.session:
                        del request.session['import_session']

                tempdir, base_file = form.write_files()
                name, __ = os.path.splitext(form.cleaned_data["base_file"].name)
                saved_layer = save(layer, base_file, request.user, overwrite=True)

                if settings.USE_UPLOADER:
                    return _uploader(request,saved_layer,form, base_file,update_mode="REPLACE",layer=layer)

                return HttpResponse(json.dumps({
                    "success": True,
                    "redirect_to": reverse('data_metadata', args=[saved_layer.typename])}))
            except Exception, e:
                logger.exception("Unexpected error during upload.")
                return HttpResponse(json.dumps({
                    "success": False,
                    "errors": ["Unexpected error during upload: " + escape(str(e))]}))
            finally:
                # can't cleanup files yet, multi request process
                if tempdir is not None and not settings.USE_UPLOADER:
                    shutil.rmtree(tempdir)

        else:
            errors = []
            for e in form.errors.values():
                errors.extend([escape(v) for v in e])
            return HttpResponse(json.dumps({ "success": False, "errors": errors}))

def json_response(body=None, errors=None, redirect_to=None, exception=None,
                  content_type=None):
   """Create a proper JSON response. If body is provided, this is the response.
   If errors is not None, the response is a success/errors json object.
   If redirect_to is not None, the response is a success=True, redirect_to object
   If the exception is provided, it will be logged. If body is a string, the
   exception message will be used as a format option to that string and the
   result will be a success=False, errors = body % exception
   """
   if content_type is None:
       content_type = "application/json"
   if errors:
       if isinstance(errors, basestring):
           errors = [errors]
       body = {
           'success' : False,
           'errors' : errors
       }
   elif redirect_to:
       body = {
           'success' : True,
           'redirect_to' : redirect_to
       }
   elif exception:
       if body is None:
           body = "Unexpected exception %s" % exception
       else:
           body = body % exception
       logger.warn(body)
       logger.warn(traceback.format_exc(exception))
       body = {
           'success' : False,
           'errors' : [ body ]
       }
   elif body:
       pass
   else:
       raise Exception("must call with body, errors or redirect_to")
 
   if not isinstance(body, basestring):
       body = json.dumps(body)
   return HttpResponse(body, content_type=content_type)

def _view_perms_context(obj, level_names):

    ctx =  obj.get_all_level_info()
    def lname(l):
        return level_names.get(l, _("???"))
    ctx[ANONYMOUS_USERS] = lname(ctx.get(ANONYMOUS_USERS, obj.LEVEL_NONE))
    ctx[AUTHENTICATED_USERS] = lname(ctx.get(AUTHENTICATED_USERS, obj.LEVEL_NONE))

    ulevs = []
    for u, l in ctx['users'].items():
        ulevs.append([u, lname(l)])
    ulevs.sort()
    ctx['users'] = ulevs

    return ctx

def _perms_info(obj, level_names):
    info = obj.get_all_level_info()
    # these are always specified even if none
    info[ANONYMOUS_USERS] = info.get(ANONYMOUS_USERS, obj.LEVEL_NONE)
    info[AUTHENTICATED_USERS] = info.get(AUTHENTICATED_USERS, obj.LEVEL_NONE)
    info['users'] = sorted(info['users'].items())
    info['levels'] = [(i, level_names[i]) for i in obj.permission_levels]
    if hasattr(obj, 'owner') and obj.owner is not None:
        info['owner'] = obj.owner.username
    return info
       

def _perms_info_json(obj, level_names):
    return json.dumps(_perms_info(obj, level_names))

def _fix_map_perms_for_editor(info):
    perms = {
        Map.LEVEL_READ: Layer.LEVEL_READ,
        Map.LEVEL_WRITE: Layer.LEVEL_WRITE,
        Map.LEVEL_ADMIN: Layer.LEVEL_ADMIN,
    }

    def fix(x): return perms.get(x, "_none")

    info[ANONYMOUS_USERS] = fix(info[ANONYMOUS_USERS])
    info[AUTHENTICATED_USERS] = fix(info[AUTHENTICATED_USERS])
    info['users'] = [(u, fix(level)) for u, level in info['users']]

    return info

INVALID_PERMISSION_MESSAGE = _("Invalid permission level.")
def _handle_perms_edit(request, obj):
    errors = []
    params = request.POST
    valid_pl = obj.permission_levels
    
    anon_level = params[ANONYMOUS_USERS]
    # validate anonymous level, disallow admin level
    if not anon_level in valid_pl or anon_level == obj.LEVEL_ADMIN:
        errors.append(_("Anonymous Users") + ": " + INVALID_PERMISSION_MESSAGE)
    
    all_auth_level = params[AUTHENTICATED_USERS]
    if not all_auth_level in valid_pl:
        errors.append(_("Registered Users") + ": " + INVALID_PERMISSION_MESSAGE)

    kpat = re.compile("^u_(.*)_level$")
    ulevs = {}
    for k, level in params.items(): 
        m = kpat.match(k)
        if m: 
            username = m.groups()[0]
            if not level in valid_pl:
                errors.append(_("User") + " " + username + ": " + INVALID_PERMISSION_MESSAGE)
            else:
                ulevs[username] = level

    if len(errors) == 0: 
        obj.set_gen_level(ANONYMOUS_USERS, anon_level)
        obj.set_gen_level(AUTHENTICATED_USERS, all_auth_level)
        
        for username, level in ulevs.items():
            user = User.objects.get(username=username)
            obj.set_user_level(user, level)

    return errors


def _get_basic_auth_info(request):
    """
    grab basic auth info
    """
    meth, auth = request.META['HTTP_AUTHORIZATION'].split()
    if meth.lower() != 'basic':
        raise ValueError
    username, password = base64.b64decode(auth).split(':')
    return username, password


def user_name(request):
    user = None
    geoserver = False
    superuser = False
    if 'HTTP_AUTHORIZATION' in request.META:
        username, password = _get_basic_auth_info(request)
        acl_user = authenticate(username=username, password=password)
        if acl_user:
            user = acl_user.username
            superuser = acl_user.is_superuser
        elif _get_basic_auth_info(request) == settings.GEOSERVER_CREDENTIALS:
            geoserver = True
            superuser = True
    if not any([user, geoserver, superuser]) and not request.user.is_anonymous():
        user = request.user.username
        superuser = request.user.is_superuser
    return HttpResponse(json.dumps({
        'user' : user,
        'geoserver' : geoserver,
        'superuser' : superuser
    }))


@never_cache
def layer_acls(request):
    """
    returns json-encoded lists of layer identifiers that 
    represent the sets of read-write and read-only layers
    for the currently authenticated user. 
    """
    
    # the layer_acls view supports basic auth, and a special 
    # user which represents the geoserver administrator that
    # is not present in django.
    acl_user = request.user
    if 'HTTP_AUTHORIZATION' in request.META:
        try:
            username, password = _get_basic_auth_info(request)
            acl_user = authenticate(username=username, password=password)

            # Nope, is it the special geoserver user?
            if (acl_user is None and 
                username == settings.GEOSERVER_CREDENTIALS[0] and
                password == settings.GEOSERVER_CREDENTIALS[1]):
                # great, tell geoserver it's an admin.
                result = {
                   'rw': [],
                   'ro': [],
                   'name': username,
                   'is_superuser':  True,
                   'is_anonymous': False
                }
                return HttpResponse(json.dumps(result), mimetype="application/json")
        except:
            pass
        
        if acl_user is None: 
            return HttpResponse(_("Bad HTTP Authorization Credentials."),
                                status=401,
                                mimetype="text/plain")

    cache_key = 'layer_acls_%s' % acl_user.id
    perms = cache.get(cache_key)
    if perms is not None:
        return HttpResponse(perms, mimetype="application/json")
            
    all_readable = set()
    all_writable = set()
    for bck in get_auth_backends():
        if hasattr(bck, 'objects_with_perm'):
            all_readable.update(bck.objects_with_perm(acl_user,
                                                      'maps.view_layer',
                                                      Layer))
            all_writable.update(bck.objects_with_perm(acl_user,
                                                      'maps.change_layer', 
                                                      Layer))
    read_only = [x for x in all_readable if x not in all_writable]
    read_write = [x for x in all_writable if x in all_readable]

    read_only = [x[0] for x in Layer.objects.filter(id__in=read_only).values_list('typename').all()]
    read_write = [x[0] for x in Layer.objects.filter(id__in=read_write).values_list('typename').all()]
    
    result = {
        'rw': read_write,
        'ro': read_only,
        'name': acl_user.username,
        'is_superuser':  acl_user.is_superuser,
        'is_anonymous': acl_user.is_anonymous()
    }
    
    result = json.dumps(result)
    cache.set(cache_key, result, 120)

    return HttpResponse(result, mimetype="application/json")


def _split_query(query):
    """
    split and strip keywords, preserve space 
    separated quoted blocks.
    """

    qq = query.split(' ')
    keywords = []
    accum = None
    for kw in qq: 
        if accum is None: 
            if kw.startswith('"'):
                accum = kw[1:]
            elif kw: 
                keywords.append(kw)
        else:
            accum += ' ' + kw
            if kw.endswith('"'):
                keywords.append(accum[0:-1])
                accum = None
    if accum is not None:
        keywords.append(accum)
    return [kw.strip() for kw in keywords if kw.strip()]



DEFAULT_SEARCH_BATCH_SIZE = 10
MAX_SEARCH_BATCH_SIZE = 25
def metadata_search(request):
    """
    handles a basic search for data using the 
    GeoNetwork catalog.

    the search accepts: 
    q - general query for keywords across all fields
    start - skip to this point in the results
    limit - max records to return

    for ajax requests, the search returns a json structure 
    like this: 
    
    {
    'total': <total result count>,
    'next': <url for next batch if exists>,
    'prev': <url for previous batch if exists>,
    'query_info': {
        'start': <integer indicating where this batch starts>,
        'limit': <integer indicating the batch size used>,
        'q': <keywords used to query>,
    },
    'rows': [
      {
        'name': <typename>,
        'abstract': '...',
        'keywords': ['foo', ...],
        'detail' = <link to geonode detail page>,
        'attribution': {
            'title': <language neutral attribution>,
            'href': <url>
        },
        'download_links': [
            ['pdf', 'PDF', <url>],
            ['kml', 'KML', <url>],
            [<format>, <name>, <url>]
            ...
        ],
        'metadata_links': [
           ['text/xml', 'TC211', <url>],
           [<mime>, <name>, <url>],
           ...
        ]
      },
      ...
    ]}
    """
    if request.method == 'GET':
        params = request.GET
    elif request.method == 'POST':
        params = request.POST
    else:
        return HttpResponse(status=405)

    # grab params directly to implement defaults as
    # opposed to panicy django forms behavior.
    query = params.get('q', '')
    try:
        start = int(params.get('start', '0'))
    except:
        start = 0
    try:
        limit = min(int(params.get('limit', DEFAULT_SEARCH_BATCH_SIZE)),
                    MAX_SEARCH_BATCH_SIZE)
    except: 
        limit = DEFAULT_SEARCH_BATCH_SIZE

    advanced = {}
    bbox = params.get('bbox', None)
    if bbox:
        try:
            bbox = [float(x) for x in bbox.split(',')]
            if len(bbox) == 4:
                advanced['bbox'] =  bbox
        except:
            # ignore...
            pass

    result = _metadata_search(query, start, limit, **advanced)

    # XXX slowdown here to dig out result permissions
    for doc in result['rows']: 
        try: 
            layer = Layer.objects.get(uuid=doc['uuid'])
            doc['_local'] = True
            doc['_permissions'] = {
                'view': request.user.has_perm('maps.view_layer', obj=layer),
                'change': request.user.has_perm('maps.change_layer', obj=layer),
                'delete': request.user.has_perm('maps.delete_layer', obj=layer),
                'change_permissions': request.user.has_perm('maps.change_layer_permissions', obj=layer),
            }
            thumbnail = Thumbnail.objects.get_thumbnail(layer)
        except Layer.DoesNotExist:
            doc['_local'] = False
            pass

    result['success'] = True
    return HttpResponse(json.dumps(result), mimetype="application/json")

def _metadata_search(query, start, limit, **kw):
    
    csw = get_csw()

    keywords = _split_query(query)
    
    csw.getrecords(keywords=keywords, startposition=start+1, maxrecords=limit, bbox=kw.get('bbox', None))
    
    
    # build results 
    # XXX this goes directly to the result xml doc to obtain 
    # correct ordering and a fuller view of the result record
    # than owslib currently parses.  This could be improved by
    # improving owslib.
    results = [_build_search_result(doc) for doc in 
               csw._exml.findall('//'+nspath('Record', namespaces['csw']))]

    result = {'rows': results, 
              'total': csw.results['matches']}

    result['query_info'] = {
        'start': start,
        'limit': limit,
        'q': query
    }
    if start > 0: 
        prev = max(start - limit, 0)
        params = urlencode({'q': query, 'start': prev, 'limit': limit})
        result['prev'] = reverse('geonode.maps.views.metadata_search') + '?' + params

    next = csw.results.get('nextrecord', 0) 
    if next > 0:
        params = urlencode({'q': query, 'start': next - 1, 'limit': limit})
        result['next'] = reverse('geonode.maps.views.metadata_search') + '?' + params
    
    return result

def search_result_detail(request):
    uuid = request.GET.get("uuid")
    csw = get_csw()
    csw.getrecordbyid([uuid], outputschema=namespaces['gmd'])
    rec = csw.records.values()[0]
    raw_xml = csw._exml.find(nspath('MD_Metadata', namespaces['gmd']))
    extra_links = _extract_links(rec, raw_xml)
    
    try:
        layer = Layer.objects.get(uuid=uuid)
        layer_is_remote = False
    except:
        layer = None
        layer_is_remote = True

    return render_to_response('maps/search_result_snippet.html', RequestContext(request, {
        'rec': rec,
        'extra_links': extra_links,
        'layer': layer,
        'layer_is_remote': layer_is_remote
    }))

def _extract_links(rec, xml):
    download_links = []
    dl_type_path = "/".join([
        nspath("CI_OnlineResource", namespaces["gmd"]),
        nspath("protocol", namespaces["gmd"]),
        nspath("CharacterString", namespaces["gco"])
        ])

    dl_name_path = "/".join([
        nspath("CI_OnlineResource", namespaces["gmd"]),
        nspath("name", namespaces["gmd"]),
        nspath("CharacterString", namespaces["gco"])
        ])

    dl_description_path = "/".join([
        nspath("CI_OnlineResource", namespaces["gmd"]),
        nspath("description", namespaces["gmd"]),
        nspath("CharacterString", namespaces["gco"])
        ])

    dl_link_path = "/".join([
        nspath("CI_OnlineResource", namespaces["gmd"]),
        nspath("linkage", namespaces["gmd"]),
        nspath("URL", namespaces["gmd"])
        ])

    format_re = re.compile(".*\((.*)(\s*Format*\s*)\).*?")

    for link in xml.findall("*//" + nspath("onLine", namespaces['gmd'])):
        dl_type = link.find(dl_type_path)
        if dl_type is not None and dl_type.text == "WWW:DOWNLOAD-1.0-http--download":
            extension = link.find(dl_name_path).text.split('.')[-1]
            format = format_re.match(link.find(dl_description_path).text).groups()[0]
            url = link.find(dl_link_path).text
            download_links.append((extension, format, url))
    return dict(
            download=download_links
        )


def _build_search_result(doc):
    """
    accepts a node representing a csw result 
    record and builds a POD structure representing 
    the search result.
    """
    if doc is None:
        return None
    # Let owslib do some parsing for us...
    rec = CswRecord(doc)
    result = {}
    result['title'] = rec.title
    result['uuid'] = rec.identifier
    result['abstract'] = rec.abstract
    result['keywords'] = [x for x in rec.subjects if x]
    result['detail'] = rec.uri or ''

    # XXX needs indexing ? how
    result['attribution'] = {'title': '', 'href': ''}

    # XXX !_! pull out geonode 'typename' if there is one
    # index this directly... 
    if rec.uri:
        try:
            result['name'] = urlparse(rec.uri).path.split('/')[-1]
        except: 
            pass
    # fallback: use geonetwork uuid
    if not result.get('name', ''):
        result['name'] = rec.identifier

    # Take BBOX from GeoNetwork Result...
    # XXX this assumes all our bboxes are in this 
    # improperly specified SRS.
    if rec.bbox is not None and rec.bbox.crs == 'urn:ogc:def:crs:::WGS 1984':
        # slight workaround for ticket 530
        result['bbox'] = {
            'minx': min(rec.bbox.minx, rec.bbox.maxx),
            'maxx': max(rec.bbox.minx, rec.bbox.maxx),
            'miny': min(rec.bbox.miny, rec.bbox.maxy),
            'maxy': max(rec.bbox.miny, rec.bbox.maxy)
        }
    
    # XXX these could be exposed in owslib record...
    # locate all download links
    format_re = re.compile(".*\((.*)(\s*Format*\s*)\).*?")
    result['download_links'] = []
    for link_el in doc.findall(nspath('URI', namespaces['dc'])):
        if link_el.get('protocol', '') == 'WWW:DOWNLOAD-1.0-http--download':
            try:
                extension = link_el.get('name', '').split('.')[-1]
                format = format_re.match(link_el.get('description')).groups()[0]
                href = link_el.text
                result['download_links'].append((extension, format, href))
            except: 
                pass

    # construct the link to the geonetwork metadata record (not self-indexed)
    md_link = settings.GEONETWORK_BASE_URL + "srv/en/csw?" + urlencode({
            "request": "GetRecordById",
            "service": "CSW",
            "version": "2.0.2",
            "OutputSchema": "http://www.isotc211.org/2005/gmd",
            "ElementSetName": "full",
            "id": rec.identifier
        })
    result['metadata_links'] = [("text/xml", "TC211", md_link)]

    return result

def browse_data(request):
    return render_to_response('data.html', RequestContext(request, {}))

def search_page(request):
    DEFAULT_MAP_CONFIG, DEFAULT_BASE_LAYERS = default_map_config(request)
    # for non-ajax requests, render a generic search page

    if request.method == 'GET':
        params = request.GET
    elif request.method == 'POST':
        params = request.POST
    else:
        return HttpResponse(status=405)

    map = Map(projection="EPSG:900913", zoom = 1, center_x = 0, center_y = 0)

    return render_to_response('search.html', RequestContext(request, {
        'init_search': json.dumps(params or {}),
        'viewer_config': json.dumps(map.viewer_json(added_layers=DEFAULT_BASE_LAYERS, authenticated=request.user.is_authenticated())),
        'GOOGLE_API_KEY' : settings.GOOGLE_API_KEY,
        "site" : settings.SITEURL
    }))

def change_poc(request, ids, template = 'maps/change_poc.html'):
    layers = Layer.objects.filter(id__in=ids.split('_'))
    if request.method == 'POST':
        form = PocForm(request.POST)
        if form.is_valid():
            for layer in layers:
                layer.poc = form.cleaned_data['contact']
                layer.save()
            # Process the data in form.cleaned_data
            # ...
            return HttpResponseRedirect('/admin/maps/layer') # Redirect after POST
    else:
        form = PocForm() # An unbound form
    return render_to_response(template, RequestContext(request, 
                                  {'layers': layers, 'form': form }))


#### MAPS SEARCHING ####

DEFAULT_MAPS_SEARCH_BATCH_SIZE = 10
MAX_MAPS_SEARCH_BATCH_SIZE = 25
def maps_search(request):
    """
    handles a basic search for maps using the 
    GeoNetwork catalog.

    the search accepts: 
    q - general query for keywords across all fields
    start - skip to this point in the results
    limit - max records to return
    sort - field to sort results on
    dir - ASC or DESC, for ascending or descending order

    for ajax requests, the search returns a json structure 
    like this: 
    
    {
    'total': <total result count>,
    'next': <url for next batch if exists>,
    'prev': <url for previous batch if exists>,
    'query_info': {
        'start': <integer indicating where this batch starts>,
        'limit': <integer indicating the batch size used>,
        'q': <keywords used to query>,
    },
    'rows': [
      {
        'title': <map title,
        'abstract': '...',
        'detail' : <url geonode detail page>,
        'owner': <name of the map's owner>,
        'owner_detail': <url of owner's profile page>,
        'last_modified': <date and time of last modification>
      },
      ...
    ]}
    """
    if request.method == 'GET':
        params = request.GET
    elif request.method == 'POST':
        params = request.POST
    else:
        return HttpResponse(status=405)

    # grab params directly to implement defaults as
    # opposed to panicy django forms behavior.
    query = params.get('q', '')
    try:
        start = int(params.get('start', '0'))
    except:
        start = 0
    try:
        limit = min(int(params.get('limit', DEFAULT_MAPS_SEARCH_BATCH_SIZE)),
                    MAX_MAPS_SEARCH_BATCH_SIZE)
    except: 
        limit = DEFAULT_MAPS_SEARCH_BATCH_SIZE


    sort_field = params.get('sort', u'')
    sort_field = unicodedata.normalize('NFKD', sort_field).encode('ascii','ignore')  
    sort_dir = params.get('dir', 'ASC')
    result = _maps_search(query, start, limit, sort_field, sort_dir)

    result['success'] = True
    return HttpResponse(json.dumps(result), mimetype="application/json")

def _maps_search(query, start, limit, sort_field, sort_dir):

    keywords = _split_query(query)

    maps = Map.objects
    for keyword in keywords:
        maps = maps.filter(
              Q(title__icontains=keyword)
            | Q(abstract__icontains=keyword))

    if sort_field:
        order_by = ("" if sort_dir == "ASC" else "-") + sort_field
        maps = maps.order_by(order_by)

    maps_list = []

    for map in maps.all()[start:start+limit]:
        try:
            owner_name = Contact.objects.get(user=map.owner).name
        except:
            owner_name = map.owner.first_name + " " + map.owner.last_name

        mapdict = {
            'id' : map.id,
            'title' : map.title,
            'abstract' : map.abstract,
            'detail' : reverse('geonode.maps.views.map_controller', args=(map.id,)),
            'owner' : owner_name,
            'owner_detail' : reverse('profiles.views.profile_detail', args=(map.owner.username,)),
            'last_modified' : map.last_modified.isoformat()
            }
        maps_list.append(mapdict)

    result = {'rows': maps_list, 
              'total': maps.count()}

    result['query_info'] = {
        'start': start,
        'limit': limit,
        'q': query
    }
    if start > 0: 
        prev = max(start - limit, 0)
        params = urlencode({'q': query, 'start': prev, 'limit': limit})
        result['prev'] = reverse('geonode.maps.views.maps_search') + '?' + params

    next = start + limit + 1
    if next < maps.count():
         params = urlencode({'q': query, 'start': next - 1, 'limit': limit})
         result['next'] = reverse('geonode.maps.views.maps_search') + '?' + params
    
    return result

def maps_search_page(request):
    # for non-ajax requests, render a generic search page

    if request.method == 'GET':
        params = request.GET
    elif request.method == 'POST':
        params = request.POST
    else:
        return HttpResponse(status=405)

    return render_to_response('maps_search.html', RequestContext(request, {
        'init_search': json.dumps(params or {}),
         "site" : settings.SITEURL
    }))

def batch_permissions(request):
    if not request.user.is_authenticated:
        return HttpResponse("You must log in to change permissions", status=401) 

    if request.method != "POST":
        return HttpResponse("Permissions API requires POST requests", status=405)

    spec = json.loads(request.raw_post_data)
    
    if "layers" in spec:
        lyrs = Layer.objects.filter(pk__in = spec['layers'])
        for lyr in lyrs:
            if not request.user.has_perm("maps.change_layer_permissions", obj=lyr):
                return HttpResponse("User not authorized to change layer permissions", status=403)

    if "maps" in spec:
        maps = Map.objects.filter(pk__in = spec['maps'])
        for map in maps:
            if not request.user.has_perm("maps.change_map_permissions", obj=map):
                return HttpResponse("User not authorized to change map permissions", status=403)

    anon_level = spec['permissions'].get("anonymous")
    auth_level = spec['permissions'].get("authenticated")
    users = spec['permissions'].get('users', [])
    user_names = [x for (x, y) in users]

    if "layers" in spec:
        lyrs = Layer.objects.filter(pk__in = spec['layers'])
        valid_perms = ['layer_readwrite', 'layer_readonly']
        if anon_level not in valid_perms:
            anon_level = "_none"
        if auth_level not in valid_perms:
            auth_level = "_none"
        for lyr in lyrs:
            lyr.get_user_levels().exclude(user__username__in = user_names + [lyr.owner.username]).delete()
            lyr.set_gen_level(ANONYMOUS_USERS, anon_level)
            lyr.set_gen_level(AUTHENTICATED_USERS, auth_level)
            for user, user_level in users:
                if user_level not in valid_perms:
                    user_level = "_none"
                lyr.set_user_level(user, user_level)

    if "maps" in spec:
        maps = Map.objects.filter(pk__in = spec['maps'])
        valid_perms = ['layer_readwrite', 'layer_readonly']
        if anon_level not in valid_perms:
            anon_level = "_none"
        if auth_level not in valid_perms:
            auth_level = "_none"
        anon_level = anon_level.replace("layer", "map")
        auth_level = auth_level.replace("layer", "map")

        for m in maps:
            m.get_user_levels().exclude(user__username__in = user_names + [m.owner.username]).delete()
            m.set_gen_level(ANONYMOUS_USERS, anon_level)
            m.set_gen_level(AUTHENTICATED_USERS, auth_level)
            for user, user_level in spec['permissions'].get("users", []):
                user_level = user_level.replace("layer", "map")
                m.set_user_level(user, valid_perms.get(user_level, "_none"))

    return HttpResponse("Not implemented yet")

def batch_delete(request):
    if not request.user.is_authenticated:
        return HttpResponse("You must log in to delete layers", status=401) 

    if request.method != "POST":
        return HttpResponse("Delete API requires POST requests", status=405)

    spec = json.loads(request.raw_post_data)

    if "layers" in spec:
        lyrs = Layer.objects.filter(pk__in = spec['layers'])
        for lyr in lyrs:
            if not request.user.has_perm("maps.delete_layer", obj=lyr):
                return HttpResponse("User not authorized to delete layer", status=403)

    if "maps" in spec:
        maps = Map.objects.filter(pk__in = spec['maps'])
        for map in maps:
            if not request.user.has_perm("maps.delete_map", obj=map):
                return HttpResponse("User not authorized to delete map", status=403)

    if "layers" in spec:
        Layer.objects.filter(pk__in = spec["layers"]).delete()

    if "maps" in spec:
        Map.objects.filter(pk__in = spec["maps"]).delete()

    nlayers = len(spec.get('layers', []))
    nmaps = len(spec.get('maps', []))

    return HttpResponse("Deleted %d layers and %d maps" % (nlayers, nmaps))

# Temp function for tschaub while working on the timeline
def time_info(request):
    if request.method != "GET":
        return HttpResponse(json.dumps({}), mimetype="application/javascript")
    else:
        from geoserver.support import xml_property, attribute_list
        cat = Layer.objects.gs_catalog
        layer_name = request.GET.get('layer', '')
        after_split = layer_name.split(":", 1)
        if len(after_split) != 2:
            # resolve by name if full name not supplied
            try:
                lyr = Layer.objects.get(name=after_split[0])
                after_split = lyr.typename.split(":", 1)
            except Layer.DoesNotExist:
                return HttpResponse(json.dumps({}), mimetype="application/javascript")
        ws, lyr = after_split
        layer = cat.get_resource(workspace=cat.get_workspace(ws), name=lyr)
        if layer is not None and 'time' in layer.metadata:
            if layer.metadata['time']:
                attributes = {}
                dimensionInfo = layer.metadata['time'].find('dimensionInfo')
                for attribute in list(dimensionInfo):
                    attributes[attribute.tag] = attribute.text
                return HttpResponse(json.dumps(attributes), mimetype="application/javascript") 
            else:
                return HttpResponse(json.dumps({}), mimetype="application/javascript")
        else:
            return HttpResponse(json.dumps({}), mimetype="application/javascript")


@login_required
def create_layer(request):
    if request.method != 'POST':
        return HttpResponse('Only POST requests supported', status='405')
    # unpack multi-values into normal dict
    return _create_layer(request.user, **dict(request.POST.items()))
    
def _create_layer(user = None, **kwargs):
    '''extracted/abstracted from view easier testing or use elsewhere'''
    from geonode.maps.gs_helpers import get_sld_for

    # @hack skip_geonode allows test to work - gscatalog responses are mocks
    # and more work required to fix this ATM
    skip_geonode = "skip_geonode" in kwargs

    errors = []
    args = {}

    def respond():
        return HttpResponse(json.dumps({
            'success' : len(errors) == 0,
            'errors' : errors
        }))

    # these must be provided or fail
    for r in ('name','srs','attributes'):
        if r not in kwargs:
            errors.append('%s is required' % r)
        else:
            args[r] = kwargs[r]

    if errors:
        return respond()

    time_attribute = kwargs.get('timeAttribute',None)
    time_presentation = kwargs.get('timePresentation',None)

    # optional arguments
    for r in ('workspace','store','title'):
        args[r] = kwargs.get(r,None)

    # default native_name to name if not provided
    args['native_name'] = kwargs.get('nativeName',args['name'])

    # use default store if not provided
    if not args['store']:
        args['store'] = settings.DB_DATASTORE_NAME

    atts = [att.split(":") for att in args['attributes'].split(',')]
    # check for optional third value that specifies nillability
    for a in atts:
        if len(a) == 3:
            a[2] = dict(nillable=a[2])
    args['attributes'] = atts

    gs_ftype = None
    gslayer = None
    cat = Layer.objects.gs_catalog
    try:
        logger.info('Creating native_layer %s',args)
        gs_ftype = cat.create_native_layer(**args)
        if not gs_ftype:
            errors.append('Internal error, layer not created')
        else:
            gslayer = cat.get_layer(gs_ftype.name)
    except Exception,ex:
        logger.exception('Error creating layer in geoserver')
        errors.append(str(ex))

    if not errors and not kwargs.get('skip_style',False):
        try:
            logging.info('Creating style %s',gs_ftype.name)
            sld = get_sld_for(gslayer)
            try:
                cat.create_style(gslayer.name, sld)
            except geoserver.catalog.ConflictingDataError, e:
                logger.warn('There was already a style named %s in GeoServer, cannot overwrite: "%s"',gslayer.name,str(e))
            gslayer.default_style = cat.get_style(gslayer.name)
            cat.save(gslayer)
        except Exception,ex:
            logger.exception('Error creating style for layer')
            errors.append(str(ex))

    if not errors and not skip_geonode:
        try:
            layer, created = Layer.objects.get_or_create(name=gs_ftype.name, defaults = {
                "workspace": gs_ftype.workspace.name,
                "store": gs_ftype.store.name,
                "storeType": gs_ftype.store.resource_type,
                "typename": "%s:%s" % (gs_ftype.workspace.name, gs_ftype.name),
                "title": '%s Annotations' % gs_ftype.name,
                "abstract": 'Store Annotations for %s' % gs_ftype.name,
                "uuid": str(uuid.uuid4()),
                "owner": user
            })
            assert created, 'Expected layer to have been created'
            layer.set_default_permissions()
            layer.save()
        except Exception,ex:
            logger.exception('Error in creating geonode layer')
            errors.append(str(ex))

     # add time dimension - @todo should be in gsconfig
    if not errors and time_attribute:
        try:
            logger.info('Setting time dimension in geoserver')
            timedata = json.dumps({
                'featureType' : {
                    'enabled' : True,
                    'metadata' : {
                        'entry' : {
                            '@key' : 'time',
                            'dimensionInfo' : {
                                'enabled' : True,
                                'attribute' : time_attribute,
                                'presentation' : time_presentation or 'LIST'
                            }
                        }
                    }
                }
            })
            resturl = gslayer.resource.href
            resturl = resturl[:resturl.rfind('.xml')]
            resp, content = cat.http.request(resturl,'PUT',timedata,{
                "Content-Type" : "application/json"
            })
            if resp.status < 200 or resp.status > 299:
                errors.append('Error setting time dimension: %s' % content)
        except Exception,ex:
            logger.exception('Error setting time dimension in geoserver')
            errors.append(str(ex))
            
    if errors and gs_ftype:
        # rollback
        # try to wipe out our layer in geoserver
        try:
            logging.info('Cleanup layer')
            cascading_delete(cat, gs_ftype)
        except:
            logger.exception('Error cleaning up created_layer %s',gs_ftype.name)

    return respond()