import os
import logging
import json
import pprint

STATE_PENDING = "PENDING"
STATE_READY = "READY"
STATE_RUNNING = "RUNNING"
STATE_INCOMPLETE = "INCOMPLETE"
STATE_COMPLETE = "COMPLETE"

_logger = logging.getLogger("gsuploader")

def parse_response(args):
    headers, response = args
    try:
        resp = json.loads(response)
    except ValueError,ex:
        _logger.warn('invalid JSON response: %s',response)
        raise ex
    if "import" in resp:
        return Session(json=resp['import'])
    elif "task" in resp:
        return Task(resp['task'])
    elif "imports" in resp:
        return [ Session(json=j) for j in resp['imports'] ]
    elif "tasks" in resp:
        # non-recognized file tasks have null source.format
        return [ Task(t) for t in resp['tasks'] ]
    raise Exception("Unknown response %s" % resp)

class _UploadBase(object):
    _uploader = None
    def __init__(self,json,parent=None):
        self._parent = parent
        if parent == self:
            raise Exception('bogus')
        self._bind_json(json)
    def _bind(self,json):
        if isinstance(json, basestring):
            raise ValueError("expected json, got '%s'" % json)
        for k in json:
            v = json[k]
            if not isinstance(v,dict):
                setattr(self,k,v)
    def _build(self,json,clazz):
        return [ clazz(j,self) for j in json ]
    def _getuploader(self):
        comp = self
        while comp:
            if comp._uploader:
                return comp._uploader
            comp = comp._parent
    def _url(self,spec,*parts):
        return self._getuploader().client.url( spec % parts )
    def _client(self):
        return self._getuploader().client
    def __repr__(self):
        # @todo fix this
        def _fields(obj):
            fields = filter( lambda kp: kp[0][0] != '_',vars(obj).items())
            fields.sort(key=lambda f: f[0])
            return map(lambda f: isinstance(f[1],_UploadBase) and (f[0],_fields(f[1])) or f, fields)
        repr = pprint.pformat(_fields(self),indent=2)
        return "%s : %s" % (self.__class__.__name__,repr)
        
class Task(_UploadBase):
    def _bind_json(self,json):
        self._bind(json)
        self.source = Source(json['source'],self)
        self.target = None
        if 'target' in json:
            self.target = Target(json['target'],self)
        self.items = self._build(json['items'],Item)
    def set_target(self,store_name,workspace):
        data = { 'task' : {
            'target' : {
                'dataStore' : {
                    'name' : store_name,
                    'workspace' : {
                        'name' : workspace
                    }
                }
            }
        }}
        self._client().put_json(self.href,json.dumps(data))
    def set_update_mode(self,update_mode):
        data = { 'task' : {
            'updateMode' : update_mode
        }}
        self._client().put_json(self.href,json.dumps(data))
    def set_charset(self,charset):
        data = { 'task' : {
            'source' : {
                'charset' : charset
            }
        }}
        self._client().put_json(self.href,json.dumps(data))

    def delete(self):
        """Delete the task"""
        resp, content = self._client().delete(self.href)
        if resp['status'] != '204':
            raise Exception('expected 204 response code, got %s' % resp['status'],content)


class Workspace(_UploadBase):
    def _bind_json(self,json):
        self._bind(json)

class Source(_UploadBase):
    def _bind_json(self,json):
        self._bind(json)
        if 'files' in json:
            self.files = [ File(f) for f in json['files'] ]


class File(_UploadBase):
    def _bind_json(self, json):
        self._bind(json)


class Target(_UploadBase):

    # this allows compatibility with the gsconfig datastore object
    resource_type = "featureType"

    def _bind_json(self,json):
        key,val = json.items()[0]
        self.target_type = key
        if self.target_type == 'coverageStore':
            self.resource_type = 'coverage'
        self._bind(val)
        self.workspace = Workspace(val['workspace'])
        # @todo more

class Item(_UploadBase):

    def _bind_json(self,json):
        self._bind(json)
        # @todo iws - why is layer nested in another layer
        self.layer = Layer(json['layer']['layer'],self)
        resource = json['resource']
        if 'featureType' in resource:
            self.resource = FeatureType(resource['featureType'],self)
        elif 'coverage' in resource:
            self.resource = Coverage(resource['coverage'], self)
        else:
            raise Exception('not handling resource %s' % resource)
        self.transformChain = json.get('transformChain',[])

    def set_transforms(self, transforms):
        """Set the transforms of this Item. transforms is a list of dicts"""
        self._transforms = transforms

    def add_transforms(self, transforms):
        if not hasattr(self, '_transforms') and 'transforms' in self.transformChain:
            self._transforms = list(self.transformChain['transforms'])
        self._transforms.extend(transforms)
    
    def remove_transforms(self, transforms, by_type=False):
        '''remove transforms by equality or list of types'''
        if not hasattr(self, '_transforms') and 'transforms' in self.transformChain:
            self._transforms = list(self.transformChain['transforms'])
        if by_type:
            self._transforms = [ t for t in self._transforms if t['type'] not in transforms ]
        else:
            self._transforms = [ t for t in self._transforms if t not in transforms ]

    def get_progress(self):
        """Get a json object representing progress of this item"""
        if self.progress:
            client = self._client()
            headers, response = client._request(self.progress)
            unicode_error = False
            try:
                response = response.decode('utf-8', 'strict')
            except UnicodeDecodeError:
                response = response.decode('utf-8', 'replace')
                unicode_error = True
            try:
                progress = json.loads(response)
                if unicode_error:
                    progress['message'] += ' - it looks like an invalid character'
                return progress
            except ValueError,ex:
                _logger.warn('invalid JSON response: %s',response)
                raise ex
        else:
            raise Exception("Item does not have a progress endpoint")

    def save(self):
        """@todo,@hack This really only saves transforms and will overwrite existing"""
        data = {
            "item" : {
                "transformChain" : {
                    "type" : "VectorTransformChain", #@todo sniff for existing
                    "transforms" : self._transforms
                }
            }
        }
        self._client().put_json(self.href,json.dumps(data))
        
