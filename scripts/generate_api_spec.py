#!/usr/bin/env python

import sys
import yaml
import re
import os

this_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(this_dir)
paths = ['src','libcloud','celerybeat-mongo']
for p in paths:
    sys.path.append(os.path.join(parent_dir,p))

import mist.api

BASE_FILE_PATH = os.path.join(this_dir,'base.yml')
OAS_FILE_PATH = os.path.join(this_dir,'spec.yml')

def patch_operation(operation):
    ret = {}
    if 'responses' in operation.keys():
        ret['responses'] = operation['responses']
    else:
        ret['responses'] = { '200': { 'description': 'Foo' } }

    if 'parameters' in operation.keys():
        ret['parameters'] = operation['parameters']
    else:
        params = []
        for key in list(set(operation.keys()) - {'parameters','requestBody','responses','description'}):
            if 'in' in operation[key].keys():
                p = {}
                p['name'] = key
                p['schema'] = {}
                for k in operation[key].keys():
                    if k in ['type','enum','default']:
                        p['schema'][k] = operation[key][k]
                    else:
                        p[k] = operation[key][k]
                params.append(p)
        if params:
            ret['parameters'] = params

    if 'requestBody' in operation.keys():
        ret['requestBody'] = operation['requestBody']
    else:
        reqB = {\
                 'description': 'Description',\
                 'required': True,\
                 'content': {\
                    'application/json': {\
                                    'schema': {\
                                        'type': 'object',\
                                        'properties': {},\
                                              }\
                                        }\
                            }\
                }
        properties = {}
        required = []
        for key in list(set(operation.keys()) - {'parameters','requestBody','responses','description'}):
            if not 'in' in operation[key].keys():
                p = { key: {} }
                for k in operation[key].keys():
                    if k != 'required':
                        p[key][k] = operation[key][k]
                properties.update(p)
                if 'required' in operation[key] and operation[key]['required']:
                    required.append(key)
        if properties:
            reqB['content']['application/json']['schema']['properties'] = properties
            if required:
                reqB['content']['application/json']['schema']['required'] = required
            ret['requestBody'] = reqB

    return ret

def docstring_to_object(docstring):
    if not docstring:
        return {}

    operation = {}
    tokens = docstring.split('---')
    if len(tokens) > 1:
        operation = yaml.safe_load(tokens[1]) or {}

    description = re.sub(r'\s+',r' ',tokens[0]).strip()
    operation['description'] = description

    return operation

def main():
    routes = []
    paths = {}
    app = mist.api.main({}).app.app
    for v in app.registry.introspector.get_category('views'):
        vi = v['introspectable']
        (route_name, request_method, func) = (vi['route_name'], vi['request_methods'], vi['callable'])
        if route_name:
            route_path = app.routes_mapper.get_route(route_name).path
            if route_path and route_name.startswith('api_v1_'):
                operation = docstring_to_object(func.func_doc)
                if isinstance(request_method,tuple):
                    for method in request_method:
                        routes.append((route_path, method.lower(), operation))
                else:
                    routes.append((route_path, request_method.lower(), operation))

    for path, method, operation in routes:
        if not path in paths:
            paths[path] = {}
        paths[path][method] = patch_operation(operation)

    with open(BASE_FILE_PATH,'r') as f:
        openapi = yaml.safe_load(f.read())
        openapi['paths'] = paths
    with open(OAS_FILE_PATH,'w') as f:
        noalias_dumper = yaml.dumper.SafeDumper
        noalias_dumper.ignore_aliases = lambda self, data: True
        yaml.dump(openapi,f,default_flow_style=False,Dumper=noalias_dumper)

if __name__ == '__main__':
    main()