class Layer(_UploadBase):
    def _bind_json(self,json):
        self.layer_type = json.pop('type')
        self._bind(json)


class ResourceMixin(object):

    def _bind_resource(self, json):
        '''common resource binding'''
        self.nativeCRS = None
        if 'nativeCRS' in json:
            self.nativeCRS = json['nativeCRS']
            # if nativeCRS is missing, this will be a dict, otherwise a string
            if isinstance(self.nativeCRS, dict):
                self.nativeCRS = self.nativeCRS['$']

    def set_srs(self,srs):
        """@todo,@hack This immediately changes srs"""
        item = self._parent
        data = {
            "item" : {
                "id" : item.id,
                "resource" : {
                    self.resource_type : {
                        "srs" : srs
                    }
                }
            }
        }
        self._client().put_json(item.href,json.dumps(data))
        self.srs = srs


class FeatureType(_UploadBase, ResourceMixin):
    resource_type = "featureType"

    def _bind_json(self,json):
        self._bind(json)

        # work around xstream oddity when single attribute is present
        attributes = json['attributes']['attribute']
        if isinstance(attributes, dict):
            attributes = [ attributes ]

        self.attributes = self._build(attributes,Attribute)
        self._bind_resource(json)

    def add_meta_data_entry(self,key,mtype,**kw):
        if not hasattr(self,'metadata'):
            self.metadata = []
        self.metadata.append((key,mtype,kw))
        
    def add_time_dimension_info(self,att_name,end_att_name,presentation,amt,period):
    
        kw = {
            'enabled' : True,
            'attribute' : att_name,
            'presentation' : presentation
        }
        if end_att_name:
            kw['endAttribute'] = end_att_name
        if amt and period:
            mult = {
                'seconds': 1,
                'minutes': 60,
                'hours': 3600,
                'days': 86400,
                'months': 2628000000, # this is the number geoserver computes for 1 month
                'years': 31536000000
            }
            kw['resolution'] = int(amt) * mult[period] * 1000 #yay millis
        self.add_meta_data_entry('time','dimensionInfo',**kw)
        
    def save(self):
        """@todo,@hack This really only saves meta_data additions and will overwrite existing"""
        item = self._parent
        entry = []
        for m in self.metadata:
            entry.append({
                "@key" : m[0],
                m[1] : m[2]
            })
        data = {
            "item" : {
                "id" : item.id,
                "resource" : {
                    "featureType" : {
                        "metadata" : {
                            "entry": entry
                        }
                    }
                }
            }

        }
        self._client().put_json(item.href, json.dumps(data))


class Coverage(_UploadBase, ResourceMixin):
    resource_type = "coverage"

    def _bind_json(self, json):
        # TODO
        self._bind(json)

        self._bind_resource(json)


class Attribute(_UploadBase):
    def _bind_json(self, json):
        self._bind(json)


class Session(_UploadBase):
    def __init__(self, json=None):
        self.tasks = []
        if json:
            self._bind(json)
            if 'tasks' in json:
                self.tasks = self._build(json['tasks'], Task)

    def delete_unrecognized_tasks(self):
        for t in list(self.tasks):
            if t.source.format is None:
                _logger.info("deleting unrecognized task %s", t)
                t.delete()
                self.tasks.remove(t)
                
    def reload(self):
        '''return a reloaded version of this session'''
        return self._uploader.get_session(self.id)

    def upload_task(self, files, use_url=False):
        """create a task with the provided files
        files - collection of files to upload or zip file
        use_url - if true, post a URL to the uploader
        """
        # @todo getting the task response updates the session tasks, but
        # neglects to retreive the overall session status field
        fname = os.path.basename(files[0])
        _,ext = os.path.splitext(fname)
        if use_url:
            if ext == '.zip':
                upload_url = files[0]
            else:
                upload_url = os.path.dirname(files[0])
            url = self._url("imports/%s/tasks" % self.id)
            upload_url = "file://%s" % os.path.abspath(upload_url)
            resp = self._client().post_upload_url(url, upload_url)
        elif ext == '.zip':
            url = self._url("imports/%s/tasks/%s" % (self.id,fname))
            resp = self._client().put_zip(url, files[0])
        else:
            url = self._url("imports/%s/tasks" % self.id)
            resp = self._client().post_multipart(url, files)
        tasks = parse_response( resp )
        if not isinstance(tasks, list):
            tasks = [tasks]
        for t in tasks:
            t._parent = self
        self.tasks.extend(tasks)

    def commit(self, async=False):
        """complete upload"""
        #@todo check status if we don't have it already
        url = self._url("imports/%s",self.id)
        if async:
            url = url + "?async"
        resp, content = self._client().post(url)
        if resp['status'] != '204':
            raise Exception("expected 204 response code, got %s" % resp['status'],content)
        
    def delete(self):
        """Delete the upload"""
        url = self._url("imports/%s",self.id)
        resp, content = self._client().delete(url)
        if resp['status'] != '204':
            raise Exception('expected 204 response code, got %s' % resp['status'],content)
    
